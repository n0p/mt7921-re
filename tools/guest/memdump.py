#!/usr/bin/env python3
"""Read/write MT7921 chip bus addresses through mt76 debugfs regidx/regval (stdlib only).

On mt7921u these map to USB vendor requests carrying the full 32-bit address, so this reaches whatever the
chip bus exposes (MCU SYSRAM/ILM/DLM aliases, ROM, PHY/RF registers).

  memdump.py <phy> read  <addr> <len> [out.bin]   dump len bytes (hexdump to stdout if no file)
  memdump.py <phy> write <addr> <u32>...          write consecutive 32-bit words
  memdump.py <phy> probe <addr> <ref.bin> [off]   compare 64 bytes at addr with ref.bin[off:]
"""
import os
import struct
import sys

DBG = "/sys/kernel/debug/ieee80211/{}/mt76"


class Chip:
    def __init__(self, phy):
        d = DBG.format(phy)
        self.idx = os.path.join(d, "regidx")
        self.val = os.path.join(d, "regval")

    def _set_idx(self, addr):
        # debugfs attribute files do not support pwrite(); a fresh open+write per access is required
        with open(self.idx, "wb") as f:
            f.write(b"%d" % addr)

    def rr(self, addr):
        self._set_idx(addr)
        with open(self.val, "rb") as f:
            return int(f.read().strip(), 0) & 0xffffffff

    def wr(self, addr, v):
        self._set_idx(addr)
        with open(self.val, "wb") as f:
            f.write(b"%d" % v)

    def read(self, addr, n):
        return b"".join(struct.pack("<I", self.rr(addr + i)) for i in range(0, n, 4))[:n]


def hexdump(base, data):
    for i in range(0, len(data), 16):
        print(f"{base + i:08x}: {data[i:i + 16].hex(' ')}")


def main():
    phy, cmd, addr = sys.argv[1], sys.argv[2], int(sys.argv[3], 0)
    c = Chip(phy)
    if cmd == "read":
        n = int(sys.argv[4], 0)
        data = c.read(addr, n)
        if len(sys.argv) > 5:
            with open(sys.argv[5], "wb") as f:
                f.write(data)
            print(f"wrote {len(data)} bytes from {addr:#x}", file=sys.stderr)
        else:
            hexdump(addr, data)
    elif cmd == "write":
        for i, w in enumerate(sys.argv[4:]):
            c.wr(addr + 4 * i, int(w, 0))
        hexdump(addr, c.read(addr, 4 * len(sys.argv[4:])))
    elif cmd == "probe":
        ref = open(sys.argv[4], "rb").read()
        off = int(sys.argv[5], 0) if len(sys.argv) > 5 else 0
        got = c.read(addr, 64)
        want = ref[off:off + 64]
        same = sum(a == b for a, b in zip(got, want))
        print(f"{addr:#010x}: {same}/64 bytes match ref+{off:#x}")
        hexdump(addr, got)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
