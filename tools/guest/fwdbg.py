#!/usr/bin/env python3
"""Talk to the instrumented firmware's debug command (CE 0xcb) through the mt7921dbg kernel module.

  fwdbg.py <phy> ping
  fwdbg.py <phy> read  <addr> <nwords>
  fwdbg.py <phy> write <addr> <u32>...
  fwdbg.py <phy> call  <addr> [a0 [a1 [a2 [a3]]]]
  fwdbg.py <phy> dump  <out.bin> <page> [page...]   4 KB per page, MCU-side bulk reads; out = pages concatenated
Runs on the MCU, so `write` also works on ILM (0x9xxxxx), unlike debugfs regval.
"""
import struct
import sys

D = "/sys/kernel/debug/mt7921dbg/"
OPS = {"ping": 0, "read": 1, "write": 2, "call": 3}
MAGIC = 0x4d543739
RXD_SZ = 0x24  # present only if the driver did not strip the MCU rxd


def request(phy, op, addr=0, n=0, args=(), data=()):
    with open(D + "target", "w") as f:
        f.write(phy)
    a = list(args) + [0] * (4 - len(args))
    req = struct.pack("<7I", op, addr, n, *a) + b"".join(struct.pack("<I", w) for w in data)
    with open(D + "cmd", "wb", buffering=0) as f:
        f.write(req)
    with open(D + "cmd", "rb") as f:
        raw = f.read()
    off = 0 if raw[:4] == struct.pack("<I", MAGIC) else RXD_SZ
    if len(raw) < off + 20:
        sys.exit(f"short event ({len(raw)} bytes): {raw.hex()}")
    magic, rop, status, ret, cnt = struct.unpack_from("<5I", raw, off)
    if magic != MAGIC:
        sys.exit(f"bad magic {magic:#x}; event {raw[:64].hex()}")
    words = struct.unpack_from(f"<{cnt}I", raw, off + 20)
    return status, ret, words


def main():
    phy, op = sys.argv[1], sys.argv[2]
    v = [int(x, 0) for x in sys.argv[3:]] if op != "dump" else []
    if op == "ping":
        st, ret, _ = request(phy, 0)
        print(f"status {st} ret {ret:#x}")
    elif op == "read":
        st, _, w = request(phy, 1, v[0], v[1])
        for i in range(0, len(w), 4):
            print(f"{v[0] + 4 * i:08x}: " + " ".join(f"{x:08x}" for x in w[i:i + 4]))
    elif op == "write":
        st, ret, _ = request(phy, 2, v[0], len(v) - 1, data=v[1:])
        print(f"status {st} wrote {ret} words")
    elif op == "call":
        st, ret, _ = request(phy, 3, v[0], args=v[1:5])
        print(f"status {st} ret {ret:#x}")
    elif op == "dump":
        out = bytearray()
        for page in (int(x, 0) for x in sys.argv[4:]):
            for a in range(page, page + 0x1000, 256):
                st, _, w = request(phy, 1, a, 64)
                out += struct.pack(f"<{len(w)}I", *w)
        with open(sys.argv[3], "wb") as f:
            f.write(out)
        print(f"dumped {len(out)} bytes")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
