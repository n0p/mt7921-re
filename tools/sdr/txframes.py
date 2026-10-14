#!/usr/bin/env python3
"""Transmit 802.11a/p OFDM data frames from the X410 (reference transmitter for 20/10/5 MHz tests).

Usage: txframes.py <center MHz> <width 20|10|5|r> [count] [gap ms] [rate Mbps] [gain dB]
  width r (any other value, Msps = 250/N): play the 20 Msps waveform unresampled at r Msps, i.e. an OFDM signal
  scaled by r/20 (subcarrier spacing 312.5 kHz * r/20). Useful to find what sample rate a receiver really uses.
Frames: broadcast data, SA 02:00:00:00:00:02, payload "X410-OFDM-<width>-<seq>". The 20 MHz baseband from
ofdm80211.ppdu() played at 20/10/5 Msps is exactly the 802.11a / 802.11p 10 MHz / 5 MHz waveform.
The X4_200 FPGA image only allows a 250 MHz master clock, so 10/5 Msps are exact (/25, /50) and 20 MHz is played at
25 Msps after an exact 5/4 FFT resampling of each frame.
"""
import sys

import numpy as np
import uhd

from ofdm80211 import mpdu_data, ppdu

ADDR = "addr=10.205.52.15"
MCR = 250e6
PLAY = {20: (25e6, 5, 4), 10: (10e6, 1, 1), 5: (5e6, 1, 1)}  # width -> (output rate, up, down)


def resample(x, up, down):
    if up == down:
        return x
    n = len(x)
    m = n * up // down
    f = np.fft.fftshift(np.fft.fft(x))
    pad = (m - n) // 2
    f = np.concatenate([np.zeros(pad, complex), f, np.zeros(m - n - pad, complex)])
    return (np.fft.ifft(np.fft.ifftshift(f)) * m / n).astype(np.complex64)


def main():
    fc = float(sys.argv[1]) * 1e6
    w = float(sys.argv[2])
    w = int(w) if w.is_integer() else w
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 200
    gap = float(sys.argv[4]) / 1e3 if len(sys.argv) > 4 else 0.005
    rate_mbps = int(sys.argv[5]) if len(sys.argv) > 5 else 6
    gain = float(sys.argv[6]) if len(sys.argv) > 6 else 20
    if w in PLAY:
        fs, up, down = PLAY[w]
    else:
        n = round(MCR / (w * 1e6))
        fs, up, down = MCR / n, 1, 1

    frames = []
    for seq in range(n):
        f = mpdu_data(bytes.fromhex("020000000002"), b"X410-OFDM-%05.2f-%06d" % (fs / 1e6, seq) + bytes(64), seq)
        x = resample(ppdu(f, rate_mbps), up, down) * 0.3
        frames.append(np.concatenate([x, np.zeros(int(gap * fs), np.complex64)]))
    wave = np.concatenate(frames).astype(np.complex64)

    usrp = uhd.usrp.MultiUSRP(f"{ADDR},master_clock_rate={MCR:.0f}")
    usrp.set_tx_antenna("TX/RX0", 0)
    usrp.set_tx_rate(fs, 0)
    usrp.set_tx_freq(uhd.types.TuneRequest(fc), 0)
    usrp.set_tx_gain(gain, 0)
    st = usrp.get_tx_stream(uhd.usrp.StreamArgs("fc32", "sc16"))
    md = uhd.types.TXMetadata()
    md.start_of_burst = True
    md.end_of_burst = True
    # one send call: UHD streams the whole buffer from C++ (python chunking underflows at 25 Msps)
    sent = st.send(wave.reshape(1, -1), md, timeout=len(wave) / fs + 5)
    if sent != len(wave):
        print(f"short send: {sent}/{len(wave)}")
    print(f"sent {n} frames, {w} MHz ({fs / 1e6:.3f} Msps, mcr {usrp.get_master_clock_rate() / 1e6:.2f} MHz),"
          f" {rate_mbps} Mbps, {len(wave) / fs:.2f} s, gain {gain}")


if __name__ == "__main__":
    main()
