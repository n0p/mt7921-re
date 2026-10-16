//! Decoder front end: rational resampling and DC removal.
//!
//! Ports what tools/guest/nbrxd.py (`decode_segment`) and tools/sdr/txscan.py (`decode`) do with scipy before
//! `rx80211.decode_all`:
//!
//! ```text
//!   y = resample_poly(x - x.mean(), up, down)          # capture rate -> channel width rate (20 Msps-equivalent)
//!   y = lfilter([1, -1], [1, -a], y),  a = 1 - 2 pi 20 kHz / fs   # one-pole DC high-pass at the decode rate fs
//! ```
//!
//! The capture rates are 10 Msps (ICAP packer 2) and 30 Msps (packer 8). The channel widths are 10, 5 and 2.5 MHz.
//! The conversions in use are therefore 1/3, 1/2, 1/4, 1/6 and 1/12, but [`Resampler`] takes any up/down pair.

use crate::complex::{dot, Complex32, Complex64};

/// scipy `resample_poly` default window: ('kaiser', 5.0).
pub const KAISER_BETA: f64 = 5.0;
/// scipy `resample_poly`: the filter's half-length is 10 * max(up, down) taps ("reasonable cutoff for our sinc-like
/// function"), i.e. 20 * max(up, down) + 1 taps in all.
pub const HALF_LEN_PER_RATE: usize = 10;
/// Corner frequency of the DC-removal high-pass used by nbrxd.py and txscan.py.
pub const HIGHPASS_CORNER_HZ: f64 = 20e3;
/// The polyphase filters are padded to a multiple of this many taps: the lane count of [`dot`].
const TAP_BLOCK: usize = 8;

/// Rational resampler by `up / down` with scipy `resample_poly` semantics.
///
/// Conceptually it upsamples by `up` (inserting up-1 zeros between samples), low-pass filters with a windowed-sinc
/// FIR, then keeps every `down`-th sample. The polyphase form computes only the kept outputs and skips the inserted
/// zeros. The details follow scipy (`resample_poly` / `upfirdn`, default `padtype='constant'`):
/// - Filter: `firwin(2 L + 1, 1 / max(up, down), window=('kaiser', 5.0)) * up`, with L = 10 * max(up, down). The
///   cutoff is the lower of the two Nyquist frequencies, and the gain `up` restores the amplitude lost to the zeros.
/// - Alignment: output sample i sits at input time i * down / up. The filter is centred (zero phase), so frames keep
///   their positions.
/// - Edges: samples outside the input count as zeros. There are ceil(n * up / down) outputs.
#[derive(Clone, Debug)]
pub struct Resampler {
    up: usize,
    down: usize,
    half_len: usize,
    /// Polyphase filters: `phase_taps[p]` holds the taps h[p], h[p + up], h[p + 2 up], ... in reverse order, so that
    /// each output is one forward dot product with consecutive input samples (see `process`). Each is front-padded
    /// with zero taps to a multiple of [`TAP_BLOCK`]; the padding meets older samples (or the zeros before the block)
    /// and adds nothing, but it spares the dot product a scalar tail.
    phase_taps: Vec<Vec<f32>>,
}

impl Resampler {
    /// Resampler by `up / down` (reduced by their common divisor, as scipy does). Panics if either is 0.
    pub fn new(up: usize, down: usize) -> Resampler {
        assert!(up >= 1 && down >= 1, "resampling factors must be positive");
        let g = gcd(up, down);
        let (up, down) = (up / g, down / g);
        if up == 1 && down == 1 {
            return Resampler { up, down, half_len: 0, phase_taps: Vec::new() }; // scipy returns a copy
        }
        let max_rate = up.max(down);
        let half_len = HALF_LEN_PER_RATE * max_rate;
        let h = firwin_lowpass(2 * half_len + 1, 1.0 / max_rate as f64, KAISER_BETA);
        let phase_taps = (0..up)
            .map(|p| {
                let mut taps: Vec<f32> = h.iter().skip(p).step_by(up).map(|&v| (v * up as f64) as f32).collect();
                taps.reverse();
                let mut padded = vec![0.0f32; taps.len().next_multiple_of(TAP_BLOCK) - taps.len()];
                padded.extend(taps);
                padded
            })
            .collect();
        Resampler { up, down, half_len, phase_taps }
    }

    /// Upsampling factor (after reduction).
    pub fn up(&self) -> usize {
        self.up
    }

    /// Downsampling factor (after reduction).
    pub fn down(&self) -> usize {
        self.down
    }

    /// True for a 1/1 ratio, where `process` just copies.
    pub fn is_identity(&self) -> bool {
        self.up == 1 && self.down == 1
    }

    /// Number of outputs for `n_in` inputs: ceil(n_in * up / down).
    pub fn output_len(&self, n_in: usize) -> usize {
        (n_in * self.up).div_ceil(self.down)
    }

    /// Resamples one block, `scipy.signal.resample_poly(x, up, down)`.
    pub fn process(&self, x: &[Complex32]) -> Vec<Complex32> {
        if self.is_identity() {
            return x.to_vec();
        }
        let n_in = x.len();
        let n_out = self.output_len(n_in);
        if n_out == 0 {
            return Vec::new();
        }
        // Output i is the filter centred on upsampled time i * down (input time i * down / up). Shifted by the
        // filter's half-length, that is upsampled position t = i * down + half_len, and the output is
        //     y[i] = sum_j h[j] * x_up[t - j],    x_up[m] = x[m / up] when up divides m, else 0.
        // Only the taps j = t mod up, t mod up + up, ... meet real samples: the polyphase filter of phase t mod up,
        // applied to the inputs ending at the newest one under the filter, t / up.
        let longest = self.phase_taps.iter().map(Vec::len).max().unwrap_or(0);
        let newest_needed = ((n_out - 1) * self.down + self.half_len) / self.up;
        let pad_left = longest;
        let pad_right = (newest_needed + 1).saturating_sub(n_in);
        // zero-padded copies of the real and imaginary parts (zeros outside the block, as scipy's constant padding)
        let mut re = vec![0.0f32; pad_left + n_in + pad_right];
        let mut im = vec![0.0f32; pad_left + n_in + pad_right];
        for (i, v) in x.iter().enumerate() {
            re[pad_left + i] = v.re;
            im[pad_left + i] = v.im;
        }
        let mut out = Vec::with_capacity(n_out);
        for i in 0..n_out {
            let t = i * self.down + self.half_len;
            let taps = &self.phase_taps[t % self.up];
            let newest = t / self.up;
            let first = pad_left + newest + 1 - taps.len(); // index of the oldest input under the filter
            let span = first..first + taps.len();
            out.push(Complex32::new(dot(&re[span.clone()], taps), dot(&im[span], taps)));
        }
        out
    }
}

/// One-pole DC-removal high-pass: y[n] = x[n] - x[n-1] + a y[n-1]
/// (`scipy.signal.lfilter([1, -1], [1, -a], x)`, starting from rest).
///
/// The receiver needs it because the card's IQ carries a large DC offset (LO leakage), and a constant input
/// autocorrelates perfectly at the STF lag. The zero at DC removes the offset. The pole at a (just inside the unit
/// circle) keeps the notch narrow: with a = 1 - 2 pi f_c / fs, the -3 dB corner is near f_c = 20 kHz. That is below
/// the first OFDM subcarrier at every width: 39 kHz at 2.5 MHz, 78 kHz at 5 MHz, 156 kHz at 10 MHz. At 2.5 MHz the
/// two innermost subcarriers lose about 1 dB, which the channel estimate absorbs. The state carries over between
/// calls, so a stream can be filtered block by block; nbrxd starts each block from rest, i.e. a new `HighPass` per
/// block.
#[derive(Clone, Debug)]
pub struct HighPass {
    a: f64,
    prev_in: Complex64,
    prev_out: Complex64,
}

impl HighPass {
    /// The nbrxd.py filter for decode rate `sample_rate_hz`: a = 1 - 2 pi * 20 kHz / fs.
    pub fn new(sample_rate_hz: f64) -> HighPass {
        HighPass::with_pole(1.0 - 2.0 * std::f64::consts::PI * HIGHPASS_CORNER_HZ / sample_rate_hz)
    }

    /// Filter with pole `a` (0 < a < 1).
    pub fn with_pole(a: f64) -> HighPass {
        HighPass { a, prev_in: Complex64::ZERO, prev_out: Complex64::ZERO }
    }

    /// The pole a.
    pub fn pole(&self) -> f64 {
        self.a
    }

    /// Filters `buf` in place (state in f64, as scipy's complex128 computation).
    pub fn process(&mut self, buf: &mut [Complex32]) {
        for v in buf.iter_mut() {
            let x = v.to_c64();
            let y = x - self.prev_in + self.prev_out * self.a;
            self.prev_in = x;
            self.prev_out = y;
            *v = y.to_c32();
        }
    }
}

/// The whole front end of nbrxd.py `decode_segment` for one block captured at `in_rate_msps`, to be decoded at
/// `width_msps` (the channel width in MHz):
/// `lfilter([1, -1], [1, -a], resample_poly(x - x.mean(), up, down))`, with up/down from [`rate_ratio`] and
/// a = 1 - 2 pi 20 kHz / (width_msps * 1e6). As in nbrxd, the mean is removed even when no resampling is needed.
/// (txscan.py differs only there: it skips the mean removal at 1/1.)
pub fn front_end(x: &[Complex32], in_rate_msps: f64, width_msps: f64) -> Vec<Complex32> {
    let (up, down) = rate_ratio(width_msps, in_rate_msps);
    let mut centred = x.to_vec();
    remove_mean(&mut centred);
    let mut y = Resampler::new(up, down).process(&centred);
    HighPass::new(width_msps * 1e6).process(&mut y);
    y
}

/// Subtracts the block mean (nbrxd.py `x - x.mean()` before `resample_poly`). This avoids the step that a DC offset
/// would make at the zero-padded block edges.
pub fn remove_mean(buf: &mut [Complex32]) {
    if buf.is_empty() {
        return;
    }
    let mut sum = Complex64::ZERO;
    for v in buf.iter() {
        sum += v.to_c64();
    }
    let mean = (sum * (1.0 / buf.len() as f64)).to_c32();
    for v in buf.iter_mut() {
        *v -= mean;
    }
}

/// The resampling ratio `out_rate / in_rate` as a reduced fraction (up, down) with down <= 1000. It is what nbrxd.py
/// and txscan.py compute with `Fraction(out / in).limit_denominator(1000)`. Exact ratios such as 10/30 give their
/// lowest terms (1, 3); other ratios give the closest fraction, the smallest denominator winning ties.
pub fn rate_ratio(out_rate: f64, in_rate: f64) -> (usize, usize) {
    assert!(out_rate > 0.0 && in_rate > 0.0, "sample rates must be positive");
    let r = out_rate / in_rate;
    let mut best = (1usize, 1usize);
    let mut best_err = f64::INFINITY;
    for den in 1..=1000usize {
        let num = (r * den as f64).round().max(1.0);
        let err = (num / den as f64 - r).abs();
        if err < best_err - 1e-12 {
            best = (num as usize, den);
            best_err = err;
        }
    }
    let g = gcd(best.0, best.1);
    (best.0 / g, best.1 / g)
}

/// Windowed-sinc low-pass FIR, scipy `firwin(numtaps, cutoff, window=('kaiser', beta))`: `cutoff` is relative to the
/// Nyquist frequency (1.0 = fs/2). The taps are normalised to unit gain at DC (firwin's `scale=True` for a low-pass).
pub fn firwin_lowpass(numtaps: usize, cutoff: f64, beta: f64) -> Vec<f64> {
    let center = (numtaps as f64 - 1.0) / 2.0;
    let window = kaiser_window(numtaps, beta);
    let mut h: Vec<f64> = (0..numtaps).map(|n| cutoff * sinc(cutoff * (n as f64 - center)) * window[n]).collect();
    let sum: f64 = h.iter().sum();
    for v in h.iter_mut() {
        *v /= sum;
    }
    h
}

/// Symmetric Kaiser window of `m` points (scipy `kaiser(m, beta)`, numpy `kaiser`):
/// w[n] = I0(beta * sqrt(1 - ((n - c) / c)^2)) / I0(beta), c = (m - 1) / 2.
pub fn kaiser_window(m: usize, beta: f64) -> Vec<f64> {
    if m == 1 {
        return vec![1.0];
    }
    let c = (m as f64 - 1.0) / 2.0;
    let norm = bessel_i0(beta);
    (0..m)
        .map(|n| {
            let r = (n as f64 - c) / c;
            bessel_i0(beta * (1.0 - r * r).max(0.0).sqrt()) / norm
        })
        .collect()
}

/// Normalised sinc: sin(pi x) / (pi x), 1 at x = 0 (numpy `sinc`).
fn sinc(x: f64) -> f64 {
    if x == 0.0 {
        1.0
    } else {
        let px = std::f64::consts::PI * x;
        px.sin() / px
    }
}

/// Modified Bessel function of the first kind, order 0, from its power series
/// I0(x) = sum_k ((x/2)^k / k!)^2. For the arguments used here (|x| <= beta = 5) it converges to full double precision
/// in about 25 terms.
fn bessel_i0(x: f64) -> f64 {
    let q = (x / 2.0) * (x / 2.0);
    let mut term = 1.0;
    let mut sum = 1.0;
    let mut k = 1.0;
    while term > 1e-17 * sum {
        term *= q / (k * k);
        sum += term;
        k += 1.0;
    }
    sum
}

fn gcd(a: usize, b: usize) -> usize {
    if b == 0 {
        a
    } else {
        gcd(b, a % b)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bessel_i0_values() {
        // reference values (scipy.special.i0)
        assert!((bessel_i0(0.0) - 1.0).abs() < 1e-15);
        assert!((bessel_i0(1.0) - 1.2660658777520082).abs() < 1e-14);
        assert!((bessel_i0(5.0) - 27.239871823604442).abs() < 1e-12);
    }

    #[test]
    fn filter_has_unit_dc_gain_and_is_symmetric() {
        let h = firwin_lowpass(61, 1.0 / 3.0, KAISER_BETA);
        assert!((h.iter().sum::<f64>() - 1.0).abs() < 1e-12);
        for i in 0..61 {
            assert!((h[i] - h[60 - i]).abs() < 1e-15);
        }
    }

    #[test]
    fn decimation_passes_dc_and_keeps_length_rule() {
        let r = Resampler::new(1, 3);
        let x = vec![Complex32::new(1.0, -2.0); 3000];
        let y = r.process(&x);
        assert_eq!(y.len(), 1000);
        // away from the zero-padded edges a constant passes with unit gain
        for v in &y[50..950] {
            assert!((v.re - 1.0).abs() < 1e-4 && (v.im + 2.0).abs() < 1e-4, "{v:?}");
        }
        assert_eq!(Resampler::new(1, 3).output_len(3001), 1001); // ceil
        assert_eq!(Resampler::new(2, 6).up(), 1); // reduced like scipy
    }

    #[test]
    fn tone_in_passband_survives_decimation() {
        // 0.05 cycles/sample at the input = 0.15 at the output of a 1/3 decimator (inside its passband)
        let x: Vec<Complex32> =
            (0..6000).map(|n| Complex64::cis(2.0 * std::f64::consts::PI * 0.05 * n as f64).to_c32()).collect();
        let y = Resampler::new(1, 3).process(&x);
        for (i, v) in y.iter().enumerate().take(1900).skip(100) {
            let want = Complex64::cis(2.0 * std::f64::consts::PI * 0.15 * i as f64).to_c32();
            assert!((*v - want).abs() < 2e-3, "i={i}: {v:?} vs {want:?}");
        }
    }

    #[test]
    fn highpass_step_response() {
        // a constant input decays as a^n: y[0] = c, y[n] = a y[n-1]
        let mut hp = HighPass::new(10e6);
        let a = hp.pole();
        assert!((a - (1.0 - 2.0 * std::f64::consts::PI * 20e3 / 10e6)).abs() < 1e-15);
        let mut buf = vec![Complex32::new(2.0, 1.0); 200];
        hp.process(&mut buf);
        for (n, v) in buf.iter().enumerate() {
            let want = a.powi(n as i32);
            assert!((v.re as f64 - 2.0 * want).abs() < 1e-5 && (v.im as f64 - want).abs() < 1e-5);
        }
    }

    #[test]
    fn rate_ratios() {
        assert_eq!(rate_ratio(10.0, 30.0), (1, 3));
        assert_eq!(rate_ratio(5.0, 10.0), (1, 2));
        assert_eq!(rate_ratio(2.5, 10.0), (1, 4));
        assert_eq!(rate_ratio(5.0, 30.0), (1, 6));
        assert_eq!(rate_ratio(2.5, 30.0), (1, 12));
        assert_eq!(rate_ratio(10.0, 10.0), (1, 1));
        assert_eq!(rate_ratio(20.0, 30.0), (2, 3));
    }
}
