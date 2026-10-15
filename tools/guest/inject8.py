#!/usr/bin/env python3
"""Inject N ÷8-framed test frames (nbrxd.tx_frame with the inner CRC and tail padding) on a monitor interface.
Usage: inject8.py <monitor iface> [count 30] [interval ms 60] [rate 12]"""
import socket
import sys
import time

import nbrxd

iface = sys.argv[1]
n = int(sys.argv[2]) if len(sys.argv) > 2 else 30
iv = float(sys.argv[3]) / 1000 if len(sys.argv) > 3 else 0.06
rate = int(sys.argv[4]) if len(sys.argv) > 4 else 12
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
s.bind((iface, 0))
for k in range(n):
    s.send(nbrxd.tx_frame(b"MT7921-NBW-%08d" % k + bytes(48), k, rate, div8=True))
    time.sleep(iv)
