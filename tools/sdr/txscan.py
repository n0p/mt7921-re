#!/usr/bin/env python3
"""Streaming TX timing scan: reads an IQ record dump (tools/sdr/iqrec.py format) record by record, finds the bursts,
measures each burst's STF period and OFDM FFT period (20 / 10 / 5 / 2.5 MHz timing: 0.8 / 1.6 / 3.2 / 6.4 us and
3.2 / 6.4 / 12.8 / 25.6 us), decodes it at the width its FFT period implies, and groups the bursts into the
configurations of a tools/guest/txscan.py run (groups are separated by silences of at least the group gap).
Memory stays small: only the first MAXKEEP of each burst is kept.
Usage: txscan.py <records.bin> [packer 8] [group gap ms 150] [labels file]"""
import struct
import sys
from fractions import Fraction

import numpy as np
from scipy.signal import lfilter, resample_poly

import iqrec
import rx80211

MAXKEEP_S = 3e-3
MAXSHOW = 6  # bursts printed per group (continuous sources make thousands)
WIDTHS = {3.2: 20.0, 6.4: 10.0, 12.8: 5.0, 25.6: 2.5}


def records(path):
    with open(path, "rb") as f:
        while True:
            h = f.read(16)
            if len(h) < 16:
                return
            mark, seq, pos, n = struct.unpack("<4I", h)
            d = f.read(n)
            if len(d) < n:
                return
            yield pos, np.frombuffer(d, np.uint32)


def posgap(pos, last, spw):
    """Samples skipped between two records (positions in ring words, mod 2^32). A backward jump (the stream restarted)
    counts as a 1 ms gap, not as 2^32 words."""
    d = (pos - last[0] - last[1]) % 2 ** 32
    return int(d * spw) if d < 2 ** 31 else int(1e-3 * 30e6)


def acorr(y, lags):
    e = np.vdot(y, y).real + 1e-9
    return np.array([abs(np.vdot(y[:-L], y[L:])) / e for L in lags])


def measure(y, fs):
    head = y[:int(40e-6 * fs)]
    sl = np.arange(int(0.4e-6 * fs), int(8e-6 * fs))
    sac = acorr(head, sl)
    stf = sl[np.argmax(sac >= 0.8 * sac.max())] / fs * 1e6
    body = y[int(80e-6 * fs):] if len(y) > int(160e-6 * fs) else y
    cl = np.arange(int(2e-6 * fs), int(30e-6 * fs))
    cac = acorr(body, cl)
    fft = cl[np.argmax(cac)] / fs * 1e6
    return stf, sac.max(), fft, cac.max()


def decode(seg, fs, width):
    fr = Fraction(width / (fs / 1e6)).limit_denominator(1000)
    y = resample_poly(seg, fr.numerator, fr.denominator).astype(np.complex64) if fr != 1 else seg
    y = lfilter([1, -1], [1, -(1 - 2 * np.pi * 20e3 / (width * 1e6))], y).astype(np.complex64)
    res = rx80211.decode_all(y, 2, 0.0)
    if not res:
        return "-"
    r = res[0]
    ok = r["fcs_ok"] or b"MT7921-NBW" in r["mpdu"]
    k = r["mpdu"].find(b"MT7921-NBW-")
    tag = r["mpdu"][k + 11:k + 19].decode("ascii", "replace") if k >= 0 else ""
    return f"{r['rate']}M/{r['length']}B {'ok' if ok else 'bad'}" + (f" id={tag}" if tag.isdigit() else "")


def bursts(path, pack):
    """Two passes: block powers (2 us) over the whole dump, then the samples of each burst only."""
    fs = iqrec.PACKS[pack][0]
    B = max(1, int(fs * 2e-6))
    ps, ts = [], []
    t, last, carry = 0, None, np.zeros(0, np.complex64)
    for pos, w in records(path):
        x = iqrec.words_to_iq(w, pack).astype(np.complex64)
        if last is not None:
            gap = posgap(pos, last, len(x) / len(w))
            if gap:
                carry = np.zeros(0, np.complex64)
                t += gap
        last = (pos, len(w))
        x0 = t - len(carry)
        x = np.concatenate([carry, x])
        n = len(x) // B * B
        carry = x[n:]
        ps.append((np.abs(x[:n].reshape(-1, B)) ** 2).mean(1).astype(np.float32))
        ts.append(x0 + B * np.arange(n // B, dtype=np.int64))
        t = x0 + len(x)
    p, bt = np.concatenate(ps), np.concatenate(ts)
    noise = float(np.median(p[:max(1, int(0.1 * fs / B))]))
    on = p > 30 * noise
    # runs of "on" blocks, bridging single low blocks between time-contiguous blocks
    on[1:-1] |= on[:-2] & on[2:] & (bt[2:] - bt[:-2] == 2 * B)
    d = np.diff(np.concatenate([[0], on.astype(np.int8), [0]]))
    starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    spans = [(int(bt[s0]), int(bt[e0 - 1]) + B) for s0, e0 in zip(starts, ends)]
    pre, keep = int(20e-6 * fs), int(MAXKEEP_S * fs)
    want = [(a - pre, min(e, a + keep)) for a, e in spans]
    bufs = [np.zeros(e - a, np.complex64) for a, e in want]
    k0, t, last = 0, 0, None
    for pos, w in records(path):
        x = iqrec.words_to_iq(w, pack).astype(np.complex64)
        if last is not None:
            t += posgap(pos, last, len(x) / len(w))
        last = (pos, len(w))
        while k0 < len(want) and want[k0][1] <= t:
            k0 += 1
        k = k0
        while k < len(want) and want[k][0] < t + len(x):
            a, e = want[k]
            lo, hi = max(a, t), min(e, t + len(x))
            if hi > lo:
                bufs[k][lo - a:hi - a] = x[lo - t:hi - t]
            k += 1
        t += len(x)
    return fs, noise, [(a, e, buf) for (a, e), buf in zip(spans, bufs)]


def main():
    path = sys.argv[1]
    pack = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    gap_ms = float(sys.argv[3]) if len(sys.argv) > 3 else 150.0
    labels = open(sys.argv[4]).read().split("\n") if len(sys.argv) > 4 else []
    fs, noise, bs = bursts(path, pack)
    bs = [b for b in bs if b[1] - b[0] >= 50e-6 * fs]
    print(f"{len(bs)} bursts >= 50 us at {fs / 1e6:g} Msps (noise {noise:.1f})")
    groups, g = [], []
    for b in bs:
        if g and (b[0] - g[-1][1]) > gap_ms * 1e-3 * fs:
            groups.append(g)
            g = []
        g.append(b)
    if g:
        groups.append(g)
    print(f"{len(groups)} groups (gap >= {gap_ms:g} ms)" + (f", {len([x for x in labels if x])} labels" if labels else ""))
    pre = int(20e-6 * fs)
    for i, g in enumerate(groups):
        lab = labels[i] if i < len(labels) else ""
        print(f"group {i} {lab}")
        tally = {}
        for j, (a, e, y) in enumerate(g):
            body = y[pre:]
            stf, sa, fft, ca = measure(body, fs)
            near = min(WIDTHS, key=lambda v: abs(v - fft))
            width = WIDTHS[near] if abs(near - fft) < 0.08 * near else None
            tally[width] = tally.get(width, 0) + 1
            if j >= MAXSHOW:
                continue
            dec = decode(y, fs, width) if width and width <= fs / 1e6 else "-"
            pw = 10 * np.log10(np.mean(np.abs(body[:int(40e-6 * fs)]) ** 2) / noise)
            S = np.abs(np.fft.fftshift(np.fft.fft(body * np.hanning(len(body)), 8192))) ** 2
            occ = ((np.arange(8192) - 4096) / 8192 * fs / 1e6)[S > S.max() * 0.01]
            print(f"  t {a / fs:8.3f} s  {(e - a) / fs * 1e6:9.1f} us  {pw:5.1f} dB  STF {stf:5.2f} us ({sa:.2f})  "
                  f"FFT {fft:5.2f} us ({ca:.2f})  span {occ.min():+5.2f}..{occ.max():+5.2f} MHz  "
                  f"-> {str(width or '?'):>4} MHz  {dec}")
        if len(g) > MAXSHOW:
            print(f"  ... {len(g)} bursts in all, by width (MHz): " +
                  ", ".join(f"{w or '?'}: {n}" for w, n in sorted(tally.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()
