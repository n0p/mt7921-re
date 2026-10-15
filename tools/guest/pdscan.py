#!/usr/bin/env python3
"""Post-detection stage scanner for narrow receive.

Card B must already be in a state where the packet detector fires but nothing reaches SIG, e.g. native narrow (cbw 4)
plus BW code 4 in 0x83081000 at a quiet frequency: noise alone then gives thousands of OFDM packet detects per second
and zero MDRDY / SIG errors. For each candidate this writes one modified word, clears the PHY stage counters
(0x83082004 STSCNT_EN [11:9] 0 -> 5), waits T seconds, reads OFDM PD / MDRDY / RxOk / SIG / TAG / FCS, restores the
word, and reports candidates whose MDRDY + SIG errors exceed the baseline: the post-detection stage started working.

  pdscan.py <phy> <T s> <page> [page...]     candidates: every non-volatile word of the pages (two dumps compared),
                                             each byte b not in (0, 0xff) replaced by 2b (if < 0x100) and b // 2
  pdscan.py <phy> <T s> list <addr=val>...   explicit candidates (all printed when there are at most 64)
A candidate is a hit when MDRDY + SIG errors exceed the baseline, or when RxOk (good-FCS frames: with a reference
transmitter on air, real decodes) does.
"""
import struct
import sys
import time

sys.path.insert(0, "/root/fwre")
from fwdbg import request  # noqa: E402


def rd(phy, a):
    return request(phy, 1, a, 1)[2][0]


def wr(phy, a, v):
    request(phy, 2, a, 1, data=[v & 0xffffffff])


def clear(phy):
    v = rd(phy, 0x83082004)
    wr(phy, 0x83082004, v & ~0xe00)
    wr(phy, 0x83082004, (v & ~0xe00) | 0xa00)


def counters(phy):
    w = request(phy, 1, 0x83081010, 6)[2]
    return dict(pd=w[0] >> 16, mdrdy=w[1] >> 16, rxok=w[2] >> 16, sig=w[4] >> 16, tag=w[4] & 0xffff, fcs=w[5] >> 16,
                cckpd=w[0] & 0xffff)


def measure(phy, t):
    clear(phy)
    time.sleep(t)
    return counters(phy)


def dump(phy, page):
    words = []
    for a in range(page, page + 0x1000, 256):
        words += list(request(phy, 1, a, 64)[2])
    return words


def main():
    phy, t = sys.argv[1], float(sys.argv[2])
    cands = []
    if sys.argv[3] == "list":
        for kv in sys.argv[4:]:
            a, v = kv.split("=")
            cands.append((int(a, 0), int(v, 0), kv))
    else:
        for page in (int(x, 0) for x in sys.argv[3:]):
            d1 = dump(phy, page)
            time.sleep(0.3)
            d2 = dump(phy, page)
            for i, (v1, v2) in enumerate(zip(d1, d2)):
                if v1 != v2 or v1 in (0, 0xffffffff):
                    continue
                a = page + 4 * i
                for lane in range(4):
                    b = (v1 >> (8 * lane)) & 0xff
                    if b in (0, 0xff):
                        continue
                    for nb in sorted({b * 2, b // 2} - {b}):
                        if nb > 0xff:
                            continue
                        nv = (v1 & ~(0xff << (8 * lane))) | (nb << (8 * lane))
                        cands.append((a, nv, f"{a:#010x}={nv:#010x} (byte{lane} {b:#x}->{nb:#x})"))
    base = [measure(phy, t) for _ in range(3)]
    thr = max(b["mdrdy"] + b["sig"] for b in base) + 3
    print(f"baseline x3: {base}; threshold mdrdy+sig > {thr}; {len(cands)} candidates", flush=True)
    for n, (a, nv, label) in enumerate(cands):
        try:
            old = rd(phy, a)
            wr(phy, a, nv)
            c = measure(phy, t)
            wr(phy, a, old)
        except SystemExit as e:
            print(f"ERROR at {label}: {e}", flush=True)
            raise
        hit = c["mdrdy"] + c["sig"] > thr or c["rxok"] > max(b["rxok"] for b in base) + 2
        if hit or n % 50 == 0 or len(cands) <= 64:
            print(f"{'HIT ' if hit else '    '}{n:4d} {label}: {c}", flush=True)


if __name__ == "__main__":
    main()
