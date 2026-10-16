#!/usr/bin/env python3
"""Snapshot the 256 KiB packet SRAM (0xe0200000..0xe023ffff) through MCU reads into a .npy file.
Usage: sramsnap.py <phy> <out.npy>"""
import sys

import numpy as np

sys.path.insert(0, "/root/fwre")
from fwdbg import request  # noqa: E402

phy, out = sys.argv[1], sys.argv[2]
w = []
for a in range(0xE0200000, 0xE0240000, 256):
    w += list(request(phy, 1, a, 64)[2])
np.save(out, np.array(w, dtype=np.uint32))
print("saved", len(w), "words")
