#!/usr/bin/env python3
"""Decode a long (continuous, ungated) IQ record file block by block, with bounded memory.

Records are read in file order (tools/sdr/iqrec.py format); contiguous runs are cut into blocks of BLOCK samples at the
capture rate, with OVERLAP samples carried into the next block so frames on a boundary are not lost (a frame seen in
both blocks is counted once, by its absolute position).

Usage: iqchunkdec.py <records.bin> [width MHz, default 5] [ICAP packer, default 0]
"""
import re
import struct
import sys
import time
from fractions import Fraction

import numpy as np
from scipy.signal import lfilter, resample_poly

import iqrec
import rx80211

BLOCK = 2_000_000
OVERLAP = 200_000
MIN_SNR = -1.0  # dB of LTF SNR: below it a candidate is noise (false alarms sit near -9 dB, decodable frames above +1)


def main():
    path = sys.argv[1]
    width = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
    pack = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    fs, per_beat = iqrec.PACKS[pack]
    units_per_sample = 4 / per_beat  # ring words per sample (the record position counts ring words)
    fr = Fraction(width / (fs / 1e6)).limit_denominator(1000)
    a = 1 - 2 * np.pi * 20e3 / (width * 1e6)
    recent, frames, good, samples, runs, seqs, snrs = [], 0, 0, 0, 0, set(), ([], [])
    buf, buf_pos, nxt = [], None, None
    t0 = time.monotonic()

    def process(x, pos0):
        nonlocal frames, good
        y = resample_poly(x - x.mean(), fr.numerator, fr.denominator).astype(np.complex64)
        y = lfilter([1, -1], [1, -a], y).astype(np.complex64)
        for r in rx80211.decode_all(y, 1000, MIN_SNR):
            p = (pos0 + round(r["start"] / (width * 1e6) * fs * units_per_sample)) % 2**32
            # the same frame seen again in the next block's overlap (start may differ by a few samples)
            if any(abs((p - q + 2**31) % 2**32 - 2**31) < 400 * units_per_sample for q in recent):
                continue
            recent[:] = recent[-31:] + [p]
            frames += 1
            good += r["fcs_ok"]
            snrs[r["fcs_ok"]].append(r["snr"])
            m = re.search(rb"X410-OFDM-[0-9.]+-([0-9]{6})", r["mpdu"]) if r["fcs_ok"] else None
            if m:
                seqs.add(int(m.group(1)))

    with open(path, "rb") as f:
        while True:
            h = f.read(16)
            if len(h) < 16:
                break
            mark, _, pos, n = struct.unpack("<4I", h)
            if mark != iqrec.REC:
                raise ValueError(f"bad record marker {mark:#x}")
            d = f.read(n)
            if len(d) < n:  # truncated last record
                break
            w = np.frombuffer(d, dtype=np.uint32)
            if nxt is not None and pos != nxt:  # discontinuity: flush the run
                if buf:
                    process(np.concatenate(buf), buf_pos)
                buf, runs = [], runs + 1
            if not buf:
                buf_pos, total = pos, 0
            buf.append(iqrec.words_to_iq(w, pack))
            total += len(buf[-1])
            samples += len(buf[-1])
            nxt = (pos + n // 4) % 2**32
            if total >= BLOCK:
                x = np.concatenate(buf)
                process(x, buf_pos)
                keep = x[-OVERLAP:]
                buf_pos = (buf_pos + round((len(x) - OVERLAP) * units_per_sample)) % 2**32
                buf, total = [keep], len(keep)
    if buf:
        process(np.concatenate(buf), buf_pos)
    print(f"{samples / fs:.3f} s of samples at {fs / 1e6:g} Msps in {runs + 1} contiguous runs, decoded in "
          f"{time.monotonic() - t0:.1f} s; {frames} frames at {width} MHz, {good} with good FCS, "
          f"{len(seqs)} distinct X410 sequence numbers")
    for ok in (1, 0):
        if snrs[ok]:
            print(f"  {'good' if ok else 'bad '} FCS: LTF SNR dB p10/p50/p90 {np.percentile(snrs[ok], [10, 50, 90]).round(1)}")


if __name__ == "__main__":
    main()
