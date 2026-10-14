#!/usr/bin/env python3
"""Raw X410 capture to a cf32 file (keep it short: lorien is shared).
Usage: capture.py <center MHz> <rate Msps> <seconds> <out.cf32> [gain]"""
import sys

import numpy as np
import uhd

fc, rate, secs = (float(x) for x in sys.argv[1:4])
gain = float(sys.argv[5]) if len(sys.argv) > 5 else 30
usrp = uhd.usrp.MultiUSRP("addr=10.205.52.15,master_clock_rate=250e6")
usrp.set_rx_antenna("TX/RX0", 0)
x = usrp.recv_num_samps(int(secs * rate * 1e6), fc * 1e6, rate * 1e6, [0], gain)[0]
x.astype(np.complex64).tofile(sys.argv[4])
print(f"{len(x)} samples at {usrp.get_rx_rate(0) / 1e6:.4f} Msps -> {sys.argv[4]}")
