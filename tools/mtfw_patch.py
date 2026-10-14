#!/usr/bin/env python3
"""Patch an MT7961 WIFI_RAM_CODE image and fix its integrity words so the boot ROM accepts it.

Two checks matter:
  * per-region PDA download checksum: the last word of every downloaded region makes a word-wise LFSR over the
    region end at 0 (checked by the download engine, MMIO 0x54000814; algorithm from BirriaSDR
    tools/rebuild_firmware.py);
  * file trailer crc = crc32(file[:-4]) (not sent to the chip, kept consistent anyway).

Addresses are firmware load addresses (file VA): for DLM data this is the *download* address, i.e. the live
runtime address minus 0x44c for data at/after 0x020170fc (see tools/mkruntime.py).

Usage: mtfw_patch.py <in.bin> <out.bin> [blob:<addr>=<file>]... [<addr>=<hexbytes>]...
       mtfw_patch.py --verify <in.bin>
  blob:<addr>=<file> places a file at addr past the end of the downloaded region just below it, growing that
  region (its old 16-byte trailer becomes padding). Blobs are applied before byte patches.
"""
import struct
import sys
import zlib
from pathlib import Path

TRAILER_SZ = 36
REGION_SZ = 40


def pda_checksum(data):
    state = 0
    for (word,) in struct.iter_unpack("<I", data):
        parity = (state & 0x10921111).bit_count() & 1
        state = (state >> 1) ^ (parity << 31) ^ word
    return state


def regions(fw):
    n = fw[-TRAILER_SZ + 2]
    tbl = len(fw) - TRAILER_SZ - n * REGION_SZ
    out, off = [], 0
    for i in range(n):
        r = fw[tbl + i * REGION_SZ: tbl + (i + 1) * REGION_SZ]
        addr, ln = struct.unpack_from("<II", r, 16)
        fs = r[24]
        dl = not fs & 0x40
        out.append(dict(idx=i, addr=addr, len=ln, off=off if dl else None))
        if dl:
            off += ln
    return out


def place_blob(fw, addr, data):
    regs = [r for r in regions(fw) if r["off"] is not None and r["addr"] <= addr]
    r = max(regs, key=lambda r: r["addr"])
    if addr < r["addr"] + r["len"] - 16:
        sys.exit(f"blob {addr:#x} overlaps region {r['idx']} payload")
    o, ln = r["off"], r["len"]
    body = bytes(fw[o:o + ln - 16])
    body += bytes(addr - r["addr"] - len(body)) + data
    body += bytes(-len(body) % 16) + bytes(16)  # PDA needs 16-byte aligned region lengths
    fw[o:o + ln] = body
    n = fw[-TRAILER_SZ + 2]
    tbl = len(fw) - TRAILER_SZ - n * REGION_SZ
    struct.pack_into("<I", fw, tbl + r["idx"] * REGION_SZ + 20, len(body))
    print(f"blob {len(data)} bytes at {addr:#010x}: region {r['idx']} {ln} -> {len(body)} bytes")


def fix(fw):
    for r in regions(fw):
        if r["off"] is None:
            continue
        o, ln = r["off"], r["len"]
        fw[o + ln - 4: o + ln] = bytes(4)
        fw[o + ln - 4: o + ln] = struct.pack("<I", pda_checksum(bytes(fw[o:o + ln])))
        assert pda_checksum(bytes(fw[o:o + ln])) == 0
    fw[-4:] = struct.pack("<I", zlib.crc32(bytes(fw[:-4])))


def verify(fw):
    ok = True
    for r in regions(fw):
        if r["off"] is None:
            continue
        c = pda_checksum(bytes(fw[r["off"]:r["off"] + r["len"]]))
        print(f"region {r['idx']} @{r['addr']:#010x} len {r['len']}: pda {'OK' if c == 0 else f'BAD {c:#x}'}")
        ok &= c == 0
    crc_ok = struct.unpack("<I", fw[-4:])[0] == zlib.crc32(bytes(fw[:-4]))
    print(f"file crc32: {'OK' if crc_ok else 'BAD'}")
    return ok and crc_ok


def main():
    if sys.argv[1] == "--verify":
        sys.exit(0 if verify(bytearray(Path(sys.argv[2]).read_bytes())) else 1)
    fw = bytearray(Path(sys.argv[1]).read_bytes())
    for spec in sys.argv[3:]:
        if spec.startswith("blob:"):
            a, f = spec[5:].split("=", 1)
            place_blob(fw, int(a, 0), Path(f).read_bytes())
    regs = regions(fw)
    for spec in sys.argv[3:]:
        if spec.startswith("blob:"):
            continue
        a, hx = spec.split("=")
        addr, data = int(a, 0), bytes.fromhex(hx)
        # the 12 zero bytes before a region's PDA word are plain padding; only the last word is reserved
        r = next((r for r in regs if r["off"] is not None and r["addr"] <= addr and addr + len(data) <= r["addr"] + r["len"] - 4), None)
        if r is None:
            sys.exit(f"{addr:#x}: not inside the payload of a downloaded region")
        o = r["off"] + addr - r["addr"]
        print(f"{addr:#010x} (region {r['idx']}, file+{o:#x}): {fw[o:o + len(data)].hex()} -> {data.hex()}")
        fw[o:o + len(data)] = data
    fix(fw)
    Path(sys.argv[2]).write_bytes(fw)
    verify(fw)


if __name__ == "__main__":
    main()
