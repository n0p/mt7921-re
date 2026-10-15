#!/usr/bin/env python3
"""Drain one of mt7921dbg's IQ rings (/sys/kernel/debug/mt7921dbg/iq$IQSLOT, default 0) into a file while a stream
runs, so the 32 MiB ring never fills. Stops when the stop file exists (after a final drain) or after the given seconds.

Usage: iqdrain.py <out.bin> <seconds> [stopfile]
"""
import os
import sys
import time

IQ = "/sys/kernel/debug/mt7921dbg/iq" + os.environ.get("IQSLOT", "0")


def main():
    out, secs = sys.argv[1], float(sys.argv[2])
    stop = sys.argv[3] if len(sys.argv) > 3 else None
    fd = os.open(IQ, os.O_RDONLY)
    t_end, total = time.monotonic() + secs, 0
    with open(out, "wb") as f:
        while True:
            d = os.read(fd, 8 << 20)
            if d:
                f.write(d)
                total += len(d)
                continue
            if time.monotonic() > t_end or (stop and os.path.exists(stop)):
                d = os.read(fd, 64 << 20)
                f.write(d)
                total += len(d)
                break
            time.sleep(0.002)
    print(f"iqdrain: {total} bytes -> {out}")


if __name__ == "__main__":
    main()
