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
LTF_FREQ = None


def _ltf_freq():
    global LTF_FREQ
    if LTF_FREQ is None:
        p = preamble()
        LTF_FREQ = np.fft.fft(p[192:256])
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


def viterbi(llr, nbits):
    """llr: soft values per coded bit (positive = 1), length 2*nbits (depunctured with 0 for erasures)."""
    llr = np.asarray(llr, float).reshape(-1, 2)[:nbits]
    INF = 1e18
    pm = np.full(64, INF)
    pm[0] = 0
    prev = np.zeros((nbits, 64), int)
    pbit = np.zeros((nbits, 64), int)
    sgn = 2 * OUT - 1  # expected symbol +-1 per (state, bit, output)
    for t in range(nbits):
        cost = -(sgn[:, :, 0] * llr[t, 0] + sgn[:, :, 1] * llr[t, 1])  # (64, 2)
        cand = pm[:, None] + cost
        new = np.full(64, INF)
        for b in range(2):
            ns = NXT[:, b]
            c = cand[:, b]
            better = c < new[ns]
            # handle collisions: two states map to same next state per bit
            order = np.argsort(c)
            for s in order:
                n = ns[s]
                if c[s] < new[n]:
                    new[n] = c[s]
                    prev[t, n] = s
                    pbit[t, n] = b
        pm = new
    s = int(np.argmin(pm))
    bits = np.zeros(nbits, int)
    for t in range(nbits - 1, -1, -1):
        bits[t] = pbit[t, s]
        s = prev[t, s]
    return bits


def deinterleave(vals, ncbps, nbpsc):
    s = max(nbpsc // 2, 1)
    out = np.zeros(ncbps)
    for k in range(ncbps):
        i = (ncbps // 16) * (k % 16) + k // 16
        j = s * (i // s) + (i + ncbps - (16 * i) // ncbps) % s
        out[k] = vals[j]
    return out


def demap(syms, nbpsc):
    """Soft demap to LLR-like values (positive = bit 1)."""
    if nbpsc == 1:
        return syms.real
    if nbpsc == 2:
        return np.stack([syms.real, syms.imag], 1).ravel()
    lev = {4: np.array([-3, -1, 3, 1]), 6: np.array([-7, -5, -1, -3, 7, 5, 1, 3])}[nbpsc]
    norm = {4: np.sqrt(10), 6: np.sqrt(42)}[nbpsc]
    h = nbpsc // 2
    out = []
    for s in syms:
        for comp in (s.real * norm, s.imag * norm):
            d = np.abs(comp - lev) ** 2
            for bit in range(h):
                m1 = min(d[k] for k in range(len(lev)) if (k >> (h - 1 - bit)) & 1)
                m0 = min(d[k] for k in range(len(lev)) if not (k >> (h - 1 - bit)) & 1)
                out.append(m0 - m1)
    return np.array(out)


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
    seq = [int(b) for b in bits[:7]]
    while len(seq) < len(bits):
        seq.append(seq[-4] ^ seq[-7])
    return np.array([int(b) ^ s for b, s in zip(bits, seq)])


def ofdm_demod(sym80, h, idx):
    f = np.fft.fft(sym80[16:80]) / h
    pil = np.array([f[k % 64] for k in PILOT_SC]) * (PILOT_BASE * POLARITY[idx % 127])
    ph = np.angle(pil.sum())
    data = np.array([f[k % 64] for k in DATA_SC]) * np.exp(-1j * ph)
    return data


def decode_at(iq, t0):
    """t0 = index of the first LTF symbol (after the 32-sample LTF CP)."""
    if t0 + 128 + 80 > len(iq) or t0 < 32 + 144:
        return None
    # coarse CFO from the STF (lag 16, range +-1/32 cycle per sample), then fine CFO from the two LTF symbols
    # (lag 64, range +-1/128): the LTF alone aliases offsets beyond +-39 kHz at 5 MHz channel width
    stf = iq[t0 - 32 - 144:t0 - 32]
    coarse = np.angle(np.vdot(stf[:-16], stf[16:])) / (2 * np.pi * 16)
    n = np.arange(len(iq) - t0 + 32)
    y = iq[t0 - 32:] * np.exp(-2j * np.pi * coarse * (n - 32))
    l1, l2 = y[32:96], y[96:160]
    cfo = coarse + np.angle(np.vdot(l1, l2)) / (2 * np.pi * 64)
    x = iq[t0 - 32:] * np.exp(-2j * np.pi * cfo * (n - 32))
    x = x[32:]
    lt = (np.fft.fft(x[:64]) + np.fft.fft(x[64:128])) / 2
    h = lt / np.where(np.abs(_ltf_freq()) > 0, _ltf_freq(), 1)
    h[np.abs(_ltf_freq()) == 0] = 1
    pos = 128
    sig = ofdm_demod(x[pos:pos + 80], h, 0)
    sb = viterbi(deinterleave(sig.real, 48, 1), 24)
    rate_bits = int("".join(map(str, sb[:4])), 2)
    length = int("".join(map(str, sb[5:17][::-1])), 2)
    if rate_bits not in RATE_BY_BITS or sum(sb[:18]) % 2 or length == 0 or length > 4095:
        return None
    rate = RATE_BY_BITS[rate_bits]
    _, nbpsc, cr = RATES[rate]
    ncbps = 48 * nbpsc
    ndbps = ncbps * cr[0] // cr[1]
    nsym = -(-(16 + 8 * length + 6) // ndbps)
    pos += 80
    if pos + 80 * nsym > len(x):
        return None
    soft = []
    for k in range(nsym):
        d = ofdm_demod(x[pos + 80 * k:pos + 80 * (k + 1)], h, k + 1)
        soft.extend(deinterleave(demap(d, nbpsc), ncbps, nbpsc))
    coded = depuncture(np.array(soft), cr)
    bits = viterbi(coded, nsym * ndbps)
    data = descramble(bits)
    psdu = np.packbits(data[16:16 + 8 * length].reshape(-1, 8)[:, ::-1], axis=1).ravel().tobytes()
    ok = len(psdu) >= 4 and struct.unpack("<I", psdu[-4:])[0] == zlib.crc32(psdu[:-4])
    return dict(rate=rate, length=length, fcs_ok=ok, mpdu=psdu, cfo=cfo)


def decode_all(iq, max_frames=50):
    iq = np.asarray(iq, np.complex64)
    # STF detection: autocorrelation at lag 16 over a 48-sample window
    a = iq[16:] * np.conj(iq[:-16])
    c = np.convolve(a, np.ones(48), "valid")
    p = np.convolve(np.abs(iq[16:]) ** 2, np.ones(48), "valid")
    m = np.abs(c) / (p + 1e-9)
    res, i = [], 0
    ltf_t = preamble()[192:256]
    while i < len(m) and len(res) < max_frames:
        if m[i] > 0.85:
            # find LTF by cross-correlation in a window after the STF start
            w0, w1 = i + 100, min(len(iq) - 64, i + 260)
            if w1 <= w0:
                break
            xc = np.abs(np.correlate(iq[w0:w1 + 64], ltf_t, "valid"))
            k = w0 + int(np.argmax(xc))
            # the strongest peak can be either LTF symbol: try both alignments
            r = decode_at(iq, k) or decode_at(iq, k - 64)
            if r:
                r["start"] = i
                res.append(r)
                i += 400 + 80 * 4
                continue
            i += 160
        else:
            i += 1
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
