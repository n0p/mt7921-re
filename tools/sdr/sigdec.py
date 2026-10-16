#!/usr/bin/env python3
"""PPDU format and coding-field analysis for the FEC exploration (notes/10): find out what a transmitter really sent.

Works on samples at the 20 Msps-equivalent rate (a 5 MHz /4 PPDU at 5 Msps), like rx80211.py. From the legacy preamble
it estimates CFO and channel, decodes L-SIG, then classifies the next OFDM symbols:
  - two QBPSK symbols (energy on the quadrature axis)       -> HT mixed format: decode HT-SIG (48 data tones, BCC)
  - an exact repeat of L-SIG (RL-SIG)                        -> HE: decode HE-SIG-A (52 data tones, BCC)
  - BPSK then QBPSK                                          -> VHT (VHT-SIG-A; only the format is reported)
  - otherwise                                                -> legacy (non-HT) data
HT-SIG and HE-SIG-A carry a CRC, so a decoded field set is trustworthy: MCS, LDPC (FEC coding bit), STBC, GI, DCM.

Library: analyze(iq) -> list of dicts (one per detected PPDU). CLI: sigdec.py <file.cf32> (20 Msps-equivalent samples)
"""
import sys

import numpy as np

import rx80211
from ofdm80211 import DATA_SC, PILOT_SC, PILOT_BASE, POLARITY

# 802.11n/ax 20 MHz tone map for the 56-tone symbols (HE-SIG-A uses it): data on -28..28 except DC and the pilots
# +-7, +-21; the four extra tones +-27, +-28 sit outside the legacy LTF, so their channel is extrapolated.
HT_PILOTS = [-21, -7, 7, 21]
HT_DATA = [k for k in range(-28, 29) if k != 0 and k not in HT_PILOTS]
assert len(HT_DATA) == 52


def crc8_ht(bits):
    """HT-SIG CRC (802.11-2020 19.3.9.4.4): polynomial x^8+x^2+x+1, register initialised to ones, output inverted,
    sent MSB first."""
    c = [1] * 8
    for b in bits:
        fb = b ^ c[7]
        c = [fb] + [c[0] ^ fb, c[1] ^ fb] + c[2:7]
    return [1 - x for x in reversed(c)]


def crc4_he(bits):
    """HE-SIG-A CRC (27.3.11.7.3): the 8-bit HT-SIG CRC computed over the 42 bits, of which the first 4 output bits
    (c7..c4) are sent."""
    return crc8_ht(bits)[:4]


def _deint48(vals):
    return rx80211.deinterleave(vals, 48, 1)


def _perm52():
    """BPSK interleaver for 52 coded bits per symbol (HT/HE 20 MHz: 13 columns x 4 rows): coded bit k goes to tone
    position i = 4 * (k % 13) + k // 13 (s = 1, so there is no second permutation)."""
    k = np.arange(52)
    return 4 * (k % 13) + k // 13


def _bits_to_int(bits):
    return sum(int(b) << n for n, b in enumerate(bits))


def _equalise56(sym80, h64):
    """One 80-sample OFDM symbol -> the 56-tone set (HT_DATA order), channel-equalised. The legacy LTF only measures
    tones -26..26; +-27/+-28 reuse the channel of +-26."""
    f = np.fft.fft(sym80[16:80])
    out = []
    for k in HT_DATA:
        kk = max(-26, min(26, k))
        out.append(f[k % 64] / h64[kk % 64])
    return np.array(out)


def _pilot_phase(f, idx):
    """Common phase error from the legacy pilots of symbol number idx (polarity sequence index)."""
    pil = np.array([f[k % 64] for k in PILOT_SC]) * np.asarray(PILOT_BASE) * POLARITY[idx % 127]
    return np.angle(pil.sum())


def analyze_at(iq, t0):
    """t0 = first L-LTF symbol (as rx80211.decode_at). Returns a dict or None (no valid L-SIG)."""
    if t0 < 176 or t0 + 128 + 80 * 6 > len(iq):
        return None
    stf = iq[t0 - 176:t0 - 32]
    coarse = np.angle(np.vdot(stf[:-16], stf[16:])) / (2 * np.pi * 16)
    k = np.arange(0, 128 + 80 * 6)
    x = iq[t0:t0 + len(k)] * np.exp(-2j * np.pi * coarse * k)
    cfo = coarse + np.angle(np.vdot(x[:64], x[64:128])) / (2 * np.pi * 64)
    x = iq[t0:t0 + len(k)] * np.exp(-2j * np.pi * cfo * k)
    ltf = (np.fft.fft(x[:64]) + np.fft.fft(x[64:128])) / 2
    ref = rx80211._ltf_freq()
    h = np.where(np.abs(ref) > 0, ltf / np.where(np.abs(ref) > 0, ref, 1), 1)

    def sym(n):  # OFDM symbol n after the LTF (0 = L-SIG), 80 samples
        return x[128 + 80 * n:128 + 80 * (n + 1)]

    def legacy48(n, pidx):  # 48 data tones of symbol n, equalised, pilot phase removed
        f = np.fft.fft(sym(n)[16:80]) / h
        return np.array([f[kk % 64] for kk in DATA_SC]) * np.exp(-1j * _pilot_phase(f, pidx))

    lsig = legacy48(0, 0)
    sb = rx80211.viterbi(_deint48(lsig.real), 24)
    rate_bits = _bits_to_int(sb[3::-1])
    length = _bits_to_int(sb[5:17])
    if sum(sb[:18]) % 2 or rate_bits not in rx80211.RATE_BY_BITS or length == 0:
        return None
    out = dict(lsig_rate=rx80211.RATE_BY_BITS[rate_bits], lsig_len=length, cfo=cfo)
    s1, s2 = legacy48(1, 1), legacy48(2, 2)

    def q_ratio(s):  # energy on the quadrature axis vs in phase: QBPSK > 1, BPSK < 1
        return float(np.sum(s.imag ** 2) / max(np.sum(s.real ** 2), 1e-12))

    q1, q2 = q_ratio(s1), q_ratio(s2)
    rl = float(np.abs(np.vdot(lsig, s1)) / max(np.linalg.norm(lsig) * np.linalg.norm(s1), 1e-12))
    # HT mixed: HT-SIG1/2 are BPSK rotated by 90 degrees. The threshold is loose (two-chain STBC transmissions measured
    # 1.5-4 with this single-chain channel estimate); the HT-SIG CRC is the real arbiter
    if q1 > 1.3 and q2 > 1.3:
        coded = np.concatenate([_deint48(s1.imag), _deint48(s2.imag)])
        b = rx80211.viterbi(coded, 48)
        sig1, sig2 = b[:24], b[24:48]
        crc_ok = list(b[34:42]) == crc8_ht(list(b[:34]))
        out.update(fmt="HT-MF", crc_ok=crc_ok, mcs=_bits_to_int(sig1[0:7]), cbw40=int(sig1[7]),
                   ht_len=_bits_to_int(sig1[8:24]), stbc=_bits_to_int(sig2[4:6]), ldpc=int(sig2[6]),
                   sgi=int(sig2[7]), ness=_bits_to_int(sig2[8:10]), aggregation=int(sig2[3]))
    elif rl > 0.9 and q1 < 0.5:  # HE: RL-SIG repeats L-SIG, then HE-SIG-A in BPSK symbols of 52 data tones
        perm = _perm52()
        e3 = _equalise56(sym(3), h)
        # HE ER SU (27.3.11.7): HE-SIG-A takes 4 symbols, A1, A1 again without interleaving on the quadrature axis
        # (QBPSK), A2, A2 again; plain HE SU sends A1, A2 in two BPSK symbols
        er = float(np.sum(e3.imag ** 2) / max(np.sum(e3.real ** 2), 1e-12)) > 2
        coded = []
        for n in ((2, 4) if er else (2, 3)):
            e = _equalise56(sym(n), h)
            coded.append(e.real[perm])
        # no pilot phase tracking on these two symbols: a residual phase of pi complements every coded bit, which this
        # code maps to the complemented data bits, so decode both polarities and keep the one whose CRC holds
        coded = np.concatenate(coded)
        for sign in (1, -1):
            b = rx80211.viterbi(sign * coded, 52)
            crc_ok = list(b[42:46]) == crc4_he(list(b[:42]))
            if crc_ok:
                break
        a1, a2 = b[:26], b[26:52]
        out.update(fmt=("HE-ER-SU" if er else "HE-SU") if a1[0] else "HE-MU/TB?", crc_ok=crc_ok, mcs=_bits_to_int(a1[3:7]), dcm=int(a1[7]),
                   bw=_bits_to_int(a1[19:21]), gi_ltf=_bits_to_int(a1[21:23]), nsts=_bits_to_int(a1[23:26]) + 1,
                   ldpc=int(a2[7]), ldpc_extra=int(a2[8]), stbc=int(a2[9]), doppler=int(a2[15]))
    elif q1 < 0.5 and q2 > 2:
        out.update(fmt="VHT")
    else:
        out.update(fmt="legacy")
    out.update(q1=round(q1, 2), q2=round(q2, 2), rl=round(rl, 2))
    return out


def analyze(iq, max_frames=10):
    """Find legacy preambles (rx80211's STF/LTF search) and analyse each."""
    iq = np.asarray(iq, np.complex64)
    if len(iq) < 400:
        return []
    a = iq[16:] * np.conj(iq[:-16])
    c = rx80211._movsum(a, 48)
    p = rx80211._movsum(np.abs(iq[16:]) ** 2, 48)
    cand = np.flatnonzero(np.abs(c) / (p + 1e-9) > 0.85)
    ltf_t = rx80211.preamble()[192:256]
    res, i = [], 0
    while len(res) < max_frames:
        k = np.searchsorted(cand, i)
        if k >= len(cand):
            break
        i = int(cand[k])
        w0, w1 = i + 100, min(len(iq) - 64, i + 260)
        if w1 <= w0:
            break
        xc = np.abs(np.correlate(iq[w0:w1 + 64], ltf_t, "valid"))
        t0 = w0 + int(np.argmax(xc))
        # the strongest LTF peak can be either LTF symbol, and a wrong alignment still passes the L-SIG parity and
        # rate check about a quarter of the time: keep the alignment whose HT-SIG / HE-SIG-A CRC holds, else one whose
        # L-SIG says 6 Mbps (HT/VHT/HE always do), else the first that decodes
        cands = [c for c in (analyze_at(iq, t0), analyze_at(iq, t0 - 64)) if c]
        r = next((c for c in cands if c.get("crc_ok")), None) or \
            next((c for c in cands if c["lsig_rate"] == 6), None) or (cands[0] if cands else None)
        if r:
            r["start"] = i
            res.append(r)
            i += 400 + 80 * 4
            continue
        i += 160
    return res


if __name__ == "__main__":
    iq = np.fromfile(sys.argv[1], np.complex64)
    for r in analyze(iq):
        print(r)
