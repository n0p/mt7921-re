#!/usr/bin/env python3
"""Packet error rate vs SNR for real card waveforms (FEC exploration, notes/10).

Takes the bursts of a tools/guest/txscan.py dump (env KEEP=<path>), keeps those our receiver decodes with a good FCS
at the given width, then adds complex white Gaussian noise at a set of SNRs and counts the decodes. The SNR is the
burst's signal power over the added noise power in the decode bandwidth (= the width, after resampling), i.e. the
receiver-input SNR over the channel. The capture's own noise (~48 dB below the signal) is negligible.

Usage: perawgn.py <dump> [width MHz 5] [trials 10] [snr range dB, e.g. -2:20:1]
Prints, per PHY rate, PER at each SNR and the SNR where PER crosses 10%.
"""
import collections
import sys

import numpy as np
from fractions import Fraction
from scipy.signal import lfilter, resample_poly

import rx80211
import txscan


def at_width(y, fs, width):
    """Resample a burst to the width's sample rate and remove DC, as nbrxd and txscan do before decoding."""
    fr = Fraction(width / (fs / 1e6)).limit_denominator(1000)
    z = resample_poly(y, fr.numerator, fr.denominator).astype(np.complex64)
    return lfilter([1, -1], [1, -(1 - 2 * np.pi * 20e3 / (width * 1e6))], z).astype(np.complex64)


def main():
    path = sys.argv[1]
    width = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
    trials = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    lo, hi, st = (float(x) for x in (sys.argv[4] if len(sys.argv) > 4 else "-2:20:1").split(":"))
    snrs = np.arange(lo, hi + st / 2, st)
    import os
    rx80211.STF_THRESH = float(os.environ.get("STF", rx80211.STF_THRESH))  # env STF: detection threshold under test
    rx80211.H_SMOOTH = int(os.environ.get("HSMOOTH", rx80211.H_SMOOTH))  # env HSMOOTH: channel smoothing under test
    fs, noise, bs = txscan.bursts(path, 8)
    ref = collections.defaultdict(list)  # rate -> clean bursts at the decode rate
    for a, e, y in bs:
        if (e - a) / fs > 5e-3 or (e - a) / fs < 100e-6:  # skip stuck-TX stretches and fragments
            continue
        z = at_width(y, fs, width)
        r = rx80211.decode_all(z, 1, 0.0)
        if r and r[0]["fcs_ok"]:
            # signal power over the burst body (preamble start .. end), from the strongest 90% of samples
            p = np.abs(z) ** 2
            ps = float(np.mean(p[p > 0.1 * np.median(p[p > 0])]))
            ref[r[0]["rate"]].append((z, ps))
    rng = np.random.default_rng(7)
    print(f"width {width} MHz; clean frames per rate: " + ", ".join(f"{k}M x{len(v)}" for k, v in sorted(ref.items())))
    print("SNR dB: " + " ".join(f"{s:5.1f}" for s in snrs))
    for rate in sorted(ref):
        per = []
        for snr in snrs:
            bad = n = 0
            for z, ps in ref[rate]:
                sigma = np.sqrt(ps / 10 ** (snr / 10) / 2)
                for _ in range(trials):
                    w = (rng.standard_normal(len(z)) + 1j * rng.standard_normal(len(z))) * sigma
                    r = rx80211.decode_all((z + w).astype(np.complex64), 1, None)
                    bad += not (r and r[0]["fcs_ok"])
                    n += 1
            per.append(bad / n)
        cross = next((s for s, p in zip(snrs, per) if p <= 0.1), None)
        print(f"{rate:2d}M  " + " ".join(f"{p:5.2f}" for p in per) + f"   PER<=10% from {cross} dB")


if __name__ == "__main__":
    main()
