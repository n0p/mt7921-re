#!/usr/bin/env python3
"""Per-symbol quality of the 802.11a/p frames in an IQ record file (tools/guest/iqstream.sh): for every decoded frame,
the residual rotation of each equalized BPSK data symbol (atan(mean|Im| / mean|Re|), ~10 deg on a clean symbol), and a
histogram of the symbol indices above a threshold. Damage at a fixed symbol index across frames points at a
deterministic cause (transmitter waveform, receiver timing); scattered damage points at noise or interference.

Usage: symcheck.py <records.bin> <width MHz> [threshold deg, default 25] [min segment energy, default 125]
"""
import collections
import sys

import numpy as np
from scipy.signal import resample_poly

import iqrec
import rx80211


def symbol_angles(y, t0, nsym):
    stf = y[t0 - 176:t0 - 32]
    coarse = np.angle(np.vdot(stf[:-16], stf[16:])) / (2 * np.pi * 16)
    n = np.arange(len(y) - t0 + 32)
    yy = y[t0 - 32:] * np.exp(-2j * np.pi * coarse * (n - 32))
    cfo = coarse + np.angle(np.vdot(yy[32:96], yy[96:160])) / (2 * np.pi * 64)
    x = (y[t0 - 32:] * np.exp(-2j * np.pi * cfo * (n - 32)))[32:]
    lt = (np.fft.fft(x[:64]) + np.fft.fft(x[64:128])) / 2
    ref = rx80211._ltf_freq()
    h = lt / np.where(np.abs(ref) > 0, ref, 1)
    h[np.abs(ref) == 0] = 1
    out = []
    for s in range(nsym + 1):
        d = rx80211.ofdm_demod(x[128 + 80 * s:128 + 80 * (s + 1)], h, s)
        out.append(np.degrees(np.arctan2(np.mean(np.abs(d.imag)), np.mean(np.abs(d.real)))))
    return np.array(out)


def main():
    path, width = sys.argv[1], float(sys.argv[2])
    thr = float(sys.argv[3]) if len(sys.argv) > 3 else 25
    emin = float(sys.argv[4]) if len(sys.argv) > 4 else 125
    dec = round(iqrec.FS / 1e6 / width)
    hist, nf, nbad, ok = collections.Counter(), 0, 0, 0
    ltf_t = rx80211.preamble()[192:256]
    for pos, x in iqrec.segments(path):
        x = x.astype(np.complex64)
        if np.mean(np.abs(x.real) + np.abs(x.imag)) < emin:
            continue
        y = resample_poly(x - x.mean(), 1, dec).astype(np.complex64)
        for r in rx80211.decode_all(y, 50):
            i = r["start"]
            w0, w1 = i + 100, min(len(y) - 64, i + 260)
            xc = np.abs(np.correlate(y[w0:w1 + 64], ltf_t, "valid"))
            k = w0 + int(np.argmax(xc))
            t0 = k if rx80211.decode_at(y, k) else k - 64
            nsym = -(-(16 + 8 * r["length"] + 6) // 24)
            if t0 + 128 + 80 * (nsym + 1) > len(y):
                continue
            a = symbol_angles(y, t0, nsym)[:nsym + 1]
            bad = np.where(a > thr)[0]
            hist.update(int(s) for s in bad)
            nf += 1
            nbad += len(bad) > 0
            ok += r["fcs_ok"]
    print(f"{nf} frames, {ok} good FCS, {nbad} with a symbol above {thr} deg; most damaged symbol indices "
          f"(0 = SIGNAL): {hist.most_common(12)}")


if __name__ == "__main__":
    main()
