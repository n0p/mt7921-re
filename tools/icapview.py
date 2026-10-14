#!/usr/bin/env python3
"""Inspect an ICAP capture (int16 I,Q interleaved): amplitude stats, spectrum (ASCII) and occupied width.
Usage: icapview.py <file> [sample rate Msps]"""
import sys
import numpy as np

x = np.fromfile(sys.argv[1], dtype=np.int16).astype(np.float32)
fs = float(sys.argv[2]) * 1e6 if len(sys.argv) > 2 else 40e6
iq = x[0::2] + 1j * x[1::2]
print(f"{len(iq)} samples, |x| mean {np.abs(iq).mean():.1f} max {np.abs(iq).max():.0f}, DC {iq.mean():.1f}")
n = 256
seg = iq[: len(iq) // n * n].reshape(-1, n) * np.hanning(n)
p = (np.abs(np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)) ** 2).mean(axis=0)
db = 10 * np.log10(p + 1e-9)
fl = np.median(db)
coarse = db.reshape(64, -1).mean(axis=1)
print(f"spectrum {-fs/2e6:+.1f}..{fs/2e6:+.1f} MHz (floor {fl:.1f} dB, peak {db.max():.1f} dB):")
print("  " + "".join(" .:-=+*#%@"[max(0, min(9, int((v - fl) / 3)))] for v in coarse))
above = np.where(db >= db.max() - 20)[0]
print(f"-20 dB width {(above[-1] - above[0] + 1) * fs / n / 1e6:.2f} MHz")
