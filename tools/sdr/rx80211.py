#!/usr/bin/env python3
"""Minimal IEEE 802.11a/p OFDM receiver (numpy). Works on samples at the 20 Msps-equivalent rate, i.e. a 10 MHz
(802.11p) capture at 10 Msps or a 5 MHz one at 5 Msps is decoded unchanged.

Library: decode_all(iq) -> list of dicts {start, rate, length, fcs_ok, mpdu, cfo}
CLI:     rx80211.py <file.cf32 | file.cs16> [selftest]
"""
import struct
import sys
import zlib

import numpy as np

from ofdm80211 import DATA_SC, PILOT_SC, PILOT_BASE, POLARITY, RATES, preamble

RATE_BY_BITS = {v[0]: k for k, v in RATES.items()}
STF_THRESH = 0.6  # detection threshold on the normalised STF autocorrelation (see decode_all; notes/10)
H_SMOOTH = 1  # channel-estimate smoothing: average each subcarrier with this many neighbours per side (0 = off; notes/10)
LTF_FREQ = None


def _ltf_freq():
    global LTF_FREQ
    if LTF_FREQ is None:
        p = preamble()
        # unit (+-1) subcarriers: the transmitter's IFFT gain 64/sqrt(52) would otherwise remain in every equalised
        # data symbol and break the QAM level decisions (BPSK/QPSK only use the sign)
        LTF_FREQ = np.fft.fft(p[192:256]) * np.sqrt(52) / 64
    return LTF_FREQ


# --- Viterbi (K=7, g0=133, g1=171) ---
def _trellis():
    nxt = np.zeros((64, 2), int)
    out = np.zeros((64, 2, 2), int)
    for s in range(64):
        for b in range(2):
            r = [b] + [(s >> (5 - i)) & 1 for i in range(6)]
            out[s, b] = [r[0] ^ r[2] ^ r[3] ^ r[5] ^ r[6], r[0] ^ r[1] ^ r[2] ^ r[3] ^ r[6]]
            nxt[s, b] = ((s >> 1) | (b << 5))
    return nxt, out


NXT, OUT = _trellis()


# Each next state ns has two predecessors, 2(ns & 31) and 2(ns & 31) + 1, both entered with input bit ns >> 5
_NS = np.arange(64)
_P0 = 2 * (_NS & 31)
_P1 = _P0 + 1
_BIN = _NS >> 5
_SG = 2 * OUT - 1  # expected symbol +-1 per (state, bit, output)
_S0A, _S0B = _SG[_P0, _BIN, 0].astype(float), _SG[_P0, _BIN, 1].astype(float)
_S1A, _S1B = _SG[_P1, _BIN, 0].astype(float), _SG[_P1, _BIN, 1].astype(float)


_CVIT = None


def _cviterbi():
    """vit.c through ctypes, compiled once per source version into the temp dir; None if no C compiler."""
    global _CVIT
    if _CVIT is None:
        _CVIT = False
        try:
            import ctypes
            import os
            import subprocess
            import tempfile
            src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vit.c")
            so = os.path.join(tempfile.gettempdir(), f"rx80211_vit_{os.getuid()}_{int(os.path.getmtime(src))}.so")
            if not os.path.exists(so):
                tmp = f"{so}.{os.getpid()}"
                subprocess.run(["cc", "-O2", "-shared", "-fPIC", "-o", tmp, src], check=True, capture_output=True)
                os.replace(tmp, so)
            f = ctypes.CDLL(so).viterbi
            f.restype = ctypes.c_int
            f.argtypes = [ctypes.c_void_p, ctypes.c_int] + [ctypes.c_void_p] * 5
            tabs = [np.ascontiguousarray(t, np.int8) for t in (_S0A, _S0B, _S1A, _S1B)]
            _CVIT = (f, tabs)
        except (OSError, subprocess.SubprocessError):
            pass
    return _CVIT or None


def viterbi(llr, nbits):
    """llr: soft values per coded bit (positive = 1), length 2*nbits (depunctured with 0 for erasures)."""
    c = _cviterbi()
    if c:
        f, tabs = c
        v = np.ascontiguousarray(np.asarray(llr, float).ravel()[:2 * nbits])
        out = np.zeros(nbits, np.uint8)
        if len(v) == 2 * nbits and f(v.ctypes.data, nbits, *(t.ctypes.data for t in tabs), out.ctypes.data) == 0:
            return out.astype(int)
    llr = np.asarray(llr, float).reshape(-1, 2)[:nbits]
    pm = np.full(64, 1e18)
    pm[0] = 0
    dec = np.zeros((nbits, 64), bool)
    for t in range(nbits):
        l0, l1 = llr[t]
        c0 = pm[_P0] - (_S0A * l0 + _S0B * l1)
        c1 = pm[_P1] - (_S1A * l0 + _S1B * l1)
        d = c1 < c0
        dec[t] = d
        pm = np.where(d, c1, c0)
        pm -= pm.min()
    s = int(np.argmin(pm))
    bits = np.zeros(nbits, int)
    for t in range(nbits - 1, -1, -1):
        bits[t] = s >> 5
        s = int(_P0[s] + dec[t, s])
    return bits


_PERM = {}


def _perm(ncbps, nbpsc):
    """Interleaved position of each coded bit of one OFDM symbol."""
    key = (ncbps, nbpsc)
    if key not in _PERM:
        s = max(nbpsc // 2, 1)
        k = np.arange(ncbps)
        i = (ncbps // 16) * (k % 16) + k // 16
        _PERM[key] = s * (i // s) + (i + ncbps - (16 * i) // ncbps) % s
    return _PERM[key]


def deinterleave(vals, ncbps, nbpsc):
    return np.asarray(vals)[_perm(ncbps, nbpsc)]


_QAM = {}


def demap(syms, nbpsc):
    """Soft demap to LLR-like values (positive = bit 1)."""
    if nbpsc == 1:
        return syms.real
    if nbpsc == 2:
        return np.stack([syms.real, syms.imag], 1).ravel()
    if nbpsc not in _QAM:
        lev = {4: np.array([-3, -1, 3, 1]), 6: np.array([-7, -5, -1, -3, 7, 5, 1, 3])}[nbpsc]
        h = nbpsc // 2
        ones = [np.array([(k >> (h - 1 - b)) & 1 for k in range(len(lev))], bool) for b in range(h)]
        _QAM[nbpsc] = (lev, {4: np.sqrt(10), 6: np.sqrt(42)}[nbpsc], ones)
    lev, norm, ones = _QAM[nbpsc]
    comp = np.stack([syms.real, syms.imag], 1) * norm  # (n, 2)
    d = np.abs(comp[:, :, None] - lev[None, None, :]) ** 2  # (n, 2, levels)
    out = [d[:, :, ~m].min(2) - d[:, :, m].min(2) for m in ones]  # per bit, (n, 2)
    return np.stack(out, 2).ravel()  # per symbol: real bits, then imaginary bits


def depuncture(vals, rate):
    if rate == (1, 2):
        return vals
    pat = {(3, 4): [1, 1, 1, 0, 0, 1], (2, 3): [1, 1, 1, 0]}[rate]
    out, k = [], 0
    while k < len(vals):
        for p in pat:
            if p:
                if k >= len(vals):
                    break
                out.append(vals[k])
                k += 1
            else:
                out.append(0.0)
    return np.array(out)


def descramble(bits):
    """x^7+x^4+1: the first 7 received SERVICE bits are the scrambler sequence itself (data bits are 0),
    and the sequence obeys s[n] = s[n-4] ^ s[n-7]."""
    bits = np.asarray(bits, dtype=np.int64)
    seq = [int(b) for b in bits[:7]]
    while len(seq) < 127 + 7:  # the sequence repeats every 127 bits
        seq.append(seq[-4] ^ seq[-7])
    return bits ^ np.resize(np.array(seq[:127], dtype=np.int64), len(bits))


_PIL = np.array([k % 64 for k in PILOT_SC])
_DAT = np.array([k % 64 for k in DATA_SC])
_POL = np.asarray(POLARITY)


def ofdm_demod(syms80, h, idx):
    """syms80: (n, 80) OFDM symbols (or one of 80 samples) with symbol indices idx.. idx+n-1 -> (n, 48) data
    subcarriers, equalised and corrected for the common phase error of the pilots."""
    syms80 = np.asarray(syms80).reshape(-1, 80)
    f = np.fft.fft(syms80[:, 16:80], axis=1) / h
    pol = _POL[(idx + np.arange(len(f))) % 127]
    pil = (f[:, _PIL] * (np.asarray(PILOT_BASE)[None, :] * pol[:, None])).sum(1)
    return f[:, _DAT] * np.exp(-1j * np.angle(pil))[:, None]


def decode_at(iq, t0, min_snr=None, max_len=4095):
    """t0 = index of the first LTF symbol (after the 32-sample LTF CP). min_snr (dB): give up before the SIGNAL
    decode when the LTF says the candidate is noise; max_len: largest PSDU worth decoding."""
    if t0 + 128 + 80 > len(iq) or t0 < 32 + 144:
        return None
    # coarse CFO from the STF (lag 16, range +-1/32 cycle per sample), then fine CFO from the two LTF symbols
    # (lag 64, range +-1/128): the LTF alone aliases offsets beyond +-39 kHz at 5 MHz channel width
    def derotate(a, b, f):  # iq[t0 + a:t0 + b] with the offset f (cycles/sample) removed, phase 0 at t0
        k = np.arange(a, b)
        return iq[t0 + a:t0 + b] * np.exp(-2j * np.pi * f * k)

    stf = iq[t0 - 32 - 144:t0 - 32]
    coarse = np.angle(np.vdot(stf[:-16], stf[16:])) / (2 * np.pi * 16)
    y = derotate(0, 128, coarse)
    cfo = coarse + np.angle(np.vdot(y[:64], y[64:])) / (2 * np.pi * 64)
    x = derotate(0, 208, cfo)  # both LTF symbols and SIGNAL
    f1, f2 = np.fft.fft(x[:64]), np.fft.fft(x[64:128])
    lt = (f1 + f2) / 2
    # SNR from the two LTF symbols on the occupied subcarriers: |f1+f2|^2/4 = S + N/2, |f1-f2|^2/4 = N/2
    occ = np.abs(_ltf_freq()) > 0
    ps, pd = np.mean(np.abs(lt[occ]) ** 2), np.mean(np.abs(f1[occ] - f2[occ]) ** 2) / 4
    snr = 10 * np.log10(max(ps - pd, 1e-12) / max(2 * pd, 1e-12))
    if min_snr is not None and snr < min_snr:
        return None
    # A CW spur autocorrelates like an STF at any lag and repeats exactly across the LTF (so its "SNR" is high), but
    # it fills one or two of the 52 subcarriers; a real LTF spreads its energy over all of them.
    e = np.abs(lt[occ]) ** 2
    if e.max() > 0.25 * e.sum():
        return None
    h = lt / np.where(np.abs(_ltf_freq()) > 0, _ltf_freq(), 1)
    h[np.abs(_ltf_freq()) == 0] = 1
    if H_SMOOTH:
        # average each occupied subcarrier's estimate with its neighbours (the two LTF symbols alone leave the estimate
        # as noisy as the data at low SNR; a narrow channel's response changes little across a few subcarriers)
        k = np.arange(-26, 27)
        k = k[k != 0]
        hs = h.copy()
        for kk in k:
            nb = [j for j in range(kk - H_SMOOTH, kk + H_SMOOTH + 1) if j != 0 and -26 <= j <= 26]
            hs[kk % 64] = np.mean([h[j % 64] for j in nb])
        h = hs
    # channel-state weights for the soft bits: zero-forcing alone lets the noise of subcarriers attenuated by the
    # receive filters' edges dominate the Viterbi metric
    w = np.abs(np.array([h[k % 64] for k in DATA_SC])) ** 2
    w /= w.mean()
    pos = 128
    sig = ofdm_demod(x[pos:pos + 80], h, 0)[0]
    sb = viterbi(deinterleave(sig.real * w, 48, 1), 24)
    rate_bits = int("".join(map(str, sb[:4])), 2)
    length = int("".join(map(str, sb[5:17][::-1])), 2)
    if rate_bits not in RATE_BY_BITS or sum(sb[:18]) % 2 or length == 0 or length > max_len:
        return None
    rate = RATE_BY_BITS[rate_bits]
    _, nbpsc, cr = RATES[rate]
    ncbps = 48 * nbpsc
    ndbps = ncbps * cr[0] // cr[1]
    nsym = -(-(16 + 8 * length + 6) // ndbps)
    if t0 + 208 + 80 * nsym > len(iq):
        return None
    d = ofdm_demod(derotate(208, 208 + 80 * nsym, cfo), h, 1)
    soft = demap(d.ravel(), nbpsc).reshape(nsym, ncbps) * np.repeat(w, nbpsc)[None, :]
    coded = depuncture(soft[:, _perm(ncbps, nbpsc)].ravel(), cr)
    bits = viterbi(coded, nsym * ndbps)
    data = descramble(bits)
    psdu = np.packbits(data[16:16 + 8 * length].reshape(-1, 8)[:, ::-1], axis=1).ravel().tobytes()
    ok = len(psdu) >= 4 and struct.unpack("<I", psdu[-4:])[0] == zlib.crc32(psdu[:-4])
    return dict(rate=rate, length=length, fcs_ok=ok, mpdu=psdu, cfo=cfo, snr=snr)


def _movsum(v, n):
    """np.convolve(v, np.ones(n), "valid") in O(len(v))."""
    cs = np.cumsum(v, dtype=np.complex128 if np.iscomplexobj(v) else np.float64)
    out = cs[n - 1:].copy()
    out[1:] -= cs[:-n]
    return out


def decode_all(iq, max_frames=50, min_snr=None, max_len=4095):
    iq = np.asarray(iq, np.complex64)
    if len(iq) < 64:
        return []
    # STF detection: autocorrelation at lag 16 over a 48-sample window
    a = iq[16:] * np.conj(iq[:-16])
    c = _movsum(a, 48)
    p = _movsum(np.abs(iq[16:]) ** 2, 48)
    m = np.abs(c) / (p + 1e-9)
    res, i = [], 0
    ltf_t = preamble()[192:256]
    # STF candidates; jump between them instead of walking every sample. The normalised lag-16 autocorrelation of an
    # STF in white noise is about SNR / (SNR + 1), so the threshold sets the detection floor: 0.85 needs ~7.5 dB
    # (notes/10). False alarms are cheap: L-SIG parity and the FCS reject them.
    cand = np.flatnonzero(m > STF_THRESH)
    while len(res) < max_frames:
        k = np.searchsorted(cand, i)
        if k >= len(cand):
            break
        i = int(cand[k])
        # find LTF by cross-correlation in a window after the STF start
        w0, w1 = i + 100, min(len(iq) - 64, i + 260)
        if w1 <= w0:
            break
        # derotate the search window by the STF's coarse CFO first: uncorrected, the 64-sample LTF correlation
        # collapses like a sinc (first null at 1/64 cycle/sample) and picks a wrong lag beyond about +-0.009
        # cycles/sample, i.e. +-45 kHz at 5 Msps, less than two +-20 ppm crystals at 2.4 GHz can differ
        seg = iq[i:i + 144]
        f0 = np.angle(np.vdot(seg[:-16], seg[16:])) / (2 * np.pi * 16)
        win = iq[w0:w1 + 64] * np.exp(-2j * np.pi * f0 * np.arange(w1 + 64 - w0))
        xc = np.abs(np.correlate(win, ltf_t, "valid"))
        k = w0 + int(np.argmax(xc))
        # the strongest peak can be either LTF symbol: try both alignments
        r = decode_at(iq, k, min_snr, max_len) or decode_at(iq, k - 64, min_snr, max_len)
        if r:
            r["start"] = i
            res.append(r)
            i += 400 + 80 * 4
            continue
        i += 160
    return res


def main():
    if sys.argv[1] == "selftest":
        from ofdm80211 import mpdu_data, ppdu
        f = mpdu_data(bytes.fromhex("020000000002"), b"RX80211-SELFTEST" + bytes(40))
        sig = np.concatenate([np.zeros(200, complex), ppdu(f, 6), np.zeros(300, complex), ppdu(f, 24), np.zeros(200)])
        rng = np.random.default_rng(1)
        sig = sig * np.exp(2j * np.pi * 0.0013 * np.arange(len(sig))) + 0.05 * (rng.standard_normal(len(sig)) + 1j * rng.standard_normal(len(sig)))
        for r in decode_all(sig):
            print({k: v for k, v in r.items() if k != "mpdu"}, r["mpdu"][24:40])
        return
    raw = np.fromfile(sys.argv[1], dtype=np.int16 if sys.argv[1].endswith("cs16") else np.complex64)
    iq = raw[0::2] + 1j * raw[1::2] if raw.dtype == np.int16 else raw
    for r in decode_all(iq):
        print({k: v for k, v in r.items() if k != "mpdu"}, r["mpdu"][:40].hex())


if __name__ == "__main__":
    main()
