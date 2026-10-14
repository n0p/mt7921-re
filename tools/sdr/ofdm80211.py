#!/usr/bin/env python3
"""Minimal IEEE 802.11a/p OFDM PPDU generator (numpy only).

Generates baseband samples at 20 Msps for one PPDU (legacy preamble + SIGNAL + DATA) carrying a MPDU with FCS.
Playing the same samples at 10 Msps gives the 802.11p 10 MHz waveform, at 5 Msps the 5 MHz one.
Supported rates (Mbps at 20 MHz): 6, 9, 12, 18, 24, 36, 48, 54.

Library use: ppdu(mpdu_bytes, rate_mbps=6) -> complex64 array
CLI self-test: ofdm80211.py  (builds a test frame and checks lengths/energy)
"""
import struct
import zlib

import numpy as np

# rate -> (SIGNAL rate bits R1..R4, bits/subcarrier, coding rate num/den)
RATES = {6: (0b1101, 1, (1, 2)), 9: (0b1111, 1, (3, 4)), 12: (0b0101, 2, (1, 2)), 18: (0b0111, 2, (3, 4)),
         24: (0b1001, 4, (1, 2)), 36: (0b1011, 4, (3, 4)), 48: (0b0001, 6, (2, 3)), 54: (0b0011, 6, (3, 4))}

DATA_SC = [k for k in range(-26, 27) if k not in (0, -21, -7, 7, 21)]
PILOT_SC = [-21, -7, 7, 21]
PILOT_BASE = np.array([1, 1, 1, -1])
# pilot polarity sequence p_0..p_126 (IEEE 802.11 17.3.5.10)
POLARITY = np.array([1, 1, 1, 1, -1, -1, -1, 1, -1, -1, -1, -1, 1, 1, -1, 1, -1, -1, 1, 1, -1, 1, 1, -1, 1, 1, 1, 1,
                     1, 1, -1, 1, 1, 1, -1, 1, 1, -1, -1, 1, 1, 1, -1, 1, -1, -1, -1, 1, -1, 1, -1, -1, 1, -1, -1,
                     1, 1, 1, 1, 1, -1, -1, 1, 1, -1, -1, 1, -1, 1, -1, 1, 1, -1, -1, -1, 1, 1, -1, -1, -1, -1, 1,
                     -1, -1, 1, -1, 1, 1, 1, 1, -1, 1, -1, 1, -1, 1, -1, -1, -1, -1, -1, 1, -1, 1, 1, -1, 1, -1, 1,
                     1, 1, -1, -1, 1, -1, -1, -1, 1, 1, 1, -1, -1, -1, -1, -1, -1, -1])


def ifft64(freq):
    """freq: dict subcarrier->value; returns 80 samples (16 CP + 64)."""
    x = np.zeros(64, complex)
    for k, v in freq.items():
        x[k % 64] = v
    t = np.fft.ifft(x) * 64 / np.sqrt(52)
    return np.concatenate([t[-16:], t])


def preamble():
    s = np.sqrt(13 / 6) * np.array([0, 0, 1 + 1j, 0, 0, 0, -1 - 1j, 0, 0, 0, 1 + 1j, 0, 0, 0, -1 - 1j, 0, 0, 0, -1 - 1j,
                                    0, 0, 0, 1 + 1j, 0, 0, 0, 0, 0, 0, 0, -1 - 1j, 0, 0, 0, -1 - 1j, 0, 0, 0, 1 + 1j,
                                    0, 0, 0, 1 + 1j, 0, 0, 0, 1 + 1j, 0, 0, 0, 1 + 1j, 0, 0])
    sf = {k: s[k + 26] for k in range(-26, 27)}
    x = np.zeros(64, complex)
    for k, v in sf.items():
        x[k % 64] = v
    short = np.fft.ifft(x) * 64 / np.sqrt(12)
    stf = np.tile(short[:16], 10)
    l_seq = [1, 1, -1, -1, 1, 1, -1, 1, -1, 1, 1, 1, 1, 1, 1, -1, -1, 1, 1, -1, 1, -1, 1, 1, 1, 1, 0,
             1, -1, -1, 1, 1, -1, 1, -1, 1, -1, -1, -1, -1, -1, 1, 1, -1, -1, 1, -1, 1, -1, 1, 1, 1, 1]
    x = np.zeros(64, complex)
    for i, k in enumerate(range(-26, 27)):
        x[k % 64] = l_seq[i]
    lt = np.fft.ifft(x) * 64 / np.sqrt(52)
    ltf = np.concatenate([lt[-32:], lt, lt])
    return np.concatenate([stf, ltf])


def conv_encode(bits):
    s = [0] * 6
    out = []
    for b in bits:
        r = [b] + s
        out.append(r[0] ^ r[2] ^ r[3] ^ r[5] ^ r[6])  # g0 = 133 octal
        out.append(r[0] ^ r[1] ^ r[2] ^ r[3] ^ r[6])  # g1 = 171 octal
        s = r[:6]
    return out


def puncture(bits, rate):
    if rate == (1, 2):
        return bits
    pat = {(3, 4): [1, 1, 1, 0, 0, 1], (2, 3): [1, 1, 1, 0]}[rate]
    return [b for i, b in enumerate(bits) if pat[i % len(pat)]]


def interleave(bits, ncbps, nbpsc):
    s = max(nbpsc // 2, 1)
    out = [0] * ncbps
    for k in range(ncbps):
        i = (ncbps // 16) * (k % 16) + k // 16
        j = s * (i // s) + (i + ncbps - (16 * i) // ncbps) % s
        out[j] = bits[k]
    return out


def modulate(bits, nbpsc):
    if nbpsc == 1:
        return [2 * b - 1 for b in bits]
    gray = {2: [-1, 1], 4: [-3, -1, 3, 1], 6: [-7, -5, -1, -3, 7, 5, 1, 3]}[nbpsc]
    norm = {2: np.sqrt(2), 4: np.sqrt(10), 6: np.sqrt(42)}[nbpsc]
    h = nbpsc // 2
    syms = []
    for i in range(0, len(bits), nbpsc):
        b = bits[i:i + nbpsc]
        iv = gray[int("".join(map(str, b[:h])), 2)] if h > 1 else (2 * b[0] - 1)
        qv = gray[int("".join(map(str, b[h:])), 2)] if h > 1 else (2 * b[1] - 1)
        syms.append((iv + 1j * qv) / norm)
    return syms


def ofdm_symbol(syms, idx):
    f = dict(zip(DATA_SC, syms))
    for k, p in zip(PILOT_SC, PILOT_BASE * POLARITY[idx % 127]):
        f[k] = p
    return ifft64(f)


def scramble(bits, seed=0b1011101):
    st = [(seed >> i) & 1 for i in range(6, -1, -1)]
    out = []
    for b in bits:
        fb = st[3] ^ st[6]
        out.append(b ^ fb)
        st = [fb] + st[:6]
    return out


def ppdu(mpdu, rate_mbps=6):
    rbits, nbpsc, cr = RATES[rate_mbps]
    ncbps = 48 * nbpsc
    ndbps = ncbps * cr[0] // cr[1]
    length = len(mpdu)
    # SIGNAL: RATE(4) R LENGTH(12, LSB first) P TAIL(6), BPSK 1/2, not scrambled
    sig = [(rbits >> (3 - i)) & 1 for i in range(4)] + [0] + [(length >> i) & 1 for i in range(12)]
    sig += [sum(sig) % 2] + [0] * 6
    sig_sym = modulate(interleave(conv_encode(sig), 48, 1), 1)
    # DATA: SERVICE(16 zeros) + PSDU (LSB first) + TAIL(6) + pad
    bits = [0] * 16 + [(byte >> i) & 1 for byte in mpdu for i in range(8)]
    nsym = -(-(len(bits) + 6) // ndbps)
    bits += [0] * (nsym * ndbps - len(bits))
    sb = scramble(bits)
    tail_pos = 16 + 8 * length
    sb[tail_pos:tail_pos + 6] = [0] * 6
    coded = puncture(conv_encode(sb), cr)
    out = [preamble(), ofdm_symbol(sig_sym, 0)]
    for n in range(nsym):
        blk = interleave(coded[n * ncbps:(n + 1) * ncbps], ncbps, nbpsc)
        out.append(ofdm_symbol(modulate(blk, nbpsc), n + 1))
    return np.concatenate(out).astype(np.complex64)


def mpdu_data(src, payload, seq=0, dst=b"\xff" * 6, bssid=None):
    """802.11 data frame (FromDS/ToDS = 0) + FCS."""
    hdr = struct.pack("<HH", 0x0008, 0) + dst + src + (bssid or src) + struct.pack("<H", (seq & 0xfff) << 4)
    body = hdr + payload
    return body + struct.pack("<I", zlib.crc32(body))


if __name__ == "__main__":
    f = mpdu_data(bytes.fromhex("020000000002"), b"OFDM80211-SELFTEST" + bytes(100))
    for r in (6, 12, 54):
        x = ppdu(f, r)
        print(f"rate {r}: {len(f)} B MPDU -> {len(x)} samples = {len(x) / 20:.1f} us @20Msps,"
              f" {len(x) / 10:.1f} us @10Msps; rms {np.sqrt(np.mean(np.abs(x) ** 2)):.3f}")
