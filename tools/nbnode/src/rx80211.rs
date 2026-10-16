//! IEEE 802.11a/p OFDM receiver: a port of `tools/sdr/rx80211.py` (`decode_all`, `decode_at` and their helpers).
//!
//! # Sample rate
//! Everything works at the "20 Msps-equivalent" rate: 64 samples per OFDM FFT period, the rate at which an
//! 802.11a 20 MHz frame would be sampled at 20 Msps. Halving the channel width halves the sample rate and leaves the
//! samples identical. So a 10 MHz (802.11p) capture at 10 Msps, a 5 MHz one at 5 Msps or a 2.5 MHz one at
//! 2.5 Msps all decode unchanged. Frequencies (CFO) are therefore given in cycles per sample.
//!
//! # PPDU layout (samples)
//! ```text
//!  |<------------ STF 160 ------------>|<GI2 32>|<-- LTF1 64 -->|<-- LTF2 64 -->|<CP 16|SIGNAL 64>|<CP 16|DATA 64>|...
//!  start                              160      192 = t0         t0+64          t0+128            t0+208
//! ```
//! - STF (short training field): a 16-sample pattern repeated 10 times. Used for detection and the coarse CFO.
//! - LTF (long training field): the same 64-sample symbol twice. Used for timing, the fine CFO, the channel
//!   estimate and the SNR estimate. `t0`, the first sample of LTF1, is the reference point of [`Receiver`]'s
//!   `decode_at`.
//! - SIGNAL: one BPSK rate-1/2 symbol carrying RATE and LENGTH.
//! - DATA: SERVICE (16 bits) + PSDU + tail (6) + pad, scrambled, coded, interleaved and mapped as RATE says.
//!
//! # Processing (decode_all)
//! 1. Detection: the STF's lag-16 autocorrelation, normalised by the signal power, exceeds a threshold.
//! 2. Timing: cross-correlation with the known LTF symbol, after removing the CFO estimated from the STF.
//! 3. `decode_at` (for the correlation peak, and 64 samples earlier, since either LTF symbol can give the peak):
//!    CFO (coarse from the STF, fine from the LTF pair), channel and SNR from the LTF, false-alarm gates, SIGNAL,
//!    then the DATA symbols. Each data symbol is equalised, its pilots correct the common phase, and it is soft
//!    demapped with channel-state weights, deinterleaved, depunctured, Viterbi decoded and descrambled. The FCS is
//!    checked last.
//!
//! # Faithfulness to the Python
//! The decisions and thresholds are the Python's (as of the version with STF_THRESH, H_SMOOTH and the derotated LTF
//! search). The arithmetic differs in precision. The Python works in complex128 after the input cast to complex64;
//! here the per-sample and per-symbol DSP runs in f32. The f64 parts are the detector's running sums, the phase
//! rotators and the Viterbi path metrics. The two agree on every frame of the test vectors; a different decision
//! could only come from a near-tie at the 1e-6 level. One Python quirk is kept on purpose. Its mask of "occupied"
//! LTF bins, `abs(_ltf_freq()) > 0`, is true on all 64 bins: the FFT leaves ~1e-16 of round-off on the 12 unused ones.
//! So the SNR estimate and the flatness gate average over all 64 bins (see [`SNR_AND_FLATNESS_BINS`]); the min_snr
//! thresholds in use (nbrxd: -1 dB) were tuned against that estimate.

use crate::complex::{dot, Complex32, Complex64};
use crate::fft::Fft;
use crate::viterbi::{depuncture, viterbi, CodeRate};
use std::f64::consts::PI;

// ------------------------------------------------------------------------------------------------------------------
// OFDM numerology, in samples at the 20 Msps-equivalent rate
// ------------------------------------------------------------------------------------------------------------------

/// FFT size = number of subcarriers (52 of them used: 48 data + 4 pilots).
const FFT_LEN: usize = 64;
/// Cyclic prefix of the SIGNAL and DATA symbols.
const CP_LEN: usize = 16;
/// One SIGNAL or DATA symbol: cyclic prefix + FFT period.
const SYMBOL_LEN: usize = CP_LEN + FFT_LEN;
/// Repetition period of the STF.
const STF_PERIOD: usize = 16;
/// Length of the long guard interval (GI2) between the STF and LTF1.
const GI2_LEN: usize = 32;
/// STF samples used for the coarse CFO, ending at the STF's end (decode_at: `iq[t0 - 32 - 144:t0 - 32]`). That is the
/// last 9 of the 10 short symbols: the first one may still be distorted while the receiver gain settles.
const STF_CFO_LEN: usize = 144;
/// Start of the SIGNAL symbol (its cyclic prefix) relative to t0: right after LTF1 and LTF2.
const SIGNAL_OFFSET: usize = 2 * FFT_LEN;
/// Start of the first DATA symbol relative to t0.
const DATA_OFFSET: usize = SIGNAL_OFFSET + SYMBOL_LEN;
/// Data subcarriers per OFDM symbol.
const DATA_CARRIERS: usize = 48;
/// SERVICE field at the start of the DATA bits (scrambler initialisation + reserved), and the encoder tail after the
/// PSDU.
const SERVICE_BITS: usize = 16;
const TAIL_BITS: usize = 6;

// ------------------------------------------------------------------------------------------------------------------
// Detection and search (decode_all)
// ------------------------------------------------------------------------------------------------------------------

/// The detector sums 48 lag-16 products: 3 STF periods. With the lag that spans 64 samples, one FFT period.
const DETECT_WINDOW: usize = 48;
/// Keeps the detector's normalisation finite on all-zero input (`m = |c| / (p + 1e-9)`).
const DETECT_EPS: f64 = 1e-9;
/// Block length of the detector's prefix sums (see `stf_candidates`); any value works, it only bounds round-off.
const DETECT_BLOCK: usize = 4096;
/// LTF search window, relative to the STF candidate i: correlation lags i+100 ..= i+260. LTF1 starts 192 samples
/// after the true STF start, so it is inside when the candidate is between 92 samples early and 68 late.
const LTF_SEARCH_FROM: usize = 100;
const LTF_SEARCH_TO: usize = 260;
/// After a decoded frame, the scan resumes 720 samples after its candidate: the preamble and SIGNAL (400) plus four
/// data symbols (`i += 400 + 80 * 4`). The rest of the frame does not look like an STF, so no more is skipped.
const SKIP_AFTER_FRAME: usize = 400 + 4 * SYMBOL_LEN;
/// After a candidate that did not decode, the scan moves on by one STF length (`i += 160`).
const SKIP_AFTER_MISS: usize = 160;

// ------------------------------------------------------------------------------------------------------------------
// Gates (decode_at)
// ------------------------------------------------------------------------------------------------------------------

/// Flatness (CW-spur) gate: reject when one FFT bin holds more than this fraction of the LTF energy. A real LTF
/// spreads its energy evenly over 52 subcarriers (~2% each). A CW tone is a perfect "STF" at any lag and repeats
/// exactly across the two LTF symbols (so its SNR estimate is high), but it fills one or two bins.
const FLATNESS_MAX_FRACTION: f64 = 0.25;
/// Floor of the SNR estimate's numerator and denominator, so that the logarithm stays finite.
const SNR_FLOOR: f64 = 1e-12;
/// The bins over which the SNR estimate and the flatness gate run: all 64. This mirrors the Python, whose mask
/// `abs(_ltf_freq()) > 0` is meant to select the 52 used subcarriers but is true everywhere (FFT round-off of ~1e-16
/// on the 12 unused bins). Noise in the unused bins therefore lowers the SNR estimate by about 0.9 dB
/// (10 log10(64/52)), and a spur on DC or a guard bin is caught by the flatness gate.
pub const SNR_AND_FLATNESS_BINS: usize = FFT_LEN;
/// Largest PSDU the 12-bit SIGNAL LENGTH field can describe (Python `max_len` default).
pub const MAX_PSDU_LEN: usize = 4095;

// ------------------------------------------------------------------------------------------------------------------
// 802.11 tables (tools/sdr/ofdm80211.py)
// ------------------------------------------------------------------------------------------------------------------

/// Pilot subcarriers (`PILOT_SC`).
const PILOT_SUBCARRIERS: [i32; 4] = [-21, -7, 7, 21];
/// Pilot values before the polarity is applied (`PILOT_BASE`).
const PILOT_BASE: [f32; 4] = [1.0, 1.0, 1.0, -1.0];
/// Data subcarriers in transmission order (`DATA_SC`): -26..=26 without DC and the pilots.
const DATA_SUBCARRIERS: [i32; DATA_CARRIERS] = data_subcarriers();

const fn data_subcarriers() -> [i32; DATA_CARRIERS] {
    let mut out = [0i32; DATA_CARRIERS];
    let mut n = 0;
    let mut k = -26;
    while k <= 26 {
        if k != 0 && k != 7 && k != -7 && k != 21 && k != -21 {
            out[n] = k;
            n += 1;
        }
        k += 1;
    }
    out
}

/// Pilot polarity p_0..p_126 (`POLARITY`; IEEE 802.11-2020 17.3.5.10): the pilots of OFDM symbol n are multiplied by
/// p_(n mod 127), where the SIGNAL symbol is n = 0 and the first DATA symbol n = 1. The sequence is the scrambler's
/// output for the all-ones seed with 0 -> +1, 1 -> -1 (checked in the tests).
const PILOT_POLARITY: [i8; 127] = [
    1, 1, 1, 1, -1, -1, -1, 1, -1, -1, -1, -1, 1, 1, -1, 1, -1, -1, 1, 1, -1, 1, 1, -1, 1, 1, 1, 1, //
    1, 1, -1, 1, 1, 1, -1, 1, 1, -1, -1, 1, 1, 1, -1, 1, -1, -1, -1, 1, -1, 1, -1, -1, 1, -1, -1, //
    1, 1, 1, 1, 1, -1, -1, 1, 1, -1, -1, 1, -1, 1, -1, 1, 1, -1, -1, -1, 1, 1, -1, -1, -1, -1, 1, //
    -1, -1, 1, -1, 1, 1, 1, 1, -1, 1, -1, 1, -1, 1, -1, -1, -1, -1, -1, 1, -1, 1, 1, -1, 1, -1, 1, //
    1, 1, -1, -1, 1, -1, -1, -1, 1, 1, 1, -1, -1, -1, -1, -1, -1, -1,
];

/// LTF subcarrier values L_-26 .. L_26 (`l_seq` in `ofdm80211.preamble`; IEEE 802.11-2020 Equation 17-8), 0 at DC.
const LTF_SEQUENCE: [i8; 53] = [
    1, 1, -1, -1, 1, 1, -1, 1, -1, 1, 1, 1, 1, 1, 1, -1, -1, 1, 1, -1, 1, -1, 1, 1, 1, 1, 0, //
    1, -1, -1, 1, 1, -1, 1, -1, 1, -1, -1, -1, -1, -1, 1, 1, -1, -1, 1, -1, 1, -1, 1, 1, 1, 1,
];

/// Period of the scrambler sequence (x^7 + x^4 + 1 is primitive: 2^7 - 1).
const SCRAMBLER_PERIOD: usize = 127;

/// One row of the rate table (`RATES`).
struct RateParams {
    /// Data rate in Mbps at 20 MHz timing (halve it for 10 MHz channels, quarter it for 5 MHz).
    mbps: u32,
    /// RATE field of SIGNAL, bits R1..R4 with R1 as the most significant bit.
    signal_bits: u8,
    /// Coded bits per subcarrier: 1 BPSK, 2 QPSK, 4 16-QAM, 6 64-QAM.
    bits_per_carrier: usize,
    code: CodeRate,
}

const RATE_TABLE: [RateParams; 8] = [
    RateParams { mbps: 6, signal_bits: 0b1101, bits_per_carrier: 1, code: CodeRate::Half },
    RateParams { mbps: 9, signal_bits: 0b1111, bits_per_carrier: 1, code: CodeRate::ThreeQuarters },
    RateParams { mbps: 12, signal_bits: 0b0101, bits_per_carrier: 2, code: CodeRate::Half },
    RateParams { mbps: 18, signal_bits: 0b0111, bits_per_carrier: 2, code: CodeRate::ThreeQuarters },
    RateParams { mbps: 24, signal_bits: 0b1001, bits_per_carrier: 4, code: CodeRate::Half },
    RateParams { mbps: 36, signal_bits: 0b1011, bits_per_carrier: 4, code: CodeRate::ThreeQuarters },
    RateParams { mbps: 48, signal_bits: 0b0001, bits_per_carrier: 6, code: CodeRate::TwoThirds },
    RateParams { mbps: 54, signal_bits: 0b0011, bits_per_carrier: 6, code: CodeRate::ThreeQuarters },
];

/// Gray-coded amplitude levels of one QAM axis (I or Q), indexed by that axis' bits read first-bit-most-significant
/// (`ofdm80211.modulate`). 16-QAM carries 2 bits per axis (00 -> -3, 01 -> -1, 10 -> +3, 11 -> +1), 64-QAM 3 bits.
const QAM16_LEVELS: [f32; 4] = [-3.0, -1.0, 3.0, 1.0];
const QAM64_LEVELS: [f32; 8] = [-7.0, -5.0, -1.0, -3.0, 7.0, 5.0, 1.0, 3.0];

/// FFT bin of subcarrier k (negative frequencies wrap to the top half, as `k % 64` in Python).
fn bin(k: i32) -> usize {
    k.rem_euclid(FFT_LEN as i32) as usize
}

// ------------------------------------------------------------------------------------------------------------------
// Public API
// ------------------------------------------------------------------------------------------------------------------

/// A decoded PPDU (the Python's result dict).
#[derive(Clone, Debug, PartialEq)]
pub struct Frame {
    /// Index of the STF candidate that led to the frame (about the first STF sample; Python `start`).
    pub start: usize,
    /// Data rate in Mbps at 20 MHz timing: 6, 9, 12, 18, 24, 36, 48 or 54 (on air: rate x width / 20 MHz).
    pub rate: u32,
    /// PSDU length in bytes from the SIGNAL field (the MPDU including its 4-byte FCS).
    pub length: usize,
    /// The FCS (CRC-32 over all but the last 4 bytes) matched.
    pub fcs_ok: bool,
    /// The decoded PSDU (`length` bytes), FCS included, valid or not.
    pub mpdu: Vec<u8>,
    /// Carrier frequency offset in cycles per sample: [`DecodeOptions::cfo_offset`] plus the frame's own estimate. In Hz:
    /// cfo x sample rate (e.g. x 10e6 for a 10 MHz channel at 10 Msps).
    pub cfo: f32,
    /// SNR estimated from the two LTF symbols, in dB (see [`SNR_AND_FLATNESS_BINS`]).
    pub snr_db: f32,
}

/// Receiver settings. `Default` gives the Python's `decode_all` defaults; [`DecodeOptions::nbrxd`] gives the
/// settings of the guest daemon tools/guest/nbrxd.py.
#[derive(Clone, Debug, PartialEq)]
pub struct DecodeOptions {
    /// Stop after this many frames (Python `max_frames`, default 50).
    pub max_frames: usize,
    /// Drop a preamble candidate whose LTF SNR estimate (dB) is below this, before decoding SIGNAL (Python
    /// `min_snr`; default None = no SNR gate). Besides noise, it rejects the misaligned decode that the second LTF
    /// symbol can produce at high SNR (`decode_at(k)` on LTF2 sees SIGNAL as its "second LTF"). Without the gate,
    /// such a decode sometimes passes the SIGNAL checks and turns a good frame into a bad-FCS one. nbrxd uses -1.0;
    /// false alarms sit near -9 dB.
    pub min_snr: Option<f32>,
    /// Largest PSDU length worth decoding (Python `max_len`, default 4095 = no limit). A false SIGNAL field claims
    /// up to 4095 bytes; nbrxd uses 1600 (a 1500-byte MTU plus headers).
    pub max_len: usize,
    /// STF detection threshold on the normalised lag-16 autocorrelation m (Python `STF_THRESH`, default 0.6).
    /// For an STF in white noise, m is about SNR / (SNR + 1), so the threshold sets the detection floor: 0.85
    /// needs ~7.5 dB, 0.6 about 1.8 dB. Measured on card frames plus AWGN, lowering it from 0.85 to 0.6 improved
    /// the BPSK-1/2 10% PER point from 7 dB to 4 dB; 0.4 gave no further gain. A lower threshold costs only more
    /// candidates, whose false alarms the SIGNAL parity, `min_snr` and the FCS reject. (An f64 so that the default
    /// equals the Python's 0.6 exactly.)
    pub stf_threshold: f64,
    /// Channel-estimate smoothing (Python `H_SMOOTH`, default 1; 0 = off). Each used subcarrier's LTF estimate is
    /// replaced by the mean over the subcarriers within +-h_smooth of it, among -26..=26 without DC (the neighbour
    /// set is cut at the band edges and at DC, not mirrored). The two LTF symbols alone leave the estimate about
    /// as noisy as the data at low SNR. A narrow channel's response changes little across a few subcarriers (78 kHz
    /// apart at 5 MHz width), so +-1 gains about 1 dB at BPSK/QPSK. Larger values would blur frequency-selective
    /// channels.
    pub h_smooth: usize,
    /// Remove the STF's coarse CFO from the LTF search window before correlating (Python: always on). Without it,
    /// the 64-sample LTF correlation collapses like a sinc of the CFO (first null at 1/64 cycle/sample) and picks
    /// a wrong lag beyond about +-0.009 cycles/sample: +-45 kHz at 5 Msps, +-22 kHz at 2.5 Msps. Two +-20 ppm
    /// crystals can differ by up to +-96 kHz at 2.4 GHz. With it, the acquisition range is that of the STF
    /// estimator, +-1/32 cycle/sample. Default true; false reproduces the original rx80211.py.
    pub ltf_search_derotate: bool,
    /// Static frequency correction in cycles per sample (default 0.0). The whole input is multiplied by
    /// e^(-j 2 pi cfo_offset n) before detection, so a link whose offset is known to lie beyond the STF
    /// estimator's +-1/32 cycle/sample (e.g. 2.5 MHz channels at 5.9 GHz) can be centred. Reported CFOs include it
    /// ([`Frame::cfo`] is the total offset). Not in the Python, where it would be `iq * exp(-2j pi cfo_offset n)`
    /// before `decode_all`.
    pub cfo_offset: f64,
}

impl Default for DecodeOptions {
    fn default() -> Self {
        DecodeOptions {
            max_frames: 50,
            min_snr: None,
            max_len: MAX_PSDU_LEN,
            stf_threshold: 0.6,
            h_smooth: 1,
            ltf_search_derotate: true,
            cfo_offset: 0.0,
        }
    }
}

impl DecodeOptions {
    /// The settings of tools/guest/nbrxd.py `decode_segment`: `decode_all(y, 50, MIN_SNR = -1.0, MAX_LEN = 1600)`.
    pub fn nbrxd() -> Self {
        DecodeOptions { min_snr: Some(-1.0), max_len: 1600, ..DecodeOptions::default() }
    }
}

/// Decodes every frame found in `iq` (Python `decode_all(iq, max_frames, min_snr, max_len)`), with the defaults
/// of [`DecodeOptions`] for the other settings.
pub fn decode_all(iq: &[Complex32], max_frames: usize, min_snr: Option<f32>, max_len: usize) -> Vec<Frame> {
    decode_all_with(iq, &DecodeOptions { max_frames, min_snr, max_len, ..DecodeOptions::default() })
}

/// Decodes every frame found in `iq` with the given settings.
pub fn decode_all_with(iq: &[Complex32], opts: &DecodeOptions) -> Vec<Frame> {
    Receiver::new().decode_all(iq, opts)
}

// ------------------------------------------------------------------------------------------------------------------
// Receiver
// ------------------------------------------------------------------------------------------------------------------

/// The receiver's constant tables, built once. Reuse one `Receiver` to decode many blocks without rebuilding them;
/// it has no state between calls.
#[derive(Clone, Debug)]
pub struct Receiver {
    fft: Fft,
    /// The LTF symbol in the time domain (`preamble()[192:256]`), real and imaginary parts, for the timing search.
    ltf_time_re: [f32; FFT_LEN],
    ltf_time_im: [f32; FFT_LEN],
    /// The LTF per FFT bin: +1 / -1 on the 52 used subcarriers, 0 on DC and the 11 guard bins.
    ltf_freq: [f32; FFT_LEN],
    /// Deinterleaver tables for 1, 2, 4 and 6 coded bits per subcarrier (see `deinterleaver`).
    deinterleavers: [Vec<usize>; 4],
}

/// Result of `decode_at` (the Python dict without `start`).
struct Decoded {
    rate: u32,
    length: usize,
    fcs_ok: bool,
    mpdu: Vec<u8>,
    cfo: f64,
    snr_db: f64,
}

impl Default for Receiver {
    fn default() -> Self {
        Receiver::new()
    }
}

impl Receiver {
    pub fn new() -> Receiver {
        // ltf_freq: the sequence placed on its FFT bins
        let mut ltf_freq = [0.0f32; FFT_LEN];
        for (i, &v) in LTF_SEQUENCE.iter().enumerate() {
            ltf_freq[bin(i as i32 - 26)] = v as f32;
        }
        // ltf_time: the transmitter's IFFT, x[n] = (1/64) sum_k X[k] e^(2 pi j k n / 64) scaled by 64 / sqrt(52)
        // (ofdm80211.preamble), evaluated directly in f64
        let mut ltf_time_re = [0.0f32; FFT_LEN];
        let mut ltf_time_im = [0.0f32; FFT_LEN];
        for n in 0..FFT_LEN {
            let mut acc = Complex64::ZERO;
            for (b, &v) in ltf_freq.iter().enumerate() {
                if v != 0.0 {
                    let turns = ((b * n) % FFT_LEN) as f64 / FFT_LEN as f64;
                    acc += Complex64::cis(2.0 * PI * turns) * v as f64;
                }
            }
            let x = acc * (1.0 / 52f64.sqrt());
            ltf_time_re[n] = x.re as f32;
            ltf_time_im[n] = x.im as f32;
        }
        Receiver {
            fft: Fft::new(FFT_LEN),
            ltf_time_re,
            ltf_time_im,
            ltf_freq,
            deinterleavers: [deinterleaver(1), deinterleaver(2), deinterleaver(4), deinterleaver(6)],
        }
    }

    /// Decodes every frame found in `iq` (port of `decode_all`).
    pub fn decode_all(&self, iq: &[Complex32], opts: &DecodeOptions) -> Vec<Frame> {
        // static frequency correction (not in the Python; a no-op by default)
        let shifted: Vec<Complex32>;
        let iq = if opts.cfo_offset != 0.0 {
            let mut buf = vec![Complex32::ZERO; iq.len()];
            derotate(iq, 0, opts.cfo_offset, &mut buf);
            shifted = buf;
            &shifted[..]
        } else {
            iq
        };
        let mut frames = Vec::new();
        if iq.len() < FFT_LEN {
            return frames;
        }
        let candidates = stf_candidates(iq, opts.stf_threshold);
        let mut i = 0;
        while frames.len() < opts.max_frames {
            // jump to the next STF candidate at or after i (Python: cand[searchsorted(cand, i)])
            let Some(next) = (i..candidates.len()).find(|&j| candidates[j]) else {
                break;
            };
            i = next;
            // find the LTF by cross-correlation in a window after the STF start
            let w0 = i + LTF_SEARCH_FROM;
            let w1 = (iq.len() - FFT_LEN).min(i + LTF_SEARCH_TO);
            if w1 <= w0 {
                break; // too close to the end of the block for a preamble
            }
            let k = w0 + self.ltf_peak(iq, i, w0, w1, opts.ltf_search_derotate);
            // the strongest peak can be either LTF symbol: try both alignments (k >= 100, so k - 64 cannot wrap)
            let decoded = self.decode_at(iq, k, opts).or_else(|| self.decode_at(iq, k - FFT_LEN, opts));
            match decoded {
                Some(d) => {
                    frames.push(Frame {
                        start: i,
                        rate: d.rate,
                        length: d.length,
                        fcs_ok: d.fcs_ok,
                        mpdu: d.mpdu,
                        cfo: (d.cfo + opts.cfo_offset) as f32,
                        snr_db: d.snr_db as f32,
                    });
                    i += SKIP_AFTER_FRAME;
                }
                None => i += SKIP_AFTER_MISS,
            }
        }
        frames
    }

    /// Offset (from w0) of the strongest cross-correlation between `iq[w0 .. w1 + 64]` and the LTF symbol: Python
    /// `argmax(abs(correlate(win, ltf_t, "valid")))`, the first maximum on ties. With `derotate_window`, the window is
    /// first multiplied by e^(-j 2 pi f0 n), where f0 is the CFO measured on the STF at the candidate,
    /// `angle(vdot(iq[i:i+128], iq[i+16:i+144])) / (2 pi 16)`.
    fn ltf_peak(&self, iq: &[Complex32], i: usize, w0: usize, w1: usize, derotate_window: bool) -> usize {
        let f0 = if derotate_window {
            lag_phase(&iq[i..i + STF_CFO_LEN], STF_PERIOD) / (2.0 * PI * STF_PERIOD as f64)
        } else {
            0.0
        };
        let mut win = vec![Complex32::ZERO; w1 + FFT_LEN - w0];
        derotate(&iq[w0..w1 + FFT_LEN], 0, f0, &mut win);
        // real/imaginary planes, so that each lag is four plain dot products:
        //   sum_n x[k+n] conj(l[n]) = sum (xr lr + xi li)  +  j sum (xi lr - xr li)
        let xr: Vec<f32> = win.iter().map(|v| v.re).collect();
        let xi: Vec<f32> = win.iter().map(|v| v.im).collect();
        let (lr, li) = (&self.ltf_time_re[..], &self.ltf_time_im[..]);
        let mut best_lag = 0;
        let mut best = -1.0f32;
        for lag in 0..=(w1 - w0) {
            let (r, q) = (&xr[lag..lag + FFT_LEN], &xi[lag..lag + FFT_LEN]);
            let re = dot(r, lr) + dot(q, li);
            let im = dot(q, lr) - dot(r, li);
            let power = re * re + im * im; // |corr|^2: same argmax as |corr|
            if power > best {
                best = power;
                best_lag = lag;
            }
        }
        best_lag
    }

    /// Port of `decode_at`: decodes the PPDU whose first LTF symbol starts at `t0`, or returns None.
    fn decode_at(&self, iq: &[Complex32], t0: usize, opts: &DecodeOptions) -> Option<Decoded> {
        // the STF before t0 and the LTF + SIGNAL after it must be inside the block
        if t0 < GI2_LEN + STF_CFO_LEN || t0 + DATA_OFFSET > iq.len() {
            return None;
        }

        // --- 1. Carrier frequency offset --------------------------------------------------------------------------
        // A CFO of f cycles/sample turns sample n into sample n * e^(j 2 pi f n). So in a signal that repeats every
        // L samples, x[n+L] = x[n] e^(j 2 pi f L), and f = angle(sum conj(x[n]) x[n+L]) / (2 pi L). It is unambiguous
        // for |f| < 1/(2L): the STF (L = 16) covers +-1/32 cycle/sample but is noisy; the two LTF symbols (L = 64)
        // are precise but cover only +-1/128. Hence coarse from the STF, then fine from the LTF after removing the
        // coarse estimate.
        let stf = &iq[t0 - GI2_LEN - STF_CFO_LEN..t0 - GI2_LEN];
        let coarse = lag_phase(stf, STF_PERIOD) / (2.0 * PI * STF_PERIOD as f64);
        let mut ltf_pair = [Complex32::ZERO; 2 * FFT_LEN];
        derotate(&iq[t0..t0 + 2 * FFT_LEN], 0, coarse, &mut ltf_pair);
        let cfo = coarse + lag_phase(&ltf_pair, FFT_LEN) / (2.0 * PI * FFT_LEN as f64);
        // both LTF symbols and SIGNAL with the full estimate removed (phase reference: t0)
        let mut x = [Complex32::ZERO; DATA_OFFSET];
        derotate(&iq[t0..t0 + DATA_OFFSET], 0, cfo, &mut x);

        // --- 2. LTF spectra and SNR -----------------------------------------------------------------------------
        let mut f1 = [Complex32::ZERO; FFT_LEN];
        let mut f2 = [Complex32::ZERO; FFT_LEN];
        f1.copy_from_slice(&x[..FFT_LEN]);
        f2.copy_from_slice(&x[FFT_LEN..2 * FFT_LEN]);
        self.fft.forward(&mut f1);
        self.fft.forward(&mut f2);
        // The two symbols carry the same signal S and independent noise of power N per bin:
        //   |(f1 + f2)/2|^2 = S + N/2,   |(f1 - f2)/2|^2 = N/2   ->   SNR = (ps - pd) / (2 pd)
        let mut lt = [Complex32::ZERO; FFT_LEN]; // the averaged LTF spectrum
        let (mut ps, mut pd) = (0.0f64, 0.0f64);
        for b in 0..SNR_AND_FLATNESS_BINS {
            lt[b] = (f1[b] + f2[b]) * 0.5;
            ps += lt[b].norm_sqr() as f64;
            pd += (f1[b] - f2[b]).norm_sqr() as f64 / 4.0;
        }
        ps /= SNR_AND_FLATNESS_BINS as f64;
        pd /= SNR_AND_FLATNESS_BINS as f64;
        let snr_db = 10.0 * ((ps - pd).max(SNR_FLOOR) / (2.0 * pd).max(SNR_FLOOR)).log10();

        // --- 3. False-alarm gates -------------------------------------------------------------------------------
        if let Some(min_snr) = opts.min_snr {
            if snr_db < min_snr as f64 {
                return None;
            }
        }
        let mut energy_max = 0.0f64;
        let mut energy_sum = 0.0f64;
        for v in lt.iter().take(SNR_AND_FLATNESS_BINS) {
            let e = v.norm_sqr() as f64;
            energy_max = energy_max.max(e);
            energy_sum += e;
        }
        if energy_max > FLATNESS_MAX_FRACTION * energy_sum {
            return None; // a CW spur, not an LTF
        }

        // --- 4. Channel estimate --------------------------------------------------------------------------------
        // h = received / transmitted LTF on the 52 used subcarriers. The transmitter's IFFT gain 64/sqrt(52) is
        // part of h, so equalised symbols come out at unit scale. That matters for the QAM level decisions; BPSK and
        // QPSK use only the sign. The unused bins keep h = 1 (never read).
        let mut h = [Complex32::ONE; FFT_LEN];
        for b in 0..FFT_LEN {
            if self.ltf_freq[b] != 0.0 {
                h[b] = lt[b] * self.ltf_freq[b]; // dividing by +-1 = multiplying by it
            }
        }
        if opts.h_smooth > 0 {
            h = smooth_channel(&h, opts.h_smooth);
        }
        // Channel-state weights for the soft bits: |h|^2 relative to its mean over the data subcarriers.
        // Zero-forcing alone would let the noise of subcarriers attenuated by the receive filters' edges dominate
        // the Viterbi metric.
        let mut weights = [0.0f32; DATA_CARRIERS];
        let mut weight_sum = 0.0f64;
        for (w, &k) in weights.iter_mut().zip(DATA_SUBCARRIERS.iter()) {
            *w = h[bin(k)].norm_sqr();
            weight_sum += *w as f64;
        }
        let weight_mean = (weight_sum / DATA_CARRIERS as f64) as f32;
        for w in weights.iter_mut() {
            *w /= weight_mean;
        }
        // zero-forcing equaliser: 1/h per bin
        let mut equaliser = [Complex32::ONE; FFT_LEN];
        for b in 0..FFT_LEN {
            equaliser[b] = Complex32::ONE / h[b];
        }

        // --- 5. SIGNAL: one BPSK symbol, rate 1/2, 24 bits, not scrambled ---------------------------------------
        let mut window = [Complex32::ZERO; FFT_LEN];
        window.copy_from_slice(&x[SIGNAL_OFFSET + CP_LEN..DATA_OFFSET]);
        let mut carriers = [Complex32::ZERO; DATA_CARRIERS];
        self.demodulate_symbol(&mut window, &equaliser, 0, &mut carriers);
        let mut soft = [0.0f32; DATA_CARRIERS];
        for i in 0..DATA_CARRIERS {
            soft[i] = carriers[i].re * weights[i];
        }
        let coded: Vec<f32> = self.deinterleavers[0].iter().map(|&p| soft[p]).collect();
        let sig = viterbi(&coded, 24);
        // RATE = bits 0..3 (R1 first, as the MSB), bit 4 reserved, LENGTH = bits 5..16 (LSB first), bit 17 = even
        // parity over bits 0..17, bits 18..23 = tail
        let rate_field = sig[..4].iter().fold(0u8, |acc, &b| (acc << 1) | b);
        let length = sig[5..17].iter().rev().fold(0usize, |acc, &b| (acc << 1) | b as usize);
        let parity_ok = sig[..18].iter().map(|&b| b as u32).sum::<u32>() % 2 == 0;
        let rate = RATE_TABLE.iter().find(|r| r.signal_bits == rate_field)?;
        if !parity_ok || length == 0 || length > opts.max_len {
            return None;
        }

        // --- 6. DATA symbols --------------------------------------------------------------------------------------
        let nbpsc = rate.bits_per_carrier;
        let ncbps = DATA_CARRIERS * nbpsc; // coded bits per symbol
        let ndbps = ncbps * rate.code.numerator() / rate.code.denominator(); // data bits per symbol
        let nsym = (SERVICE_BITS + 8 * length + TAIL_BITS).div_ceil(ndbps);
        if t0 + DATA_OFFSET + SYMBOL_LEN * nsym > iq.len() {
            return None; // the frame runs past the end of the block
        }
        let deinterleaver = &self.deinterleavers[match nbpsc {
            1 => 0,
            2 => 1,
            4 => 2,
            _ => 3,
        }];
        let mut received = Vec::with_capacity(nsym * ncbps); // soft values, deinterleaved, still punctured
        let mut symbol_soft = vec![0.0f32; ncbps];
        for s in 0..nsym {
            // FFT window = the symbol after its cyclic prefix; CFO removed with the phase continuing from t0
            let start = DATA_OFFSET + s * SYMBOL_LEN + CP_LEN;
            derotate(&iq[t0 + start..t0 + start + FFT_LEN], start, cfo, &mut window);
            self.demodulate_symbol(&mut window, &equaliser, s + 1, &mut carriers);
            for i in 0..DATA_CARRIERS {
                let out = &mut symbol_soft[i * nbpsc..(i + 1) * nbpsc];
                demap(carriers[i], nbpsc, out);
                for v in out.iter_mut() {
                    *v *= weights[i];
                }
            }
            received.extend(deinterleaver.iter().map(|&p| symbol_soft[p]));
        }
        let coded = depuncture(&received, rate.code);
        let mut bits = viterbi(&coded, nsym * ndbps);
        descramble(&mut bits);

        // --- 7. PSDU bytes (LSB first) and FCS ------------------------------------------------------------------
        let mpdu: Vec<u8> = bits[SERVICE_BITS..SERVICE_BITS + 8 * length]
            .as_chunks::<8>()
            .0
            .iter()
            .map(|byte| byte.iter().enumerate().fold(0u8, |acc, (i, &b)| acc | (b << i)))
            .collect();
        let fcs_ok = mpdu.len() >= 4 && {
            let (body, fcs) = mpdu.split_at(mpdu.len() - 4);
            crc32(body) == u32::from_le_bytes([fcs[0], fcs[1], fcs[2], fcs[3]])
        };
        Some(Decoded { rate: rate.mbps, length, fcs_ok, mpdu, cfo, snr_db })
    }

    /// Port of `ofdm_demod` for one symbol. `window` holds the 64 CFO-corrected samples after the cyclic prefix and
    /// is transformed in place. `out` receives the 48 data subcarriers, equalised and corrected for the common
    /// phase error. `symbol_index` selects the pilot polarity: 0 = SIGNAL, 1.. = DATA.
    fn demodulate_symbol(
        &self,
        window: &mut [Complex32; FFT_LEN],
        equaliser: &[Complex32; FFT_LEN],
        symbol_index: usize,
        out: &mut [Complex32; DATA_CARRIERS],
    ) {
        self.fft.forward(window);
        // Common phase error: what is left of the CFO, plus phase noise, turns every subcarrier of a symbol by the
        // same angle. The four pilots (known values) measure it: their equalised sum, multiplied by the expected
        // pilot values, points at that angle.
        let polarity = PILOT_POLARITY[symbol_index % PILOT_POLARITY.len()] as f32;
        let mut pilots = Complex32::ZERO;
        for (&k, &base) in PILOT_SUBCARRIERS.iter().zip(PILOT_BASE.iter()) {
            pilots += window[bin(k)] * equaliser[bin(k)] * (base * polarity);
        }
        // e^(-j angle(pilots)); numpy's angle(0) = 0 gives no correction
        let correction = if pilots == Complex32::ZERO { Complex32::ONE } else { pilots.conj() / pilots.abs() };
        for (o, &k) in out.iter_mut().zip(DATA_SUBCARRIERS.iter()) {
            *o = window[bin(k)] * equaliser[bin(k)] * correction;
        }
    }
}

// ------------------------------------------------------------------------------------------------------------------
// Helpers
// ------------------------------------------------------------------------------------------------------------------

/// The preamble detector of `decode_all`. For each j in 0..len-63 it computes
///
/// ```text
///   c[j] = sum_{n=j}^{j+47} iq[n+16] conj(iq[n])      (lag-16 autocorrelation over 48 products)
///   p[j] = sum_{n=j}^{j+47} |iq[n+16]|^2              (power of the later samples)
///   m[j] = |c[j]| / (p[j] + 1e-9)
/// ```
///
/// and flags j when m[j] > threshold. During an STF, x[n+16] = x[n] up to noise and a CFO rotation, which changes
/// only the angle of c, so m is about SNR/(SNR+1). Noise and OFDM data symbols give small m.
///
/// The window sums are differences of prefix sums, as the Python's `_movsum` does (cumsum in f64). The prefix sums
/// restart every DETECT_BLOCK outputs, so the round-off depends on the local signal only, not on everything earlier
/// in the buffer; a stretch of exact zeros still gives exactly c = p = 0.
fn stf_candidates(iq: &[Complex32], threshold: f64) -> Vec<bool> {
    let span = STF_PERIOD + DETECT_WINDOW - 1; // the last window starts 63 samples before the end
    if iq.len() <= span {
        return Vec::new();
    }
    let n_out = iq.len() - span;
    let mut flags = vec![false; n_out];
    let mut prefix_c = vec![Complex64::ZERO; DETECT_BLOCK + DETECT_WINDOW];
    let mut prefix_p = vec![0.0f64; DETECT_BLOCK + DETECT_WINDOW];
    let mut j0 = 0;
    while j0 < n_out {
        let j1 = (j0 + DETECT_BLOCK).min(n_out);
        // prefix sums over the products n = j0 .. j1 + 46 (the last window of this block ends there)
        let terms = j1 - j0 + DETECT_WINDOW - 1;
        let mut acc_c = Complex64::ZERO;
        let mut acc_p = 0.0f64;
        for t in 0..terms {
            let n = j0 + t;
            let late = iq[n + STF_PERIOD];
            acc_c += (late * iq[n].conj()).to_c64(); // product in f32 (numpy complex64), sum in f64
            acc_p += late.norm_sqr() as f64;
            prefix_c[t + 1] = acc_c;
            prefix_p[t + 1] = acc_p;
        }
        for (t, flag) in flags[j0..j1].iter_mut().enumerate() {
            // window j = j0 + t: the products t .. t+47 of this block's prefix sums
            let c = prefix_c[t + DETECT_WINDOW] - prefix_c[t];
            let p = prefix_p[t + DETECT_WINDOW] - prefix_p[t];
            // |c| as sqrt(re^2 + im^2) rather than hypot: the sums of f32 products cannot overflow f64, and hypot
            // (a libm call per sample) would dominate the detector's cost
            *flag = c.norm_sqr().sqrt() / (p + DETECT_EPS) > threshold;
        }
        j0 = j1;
    }
    flags
}

/// angle(sum_n conj(x[n]) x[n + lag]): the phase advance of `x` over `lag` samples (numpy
/// `angle(vdot(x[:-lag], x[lag:]))`), summed in f64.
fn lag_phase(x: &[Complex32], lag: usize) -> f64 {
    let mut acc = Complex64::ZERO;
    for n in 0..x.len() - lag {
        acc += (x[n].conj().to_c64()) * x[n + lag].to_c64();
    }
    acc.arg()
}

/// out[n] = src[n] * e^(-j 2 pi freq (first + n)): removes a frequency offset of `freq` cycles/sample, with phase
/// 0 at index -first relative to `src[0]` (the Python's `derotate(a, b, f)`: first = a, phase 0 at t0).
///
/// The phasor is stepped by e^(-j 2 pi freq) in f64 and recomputed from the exact phase every 1024 samples, which
/// keeps the error at the f64 round-off level over arbitrarily long inputs.
fn derotate(src: &[Complex32], first: usize, freq: f64, out: &mut [Complex32]) {
    const RESYNC: usize = 1024;
    let step = Complex64::cis(-2.0 * PI * freq);
    let mut phasor = Complex64::ONE;
    for (n, (s, o)) in src.iter().zip(out.iter_mut()).enumerate() {
        if n % RESYNC == 0 {
            let turns = (freq * (first + n) as f64).fract(); // cycles, reduced to keep sin/cos accurate
            phasor = Complex64::cis(-2.0 * PI * turns);
        }
        *o = (s.to_c64() * phasor).to_c32();
        phasor *= step;
    }
}

/// Channel-estimate smoothing (the `H_SMOOTH` block of `decode_at`): each used subcarrier k gets the mean of the
/// original estimates at subcarriers k-n ..= k+n that are used (-26..=26, not DC).
fn smooth_channel(h: &[Complex32; FFT_LEN], n: usize) -> [Complex32; FFT_LEN] {
    let n = n.min(52) as i32; // beyond 52 every subcarrier already averages all of them
    let mut out = *h;
    for k in -26i32..=26 {
        if k == 0 {
            continue;
        }
        let mut sum = Complex32::ZERO;
        let mut count = 0.0f32;
        for j in (k - n)..=(k + n) {
            if j != 0 && (-26..=26).contains(&j) {
                sum += h[bin(j)];
                count += 1.0;
            }
        }
        out[bin(k)] = sum / count;
    }
    out
}

/// Deinterleaver table (port of `_perm`): `table[k]` is the position, among one symbol's coded bits in subcarrier
/// order, of the k-th coded bit, so `deinterleaved[k] = received[table[k]]`. The 802.11 interleaver
/// (IEEE 802.11-2020 17.3.5.7) maps coded bit k in two steps:
///
/// ```text
///   i = (ncbps/16) (k mod 16) + floor(k/16)            adjacent coded bits go to subcarriers 3 or more apart
///   j = s floor(i/s) + (i + ncbps - floor(16 i/ncbps)) mod s,   s = max(nbpsc/2, 1)
///                                                      and alternate between more and less reliable QAM bits
/// ```
fn deinterleaver(nbpsc: usize) -> Vec<usize> {
    let ncbps = DATA_CARRIERS * nbpsc;
    let s = (nbpsc / 2).max(1);
    (0..ncbps)
        .map(|k| {
            let i = (ncbps / 16) * (k % 16) + k / 16;
            s * (i / s) + (i + ncbps - (16 * i) / ncbps) % s
        })
        .collect()
}

/// Soft demapper (port of `demap`) for one equalised subcarrier: writes `nbpsc` values, positive = bit 1, in the
/// transmitter's bit order (the I-axis bits, then the Q-axis bits).
/// - BPSK: the bit is the sign of I.
/// - QPSK: first bit on I, second on Q.
/// - 16/64-QAM: max-log soft bits per axis (`demap_axis`), on the integer level grid (x sqrt(10) / sqrt(42)).
fn demap(sym: Complex32, nbpsc: usize, out: &mut [f32]) {
    match nbpsc {
        1 => out[0] = sym.re,
        2 => {
            out[0] = sym.re;
            out[1] = sym.im;
        }
        4 => {
            let scale = 10f32.sqrt();
            demap_axis(sym.re * scale, &QAM16_LEVELS, &mut out[..2]);
            demap_axis(sym.im * scale, &QAM16_LEVELS, &mut out[2..]);
        }
        6 => {
            let scale = 42f32.sqrt();
            demap_axis(sym.re * scale, &QAM64_LEVELS, &mut out[..3]);
            demap_axis(sym.im * scale, &QAM64_LEVELS, &mut out[3..]);
        }
        _ => unreachable!("no 802.11 OFDM rate has {nbpsc} bits per subcarrier"),
    }
}

/// Max-log soft bits of one QAM axis value `x` (on the integer grid). For each of the axis' bits (first =
/// most significant): the squared distance to the nearest level whose bit is 0, minus the same for bit 1.
fn demap_axis(x: f32, levels: &[f32], out: &mut [f32]) {
    let nbits = out.len();
    let mut dist = [0.0f32; 8]; // squared distance to each level (at most 8: 64-QAM)
    for (d, &level) in dist.iter_mut().zip(levels) {
        *d = (x - level) * (x - level);
    }
    for (b, o) in out.iter_mut().enumerate() {
        let mask = 1 << (nbits - 1 - b);
        let mut nearest0 = f32::INFINITY;
        let mut nearest1 = f32::INFINITY;
        for (index, &d) in dist.iter().enumerate().take(levels.len()) {
            if index & mask != 0 {
                nearest1 = nearest1.min(d);
            } else {
                nearest0 = nearest0.min(d);
            }
        }
        *o = nearest0 - nearest1;
    }
}

/// Descrambler (port of `descramble`). The 802.11 scrambler XORs the DATA bits with the 127-bit sequence of the
/// generator x^7 + x^4 + 1, s[n] = s[n-4] ^ s[n-7], from a seed the receiver does not know. The first 7 SERVICE
/// bits are zeros before scrambling, so the first 7 received bits are the sequence itself; the recurrence gives the
/// rest.
fn descramble(bits: &mut [u8]) {
    if bits.len() < 7 {
        return;
    }
    let mut seq = [0u8; SCRAMBLER_PERIOD];
    seq[..7].copy_from_slice(&bits[..7]);
    for n in 7..SCRAMBLER_PERIOD {
        seq[n] = seq[n - 4] ^ seq[n - 7];
    }
    for (n, b) in bits.iter_mut().enumerate() {
        *b ^= seq[n % SCRAMBLER_PERIOD];
    }
}

/// Byte-wise lookup table of the CRC-32 below.
const CRC32_TABLE: [u32; 256] = crc32_table();

const fn crc32_table() -> [u32; 256] {
    let mut table = [0u32; 256];
    let mut i = 0;
    while i < 256 {
        let mut c = i as u32;
        let mut k = 0;
        while k < 8 {
            c = if c & 1 != 0 { (c >> 1) ^ 0xEDB8_8320 } else { c >> 1 };
            k += 1;
        }
        table[i] = c;
        i += 1;
    }
    table
}

/// CRC-32 of IEEE 802.3, which is the 802.11 FCS and Python's `zlib.crc32`: reflected polynomial 0xEDB88320,
/// initial value and final XOR 0xFFFFFFFF. The FCS is sent little-endian after the MPDU body.
pub fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &byte in data {
        crc = CRC32_TABLE[((crc ^ byte as u32) & 0xFF) as usize] ^ (crc >> 8);
    }
    !crc
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc32_check_value() {
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
        assert_eq!(crc32(b""), 0);
    }

    /// The polarity table is the scrambler sequence for the all-ones seed (IEEE 802.11-2020 17.3.5.10).
    #[test]
    fn pilot_polarity_is_scrambler_sequence() {
        let mut state = [1u8; 7]; // x1..x7
        for &p in PILOT_POLARITY.iter() {
            let out = state[3] ^ state[6]; // x4 ^ x7
            state.rotate_right(1);
            state[0] = out;
            assert_eq!(p, if out == 0 { 1 } else { -1 });
        }
    }

    #[test]
    fn tables() {
        assert_eq!(DATA_SUBCARRIERS[0], -26);
        assert_eq!(DATA_SUBCARRIERS[47], 26);
        assert!(!DATA_SUBCARRIERS.iter().any(|k| [0, 7, -7, 21, -21].contains(k)));
        assert_eq!(LTF_SEQUENCE.iter().filter(|&&v| v != 0).count(), 52);
        let rx = Receiver::new();
        // LTF time-domain symbol: unit mean power (the 64 / sqrt(52) IFFT scaling), and its FFT gives the sequence
        // back, scaled by 64 / sqrt(52)
        let power: f32 = (0..FFT_LEN).map(|n| rx.ltf_time_re[n].powi(2) + rx.ltf_time_im[n].powi(2)).sum::<f32>();
        assert!((power / FFT_LEN as f32 - 1.0).abs() < 1e-5);
        let mut f: Vec<Complex32> = (0..FFT_LEN).map(|n| Complex32::new(rx.ltf_time_re[n], rx.ltf_time_im[n])).collect();
        rx.fft.forward(&mut f);
        for (v, &l) in f.iter().zip(rx.ltf_freq.iter()) {
            let want = l * 64.0 / 52f32.sqrt();
            assert!((v.re - want).abs() < 1e-4 && v.im.abs() < 1e-4);
        }
        // deinterleavers are permutations
        for t in &rx.deinterleavers {
            let mut seen = vec![false; t.len()];
            for &p in t {
                assert!(!seen[p]);
                seen[p] = true;
            }
        }
    }

    #[test]
    fn descrambler_inverts_scrambler() {
        // scramble 16 zero SERVICE bits + data with seed 1011101 (ofdm80211.scramble), then descramble
        let data: Vec<u8> = (0..300).map(|i| ((i * 7 + i / 3) % 2) as u8).collect();
        let mut bits = vec![0u8; 16];
        bits.extend(&data);
        let mut st = [1u8, 0, 1, 1, 1, 0, 1];
        let mut scrambled: Vec<u8> = bits
            .iter()
            .map(|&b| {
                let fb = st[3] ^ st[6];
                st.rotate_right(1);
                st[0] = fb;
                b ^ fb
            })
            .collect();
        descramble(&mut scrambled);
        assert_eq!(scrambled, bits);
    }

    #[test]
    fn qam_demap_signs() {
        // each 64-QAM level, transmitted noiselessly, demaps to soft bits whose signs are its Gray bits
        for (index, &level) in QAM64_LEVELS.iter().enumerate() {
            let mut out = [0.0f32; 6];
            let v = level / 42f32.sqrt();
            demap(Complex32::new(v, v), 6, &mut out);
            for b in 0..3 {
                let bit = (index >> (2 - b)) & 1;
                assert_eq!(out[b] > 0.0, bit == 1, "level {level} bit {b}");
                assert_eq!(out[3 + b] > 0.0, bit == 1);
            }
        }
    }

    #[test]
    fn no_frames_in_silence_or_short_input() {
        assert!(decode_all(&[Complex32::ZERO; 10], 50, None, 4095).is_empty());
        assert!(decode_all(&vec![Complex32::ZERO; 5000], 50, None, 4095).is_empty());
    }

    #[test]
    fn derotate_matches_direct_phase() {
        let src = vec![Complex32::ONE; 3000];
        let mut out = vec![Complex32::ZERO; 3000];
        let f = 0.0123;
        derotate(&src, 500, f, &mut out);
        for (n, v) in out.iter().enumerate() {
            let want = Complex64::cis(-2.0 * PI * f * (500 + n) as f64).to_c32();
            assert!((*v - want).abs() < 1e-6, "n={n}");
        }
    }
}
