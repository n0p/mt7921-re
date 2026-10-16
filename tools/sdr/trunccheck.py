#!/usr/bin/env python3
"""TX truncation check for narrow PPDUs (notes/10 §5): how many OFDM symbols of each PPDU a card really sends.

The card under test runs tools/nbnode nbmesh (TUN, --fec off) and pings an absent peer with `ping -p 5a -s <size>`, so
every ICMP data byte is 0x5a. This decodes an X410 capture of those frames and, per PSDU length, reports the
symbols the PPDU has and how many arrived intact (up to the first data byte that is not 0x5a). A ÷8 frame also
carries its inner CRC + padding (frame.rs), which is excluded from the data range.

Usage: trunccheck.py <capture.cf32> <rate Msps = channel width in MHz: 5 or 2.5> [source MAC 00c0cab7962a]
"""
import collections
import os
import sys

import numpy as np
from scipy.signal import lfilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rx80211  # noqa: E402


def main():
    path, width = sys.argv[1], float(sys.argv[2])
    src = bytes.fromhex(sys.argv[3] if len(sys.argv) > 3 else "00c0cab7962a")
    pad = 4 + 12 if width == 2.5 else 0  # ÷8: inner CRC + padding before the FCS (6 Mbps: 4 symbols of 3 bytes)
    x = np.fromfile(path, np.complex64)
    x = lfilter([1, -1], [1, -(1 - 2 * np.pi * 20e3 / (width * 1e6))], (x - x.mean())).astype(np.complex64)
    by_len = collections.defaultdict(list)
    for r in rx80211.decode_all(x, 5000, -1.0, 1600):
        m = r["mpdu"]
        if m[:2] != b"\x08\x00" or m[10:16] != src or len(m) < 100 or r["rate"] != 6:
            continue
        n = len(m)
        data0 = 24 + 8 + 20 + 8 + 16  # 802.11 + SNAP + IPv4 + ICMP headers + ping's 16-byte timestamp
        data1 = n - 4 - pad  # ICMP data ends at the inner CRC (÷8) or at the FCS (÷4)
        bad = [i for i in range(data0, data1) if m[i] != 0x5A]
        first_bad = bad[0] if bad else n  # data intact: report the whole PPDU
        nsym = -(-(16 + 8 * n + 6) // 24)
        kept = min(nsym, (16 + 8 * first_bad) // 24)  # whole symbols before the first bad byte
        by_len[n].append((nsym, kept, bool(bad), r["fcs_ok"]))
    sym_us = 4 * 20 / width
    for n, v in sorted(by_len.items()):
        nsym = v[0][0]
        kept = sorted(k for _, k, _, _ in v)
        print(f"PSDU {n:4d}: {nsym:3d} symbols ({(20 + 4 * nsym) * 20 / width:6.0f} us), {len(v):2d} frames, "
              f"FCS ok {sum(f for *_, f in v):2d}; symbols intact: min {kept[0]} median {kept[len(kept) // 2]} "
              f"max {kept[-1]} ({sum(b for _, _, b, _ in v)} cut inside the data)")
    if not by_len:
        print("no frames from", src.hex())


if __name__ == "__main__":
    main()
