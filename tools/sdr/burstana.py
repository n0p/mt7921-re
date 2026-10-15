"""Structure of bursts in a continuous packer-2 dump: duration, -20 dB bandwidth, STF period, OFDM symbol lag."""
import struct, sys
import numpy as np
import iqrec
path = sys.argv[1]
fs = 10e6
ws, poss = [], []
with open(path, "rb") as f:
    while True:
        h = f.read(16)
        if len(h) < 16: break
        mark, seq, pos, n = struct.unpack("<4I", h)
        d = f.read(n)
        if len(d) < n: break
        ws.append(np.frombuffer(d, np.uint32)); poss.append(pos)
# longest contiguous run
runs, cur = [], [0]
for k in range(1, len(ws)):
    if (poss[k] - poss[k - 1] - len(ws[k - 1])) % 2**32 == 0: cur.append(k)
    else: runs.append(cur); cur = [k]
runs.append(cur)
run = max(runs, key=len)
x = iqrec.words_to_iq(np.concatenate([ws[k] for k in run]), 2)
x = x - x.mean()
B = 20  # 2 us
p = (np.abs(x[:len(x) // B * B]) ** 2).reshape(-1, B).mean(1)
med = np.median(p)
on = p > 30 * med
edges = np.flatnonzero(np.diff(on.astype(np.int8)) == 1) + 1
bursts = []
for e in edges:
    k = e
    while k < len(on) and on[k]: k += 1
    if k - e >= 25:  # >= 50 us
        bursts.append((e * B, k * B))
print(f"{len(x) / fs:.2f} s, {len(bursts)} bursts >= 50 us; durations us: {sorted(set(round((b - a) / fs * 1e6, -1) for a, b in bursts))[:12]}")
for a, b in bursts[:4]:
    y = x[a:b]
    S = np.abs(np.fft.fftshift(np.fft.fft(y * np.hanning(len(y)), 4096))) ** 2
    S /= S.max()
    fr = (np.arange(4096) - 2048) / 4096 * fs / 1e6
    occ = fr[S > 0.01]
    lags = np.arange(8, 400)
    ac = np.array([abs(np.vdot(y[:-L], y[L:])) / np.vdot(y, y).real for L in lags])
    top = lags[np.argsort(ac)[::-1][:6]]
    # STF region: first 30 us
    st = x[a:a + 300]
    sl = np.arange(8, 120)
    sac = np.array([abs(np.vdot(st[:-L], st[L:])) / np.vdot(st, st).real for L in sl])
    print(f"burst {(b - a) / fs * 1e6:7.1f} us: -20 dB span {occ.min():+.2f}..{occ.max():+.2f} MHz; "
          f"strongest lags (us) {[round(L / fs * 1e6, 1) for L in top]} ac {np.sort(ac)[::-1][:3].round(2)}; "
          f"head period (us) {[round(L / fs * 1e6, 1) for L in sl[np.argsort(sac)[::-1][:4]]]}")
