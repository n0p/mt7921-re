#!/usr/bin/env python3
"""Decode ÷8 (2.5 MHz OFDM) frames of the MT7921 node framing (nbrxd --width 2.5: inner CRC32 + tail padding, since
the transmitter drops the last 3 OFDM symbols) from an X410 cf32 capture (tools/sdr/capture.py).
Usage: div8dec.py <capture.cf32> <rate Msps>   prints the decoded NBTX/inner-CRC frames and a summary."""
import re
import struct
import sys
import zlib
from fractions import Fraction

import numpy as np
from scipy.signal import lfilter, resample_poly

import rx80211

NDBPS = {12: 24, 18: 36, 24: 48, 36: 72, 48: 96, 72: 144, 96: 192, 108: 216}


def unwrap(r, pad_syms=3):
    m = r["mpdu"]
    pad = (pad_syms + 1) * NDBPS[r["rate"] * 2] // 8
    end = len(m) - 4 - pad
    if end >= 36 and struct.unpack("<I", m[end - 4:end])[0] == zlib.crc32(m[24:end - 4]):
        return m[:end - 4]
    return None


def main():
    x = np.fromfile(sys.argv[1], dtype=np.complex64)
    fs = float(sys.argv[2]) * 1e6
    fr = Fraction(2.5e6 / fs).limit_denominator(1000)
    good, bad, ids = 0, 0, []
    B = int(fs)  # 1 s blocks with 50 ms overlap
    for s0 in range(0, len(x), B):
        seg = x[max(0, s0 - int(0.05 * fs)):s0 + B].astype(np.complex64)
        y = resample_poly(seg - seg.mean(), fr.numerator, fr.denominator).astype(np.complex64) if fr != 1 else seg
        y = lfilter([1, -1], [1, -(1 - 2 * np.pi * 20e3 / 2.5e6)], y).astype(np.complex64)
        for r in rx80211.decode_all(y, 2000, -1.0):
            core = unwrap(r)
            if core:
                good += 1
                m = re.search(rb"NBTX-(\d{3})-(\d{3})", core)
                ids.append(m.group(0).decode() if m else core[24:40])
            else:
                bad += 1
    uniq = sorted(set(i for i in ids if isinstance(i, str)))
    print(f"{len(x) / fs:.1f} s: {good} frames with a valid inner CRC ({len(uniq)} distinct NBTX ids), {bad} others")
    windows = sorted(set(i.split("-")[1] for i in uniq))
    print(f"TX windows seen: {len(windows)}; frames per window: "
          f"{[sum(1 for i in uniq if i.split('-')[1] == w) for w in windows][:40]}")


if __name__ == "__main__":
    main()
