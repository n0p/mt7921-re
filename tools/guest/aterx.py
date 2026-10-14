#!/usr/bin/env python3
"""ATE receive test helper (card in RF test state via ate.py).

  aterx.py <phy> start <freq kHz> <jmode 0|1|2>   set channel + J-mode, COMMAND=STARTRX
  aterx.py <phy> stats                             query TXOK (0x21) / RXOK (0x22) / RXERR (0x23) / RSSI (0x2e)
  aterx.py <phy> stop                              COMMAND=STOPTEST
"""
import importlib.util
import struct
import sys
import time

spec = importlib.util.spec_from_file_location("ate", "/root/fwre/ate.py")
ate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ate)


def query(phy, fid):
    r = ate.send(phy, 2, fid, 0, True)
    return struct.unpack("<II", r[-8:]) if len(r) >= 8 else (None, None)


def main():
    phy, op = sys.argv[1], sys.argv[2]
    if op == "start":
        ate.send(phy, 1, 0x01, 0, False)			# stop whatever runs
        time.sleep(0.1)
        ate.send(phy, 5, int(sys.argv[3]), 0, False)		# channel frequency, kHz
        ate.send(phy, 1, 0x4b, int(sys.argv[4]), False)		# J-mode
        time.sleep(0.1)
        ate.send(phy, 1, 0x01, 2, False)			# COMMAND = STARTRX
        print(f"ATE RX started {sys.argv[3]} kHz jmode {sys.argv[4]}")
    elif op == "stats":
        for name, fid in (("txok", 0x21), ("rxok", 0x22), ("rxerr", 0x23), ("rssi", 0x2e)):
            p0, p1 = query(phy, fid)
            print(f"{name}: p0={p0} p1={p1}")
    elif op == "stop":
        ate.send(phy, 1, 0x01, 0, False)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
