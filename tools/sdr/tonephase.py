"""Sampling instants of the ICAP packers from a CW tone: mean phase step between consecutive words of each beat."""
import struct, sys
import numpy as np
import iqrec

def load(path):
    ws, poss = [], []
    with open(path, "rb") as f:
        while True:
            h = f.read(16)
            if len(h) < 16: break
            mark, seq, pos, n = struct.unpack("<4I", h)
            d = f.read(n)
            if len(d) < n: break
            ws.append(np.frombuffer(d, dtype=np.uint32)); poss.append(pos)
    return ws, poss

for path in sys.argv[1:]:
    ws, poss = load(path)
    # longest contiguous run
    runs, cur = [], [0]
    for k in range(1, len(ws)):
        if (poss[k] - poss[k - 1] - len(ws[k - 1])) % 2**32 == 0: cur.append(k)
        else: runs.append(cur); cur = [k]
    runs.append(cur)
    run = max(runs, key=len)
    w = np.concatenate([ws[k] for k in run])
    w = w[:len(w) // 4 * 4].reshape(-1, 4)
    lanes = [k for k in range(4) if (w[:, k] != 0).mean() > 0.5]
    x = np.stack([iqrec.words_to_iq(w[:, k]) for k in range(4)], 1)
    x = x - x.mean(0)
    seq = x[:, lanes].ravel()  # samples in word order
    nl = len(lanes)
    print(f"{path}: {len(run)} contiguous records, {len(w)} beats, sample lanes {lanes}, nonzero/lane "
          f"{[round(float((w[:, k] != 0).mean()), 3) for k in range(4)]}")
    print("  rms per lane", np.sqrt((np.abs(x) ** 2).mean(0)).round(1))
    # phase step from word j to word j+1 (within beat) and last -> first of next beat
    steps = []
    for j in range(nl):
        a = seq[j::nl]
        b = seq[j + 1::nl]
        m = min(len(a), len(b))
        c = np.vdot(a[:m], b[:m])
        coh = abs(c) / np.sqrt(np.vdot(a[:m], a[:m]).real * np.vdot(b[:m], b[:m]).real)
        steps.append((np.degrees(np.angle(c)), coh))
    tot = sum(s for s, _ in steps)
    print("  phase step per word (deg, coherence):", [(round(s, 2), round(c, 3)) for s, c in steps], "beat total", round(tot, 2))
    # spectrum of the word-order sequence: strongest 6 peaks (bins of 1/len)
    n = 1 << int(np.log2(min(len(seq), 1 << 20)))
    S = np.abs(np.fft.fftshift(np.fft.fft(seq[:n] * np.hanning(n)))) ** 2
    S /= S.max()
    pk = np.argsort(S)[::-1]
    shown = []
    for i in pk:
        if all(abs(i - j) > n // 2000 for j in shown):
            shown.append(i)
        if len(shown) == 6: break
    print("  top spectral peaks (fraction of word-order rate, dB):", [(round((i - n // 2) / n, 4), round(10 * np.log10(S[i]), 1)) for i in shown])
