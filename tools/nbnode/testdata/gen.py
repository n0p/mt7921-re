#!/usr/bin/env python3
"""Test vectors for the nbnode crate (tools/nbnode, the Rust port of tools/sdr/rx80211.py).

Run:  python3 -I tools/nbnode/testdata/gen.py
It needs numpy only and is deterministic: fixed seeds give the same files on every run. It imports
tools/sdr/ofdm80211.py (to build the frames) and tools/sdr/rx80211.py (for the Python reference decodes) from their
directory.

Each frame vector is a .cf32 file (numpy complex64: interleaved little-endian float32 I/Q) at the 20 Msps-equivalent
rate, unless noted otherwise, with:
  <name>.truth          what was transmitted, one "frame" line per PPDU
  <name>.<mode>.pyref   what rx80211.decode_all finds in it, one "frame" line per result, plus the "options" (and
                        "frontend") lines the Rust tests read back so that both sides run the same settings
manifest.txt (also written here) lists every file with its parameters.

SNR convention: a frame from ofdm80211 has unit mean power per sample (its IFFT is scaled by 64/sqrt(52)). The noise
is complex white Gaussian with variance 10^(-SNR/10) per sample at the frame's own rate. The 52 used subcarriers
therefore see an SNR 10 log10(64/52) = 0.9 dB above the nominal value.
"""
import hashlib
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SDR = os.path.normpath(os.path.join(HERE, "..", "..", "sdr"))
sys.path.insert(0, SDR)
import ofdm80211  # noqa: E402
import rx80211  # noqa: E402

# Receiver settings of the Python references = the Rust DecodeOptions defaults. They are written into every .pyref
# "options" line, and the Rust tests apply the same values.
STF_THRESH = 0.6
H_SMOOTH = 1
rx80211.STF_THRESH = STF_THRESH
rx80211.H_SMOOTH = H_SMOOTH
RX_SHA1 = hashlib.sha1(open(os.path.join(SDR, "rx80211.py"), "rb").read()).hexdigest()

RATES = [6, 9, 12, 18, 24, 36, 48, 54]
SRC = bytes.fromhex("020000000002")
# decode_all settings of the reference decodes: "plain" = the Python defaults (no SNR gate, max_len 4095), "nbrxd" =
# those of tools/guest/nbrxd.py. Both raise max_frames from 50 to 200 so that no vector is cut short (the cap itself
# is tested on the Rust side).
MODES = {
    "plain": dict(max_frames=200, min_snr=None, max_len=4095),
    "nbrxd": dict(max_frames=200, min_snr=-1.0, max_len=1600),
}
# SNR near each rate's decoding threshold with STF_THRESH 0.6 / H_SMOOTH 1: rx80211 decodes roughly 70-90% of
# 128-byte MPDUs there (measured with this generator, 24 frames per point)
LOW_SNR = {6: 1.5, 9: 2.5, 12: 4.0, 18: 6.5, 24: 8.5, 36: 12.0, 48: 15.5, 54: 17.0}

manifest = []


# ----------------------------------------------------------------------------------------------------------------------
# signal construction
# ----------------------------------------------------------------------------------------------------------------------

def noise(rng, n, var):
    """Complex white Gaussian noise, variance `var` per sample (scalar or per-sample array)."""
    return np.sqrt(np.asarray(var) / 2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))


def make_mpdu(rng, rate, n_payload, seq):
    """802.11 data frame (ofdm80211.mpdu_data, FCS appended): a readable tag, then random bytes."""
    tag = f"NBNODE rate={rate:02d} seq={seq:03d} ".encode()
    body = (tag + rng.integers(0, 256, max(0, n_payload - len(tag)), dtype=np.uint8).tobytes())[:n_payload]
    return ofdm80211.mpdu_data(SRC, body, seq=seq)


def assemble(rng, frames, lead, tail, gaps):
    """Concatenates silence gaps and frames. Each frame dict has mpdu, rate, cfo (cycles/sample, applied from the
    frame's first sample, random initial phase) and snr (dB, or None). Returns (clean signal, per-sample noise
    variance, truth rows). A gap takes the noise level of the frame after it; the tail, that of the last frame."""
    parts, var, truth, pos = [], [], [], 0
    for i, f in enumerate(frames):
        gap = lead if i == 0 else int(rng.integers(*gaps))
        x = ofdm80211.ppdu(f["mpdu"], f["rate"]).astype(complex)
        x *= np.exp(1j * (2 * np.pi * f["cfo"] * np.arange(len(x)) + rng.uniform(0, 2 * np.pi)))
        v = 0.0 if f["snr"] is None else 10 ** (-f["snr"] / 10)
        parts += [np.zeros(gap, complex), x]
        var += [np.full(gap + len(x), v)]
        truth.append(dict(start=pos + gap, rate=f["rate"], length=len(f["mpdu"]), cfo=f["cfo"], snr=f["snr"],
                          mpdu=f["mpdu"]))
        pos += gap + len(x)
    parts.append(np.zeros(tail, complex))
    var.append(np.full(tail, var[-1][-1] if var else 0.0))
    return np.concatenate(parts), np.concatenate(var), truth


# numpy ports of the scipy front end of tools/guest/nbrxd.py (decode_segment). In double precision they equal
# scipy.signal.resample_poly / lfilter to 1e-15 (checked against scipy 1.18).

def resample_poly_np(x, up, down):
    """scipy.signal.resample_poly(x, up, down) with its defaults (Kaiser beta 5 window, half-length 10*max(up, down),
    zero padding): output i is the filter centred on upsampled sample i*down."""
    g = math.gcd(up, down)
    up, down = up // g, down // g
    if up == down == 1:
        return np.array(x, dtype=complex)
    max_rate = max(up, down)
    half_len = 10 * max_rate
    n = 2 * half_len + 1
    m = np.arange(n) - half_len
    fc = 1.0 / max_rate
    h = fc * np.sinc(fc * m) * np.kaiser(n, 5.0)
    h = h / h.sum() * up
    n_out = -(-len(x) * up // down)
    xu = np.zeros(len(x) * up, complex)
    xu[::up] = x
    return np.convolve(xu, h)[np.arange(n_out) * down + half_len]


def highpass_np(x, fs):
    """lfilter([1, -1], [1, -a], x), a = 1 - 2 pi 20 kHz / fs, from rest."""
    a = 1 - 2 * np.pi * 20e3 / fs
    y = np.empty(len(x), complex)
    px = py = 0j
    for i, v in enumerate(x):
        py = v - px + a * py
        px = v
        y[i] = py
    return y


def nbrxd_front_end(x, in_rate, width_rate):
    """nbrxd.py decode_segment with its dtype steps: complex64 in, mean removal, resample, high-pass, complex64."""
    x = np.asarray(x, np.complex64)
    y = resample_poly_np(x - x.mean(), *rx_ratio(width_rate, in_rate)).astype(np.complex64)  # a copy at 1/1
    return highpass_np(y, width_rate * 1e6).astype(np.complex64)


def rx_ratio(width_rate, in_rate):
    from fractions import Fraction
    fr = Fraction(width_rate / in_rate).limit_denominator(1000)
    return fr.numerator, fr.denominator


# ----------------------------------------------------------------------------------------------------------------------
# output files
# ----------------------------------------------------------------------------------------------------------------------

def path(name):
    return os.path.join(HERE, name)


def write_cf32(name, x, description):
    x = np.asarray(x, np.complex64)
    x.tofile(path(name))
    manifest.append(f"{name}  ({len(x)} samples, {os.path.getsize(path(name))} bytes)\n    {description}")
    return x


def write_truth(name, truth, header):
    with open(path(name), "w") as f:
        f.write(f"# {header}\n# frame <start sample> <rate Mbps> <PSDU bytes> <cfo cycles/sample> <snr dB> <PSDU hex>\n")
        for t in truth:
            snr = "none" if t["snr"] is None else f"{t['snr']:.2f}"
            f.write(f"frame {t['start']} {t['rate']} {t['length']} {t['cfo']:.9f} {snr} {t['mpdu'].hex()}\n")
    manifest.append(f"{name}\n    {len(truth)} transmitted frames: {header}")


def write_pyref(name, iq, mode, frontend=None, cfo_offset=0.0):
    """Decodes `iq` (already through the front end, if any) with rx80211.decode_all in `mode` and records the result.
    cfo_offset emulates the Rust DecodeOptions::cfo_offset: the input is multiplied by e^(-j 2 pi cfo_offset n)
    first, and the offset is added back to the reported CFOs."""
    kw = MODES[mode]
    y = np.asarray(iq, np.complex64)
    if cfo_offset:
        y = (y * np.exp(-2j * np.pi * cfo_offset * np.arange(len(y)))).astype(np.complex64)
    res = rx80211.decode_all(y, **kw)
    min_snr = "none" if kw["min_snr"] is None else f"{kw['min_snr']:g}"
    with open(path(name), "w") as f:
        f.write(f"# Python reference: rx80211.decode_all (rx80211.py sha1 {RX_SHA1})\n")
        f.write(f"options max_frames={kw['max_frames']} min_snr={min_snr} max_len={kw['max_len']} "
                f"stf_threshold={STF_THRESH} h_smooth={H_SMOOTH} ltf_search_derotate=1 cfo_offset={cfo_offset!r}\n")
        if frontend:
            f.write(f"frontend in_rate={frontend[0]!r} width_rate={frontend[1]!r}\n")
        f.write("# frame <start> <rate Mbps> <PSDU bytes> <fcs ok 0/1> <snr dB> <cfo cycles/sample> <PSDU hex>\n")
        for r in res:
            f.write(f"frame {r['start']} {r['rate']} {r['length']} {int(r['fcs_ok'])} {float(r['snr']):.6f} "
                    f"{float(r['cfo']) + cfo_offset:.9f} {r['mpdu'].hex()}\n")
    ok = sum(r["fcs_ok"] for r in res)
    manifest.append(f"{name}\n    rx80211.decode_all, {mode} settings{' + cfo_offset ' + repr(cfo_offset) if cfo_offset else ''}: "
                    f"{len(res)} frames, {ok} with a good FCS")
    return res


# ----------------------------------------------------------------------------------------------------------------------
# vectors
# ----------------------------------------------------------------------------------------------------------------------

def vec_rates_hi():
    """All 8 rates, 3 MPDU sizes each, 30 dB, CFO up to +-0.025 cycles/sample."""
    rng = np.random.default_rng(1001)
    frames = []
    for rate in RATES:
        for n in (40, 300, 1200):
            frames.append(dict(mpdu=make_mpdu(rng, rate, n, len(frames)), rate=rate, cfo=rng.uniform(-0.025, 0.025),
                               snr=30.0))
    clean, var, truth = assemble(rng, frames, lead=1500, tail=1000, gaps=(600, 1400))
    x = write_cf32("rates_hi.cf32", clean + noise(rng, len(clean), var),
                   "24 frames: rates 6..54 Mbps x payloads of 40/300/1200 bytes (+28 header/FCS), 30 dB, CFO uniform "
                   "in +-0.025 cycles/sample (beyond the +-0.009 of the original non-derotated LTF search), "
                   "1500 samples of noise before, 600-1400 between, 1000 after")
    write_truth("rates_hi.truth", truth, "rates_hi.cf32, 20 Msps-equivalent rate")
    for mode in MODES:
        write_pyref(f"rates_hi.{mode}.pyref", x, mode)


def vec_rates_lo():
    """8 frames per rate at an SNR near that rate's threshold."""
    rng = np.random.default_rng(1002)
    frames = []
    for rate in RATES:
        for _ in range(8):
            frames.append(dict(mpdu=make_mpdu(rng, rate, 100, len(frames)), rate=rate,
                               cfo=rng.uniform(-0.02, 0.02), snr=LOW_SNR[rate]))
    clean, var, truth = assemble(rng, frames, lead=1000, tail=1000, gaps=(500, 900))
    snrs = ", ".join(f"{r}M {s:g} dB" for r, s in LOW_SNR.items())
    x = write_cf32("rates_lo.cf32", clean + noise(rng, len(clean), var),
                   f"64 frames, 8 per rate, 128-byte MPDUs, each rate at an SNR near its threshold ({snrs}); "
                   "CFO uniform in +-0.02 cycles/sample")
    write_truth("rates_lo.truth", truth, "rates_lo.cf32, 20 Msps-equivalent rate")
    for mode in MODES:
        write_pyref(f"rates_lo.{mode}.pyref", x, mode)


def vec_wide30():
    """A 10 MHz channel captured at 30 Msps (ICAP packer 8): needs the 1/3 resampler and the high-pass."""
    rng = np.random.default_rng(1003)
    frames = [dict(mpdu=make_mpdu(rng, rate, 150, i), rate=rate, cfo=rng.uniform(-0.02, 0.02), snr=None)
              for i, rate in enumerate(RATES)]
    clean10, _, truth = assemble(rng, frames, lead=1200, tail=1200, gaps=(600, 1200))
    snr = 25.0  # in band, i.e. after the decimation to 10 Msps (white noise at 30 Msps loses 2/3 of its power there)
    x30 = resample_poly_np(clean10, 3, 1) + (0.4 - 0.3j) + noise(rng, 3 * len(clean10), 3 * 10 ** (-snr / 10))
    x = write_cf32("wide30.cf32", x30,
                   "30 Msps capture of a 10 MHz channel: 8 frames (one per rate, 178-byte MPDUs) built at 10 Msps, "
                   "upsampled x3 (resample_poly), plus a 0.4-0.3j DC offset and white noise for 25 dB in-band SNR; "
                   "CFO uniform in +-0.02 cycles/sample at 10 Msps. Decode with --in-rate 30 --width-rate 10")
    for t in truth:
        t["snr"] = snr
    write_truth("wide30.truth", truth, "wide30.cf32; start samples at the 10 Msps decode rate")
    y = nbrxd_front_end(x, 30.0, 10.0)
    write_pyref("wide30.nbrxd.pyref", y, "nbrxd", frontend=(30.0, 10.0))


def vec_cw():
    """False-alarm material: a strong CW tone, then a DC offset, then frames with a weak CW underneath."""
    rng = np.random.default_rng(1004)
    n_cw, n_dc = 30000, 10000
    cw = 3.0 * np.exp(1j * (2 * np.pi * 0.11 * np.arange(n_cw) + rng.uniform(0, 2 * np.pi)))
    dc = np.full(n_dc, 2.0 + 0.5j)
    frames = [dict(mpdu=make_mpdu(rng, rate, 200, i), rate=rate, cfo=rng.uniform(-0.01, 0.01), snr=25.0)
              for i, rate in enumerate((6, 12, 24))]
    clean, var, truth = assemble(rng, frames, lead=800, tail=800, gaps=(600, 900))
    weak = 0.1 * np.exp(1j * (2 * np.pi * 0.11 * np.arange(len(clean)) + rng.uniform(0, 2 * np.pi)))  # -20 dBc
    x = np.concatenate([cw, dc, clean + weak])
    x = x + noise(rng, len(x), 10 ** (-25 / 10))
    for t in truth:
        t["start"] += n_cw + n_dc
    x = write_cf32("cw.cf32", x,
                   f"{n_cw} samples of a CW tone at 0.11 cycles/sample (+9.5 dB re unit power), then {n_dc} samples "
                   "of a 2+0.5j DC offset (both autocorrelate like an STF: the flatness gate must reject them), then "
                   "3 frames (6/12/24 Mbps, 228-byte MPDUs, 25 dB) with the tone continuing at -20 dBc; noise "
                   "throughout at -25 dB")
    write_truth("cw.truth", truth, "cw.cf32, 20 Msps-equivalent rate; no frames before the third section")
    for mode in MODES:
        write_pyref(f"cw.{mode}.pyref", x, mode)


CFO_WIDE = 0.045  # beyond the STF estimator's +-1/32 = 0.03125 cycles/sample
CFO_WIDE_CORRECTION = 0.04  # the static correction (DecodeOptions::cfo_offset) the test applies


def vec_cfo_wide():
    """Frames beyond the STF estimator's range: decodable only with a static frequency correction."""
    rng = np.random.default_rng(1005)
    frames = [dict(mpdu=make_mpdu(rng, rate, 100, i), rate=rate, cfo=CFO_WIDE + rng.uniform(-0.002, 0.002),
                   snr=25.0) for i, rate in enumerate(RATES)]
    clean, var, truth = assemble(rng, frames, lead=1000, tail=800, gaps=(600, 900))
    x = write_cf32("cfo_wide.cf32", clean + noise(rng, len(clean), var),
                   f"8 frames (one per rate, 128-byte MPDUs, 25 dB) with CFO {CFO_WIDE} +- 0.002 cycles/sample, "
                   f"beyond the STF estimator's +-1/32; decodable with cfo_offset {CFO_WIDE_CORRECTION}")
    write_truth("cfo_wide.truth", truth, "cfo_wide.cf32, 20 Msps-equivalent rate")
    write_pyref("cfo_wide.nbrxd.pyref", x, "nbrxd")
    write_pyref("cfo_wide.nbrxd_offset.pyref", x, "nbrxd", cfo_offset=CFO_WIDE_CORRECTION)


RESAMPLE_RATIOS = [(1, 2), (1, 3), (1, 4), (1, 6), (1, 12), (3, 1), (2, 3)]


def vec_resample():
    """Reference outputs of resample_poly (numpy port, double precision) for the Rust resampler."""
    rng = np.random.default_rng(1006)
    n = 3000
    x = noise(rng, n, 1.0) + np.exp(2j * np.pi * 0.01 * np.arange(n))
    x = write_cf32("rs_in.cf32", x, f"{n} samples: unit complex white noise plus a 0.01 cycles/sample tone, the "
                                    "resampler test input")
    for up, down in RESAMPLE_RATIOS:
        write_cf32(f"rs_{up}_{down}.cf32", resample_poly_np(x.astype(complex), up, down),
                   f"scipy.signal.resample_poly(rs_in, {up}, {down}) (numpy port, computed in double precision)")


def vec_viterbi():
    """Soft-value sequences and the bits vit.c decodes from them (also checked against the numpy fallback)."""
    rng = np.random.default_rng(1007)
    has_c = bool(rx80211._cviterbi())
    cases = []

    def encoded(nbits):
        bits = np.concatenate([rng.integers(0, 2, nbits - 6), np.zeros(6, int)])
        return bits, 2.0 * np.array(ofdm80211.conv_encode(list(bits))) - 1.0

    _, c = encoded(200)
    cases.append(("clean", 200, c))
    for name, sigma in (("noisy_0.7", 0.7), ("noisy_1.0", 1.0)):
        _, c = encoded(500)
        cases.append((name, 500, c + sigma * rng.standard_normal(len(c))))
    # rate 3/4: puncture, add noise, re-insert erasures with rx80211.depuncture
    bits = np.concatenate([rng.integers(0, 2, 594), np.zeros(6, int)])
    sent = ofdm80211.puncture(ofdm80211.conv_encode(list(bits)), (3, 4))
    soft = 2.0 * np.array(sent) - 1.0 + 0.5 * rng.standard_normal(len(sent))
    cases.append(("punctured_3_4", 600, rx80211.depuncture(soft.astype(np.float32), (3, 4))))
    cases.append(("integers", 300, rng.integers(-3, 4, 600).astype(float)))  # many exact ties
    cases.append(("zeros", 100, np.zeros(200)))  # nothing but ties
    _, c = encoded(200)
    cases.append(("large", 200, 1e4 * (c + 0.8 * rng.standard_normal(len(c)))))
    llr_all, bits_all = [], []
    with open(path("vit_cases.txt"), "w") as f:
        f.write(f"# Viterbi reference cases: <name> <nbits> <offset into vit_llr.f32 (values)> <offset into vit_bits.u8>"
                f"\n# expected bits from rx80211.viterbi ({'vit.c' if has_c else 'numpy fallback only'}; the numpy "
                f"fallback gives the same bits)\n")
        lo = bo = 0
        for name, nbits, llr in cases:
            llr = np.asarray(llr, np.float32)  # the Rust decoder takes f32: store exactly what both sides decode
            assert len(llr) == 2 * nbits
            ref = rx80211.viterbi(llr.astype(np.float64), nbits)
            saved = rx80211._CVIT
            rx80211._CVIT = False  # force the numpy fallback
            try:
                fallback = rx80211.viterbi(llr.astype(np.float64), nbits)
            finally:
                rx80211._CVIT = saved
            assert np.array_equal(ref, fallback), name
            f.write(f"case {name} {nbits} {lo} {bo}\n")
            llr_all.append(llr)
            bits_all.append(np.asarray(ref, np.uint8))
            lo += len(llr)
            bo += nbits
    np.concatenate(llr_all).astype("<f4").tofile(path("vit_llr.f32"))
    np.concatenate(bits_all).tofile(path("vit_bits.u8"))
    manifest.append(f"vit_cases.txt, vit_llr.f32, vit_bits.u8\n    {len(cases)} Viterbi cases (clean, noisy, punctured "
                    f"3/4 with erasures, integer values with exact ties, all zeros, large values): float32 soft values "
                    f"and the bits {'vit.c' if has_c else 'the numpy fallback'} decodes from them")


def main():
    vec_rates_hi()
    vec_rates_lo()
    vec_wide30()
    vec_cw()
    vec_cfo_wide()
    vec_resample()
    vec_viterbi()
    with open(path("manifest.txt"), "w") as f:
        f.write("nbnode test vectors, written by gen.py (python3 -I tools/nbnode/testdata/gen.py; deterministic).\n"
                f"Python reference decodes: tools/sdr/rx80211.py sha1 {RX_SHA1}, STF_THRESH {STF_THRESH}, "
                f"H_SMOOTH {H_SMOOTH}, LTF search derotated by the STF CFO.\n"
                ".cf32 = numpy complex64 (interleaved little-endian float32 I/Q); unless noted, at the 20 Msps-"
                "equivalent rate\n(e.g. a 10 MHz channel at 10 Msps). Frames: unit power, noise variance 10^(-SNR/10) "
                "per sample.\n.truth: transmitted frames; .<mode>.pyref: rx80211.decode_all results, modes "
                "plain (max_frames 200, min_snr None, max_len 4095) and nbrxd (200, -1 dB, 1600).\n\n")
        f.write("\n".join(manifest) + "\n")
    print(f"wrote {len(manifest)} manifest entries to {HERE}")


if __name__ == "__main__":
    main()
