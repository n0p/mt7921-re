#!/usr/bin/env python3
"""Transmit a continuous complex tone from the X410 (reference signal for receiver-chain measurements).

Usage: txtone.py <center MHz> <offset MHz> <seconds> [gain dB]
"""
import sys

import numpy as np
import uhd

ADDR = "addr=10.205.52.15,master_clock_rate=250e6"


def main():
    fc, off, secs = (float(x) for x in sys.argv[1:4])
    gain = float(sys.argv[4]) if len(sys.argv) > 4 else 20
    fs = 12.5e6
    n = int(fs / 1e5)	# 100 kHz grid: 125 samples per period of the shortest tone step
    t = np.arange(n * 100) / fs
    wave = (0.3 * np.exp(2j * np.pi * off * 1e6 * t)).astype(np.complex64)
    usrp = uhd.usrp.MultiUSRP(ADDR)
    usrp.set_tx_antenna("TX/RX0", 0)
    usrp.send_waveform(wave, secs, fc * 1e6, fs, [0], gain)
    print(f"tone {fc + off:.3f} MHz for {secs} s, gain {gain}")


if __name__ == "__main__":
    main()
