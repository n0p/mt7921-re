#!/usr/bin/env python3
"""Lines around a keyed carrier in an X410 cf32 capture (tools/guest/dacbw.sh): per 0.25 s segment while the carrier is
on, the 4 strongest spectral lines as (offset from the carrier, dB relative to the strongest line). The carrier (the
tone generator's LO leakage + DC word) is taken as the strongest line of the first segment.
Usage: dacana.py <capture.cf32> <rate Msps>"""
import sys

import numpy as np

x = np.fromfile(sys.argv[1], dtype=np.complex64)
fs = float(sys.argv[2]) * 1e6
W = int(fs / 4)
nw = len(x) // W
X = x[:nw * W].reshape(nw, W)
p = (np.abs(X) ** 2).mean(1)
on = np.flatnonzero(p > 20 * np.percentile(p, 5))
if not len(on):
    sys.exit("no carrier")
win = np.hanning(W)
f = (np.arange(W) - W // 2) * fs / W
fc = None
for k in on:
    S = np.abs(np.fft.fftshift(np.fft.fft(X[k] * win))) ** 2
    pk = []
    for j in np.argsort(S)[::-1]:
        if all(abs(j - q) > 8 for q in pk):
            pk.append(j)
        if len(pk) == 4:
            break
    if fc is None:
        fc = f[pk[0]]
        print(f"carrier at {fc / 1e3:+.2f} kHz from the X410 LO")
    print(f"  t {k / 4:6.2f} s  " + "  ".join(f"{(f[j] - fc) / 1e3:+8.2f} kHz {10 * np.log10(S[j] / S[pk[0]]):+6.1f} dB"
                                       for j in pk))
