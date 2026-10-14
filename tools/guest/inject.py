#!/usr/bin/env python3
"""Inject broadcast 802.11 data frames on a monitor interface (stdlib only).

Usage: inject.py <monitor iface> [count] [interval_ms] [rate_500k] [pad bytes, default 200]
Frames: radiotap (rate + NOACK) + data frame from 02:00:00:00:00:01, payload "MT7921-NBW-<n>".
Default rate 12 (= 6 Mbps OFDM) so the transmission is OFDM, which makes occupied bandwidth easy to read on an SDR.
"""
import socket
import struct
import sys
import time

SRC = bytes.fromhex("020000000001")


def frame(seq, rate, pad=200):
    # radiotap: version 0, pad, len 12, present = RATE (bit 2) | TX_FLAGS (bit 15)
    rt = struct.pack("<BBHI", 0, 0, 12, (1 << 2) | (1 << 15)) + struct.pack("<BxH", rate, 0x0008)  # NOACK
    hdr = struct.pack("<HH", 0x0008, 0) + b"\xff" * 6 + SRC + SRC + struct.pack("<H", (seq & 0xfff) << 4)
    llc = bytes.fromhex("aaaa03000000") + struct.pack(">H", 0x88b5)  # local experimental ethertype
    return rt + hdr + llc + b"MT7921-NBW-%08d" % seq + bytes(pad)


def main():
    iface = sys.argv[1]
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
    interval = (int(sys.argv[3]) if len(sys.argv) > 3 else 10) / 1000.0
    rate = int(sys.argv[4]) if len(sys.argv) > 4 else 12
    pad = int(sys.argv[5]) if len(sys.argv) > 5 else 200
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    s.bind((iface, 0))
    sent = 0
    for n in range(count):
        try:
            s.send(frame(n, rate, pad))
            sent += 1
        except OSError as e:
            print(f"send {n}: {e}", file=sys.stderr)
        time.sleep(interval)
    print(f"sent {sent}/{count} on {iface} rate={rate * 500}kbps")


if __name__ == "__main__":
    main()
