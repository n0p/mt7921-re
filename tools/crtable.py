#!/usr/bin/env python3
"""Decode the 2026 RAM firmware's compressed PHY CR tables (applied by FUN_0093e5d2) into register x column CSV.

Table A (FUN_0093e62a): 185 registers, column = index of code t0 + 3*t1 + 9*t2 + 18*t3 + 36*t4 in the byte list at
0x0202a004 (t1 = bandwidth 0/1/2, 5 MHz -> 3, 10 MHz -> 4; see FUN_0093c6f8).
Table B (FUN_0093e706): 143 registers, code t0 + 2*t1 + 4*t2 + 12*t3 + 24*t4 + 48*t5 in the list at 0x0202ac94.
Usage: crtable.py A|B [csv]
"""
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DLM = (ROOT / "work/regions/ram_1_02015c00.bin").read_bytes()
SKIP = 0xF1F2F3F7
TABLES = {
    "A": dict(desc=0x02029820, addr=0x0202a024, pool=0x0202a308, idx=0x02029b54, stride=40, groups=40, nreg=185,
              codes=0x0202a004, ncodes=30, radix=(1, 3, 9, 18, 36)),
    "B": dict(desc=0x0202a80c, addr=0x0202aca8, pool=0x0202aee4, idx=0x0202aa8c, stride=26, groups=26, nreg=143,
              codes=0x0202ac94, ncodes=20, radix=(1, 2, 4, 12, 24, 48)),
}


def off(live):
    return live - 0x44c - 0x02015c00


def u32(live):
    return struct.unpack_from("<I", DLM, off(live))[0]


def u16(live):
    return struct.unpack_from("<H", DLM, off(live))[0]


def u8(live):
    return DLM[off(live)]


def decode_code(code, radix):
    out = []
    for r in reversed(radix):
        out.append(code // r)
        code %= r
    return tuple(reversed(out))


def column_values(t, col):
    vals = {}
    reg = 0
    row = t["idx"] + t["stride"] * col
    for g in range(t["groups"]):
        d = t["desc"] + 8 * g
        idxptr, n, stride = u32(d), u16(d + 4), u16(d + 6)
        b = u8(row + g)
        for i in range(n):
            pi = u8(idxptr + b + i * stride)
            v = u32(t["pool"] + 4 * pi)
            a = u32(t["addr"] + 4 * (reg + i))
            vals[a] = None if v == SKIP else v
        reg += n
    return vals, reg


def main():
    name = sys.argv[1]
    t = TABLES[name]
    codes = [u8(t["codes"] + i) for i in range(t["ncodes"])]
    cols = []
    for c in range(len(codes)):
        v, nreg = column_values(t, c)
        cols.append(v)
    regs = [u32(t["addr"] + 4 * i) for i in range(nreg)]
    hdr = ["reg"] + [f"c{c}:{codes[c]}" for c in range(len(codes))]
    print("# columns: index:code; code digits (radix " + str(t["radix"]) + ") per column:")
    for c, code in enumerate(codes):
        print(f"#  c{c} code {code} -> {decode_code(code, t['radix'])}")
    print(",".join(hdr))
    for a in regs:
        print(",".join([f"{a:#010x}"] + [("-" if cols[c].get(a) is None else f"{cols[c][a]:08x}") for c in range(len(codes))]))


if __name__ == "__main__":
    main()
