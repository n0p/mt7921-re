#!/usr/bin/env python3
"""Transmit 802.11a/p OFDM data frames from the X410 continuously, for receiver scans that need traffic on air.

Usage: txloop.py <center MHz> <width 20|10|5> <seconds> [gap ms] [rate Mbps] [gain dB]
Builds one second of frames (payload "X410-OFDM-<Msps>-<seq>", SA 02:00:00:00:00:02, as txframes.py) and sends that
buffer back to back until <seconds> have passed. Each send call streams the whole buffer from C++ (python chunking
underflows); the short gap between calls only adds idle time between frames.
"""
import sys
import time

import numpy as np
import uhd

from ofdm80211 import mpdu_data, ppdu
from txframes import ADDR, MCR, PLAY, resample


def main():
    fc = float(sys.argv[1]) * 1e6
    w = int(sys.argv[2])
    secs = float(sys.argv[3])
    gap = float(sys.argv[4]) / 1e3 if len(sys.argv) > 4 else 0.002
    rate_mbps = int(sys.argv[5]) if len(sys.argv) > 5 else 6
    gain = float(sys.argv[6]) if len(sys.argv) > 6 else 40
    fs, up, down = PLAY[w]

    frames, total, seq = [], 0, 0
    while total < fs:
        f = mpdu_data(bytes.fromhex("020000000002"), b"X410-OFDM-%05.2f-%06d" % (fs / 1e6, seq) + bytes(64), seq)
        x = resample(ppdu(f, rate_mbps), up, down) * 0.3
        frames.append(np.concatenate([x, np.zeros(int(gap * fs), np.complex64)]))
        total += len(frames[-1])
        seq += 1
    wave = np.concatenate(frames).astype(np.complex64).reshape(1, -1)

    usrp = uhd.usrp.MultiUSRP(f"{ADDR},master_clock_rate={MCR:.0f}")
    usrp.set_tx_antenna("TX/RX0", 0)
    usrp.set_tx_rate(fs, 0)
    usrp.set_tx_freq(uhd.types.TuneRequest(fc), 0)
    usrp.set_tx_gain(gain, 0)
    st = usrp.get_tx_stream(uhd.usrp.StreamArgs("fc32", "sc16"))
    md = uhd.types.TXMetadata()
    md.start_of_burst = True
    md.end_of_burst = True
    t_end, loops = time.monotonic() + secs, 0
    while time.monotonic() < t_end:
        st.send(wave, md, timeout=wave.shape[1] / fs + 5)
        loops += 1
    print(f"txloop: {loops} x {seq} frames, {w} MHz at {fs / 1e6:.3f} Msps, {rate_mbps} Mbps, gain {gain}")


if __name__ == "__main__":
    main()
