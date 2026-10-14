#!/usr/bin/env python3
"""Decode 802.11a/p frames in a raw capture under several channel-width hypotheses.

Usage: decmulti.py <file.cf32> <capture rate Msps> [widths MHz, default 20,10,5,2.5] [max samples]
For each width W the capture is resampled to W Msps (the 20 Msps-equivalent rate of a W MHz OFDM signal) and fed to
rx80211.decode_all. Prints, per width, decoded frames / good FCS / most common SA.
"""
import sys
from collections import Counter
from fractions import Fraction

import numpy as np
from scipy.signal import resample_poly

import rx80211


def main():
    iq = np.fromfile(sys.argv[1], dtype=np.complex64)
    rate = float(sys.argv[2])
    widths = [float(w) for w in sys.argv[3].split(",")] if len(sys.argv) > 3 else [20, 10, 5, 2.5]
    nmax = int(float(sys.argv[4])) if len(sys.argv) > 4 else len(iq)
    iq = iq[:nmax]
    for w in widths:
        fr = Fraction(w / rate).limit_denominator(1000)
        x = resample_poly(iq, fr.numerator, fr.denominator).astype(np.complex64) if fr != 1 else iq
        res = rx80211.decode_all(x, 60)
        ok = [r for r in res if r["fcs_ok"]]
        sas = Counter(r["mpdu"][10:16].hex() for r in ok)
        print(f"width {w:5} MHz: decoded {len(res):3d} fcs_ok {len(ok):3d} SA {dict(sas)}"
              + (f" e.g. {ok[0]['mpdu'][32:52]}" if ok else ""))


if __name__ == "__main__":
    main()
