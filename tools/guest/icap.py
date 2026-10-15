#!/usr/bin/env python3
"""ICAP (internal IQ capture) on an MT7921 card through host register access (debugfs regidx/regval).

Register sequence from BirriaSDR (docs/mt7921.md, src/chips/mt7921_capture.c). The ring is SRAM 0xe0200000..
0xe023ffff, which the Wi-Fi firmware otherwise uses for packet buffers: put the firmware in ICAP test mode first
(ate.py <phy> mode 2) so the normal datapath is idle.

  icap.py <phy> <out.bin> [tap] [div] [ms] [nsamples]
    tap: capture SEL (domain 4/1: 0x830ad448[15:8]; domain 2: 0x830ad44c[7:0]); env ICAP_DOM = 4 (default,
         BirriaSDR's node), 1 or 2 (the firmware's other ICAP node domains, FUN_96c61c)
    div: 0x830ad448[19:18] (3 = 40 Msps for the 20 MHz profile)
Env ICAP_SETUP_ONLY=1 programs the capture (tap, rate, ring, control 0x4f1) and returns without starting it.
Env ICAP_RING=1 uses the firmware's ring mode 1 (64 KiB at 0xe0280000, which the datapath leaves alone) instead of the
256 KiB packet area, so the card need not be in test mode.
Writes nsamples complex samples (int16 I,Q interleaved, little endian) ending at the producer pointer.
Env ICAP_MODE overrides domain 4's MODE byte (0x830ad440[7:0], default 0x13). Env ICAP_RATE_HOOK=<addr of fw
hk_icaprate> runs the capture inside one firmware call and measures the real capture rate: producer-index advance over
500 us of the MCU timer (the debug command cannot answer while the ring is being written).
The ring is read word by word through regval. ICAP_BULK=1 reads it through /sys/kernel/debug/mt7921dbg/mem instead,
but after a capture those reads time out and the driver resets the card (2026-10-14): leave it off.
"""
import os
import struct
import sys
import time

MEM = "/sys/kernel/debug/mt7921dbg/"
RING, RING_SAMPLES = 0xe0200000, 65536
if os.environ.get("ICAP_RING") == "1":	# firmware ring mode 1: 64 KiB at 0xe0280000, outside the live datapath
    RING, RING_SAMPLES = 0xe0280000, 16384

D = "/sys/kernel/debug/ieee80211/{}/mt76/"


class Reg:
    def __init__(self, phy):
        self.idx, self.val = D.format(phy) + "regidx", D.format(phy) + "regval"

    def rr(self, a):
        with open(self.idx, "w") as f:
            f.write(str(a))
        with open(self.val) as f:
            return int(f.read().strip(), 0) & 0xffffffff

    def wr(self, a, v):
        with open(self.idx, "w") as f:
            f.write(str(a))
        with open(self.val, "w") as f:
            f.write(str(v & 0xffffffff))

    def upd(self, a, clr, st):
        self.wr(a, (self.rr(a) & ~clr) | st)


def main():
    phy, out = sys.argv[1], sys.argv[2]
    tap = int(sys.argv[3], 0) if len(sys.argv) > 3 else 4
    div = int(sys.argv[4], 0) if len(sys.argv) > 4 else 3
    ms = float(sys.argv[5]) if len(sys.argv) > 5 else 5
    n = int(sys.argv[6], 0) if len(sys.argv) > 6 else 8192
    r = Reg(phy)
    r.upd(0x820e3000, 0x200, 0x100)
    r.upd(0x83080004, 0, 0x3840)
    r.upd(0x83080008, 0, 0x3840)
    r.upd(0x830a3008, 3 << 21, 0)
    r.wr(0x830a1000, 0x04090100)
    r.upd(0x830a1004, 0xff, 0x10)
    r.wr(0x830a1008, 0)
    r.wr(0x80021090, 0xf1)
    first = (RING - 0xe0200000) // 4
    r.wr(0x80021098, first)
    r.wr(0x8002109c, first + RING_SAMPLES - 4)
    r.upd(0x800210b8, 0xf0000, 0x9c000000 | (int(os.environ.get("ICAP_PACK", "0"), 0) & 0xf) << 16)	# [19:16] packing
    dom = int(os.environ.get("ICAP_DOM", "4"))
    if dom == 1:	# firmware node domain 1: MODE 0x14, 0x830ad448 SEL [15:8], rate [19:18], activation bit 24
        r.upd(0x830ad440, 0xff, 0x80000014)
        r.upd(0x830ad448, 0x0fffffff, 0x01000000 | (div & 3) << 18 | (tap & 0xff) << 8)
    elif dom == 2:	# domain 2: MODE 0x21, 0x830ad44c SEL [7:0], rate [18:16] (env ICAP_RATE2, default 7), bit 21
        r.upd(0x830ad440, 0xff, 0x80000021)
        r.wr(0x830ad448, 0)
        r.upd(0x830ad44c, 0x0fffffff, 0x00200000 | (int(os.environ.get("ICAP_RATE2", "7"), 0) & 7) << 16 | (tap & 0xff))
    else:		# domain 4 (BirriaSDR): MODE 0x13, SEL [15:8], rate [19:18], bit 23
        r.upd(0x830ad440, 0xff, 0x80000000 | (int(os.environ.get("ICAP_MODE", "0x13"), 0) & 0xff))
        r.upd(0x830ad448, 0x0fffffff, 0x00800000 | (div & 3) << 18 | (tap & 0xff) << 8)
    r.upd(0x88009004, 0, 0x0f000000)
    r.wr(0x80021090, 0x4f1)
    if os.environ.get("ICAP_SETUP_ONLY"):	# capture registers only; a firmware hook starts it (hk_iqstream)
        print("icap: set up, not started")
        return
    p0 = r.rr(0x800210b4)
    if os.environ.get("ICAP_RATE_HOOK"):	# fw hk_icaprate: start, settle 200 us, count 500 us, stop, on the MCU
        sys.path.insert(0, "/root/fwre")
        from fwdbg import request
        US = int(os.environ.get("ICAP_RATE_US", "200"))
        d = request(phy, 3, int(os.environ["ICAP_RATE_HOOK"], 0), 0, args=[US, 0x24f3, 0x4f0, 200])[1]
        print(f"rate: producer advance per {US} us {d} -> {d / US:.2f} Msps")
    else:
        r.wr(0x80021090, 0x24f3)		# start circular capture
        time.sleep(ms / 1000)
        r.wr(0x80021090, 0x4f0)		# stop
    p1 = r.rr(0x800210b4)
    prod = ((p1 & 0xffff) - (RING - 0xe0200000) // 4) % RING_SAMPLES	# sample units (4 bytes each)
    start = (prod - n) % RING_SAMPLES
    if os.environ.get("ICAP_BULK") and os.path.exists(MEM + "mem"):
        with open(MEM + "target", "w") as f:
            f.write(phy)
        with open(MEM + "mem", "w") as f:
            f.write(f"{RING:#x} {RING_SAMPLES * 4}")
        with open(MEM + "mem", "rb") as f:
            ring = f.read()
        words = struct.unpack(f"<{RING_SAMPLES}I", ring)
        seq = [words[(start + i) % RING_SAMPLES] for i in range(n)]
    else:
        seq = [r.rr(RING + 4 * ((start + i) % RING_SAMPLES)) for i in range(n)]
    data = bytearray()
    if os.environ.get("ICAP_RAW"):	# raw 32-bit ring words instead of parsed I/Q
        seq, data = [], bytearray(struct.pack(f"<{len(seq)}I", *seq))
    for w in seq:
        q, iv = w & 0x3fff, (w >> 16) & 0x3fff
        q -= 0x4000 if q & 0x2000 else 0
        iv -= 0x4000 if iv & 0x2000 else 0
        data += struct.pack("<hh", iv, q)
    with open(out, "wb") as f:
        f.write(data)
    print(f"producer {p0:#x} -> {p1:#x}; saved {n} samples ending at {prod:#x} (tap {tap}, div {div})")


if __name__ == "__main__":
    main()
