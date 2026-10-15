#!/usr/bin/env python3
"""Send EXT CHANNEL_SWITCH (and SET_RX_PATH) directly to the MT7961 firmware through mt7921dbg raw.

  chsw.py <phy> <ctrl ch> <band 0=2.4G|1=5G> <cbw> [tx_streams] [rx_mask] [outband MHz] [center ch]
    cbw: driver CMD_CBW_* encoding (0=20, 1=40, 2=80, 3=160, 4=10 MHz, 5=5 MHz)
Afterwards prints the firmware state through fwdbg: cached internal bw, clock-mode regs for both paths, freq.
The firmware skips a switch to the same channel number, so hop to another channel first (e.g. chsw.py phy 11 0 0).
"""
import os
import struct
import subprocess
import sys

sys.path.insert(0, "/root/fwre")
from dbglock import held  # noqa: E402

RAW = "/sys/kernel/debug/mt7921dbg/raw"
EXT = 0xed


def fw(phy, *a):
    out = subprocess.run(["python3", "/root/fwre/fwdbg.py", phy] + list(a), capture_output=True, text=True).stdout
    return out.split()


def send(phy, ext_id, req):
    with held():  # target + raw as one step: another tool may be rewriting target for the other card
        with open("/sys/kernel/debug/mt7921dbg/target", "w") as f:
            f.write(phy)
        with open(RAW, "wb", buffering=0) as f:
            f.write(struct.pack("<I", EXT | (ext_id << 8)) + req)


def main():
    phy = sys.argv[1]
    ch, band, cbw = (int(x, 0) for x in sys.argv[2:5])
    tx = int(sys.argv[5], 0) if len(sys.argv) > 5 else 1
    rx = int(sys.argv[6], 0) if len(sys.argv) > 6 else 1
    outband = int(sys.argv[7], 0) if len(sys.argv) > 7 else 0
    center = int(sys.argv[8], 0) if len(sys.argv) > 8 else ch
    req = struct.pack("<BBBBBBBBHBBIBBB57x", ch, center, cbw, tx, rx, 0, 0, 0, 0, band, 0, outband, 0, 0, 0)
    send(phy, 0x08, req)	# CHANNEL_SWITCH
    send(phy, 0x4e, req)	# SET_RX_PATH (the driver sends both)
    if os.environ.get("NOSTAT"):	# firmware without the 0xcb debug command (e.g. stock 2022): host register read only
        import importlib.util
        spec = importlib.util.spec_from_file_location("icap", "/root/fwre/icap.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        r = m.Reg(phy)
        print(f"ch {ch} band {band} cbw {cbw} tx {tx} rx {rx:#x} outband {outband}: clk0={r.rr(0x83081000):08x}"
              f" clk1={r.rr(0x83091000):08x} 0x83086010={r.rr(0x83086010):08x}")
        return
    bw = fw(phy, "call", "0x0094252e", "3", "0")[-1]
    clk0 = fw(phy, "read", "0x83081000", "1")[-1]
    clk1 = fw(phy, "read", "0x83091000", "1")[-1]
    f184 = int(fw(phy, "call", "0x0083a662", "0x184", "0")[-1], 16)
    print(f"ch {ch} band {band} cbw {cbw} tx {tx} rx {rx:#x} outband {outband}: fw bw={bw} clk0={clk0} clk1={clk1}"
          f" RF0x184={f184} kHz")


if __name__ == "__main__":
    main()
