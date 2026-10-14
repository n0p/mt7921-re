#!/usr/bin/env python3
"""Turn the downloaded DLM region (region 1) into its runtime layout, as ram_init_data_bss (0x91500c) does.

At boot the RAM code moves .data up by 0x44c and clears bss:
    copy  [gp+82172, +len) -> [gp+83272, gp+195868)     (memmove, len = 195868-83272)
    zero  [gp+82172, gp+83272)
    zero  [gp+195872, 0x020504a8)
The real $gp is 0x02003000 (verified on hardware: the CE command table is live at 0x02026118 = file VA + 0x44c).
Usage: mkruntime.py <regions dir> <out dir>   (copies all regions, replacing ram_1 with its runtime image)
"""
import shutil
import sys
from pathlib import Path

GP = 0x02003000
DLM_BASE = 0x02015c00


def main():
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if f.is_file():
            shutil.copy(f, out / f.name)
    dlm = bytearray((src / "ram_1_02015c00.bin").read_bytes())

    def off(va):
        return va - DLM_BASE

    s, d, e = GP + 82172, GP + 83272, GP + 195868
    n = e - d
    end = DLM_BASE + len(dlm)
    if e > end:
        dlm.extend(bytes(e - end))
    dlm[off(d):off(e)] = bytes(dlm[off(s):off(s) + n])
    dlm[off(s):off(d)] = bytes(d - s)
    z0, z1 = GP + 195872, 0x020504a8
    dlm[off(z0):off(min(z1, DLM_BASE + len(dlm)))] = bytes(min(z1, DLM_BASE + len(dlm)) - z0)
    (out / "ram_1_02015c00.bin").write_bytes(dlm)
    print(f"data moved {s:#x}->{d:#x} ({n} bytes), bss {s:#x}-{d:#x} and {z0:#x}-{z1:#x} cleared")


if __name__ == "__main__":
    main()
