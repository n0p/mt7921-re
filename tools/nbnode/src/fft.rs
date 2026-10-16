//! In-place radix-2 complex FFT for power-of-two sizes.
//!
//! Same conventions as numpy:
//! - `forward`: X[k] = sum_n x[n] e^(-2 pi j k n / N), unnormalised (`numpy.fft.fft`);
//! - `inverse`: x[n] = (1/N) sum_k X[k] e^(+2 pi j k n / N) (`numpy.fft.ifft`).
//!
//! The receiver's hot path is N = 64, one transform per OFDM symbol. The algorithm is the textbook iterative
//! decimation-in-time Cooley-Tukey:
//! 1. reorder the input into bit-reversed index order;
//! 2. run log2(N) stages of butterflies. Stage `size` merges pairs of size/2-point DFTs into size-point DFTs.
//!
//! The twiddle factors and the bit-reversal table are computed once per [`Fft`] (twiddles in f64, then rounded to
//! f32).

use crate::complex::{Complex32, Complex64};

/// A precomputed FFT plan for one power-of-two size.
#[derive(Clone, Debug)]
pub struct Fft {
    n: usize,
    /// twiddles[k] = e^(-2 pi j k / n), k = 0..n/2.
    twiddles: Vec<Complex32>,
    /// bitrev[i] = i with its log2(n) index bits reversed.
    bitrev: Vec<usize>,
}

impl Fft {
    /// Plan for size `n`. Panics unless `n` is a power of two.
    pub fn new(n: usize) -> Fft {
        assert!(n.is_power_of_two(), "FFT size {n} is not a power of two");
        let bits = n.trailing_zeros();
        let twiddles = (0..n / 2)
            .map(|k| Complex64::cis(-2.0 * std::f64::consts::PI * k as f64 / n as f64).to_c32())
            .collect();
        let bitrev = (0..n)
            .map(|i| if bits == 0 { 0 } else { i.reverse_bits() >> (usize::BITS - bits) })
            .collect();
        Fft { n, twiddles, bitrev }
    }

    /// Transform size.
    pub fn len(&self) -> usize {
        self.n
    }

    /// Always false: a plan has at least one point.
    pub fn is_empty(&self) -> bool {
        false
    }

    /// Forward transform in place (numpy `fft`). `buf.len()` must equal the plan size.
    pub fn forward(&self, buf: &mut [Complex32]) {
        self.transform(buf, false);
    }

    /// Inverse transform in place, including the 1/N scaling (numpy `ifft`).
    pub fn inverse(&self, buf: &mut [Complex32]) {
        self.transform(buf, true);
        let k = 1.0 / self.n as f32;
        for v in buf.iter_mut() {
            *v = v.scale(k);
        }
    }

    fn transform(&self, buf: &mut [Complex32], inverse: bool) {
        let n = self.n;
        assert_eq!(buf.len(), n, "buffer length does not match the FFT size");
        // 1. bit-reversed order: afterwards each run of `size` elements holds the inputs of one size-point DFT
        for i in 0..n {
            let j = self.bitrev[i];
            if j > i {
                buf.swap(i, j);
            }
        }
        // 2. butterflies. A size-point DFT is built from its two half-size DFTs E (even inputs) and O (odd inputs):
        //    X[k] = E[k] + w^k O[k],  X[k + size/2] = E[k] - w^k O[k],  w = e^(-2 pi j / size)
        let mut size = 2;
        while size <= n {
            let half = size / 2;
            let stride = n / size; // w^k = twiddles[k * stride]
            for start in (0..n).step_by(size) {
                for k in 0..half {
                    let w = self.twiddles[k * stride];
                    let w = if inverse { w.conj() } else { w };
                    let even = buf[start + k];
                    let odd = buf[start + k + half] * w;
                    buf[start + k] = even + odd;
                    buf[start + k + half] = even - odd;
                }
            }
            size *= 2;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Direct O(N^2) DFT in f64, the reference.
    fn dft(x: &[Complex32], sign: f64) -> Vec<Complex64> {
        let n = x.len();
        (0..n)
            .map(|k| {
                let mut acc = Complex64::ZERO;
                for (i, v) in x.iter().enumerate() {
                    let ang = sign * 2.0 * std::f64::consts::PI * ((k * i) % n) as f64 / n as f64;
                    acc += v.to_c64() * Complex64::cis(ang);
                }
                acc
            })
            .collect()
    }

    fn test_signal(n: usize) -> Vec<Complex32> {
        // deterministic pseudo-random values (a simple LCG keeps the test free of dependencies)
        let mut s: u32 = 12345;
        let mut next = || {
            s = s.wrapping_mul(1664525).wrapping_add(1013904223);
            (s >> 8) as f32 / (1u32 << 24) as f32 - 0.5
        };
        (0..n).map(|_| Complex32::new(next(), next())).collect()
    }

    #[test]
    fn forward_matches_dft() {
        for n in [1usize, 2, 4, 8, 64, 256] {
            let x = test_signal(n);
            let reference = dft(&x, -1.0);
            let mut y = x.clone();
            Fft::new(n).forward(&mut y);
            for (a, b) in y.iter().zip(&reference) {
                assert!((a.to_c64() - *b).abs() < 1e-5 * n as f64, "n={n}: {a:?} vs {b:?}");
            }
        }
    }

    #[test]
    fn inverse_undoes_forward() {
        let x = test_signal(64);
        let fft = Fft::new(64);
        let mut y = x.clone();
        fft.forward(&mut y);
        fft.inverse(&mut y);
        for (a, b) in y.iter().zip(&x) {
            assert!((*a - *b).abs() < 1e-6);
        }
    }

    #[test]
    fn inverse_matches_dft() {
        let x = test_signal(64);
        let reference = dft(&x, 1.0);
        let mut y = x.clone();
        Fft::new(64).inverse(&mut y);
        for (a, b) in y.iter().zip(&reference) {
            assert!((a.to_c64() - *b * (1.0 / 64.0)).abs() < 1e-6);
        }
    }
}
