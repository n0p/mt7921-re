#!/usr/bin/env python3
"""Burst-gated spectrum measurement with a UHD device (runs on lorien, X410 at 10.205.52.15).

Usage: psd.py <center MHz> [seconds] [rate Msps] [gain dB] [antenna] [label]
Prints one JSON line: peak/floor dBFS, OBW at -20 dB from peak and 99 % power, centre of gravity, number of
signal blocks, and a coarse 128-bin spectrum. Only FFT blocks whose power is >= floor + 10 dB are averaged,
so the gaps between 802.11 frames do not dilute the measurement.
"""
import json
import sys

import numpy as np
import uhd

ARGS = "addr=10.205.52.15"
NFFT = 1024


def main():
    fc = float(sys.argv[1]) * 1e6
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    rate = float(sys.argv[3]) * 1e6 if len(sys.argv) > 3 else 31.25e6
    gain = float(sys.argv[4]) if len(sys.argv) > 4 else 40
    ant = sys.argv[5] if len(sys.argv) > 5 else "TX/RX0"
    label = sys.argv[6] if len(sys.argv) > 6 else ""

    usrp = uhd.usrp.MultiUSRP(ARGS)
    usrp.set_rx_antenna(ant, 0)
    x = usrp.recv_num_samps(int(secs * rate), fc, rate, [0], gain)[0]
    rate = usrp.get_rx_rate(0)  # UHD coerces to MCR/N (250 MHz MCR: 30.72 -> 31.25 Msps); use the real rate

    blocks = x[: len(x) // NFFT * NFFT].reshape(-1, NFFT)
    win = np.hanning(NFFT)
    spec = np.abs(np.fft.fftshift(np.fft.fft(blocks * win, axis=1), axes=1)) ** 2
    bpow = 10 * np.log10(spec.mean(axis=1) + 1e-20)
    floor_blk = np.percentile(bpow, 20)
    sig = bpow >= floor_blk + 10
    noise = spec[~sig].mean(axis=0) if (~sig).any() else spec.mean(axis=0)
    psd = spec[sig].mean(axis=0) if sig.any() else spec.mean(axis=0)
    psd_db = 10 * np.log10(psd + 1e-20)
    noise_db = 10 * np.log10(noise + 1e-20)
    freqs = (np.arange(NFFT) - NFFT / 2) * rate / NFFT

    peak = psd_db.max()
    above = np.where(psd_db >= peak - 20)[0]
    obw20 = (above[-1] - above[0] + 1) * rate / NFFT if len(above) else 0
    excess = np.clip(psd - noise, 0, None)
    c = np.cumsum(excess)
    lo, hi = np.searchsorted(c, 0.005 * c[-1]), np.searchsorted(c, 0.995 * c[-1])
    obw99 = (hi - lo) * rate / NFFT
    cog = float((excess * freqs).sum() / excess.sum()) if excess.sum() > 0 else 0.0
    coarse = psd_db.reshape(128, -1).mean(axis=1)

    print(json.dumps({
        "label": label, "fc_mhz": fc / 1e6, "rate_msps": rate / 1e6, "gain": gain,
        "signal_blocks": int(sig.sum()), "blocks": len(bpow),
        "peak_db": round(float(peak), 1), "floor_db": round(float(np.median(noise_db)), 1),
        "obw20_mhz": round(obw20 / 1e6, 2), "obw99_mhz": round(obw99 / 1e6, 2), "cog_khz": round(cog / 1e3, 1),
        "spectrum_db": [round(float(v), 1) for v in coarse],
        # block power over time (1024-sample blocks), as dB above the 5th percentile: first 3000 blocks raw,
        # plus the whole capture max-pooled to <= 3000 points (timeline_step blocks per point)
        "timeline_db": [round(float(v - np.percentile(bpow, 5)), 1) for v in bpow[:3000]],
        "timeline_step": int(np.ceil(len(bpow) / 3000)),
        "timeline_full_db": [round(float(v - np.percentile(bpow, 5)), 1) for v in
                             bpow[: len(bpow) // int(np.ceil(len(bpow) / 3000)) * int(np.ceil(len(bpow) / 3000))]
                             .reshape(-1, int(np.ceil(len(bpow) / 3000))).max(axis=1)],
    }))


if __name__ == "__main__":
    main()
