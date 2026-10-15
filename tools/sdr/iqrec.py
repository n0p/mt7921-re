#!/usr/bin/env python3
"""Parse the IQ records of mt7921dbg's iq ring (tools/guest/iqstream.sh, fw/src/iqstream.c) into contiguous segments.

Record: u32 "QIQR" (0x52514951), u32 packet sequence, u32 absolute sample position (40 Msps clock, mod 2^32),
u32 payload bytes, then the payload: raw ICAP words, Q [13:0] and I [29:16] as 14-bit two's complement.

Library: segments(path) -> list of (position, complex64 samples at 40 Msps), adjacent records merged
CLI:     iqrec.py <file>          summary of the segments and their mean |I|+|Q|
"""
import struct
import sys

import numpy as np

REC = 0x52514951
FS = 40e6
# ICAP packer (0x800210b8[19:16], tools/guest/icap.py ICAP_PACK) -> (sample rate, ring words per 4-word beat that hold
# samples), measured 2026-10-15 with an X410 tone (native cbw 4, tap 4, which is 20 Msps held x2 at 40 Msps):
# 0 = 40 Msps dense; 2 = uniform 10 Msps, one sample pair per beat; 8 = 30 Msps dense (the 20 Msps samples re-timed by
# repetition, images at +-10 MHz, -23 dB)
PACKS = {0: (40e6, 4), 2: (10e6, 2), 8: (30e6, 4)}


def sample_words(w, pack=0):
    """Ring words (beat-aligned) -> one word per sample, in time order."""
    w = np.asarray(w, dtype=np.uint32)
    if pack == 2:
        # The pair normally sits in words 0-1 with 2-3 zero, but runs of beats (~2% of them) carry it in words 2-3, and
        # the beat where it moves holds the same pair in both halves. Take the half with data, once per beat.
        b = w[:len(w) // 4 * 4].reshape(-1, 2, 2)
        hi = (b[:, 1] != 0).any(1) & ~(b[:, 0] != 0).any(1)
        w = b[np.arange(len(b)), hi.astype(np.intp)].ravel()
    return w


def words_to_iq(w, pack=0):
    w = sample_words(w, pack)
    q = (w & 0x3fff).astype(np.int32)
    q -= (q & 0x2000) << 1
    i = ((w >> 16) & 0x3fff).astype(np.int32)
    i -= (i & 0x2000) << 1
    return (i + 1j * q).astype(np.complex64)


def records(path):
    data = open(path, "rb").read()
    off = 0
    while off + 16 <= len(data):
        mark, seq, pos, n = struct.unpack_from("<4I", data, off)
        if mark == REC and off + 16 + n > len(data):
            break  # truncated last record (copy cut short)
        if mark != REC:
            raise ValueError(f"bad record at byte {off}: {mark:#x} seq {seq} n {n}")
        yield seq, pos, np.frombuffer(data, dtype=np.uint32, count=n // 4, offset=off + 16)
        off += 16 + n


def segments(path, pack=0):
    segs, cur_pos, cur = [], None, []
    for _, pos, w in records(path):
        if cur and (pos - cur_pos - sum(len(c) for c in cur)) % 2**32 != 0:
            segs.append((cur_pos, words_to_iq(np.concatenate(cur), pack)))
            cur = []
        if not cur:
            cur_pos = pos
        cur.append(w)
    if cur:
        segs.append((cur_pos, words_to_iq(np.concatenate(cur), pack)))
    return segs


def main():
    segs = segments(sys.argv[1])
    print(f"{len(segs)} segments")
    t0 = segs[0][0] if segs else 0
    for pos, x in segs:
        e = np.abs(x.real) + np.abs(x.imag)
        print(f"  t {((pos - t0) % 2**32) / FS * 1e3:9.3f} ms  {len(x) / FS * 1e3:7.3f} ms  ({len(x)} samples)  "
              f"mean|I|+|Q| {e.mean():6.1f}  max {e.max():6.0f}")


if __name__ == "__main__":
    main()
