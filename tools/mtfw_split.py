#!/usr/bin/env python3
"""Split MT7961 (connac2) RAM code and ROM patch images into load regions.

Layouts follow mt76/mt76_connac2_mac.h / mt76_connac_mcu.c:
  RAM:   [region data ...][fw_region * n_region][fw_trailer (36 B)]
  PATCH: [patch_hdr (96 B)][patch_sec * n_region (64 B)][section data ...]
Usage: mtfw_split.py <ram.bin> <patch.bin> <outdir>
"""
import json
import struct
import sys
import zlib
from pathlib import Path

TRAILER_FMT = "<BBBBB2s10s15sI"      # chip_id eco n_region fmt_ver fmt_flag rsv fw_ver build_date crc
TRAILER_SZ = struct.calcsize(TRAILER_FMT)
REGION_FMT = "<III4sIIBB14s"         # decomp_crc decomp_len decomp_blk_sz rsv addr len feature_set type rsv1
REGION_SZ = struct.calcsize(REGION_FMT)

FEAT = {0: "ENCRYPT", 1: "KEY_IDX0", 2: "KEY_IDX1", 4: "ENCRY_MODE", 5: "OVERRIDE_ADDR", 6: "NON_DL"}


def feat_str(v):
    return "|".join(n for b, n in FEAT.items() if v & (1 << b)) or "-"


def split_ram(data, outdir):
    assert TRAILER_SZ == 36 and REGION_SZ == 40
    t = struct.unpack(TRAILER_FMT, data[-TRAILER_SZ:])
    chip, eco, n_region, fmt_ver, fmt_flag, _, fw_ver, build_date, crc = t
    trailer = dict(chip_id=chip, eco=eco, n_region=n_region, format_ver=fmt_ver,
                   format_flag=fmt_flag, fw_ver=fw_ver.rstrip(b"\0").decode(errors="replace"),
                   build_date=build_date.rstrip(b"\0").decode(errors="replace"), crc=f"{crc:#010x}")
    tbl = len(data) - TRAILER_SZ - n_region * REGION_SZ
    regions, offset = [], 0
    for i in range(n_region):
        r = struct.unpack(REGION_FMT, data[tbl + i * REGION_SZ: tbl + (i + 1) * REGION_SZ])
        dcrc, dlen, dblk, _, addr, ln, fs, typ, _ = r
        name = f"ram_{i}_{addr:08x}.bin"
        dl = not (fs & (1 << 6))
        blob = data[offset: offset + ln] if dl else b""
        if dl:
            (outdir / name).write_bytes(blob)
        regions.append(dict(idx=i, file=name if dl else None, file_offset=offset if dl else None,
                            addr=f"{addr:#010x}", len=ln, feature_set=fs, features=feat_str(fs),
                            type=typ, decomp_len=dlen, decomp_crc=f"{dcrc:#010x}",
                            crc32=f"{zlib.crc32(blob):#010x}" if dl else None))
        if dl:
            offset += ln
    trailer["region_table_offset"] = tbl
    trailer["data_end"] = offset
    trailer["crc32_of_body_guess"] = f"{zlib.crc32(data[:-4]):#010x}"
    return dict(trailer=trailer, regions=regions)


def split_patch(data, outdir):
    build_date = data[0:16].rstrip(b"\0\n").decode(errors="replace")
    platform = data[16:20].decode(errors="replace")
    hw_sw_ver, patch_ver, checksum = struct.unpack(">IIH", data[20:30])
    d_patch_ver, subsys, feature, n_region, dcrc = struct.unpack(">IIIII", data[32:52])
    hdr = dict(build_date=build_date, platform=platform, hw_sw_ver=f"{hw_sw_ver:#010x}",
               patch_ver=f"{patch_ver:#010x}", checksum=f"{checksum:#06x}",
               desc_patch_ver=f"{d_patch_ver:#010x}", subsys=subsys, feature=f"{feature:#x}",
               n_region=n_region, crc=f"{dcrc:#010x}")
    secs = []
    for i in range(n_region):
        o = 96 + i * 64
        typ, offs, size = struct.unpack(">III", data[o:o + 12])
        addr, ln, key_idx, align_len = struct.unpack(">IIII", data[o + 12:o + 28])
        name = f"patch_{i}_{addr:08x}.bin"
        blob = data[offs:offs + size]
        (outdir / name).write_bytes(blob)
        secs.append(dict(idx=i, file=name, type=f"{typ:#010x}", file_offset=offs, size=size,
                         addr=f"{addr:#010x}", len=ln, sec_key_idx=key_idx, align_len=align_len,
                         crc32=f"{zlib.crc32(blob):#010x}"))
    return dict(header=hdr, sections=secs)


def main():
    ram, patch, outdir = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    outdir.mkdir(parents=True, exist_ok=True)
    info = dict(ram=split_ram(ram.read_bytes(), outdir), patch=split_patch(patch.read_bytes(), outdir))
    (outdir / "regions.json").write_text(json.dumps(info, indent=2))
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
