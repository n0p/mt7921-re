//! Minimal complex number types; no external crates.
//!
//! [`Complex32`] (f32 parts, like numpy `complex64`) is the sample type of the whole crate: IQ samples, FFT bins and
//! equalised subcarriers. [`Complex64`] (f64 parts) is used only where single precision would lose accuracy:
//! - the long running sums of the preamble detector;
//! - phase rotators stepped thousands of times (carrier frequency offset correction).

use std::ops::{Add, AddAssign, Div, Mul, MulAssign, Neg, Sub, SubAssign};

/// Complex number with `f32` parts.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Complex32 {
    pub re: f32,
    pub im: f32,
}

/// Complex number with `f64` parts.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Complex64 {
    pub re: f64,
    pub im: f64,
}

impl Complex32 {
    pub const ZERO: Complex32 = Complex32 { re: 0.0, im: 0.0 };
    pub const ONE: Complex32 = Complex32 { re: 1.0, im: 0.0 };

    #[inline]
    pub const fn new(re: f32, im: f32) -> Self {
        Complex32 { re, im }
    }

    /// Complex conjugate.
    #[inline]
    pub fn conj(self) -> Self {
        Complex32::new(self.re, -self.im)
    }

    /// |z|^2.
    #[inline]
    pub fn norm_sqr(self) -> f32 {
        self.re * self.re + self.im * self.im
    }

    /// |z| (computed with `hypot`, as numpy's `abs` does).
    #[inline]
    pub fn abs(self) -> f32 {
        self.re.hypot(self.im)
    }

    /// Phase angle in (-pi, pi] (numpy `angle`; 0 for z = 0).
    #[inline]
    pub fn arg(self) -> f32 {
        self.im.atan2(self.re)
    }

    /// Multiplication by a real number.
    #[inline]
    pub fn scale(self, k: f32) -> Self {
        Complex32::new(self.re * k, self.im * k)
    }

    /// Widening conversion to [`Complex64`] (exact).
    #[inline]
    pub fn to_c64(self) -> Complex64 {
        Complex64::new(self.re as f64, self.im as f64)
    }
}

impl Complex64 {
    pub const ZERO: Complex64 = Complex64 { re: 0.0, im: 0.0 };
    pub const ONE: Complex64 = Complex64 { re: 1.0, im: 0.0 };

    #[inline]
    pub const fn new(re: f64, im: f64) -> Self {
        Complex64 { re, im }
    }

    /// e^(j theta).
    #[inline]
    pub fn cis(theta: f64) -> Self {
        let (s, c) = theta.sin_cos();
        Complex64::new(c, s)
    }

    /// Complex conjugate.
    #[inline]
    pub fn conj(self) -> Self {
        Complex64::new(self.re, -self.im)
    }

    /// |z|^2.
    #[inline]
    pub fn norm_sqr(self) -> f64 {
        self.re * self.re + self.im * self.im
    }

    /// |z| (computed with `hypot`).
    #[inline]
    pub fn abs(self) -> f64 {
        self.re.hypot(self.im)
    }

    /// Phase angle in (-pi, pi] (0 for z = 0).
    #[inline]
    pub fn arg(self) -> f64 {
        self.im.atan2(self.re)
    }

    /// Narrowing conversion to [`Complex32`] (rounds each part to f32).
    #[inline]
    pub fn to_c32(self) -> Complex32 {
        Complex32::new(self.re as f32, self.im as f32)
    }
}

// Arithmetic operators, implemented identically for both types. Division is the textbook formula
// a / b = a * conj(b) / |b|^2; a zero divisor gives inf/NaN parts, as in numpy.
macro_rules! complex_ops {
    ($t:ident, $f:ty) => {
        impl Add for $t {
            type Output = $t;
            #[inline]
            fn add(self, o: $t) -> $t {
                $t::new(self.re + o.re, self.im + o.im)
            }
        }
        impl Sub for $t {
            type Output = $t;
            #[inline]
            fn sub(self, o: $t) -> $t {
                $t::new(self.re - o.re, self.im - o.im)
            }
        }
        impl Mul for $t {
            type Output = $t;
            #[inline]
            fn mul(self, o: $t) -> $t {
                $t::new(self.re * o.re - self.im * o.im, self.re * o.im + self.im * o.re)
            }
        }
        impl Mul<$f> for $t {
            type Output = $t;
            #[inline]
            fn mul(self, k: $f) -> $t {
                $t::new(self.re * k, self.im * k)
            }
        }
        impl Div for $t {
            type Output = $t;
            #[inline]
            fn div(self, o: $t) -> $t {
                let d = o.re * o.re + o.im * o.im;
                $t::new((self.re * o.re + self.im * o.im) / d, (self.im * o.re - self.re * o.im) / d)
            }
        }
        impl Div<$f> for $t {
            type Output = $t;
            #[inline]
            fn div(self, k: $f) -> $t {
                $t::new(self.re / k, self.im / k)
            }
        }
        impl Neg for $t {
            type Output = $t;
            #[inline]
            fn neg(self) -> $t {
                $t::new(-self.re, -self.im)
            }
        }
        impl AddAssign for $t {
            #[inline]
            fn add_assign(&mut self, o: $t) {
                self.re += o.re;
                self.im += o.im;
            }
        }
        impl SubAssign for $t {
            #[inline]
            fn sub_assign(&mut self, o: $t) {
                self.re -= o.re;
                self.im -= o.im;
            }
        }
        impl MulAssign for $t {
            #[inline]
            fn mul_assign(&mut self, o: $t) {
                *self = *self * o;
            }
        }
    };
}

complex_ops!(Complex32, f32);
complex_ops!(Complex64, f64);

/// Dot product of two real slices of equal length (the inner loop of the FIR filters and correlations).
///
/// It keeps eight independent partial sums. With a single running sum the additions would have to happen strictly
/// in order: float addition is not associative, so the compiler may not reorder it. The independent partial sums
/// let it use SIMD registers instead.
#[inline]
pub fn dot(a: &[f32], b: &[f32]) -> f32 {
    assert_eq!(a.len(), b.len(), "dot: slices of different lengths");
    const LANES: usize = 8;
    let (chunks_a, tail_a) = a.as_chunks::<LANES>();
    let (chunks_b, tail_b) = b.as_chunks::<LANES>();
    let tail: f32 = tail_a.iter().zip(tail_b).map(|(x, y)| x * y).sum();
    let mut acc = [0.0f32; LANES];
    for (x, y) in chunks_a.iter().zip(chunks_b) {
        for lane in 0..LANES {
            acc[lane] += x[lane] * y[lane];
        }
    }
    acc.iter().sum::<f32>() + tail
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn arithmetic() {
        let a = Complex32::new(1.0, 2.0);
        let b = Complex32::new(3.0, -1.0);
        assert_eq!(a + b, Complex32::new(4.0, 1.0));
        assert_eq!(a - b, Complex32::new(-2.0, 3.0));
        assert_eq!(a * b, Complex32::new(5.0, 5.0)); // (1+2j)(3-j) = 3 - j + 6j + 2 = 5 + 5j
        let q = (a * b) / b;
        assert!((q - a).abs() < 1e-6);
        assert_eq!(a.conj(), Complex32::new(1.0, -2.0));
        assert_eq!(a.norm_sqr(), 5.0);
        assert!((Complex64::cis(0.5).arg() - 0.5).abs() < 1e-15);
        assert_eq!(Complex32::ZERO.arg(), 0.0); // numpy.angle(0) == 0
    }

    #[test]
    fn dot_product() {
        for n in [0usize, 1, 7, 8, 9, 61, 64] {
            let a: Vec<f32> = (0..n).map(|i| i as f32 * 0.5).collect();
            let b: Vec<f32> = (0..n).map(|i| 1.0 - i as f32 * 0.25).collect();
            let expect: f32 = a.iter().zip(&b).map(|(x, y)| x * y).sum();
            assert!((dot(&a, &b) - expect).abs() <= 1e-4 * expect.abs().max(1.0), "n={n}");
        }
    }
}
