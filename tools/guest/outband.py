#!/usr/bin/env python3
"""Send EXT CHANNEL_SWITCH with an outband (arbitrary) frequency through mt7921dbg raw, then read back the
synthesizer state through fwdbg calls (instrumented firmware). No TX is started by this tool.

  outband.py <phy> <outband MHz> [control_ch] [channel_band]
"""
import struct
import subprocess
import sys

RAW = "/sys/kernel/debug/mt7921dbg/raw"
CHANNEL_SWITCH = 0xed | (0x08 << 8)	# MCU_EXT_CMD(CHANNEL_SWITCH)


def call(phy, *a):
    out = subprocess.run(["python3", "/root/fwre/fwdbg.py", phy, "call"] + [hex(x) for x in a],
                         capture_output=True, text=True).stdout.split()
    return int(out[-1], 16) if out else None


def main():
    phy, mhz = sys.argv[1], int(sys.argv[2])
    ch = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    band = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    # mt7921_mcu_set_chan_info request (76 bytes)
    req = struct.pack("<BBBBBBBBHBBIBBB57x", ch, ch, 0, 1, 1, 0, 0, 0, 0, band, 0, mhz, 0, 0, 0)
    with open("/sys/kernel/debug/mt7921dbg/target", "w") as f:
        f.write(phy)
    with open(RAW, "wb", buffering=0) as f:
        f.write(struct.pack("<I", CHANNEL_SWITCH) + req)
    outband = call(phy, 0x0094252e, 0xe, 0)
    f184 = call(phy, 0x0083a662, 0x184, 0)
    d7c = call(phy, 0x0083a662, 0xd7c, 0)
    print(f"outband req {mhz} MHz: cached outband {outband} kHz, RF0x184 {f184} kHz, RF0xd7c {d7c:#010x}"
          f" (bit21 {(d7c >> 21) & 1})")


if __name__ == "__main__":
    main()
