#!/usr/bin/env python3
"""TX rate meter: what timing does a transmitter's OFDM use? Reads an IQ record dump (tools/sdr/iqrec.py format, any
ICAP packer) that caught another card's bursts, and for the strongest bursts prints their duration, -20 dB span, STF
period (0.8 / 1.6 / 3.2 / 6.4 us for 20 / 10 / 5 / 2.5 MHz timing) and OFDM FFT period (3.2 / 6.4 / 12.8 / 25.6 us, from
the cyclic-prefix correlation); then tries rx80211 at 20 (if the capture is wide enough), 10, 5 and 2.5 MHz and counts
decodes with a valid SIGNAL field and with a valid FCS.
Usage: txmeter.py <records.bin> [packer 8]"""
import struct
import sys
from fractions import Fraction

import numpy as np
from scipy.signal import lfilter, resample_poly

import iqrec
import rx80211


def load(path, pack):
    ws, poss = [], []
    with open(path, "rb") as f:
        while True:
            h = f.read(16)
            if len(h) < 16:
                break
            mark, seq, pos, n = struct.unpack("<4I", h)
            d = f.read(n)
            if len(d) < n:
                break
            ws.append(np.frombuffer(d, np.uint32))
            poss.append(pos)
    runs, cur = [], [0]
    for k in range(1, len(ws)):
        if (poss[k] - poss[k - 1] - len(ws[k - 1])) % 2**32 == 0:
            cur.append(k)
        else:
            runs.append(cur)
            cur = [k]
    runs.append(cur)
    run = max(runs, key=len)
    # one preallocated complex64 array, filled record by record (a 10 s capture is 300 M samples)
    per = [len(iqrec.sample_words(ws[k][:8], pack)) * len(ws[k]) // 8 for k in run]
    x = np.empty(sum(per), np.complex64)
    o = 0
    for k, n in zip(run, per):
        x[o:o + n] = iqrec.words_to_iq(ws[k], pack)
        o += n
        ws[k] = None
    return x


def acorr_peak(y, lags):
    e = np.vdot(y, y).real + 1e-9
    ac = np.array([abs(np.vdot(y[:-L], y[L:])) / e for L in lags])
    return ac


def main():
    path = sys.argv[1]
    pack = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    fs = iqrec.PACKS[pack][0]
    x = load(path, pack)
    x -= x.mean()
    B = max(1, int(fs * 2e-6))  # 2 us power blocks
    p = (np.abs(x[:len(x) // B * B]) ** 2).reshape(-1, B).mean(1)
    on = p > 30 * np.median(p)
    edges = np.flatnonzero(np.diff(on.astype(np.int8)) == 1) + 1
    bursts = []
    for e in edges:
        k = e
        while k < len(on) and on[k]:
            k += 1
        if k - e >= 25:
            bursts.append((e * B, k * B))
    print(f"{len(x) / fs:.2f} s at {fs / 1e6:g} Msps: {len(bursts)} bursts >= 50 us")
    if not bursts:
        return
    durs = sorted(round((b - a) / fs * 1e6) for a, b in bursts)
    print(f"  burst durations us: median {durs[len(durs) // 2]}, range {durs[0]}..{durs[-1]}")
    hist = np.histogram(durs, [50, 100, 200, 400, 800, 1600, 3200, 1e9])[0]
    print("  durations " + ", ".join(f"{lo}-{hi}: {n}" for lo, hi, n in
                                     zip([50, 100, 200, 400, 800, 1600, 3200], [100, 200, 400, 800, 1600, 3200, "..."], hist) if n))
    bursts = sorted(bursts, key=lambda ab: -np.abs(x[ab[0]:ab[1]]).mean())  # strongest first: the card under test
    for a, b in bursts[:3]:
        y = x[a:b]
        S = np.abs(np.fft.fftshift(np.fft.fft(y * np.hanning(len(y)), 8192))) ** 2
        fr = (np.arange(8192) - 4096) / 8192 * fs / 1e6
        occ = fr[S > S.max() * 0.01]
        head = x[a:a + int(40e-6 * fs)]
        sl = np.arange(int(0.4e-6 * fs), int(8e-6 * fs))
        sac = acorr_peak(head, sl)
        stf = sl[np.argmax(sac >= 0.8 * sac.max())] / fs * 1e6  # smallest lag near the maximum
        body = y[int(80e-6 * fs):] if len(y) > int(160e-6 * fs) else y
        cl = np.arange(int(2e-6 * fs), int(30e-6 * fs))
        cac = acorr_peak(body, cl)
        cp = cl[np.argmax(cac)] / fs * 1e6
        print(f"  burst {(b - a) / fs * 1e6:7.1f} us: span {occ.min():+.2f}..{occ.max():+.2f} MHz; STF period {stf:.2f} us "
              f"(ac {sac.max():.2f}); FFT period {cp:.2f} us (ac {cac.max():.2f})")
    for width in (20.0, 10.0, 5.0, 2.5):
        if width > fs / 1e6:
            continue
        fr_ = Fraction(width / (fs / 1e6)).limit_denominator(1000)
        sig, fcs = 0, 0
        for a, b in bursts[:60]:
            seg = x[max(0, a - int(20e-6 * fs)):b + int(20e-6 * fs)]
            y = resample_poly(seg, fr_.numerator, fr_.denominator).astype(np.complex64) if fr_ != 1 else seg
            y = lfilter([1, -1], [1, -(1 - 2 * np.pi * 20e3 / (width * 1e6))], y).astype(np.complex64)
            res = rx80211.decode_all(y, 4, 0.0)
            sig += bool(res)
            fcs += any(r["fcs_ok"] or b"MT7921-NBW" in r["mpdu"] for r in res)
        print(f"  decode at {width:4.1f} MHz: SIGNAL ok in {sig} of {min(len(bursts), 60)} bursts, "
              f"FCS ok or payload intact in {fcs}")


if __name__ == "__main__":
    main()
