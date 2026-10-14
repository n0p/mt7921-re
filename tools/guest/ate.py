#!/usr/bin/env python3
"""MediaTek ATE (RF test) client for the MT7961 firmware, via mt7921dbg's raw MCU command file.

  ate.py <phy> mode <0 normal|1 testmode|2 icap|3 icap-overlap|4 wifispectrum>
  ate.py <phy> set <func id> <value>         (AT set: action 1)
  ate.py <phy> get <func id> [param1]        (AT query: action 2, prints param0/param1 of the reply)
  ate.py <phy> state                         (test mode and firmware state machine, host regval reads)
TEST_CTRL = CE cid 0x01; payload {u8 action; u8 rsv[3]; le32 param0; le32 param1} (mt7921_rftest_cmd).
"""
import struct
import subprocess
import sys

RAW = "/sys/kernel/debug/mt7921dbg/raw"
TEST_CTRL = 0x01 | (1 << 18)		# MCU_CE_CMD(TEST_CTRL)
NOWAIT = 1 << 31


def send(phy, action, p0, p1, wait):
    with open("/sys/kernel/debug/mt7921dbg/target", "w") as f:
        f.write(phy)
    cmd = TEST_CTRL | (0 if wait else NOWAIT)
    with open(RAW, "wb", buffering=0) as f:
        f.write(struct.pack("<IB3xII", cmd, action, p0 & 0xffffffff, p1 & 0xffffffff))
    if wait:
        with open(RAW, "rb") as f:
            return f.read()
    return b""


def fwread(phy, addr):
    # host-side debugfs regval read: the firmware debug command (CE 0xcb) is not dispatched in test state
    out = subprocess.run(["python3", "/root/fwre/memdump.py", phy, "read", hex(addr), "4"],
                         capture_output=True, text=True).stdout.split()
    return int.from_bytes(bytes.fromhex("".join(out[1:5])), "little")


def main():
    phy, op = sys.argv[1], sys.argv[2]
    v = [int(x, 0) for x in sys.argv[3:]]
    if op == "mode":
        send(phy, 0, v[0], 0, False)
    elif op == "set":
        send(phy, 1, v[0], v[1], False)
    elif op == "get":
        r = send(phy, 2, v[0], v[1] if len(v) > 1 else 0, True)
        p = r[-8:] if len(r) >= 8 else r
        print(f"raw {r.hex()}")
        if len(p) == 8:
            a, b = struct.unpack("<II", p)
            print(f"param0 {a:#x} param1 {b:#x}")
        return
    elif op != "state":
        sys.exit(__doc__)
    print(f"testmode {fwread(phy, 0x02032d28)} fwstate {fwread(phy, 0x0201712c)}")


if __name__ == "__main__":
    main()
