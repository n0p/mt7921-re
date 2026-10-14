#!/usr/bin/env python3
"""Diff PHY/RF register snapshots taken with `fwdbg.py dump` (pages concatenated, 4 KB each).

Usage: phydiff.py <pages.txt> <noiseA.bin> <noiseB.bin> <name>=<snap.bin>...
Registers that differ between the two noise snapshots (same configuration taken twice) are treated as volatile
and dropped. Prints every remaining register whose value differs among the named snapshots.
"""
import struct
import sys
from pathlib import Path


def load(path):
    d = Path(path).read_bytes()
    return struct.unpack(f"<{len(d) // 4}I", d)


def main():
    pages = [int(x, 0) for x in Path(sys.argv[1]).read_text().split()]
    addrs = [p + 4 * i for p in pages for i in range(1024)]
    na, nb = load(sys.argv[2]), load(sys.argv[3])
    snaps = [(s.split("=")[0], load(s.split("=")[1])) for s in sys.argv[4:]]
    volatile = {i for i in range(len(addrs)) if na[i] != nb[i]}
    names = [n for n, _ in snaps]
    print(f"{len(volatile)} volatile registers dropped")
    print("address     " + " ".join(f"{n:>10}" for n in names))
    for i, a in enumerate(addrs):
        if i in volatile:
            continue
        vals = [s[i] for _, s in snaps]
        if len(set(vals)) > 1:
            print(f"{a:#010x}  " + " ".join(f"{v:#010x}" for v in vals))


if __name__ == "__main__":
    main()
