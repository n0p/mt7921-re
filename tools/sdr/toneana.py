#!/usr/bin/env python3
"""Phase/amplitude track of a near-DC CW (tools/guest/tonedac.sh): per 10 ms window, the CW's frequency is removed
(estimated over the whole run) and the mean complex value printed, so a live change of the transmitter's DC I/Q value
shows up as a phase or amplitude step.
Usage: toneana.py <packer-2 records.bin>  |  toneana.py <capture.cf32> <rate Msps>  (an X410 capture, capture.py)"""
import struct
import sys

import numpy as np

import iqrec

if len(sys.argv) > 2:  # raw cf32 capture
    fs = float(sys.argv[2]) * 1e6
    x = np.fromfile(sys.argv[1], dtype=np.complex64).astype(np.complex128)
    ws = None
else:
    ws, poss = [], []
    fs = 10e6
f = open(sys.argv[1], "rb") if ws is not None else None
while f:
    h = f.read(16)
    if len(h) < 16:
        break
    mark, seq, pos, n = struct.unpack("<4I", h)
    d = f.read(n)
    if len(d) < n:
        break
    ws.append(np.frombuffer(d, np.uint32))
    poss.append(pos)
if ws is not None:
    runs, cur = [], [0]
    for k in range(1, len(ws)):
        if (poss[k] - poss[k - 1] - len(ws[k - 1])) % 2**32 == 0:
            cur.append(k)
        else:
            runs.append(cur)
            cur = [k]
    runs.append(cur)
    run = max(runs, key=len)
    x = iqrec.words_to_iq(np.concatenate([ws[k] for k in run]), 2).astype(np.complex128)
W = int(fs / 100)  # 10 ms
nw = len(x) // W
p = (np.abs(x[:nw * W]) ** 2).reshape(nw, W).mean(1)
on = p > 20 * np.percentile(p, 5)  # the tone may be on for most of the capture: compare with the noise floor
print(f"{len(x) / fs:.2f} s contiguous; tone present in {on.sum()} of {nw} 10 ms windows")
if on.any():
    y = x[:nw * W].reshape(nw, W)[on]
    # CW frequency from the lag-1 product over the tone windows
    f0 = np.angle(np.sum(y[:, 1:] * np.conj(y[:, :-1]))) / (2 * np.pi) * fs
    idx = np.flatnonzero(on)
    n0 = idx[:, None] * W + np.arange(W)[None, :]
    z = (y * np.exp(-2j * np.pi * f0 / fs * n0)).mean(1)
    print(f"CW at {f0 / 1e3:+.2f} kHz from the receiver's LO")
    prev = None
    for k, v in zip(idx, z):
        ph = np.degrees(np.angle(v))
        step = "" if prev is None else f"  step {((ph - prev + 180) % 360) - 180:+7.1f} deg"
        if prev is None or abs(((ph - prev + 180) % 360) - 180) > 10:
            print(f"  t {k * 10:6d} ms  |z| {abs(v):8.1f}  phase {ph:+7.1f} deg{step}")
        prev = ph
