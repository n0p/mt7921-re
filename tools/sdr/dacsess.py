#!/usr/bin/env python3
"""Tone-DAC session analysis for tools/guest/dactssi.sh (notes/09 §5b): in an X410 capture (tools/sdr/capture.py),
find the carrier sessions (the keyed tone generator on for >= 0.3 s) and, for each, list the 5 strongest spectral
lines in its playback window (0.35-0.75 s into the session) as offset from the carrier line (expected near -17.6 kHz,
the card's frequency offset from the X410) and level relative to it. A playback that works shows the played tone
beside the carrier; a collapsed one shows the carrier alone.
Usage: dacsess.py <capture.cf32> <rate Msps>
"""
import sys
import numpy as np
x = np.fromfile(sys.argv[1], dtype=np.complex64)
fs = float(sys.argv[2]) * 1e6
W = int(fs / 100)
nw = len(x) // W
p = (np.abs(x[:nw * W].reshape(nw, W)) ** 2).mean(1)
on = p > 20 * np.percentile(p, 5)
runs, k = [], 0
while k < nw:
    if on[k]:
        j = k
        while j < nw and on[j]:
            j += 1
        if j - k >= 30:
            runs.append((k, j))
        k = j
    else:
        k += 1
print(f"{len(runs)} carrier sessions >= 0.3 s")
for a, b in runs:
    # the play happens ~0.3 s after the session start and lasts ~0.5 s: analyse 0.35..0.75 s into the session
    s0, s1 = int((a / 100 + 0.35) * fs), int((a / 100 + 0.75) * fs)
    seg = x[s0:s1]
    N = len(seg)
    S = np.abs(np.fft.fftshift(np.fft.fft(seg * np.hanning(N)))) ** 2
    f = (np.arange(N) - N // 2) * fs / N
    pk = []
    for j in np.argsort(S)[::-1]:
        if all(abs(f[j] - f[q]) > 300 for q in pk):
            pk.append(j)
        if len(pk) == 5:
            break
    c = pk[0] if abs(f[pk[0]] + 17.6e3) < 3e3 else min(pk, key=lambda q: abs(f[q] + 17.6e3))
    print(f"  {a / 100:6.2f}-{b / 100:6.2f} s ({(b - a) / 100:.2f} s, {10 * np.log10(p[a:b].mean() / np.percentile(p, 5)):.0f} dB): "
          + "  ".join(f"{(f[j] - f[c]) / 1e3:+8.2f} kHz {10 * np.log10(S[j] / S[c]):+6.1f}" for j in pk))
