#!/usr/bin/env python3
"""Identify the per-region 4-byte check value at the end of MT7961 firmware regions.

Each region ends with 12 zero bytes + a 32-bit value. Try common CRC-32 parameterisations over a range of
data spans and report any that reproduce the stored value.
Usage: crc_probe.py <region.bin>...
"""
import struct
import sys
from pathlib import Path


def reflect(v, w):
    r = 0
    for _ in range(w):
        r = (r << 1) | (v & 1)
        v >>= 1
    return r


def make_crc(poly, init, refin, refout, xorout):
    table = []
    for i in range(256):
        if refin:
            c = i
            rp = reflect(poly, 32)
            for _ in range(8):
                c = (c >> 1) ^ rp if c & 1 else c >> 1
        else:
            c = i << 24
            for _ in range(8):
                c = ((c << 1) ^ poly) & 0xffffffff if c & 0x80000000 else (c << 1) & 0xffffffff
        table.append(c)

    def crc(data):
        c = init
        if refin:
            for b in data:
                c = (c >> 8) ^ table[(c ^ b) & 0xff]
        else:
            for b in data:
                c = ((c << 8) & 0xffffffff) ^ table[((c >> 24) ^ b) & 0xff]
        if refin != refout:
            c = reflect(c, 32)
        return c ^ xorout
    return crc


VARIANTS = {
    "CRC-32 (zlib)": (0x04C11DB7, 0xFFFFFFFF, True, True, 0xFFFFFFFF),
    "CRC-32/MPEG-2": (0x04C11DB7, 0xFFFFFFFF, False, False, 0),
    "CRC-32/BZIP2": (0x04C11DB7, 0xFFFFFFFF, False, False, 0xFFFFFFFF),
    "CRC-32/POSIX": (0x04C11DB7, 0, False, False, 0xFFFFFFFF),
    "CRC-32/JAMCRC": (0x04C11DB7, 0xFFFFFFFF, True, True, 0),
    "CRC-32C": (0x1EDC6F41, 0xFFFFFFFF, True, True, 0xFFFFFFFF),
    "CRC-32/refl-init0": (0x04C11DB7, 0, True, True, 0),
    "CRC-32/norefl-init0": (0x04C11DB7, 0, False, False, 0),
}


def main():
    for f in sys.argv[1:]:
        d = Path(f).read_bytes()
        stored = d[-4:]
        targets = {struct.unpack("<I", stored)[0]: "LE", struct.unpack(">I", stored)[0]: "BE"}
        print(f"{f}: stored {stored.hex()}")
        found = False
        for name, params in VARIANTS.items():
            crc = make_crc(*params)
            # data spans: everything before the value, before the zero pad, and word-swapped variants
            spans = {"[:-4]": d[:-4], "[:-16]": d[:-16]}
            spans["[:-16] be-words"] = b"".join(d[i:i + 4][::-1] for i in range(0, len(d) - 16, 4))
            for sname, blob in spans.items():
                v = crc(blob)
                if v in targets:
                    print(f"   MATCH {name} over {sname} -> {v:#010x} stored {targets[v]}")
                    found = True
        if not found:
            print("   no match")


if __name__ == "__main__":
    main()
