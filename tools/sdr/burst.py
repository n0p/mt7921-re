#!/usr/bin/env python3
"""Analyse the first strong burst in a UHD capture (runs on lorien).

Usage: burst.py <center MHz> [seconds] [rate Msps] [gain] [antenna]
Prints JSON with:
  start_ms / dur_ms     first burst (power > floor + 15 dB) and its duration
  envelope_db           mean power per 64 samples over the first 40 ms of the burst
  gaps_us               gaps (> 3 envelope points below -10 dB of burst median) inside the burst
  cp_lag_us, cp_corr    OFDM cyclic-prefix autocorrelation peak: lag (= FFT period) and normalised strength,
                        searched over 1..400 us inside the burst
"""
import json
import sys

import numpy as np
import uhd

ARGS = "addr=10.205.52.15"


def main():
    fc = float(sys.argv[1]) * 1e6
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 0.45
    rate = float(sys.argv[3]) * 1e6 if len(sys.argv) > 3 else 7.68e6
    gain = float(sys.argv[4]) if len(sys.argv) > 4 else 30
    ant = sys.argv[5] if len(sys.argv) > 5 else "TX/RX0"
    usrp = uhd.usrp.MultiUSRP(ARGS)
    usrp.set_rx_antenna(ant, 0)
    x = usrp.recv_num_samps(int(secs * rate), fc, rate, [0], gain)[0]
    rate = usrp.get_rx_rate(0)  # UHD coerces to MCR/N (250 MHz MCR: 30.72 -> 31.25 Msps); use the real rate

    blk = 64
    p = (np.abs(x[: len(x) // blk * blk]) ** 2).reshape(-1, blk).mean(axis=1)
    pdb = 10 * np.log10(p + 1e-20)
    floor = np.percentile(pdb, 10)
    on = pdb > floor + 15
    out = {"rate_msps": rate / 1e6, "floor_db": round(float(floor), 1)}
    if not on.any():
        out["burst"] = None
        print(json.dumps(out))
        return
    s = int(np.argmax(on))
    e = s
    while e < len(on) and (on[e] or on[e:e + 8].any()):
        e += 1
    us = blk / rate * 1e6
    out.update(start_ms=round(s * us / 1000, 3), dur_ms=round((e - s) * us / 1000, 3))
    seg = pdb[s:min(e, s + int(40e3 / us))]
    out["envelope_db"] = [round(float(v - floor), 1) for v in seg]
    med = np.median(seg)
    gaps, run = [], 0
    for v in seg:
        if v < med - 10:
            run += 1
        elif run:
            if run > 3:
                gaps.append(round(run * us, 1))
            run = 0
    out["gaps_us"] = gaps[:40]

    # cyclic-prefix autocorrelation inside the burst (FFT based, first 2^17 samples of it), lags 2..400 us;
    # short lags are skipped because a narrow signal is correlated with itself over ~1/bandwidth anyway
    seg_iq = x[s * blk: min(e * blk, s * blk + (1 << 17))]
    n = 1 << int(np.ceil(np.log2(2 * len(seg_iq))))
    f = np.fft.fft(seg_iq, n)
    r = np.abs(np.fft.ifft(f * np.conj(f))[: len(seg_iq)])
    r /= r[0] * (len(seg_iq) - np.arange(len(seg_iq))) / len(seg_iq) + 1e-20
    lo, hi = int(2e-6 * rate), min(int(400e-6 * rate), len(seg_iq) // 4)
    lag = lo + int(np.argmax(r[lo:hi]))
    top = sorted(range(lo, hi), key=lambda k: -r[k])[:5]
    # spectrum of the burst itself (Welch, 256-point), -20 dB occupied width
    nb = 256
    segs = seg_iq[: len(seg_iq) // nb * nb].reshape(-1, nb) * np.hanning(nb)
    psd = (np.abs(np.fft.fftshift(np.fft.fft(segs, axis=1), axes=1)) ** 2).mean(axis=0)
    pdb_f = 10 * np.log10(psd + 1e-20)
    above = np.where(pdb_f >= pdb_f.max() - 20)[0]
    out["burst_obw20_mhz"] = round((above[-1] - above[0] + 1) * rate / nb / 1e6, 2) if len(above) else 0
    out.update(cp_lag_us=round(lag / rate * 1e6, 2), cp_corr=round(float(r[lag]), 3),
               cp_top=[(round(k / rate * 1e6, 2), round(float(r[k]), 3)) for k in top])
    # every burst: CP lag (2..60 us) and -20 dB width per burst, summarised as a histogram (our frames dominate)
    allb, i = [], 0
    while i < len(on):
        if on[i]:
            j = i
            while j < len(on) and (on[j] or on[j:j + 8].any()):
                j += 1
            iq = x[i * blk: j * blk]
            if len(iq) > int(40e-6 * rate):
                n2 = 1 << int(np.ceil(np.log2(2 * len(iq))))
                f2 = np.fft.fft(iq, n2)
                r2 = np.abs(np.fft.ifft(f2 * np.conj(f2))[: len(iq)])
                r2 /= r2[0] * (len(iq) - np.arange(len(iq))) / len(iq) + 1e-20
                lo2, hi2 = int(2e-6 * rate), min(int(60e-6 * rate), len(iq) // 3)
                if hi2 > lo2:
                    k = lo2 + int(np.argmax(r2[lo2:hi2]))
                    allb.append((round(k / rate * 1e6, 1), round((j - i) * us / 1000, 3), round(float(r2[k]), 2)))
            i = j
        i += 1
    hist = {}
    for lag, dur, c in allb:
        if c >= 0.15:
            hist.setdefault(lag, []).append(dur)
    out["bursts_by_cp_lag"] = {f"{k}us": {"n": len(v), "dur_ms_med": sorted(v)[len(v) // 2]}
                               for k, v in sorted(hist.items(), key=lambda kv: -len(kv[1]))[:5]}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
