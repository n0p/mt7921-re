#!/usr/bin/env python3
"""Software narrow-band receiver for the MT7921 IQ stream (tools/guest/iqstream.sh -> mt7921dbg iq records).

Each contiguous segment of the 40 Msps stream is resampled to W Msps (the 20 Msps-equivalent rate of a W MHz OFDM
channel, so 5 for a 5 MHz channel, 10 for 802.11p) and fed to the rx80211 802.11a/p receiver.

Usage: iqdec.py <records.bin> [width MHz, default 5] [offset MHz, default 0]
  offset: channel centre relative to the card's LO (the capture spans +-20 MHz)
Prints one line per decoded frame (segment time, rate, length, FCS, SA, payload start) and a summary.
"""
import sys
from fractions import Fraction

import numpy as np
from scipy.signal import lfilter, resample_poly

import iqrec
import rx80211


def decode(path, width=5.0, offset=0.0, verbose=True):
    segs = iqrec.segments(path)
    fr = Fraction(width / (iqrec.FS / 1e6)).limit_denominator(1000)
    t0 = segs[0][0] if segs else 0
    out = []
    for pos, x in segs:
        x = x.astype(np.complex64)
        if offset:
            x = x * np.exp(-2j * np.pi * offset * 1e6 / iqrec.FS * np.arange(len(x))).astype(np.complex64)
        y = resample_poly(x - x.mean(), fr.numerator, fr.denominator).astype(np.complex64)
        if len(y) < 400:
            continue
        # DC notch (one-pole high-pass, ~20 kHz): removes the receiver's DC offset and the carrier leakage of a nearby
        # transmitter (a few kHz off DC), which would otherwise look like an endless STF to the detector. 802.11 OFDM
        # leaves the DC subcarrier empty and the first subcarrier is 78 kHz away even at 5 MHz.
        y = lfilter([1, -1], [1, -(1 - 2 * np.pi * 20e3 / (width * 1e6))], y).astype(np.complex64)
        for r in rx80211.decode_all(y, 200):
            t = ((pos - t0) % 2**32) / iqrec.FS + r["start"] / (width * 1e6)
            r["t"] = t
            out.append(r)
            if verbose:
                m = r["mpdu"]
                print(f"  t {t * 1e3:9.3f} ms  {r['rate']:2d} Mbps-eq  len {r['length']:4d}  fcs {'ok ' if r['fcs_ok'] else 'BAD'}"
                      f"  SA {m[10:16].hex() if len(m) >= 16 else '-'}  {m[24:48] if len(m) >= 48 else m[24:]}")
    return segs, out


def main():
    width = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
    offset = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    segs, res = decode(sys.argv[1], width, offset)
    ok = [r for r in res if r["fcs_ok"]]
    span = sum(len(x) for _, x in segs) / iqrec.FS
    print(f"{len(segs)} segments ({span * 1e3:.1f} ms of samples); {len(res)} frames decoded at {width} MHz, "
          f"{len(ok)} with good FCS")


if __name__ == "__main__":
    main()
