//! Raw IQ files, as `rx80211.py <file>` reads them:
//! - `.cf32`: numpy complex64 (`ndarray.tofile`), interleaved little-endian f32 I, Q.
//! - `.cs16`: interleaved little-endian i16 I, Q. The values are kept unscaled, as in the Python; the receiver does
//!   not depend on the input scale.
//!
//! A trailing partial sample (a file cut mid-sample) is ignored.

use crate::complex::Complex32;
use std::fs;
use std::io;
use std::path::Path;

/// Sample format of an IQ file.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IqFormat {
    Cf32,
    Cs16,
}

impl IqFormat {
    /// The format by file name: names ending in "cs16" are cs16, everything else cf32 (`rx80211.py main`).
    pub fn from_path(path: &Path) -> IqFormat {
        if path.to_string_lossy().ends_with("cs16") {
            IqFormat::Cs16
        } else {
            IqFormat::Cf32
        }
    }
}

/// Reads a whole IQ file, choosing the format by name (see [`IqFormat::from_path`]).
pub fn read_iq(path: &Path) -> io::Result<Vec<Complex32>> {
    let bytes = fs::read(path)?;
    Ok(match IqFormat::from_path(path) {
        IqFormat::Cf32 => cf32_from_bytes(&bytes),
        IqFormat::Cs16 => cs16_from_bytes(&bytes),
    })
}

/// Reads a `.cf32` file.
pub fn read_cf32(path: &Path) -> io::Result<Vec<Complex32>> {
    Ok(cf32_from_bytes(&fs::read(path)?))
}

/// Writes samples as `.cf32`.
pub fn write_cf32(path: &Path, iq: &[Complex32]) -> io::Result<()> {
    let mut bytes = Vec::with_capacity(iq.len() * 8);
    for v in iq {
        bytes.extend_from_slice(&v.re.to_le_bytes());
        bytes.extend_from_slice(&v.im.to_le_bytes());
    }
    fs::write(path, bytes)
}

/// Decodes interleaved little-endian f32 I/Q.
pub fn cf32_from_bytes(bytes: &[u8]) -> Vec<Complex32> {
    bytes
        .as_chunks::<8>()
        .0
        .iter()
        .map(|c| {
            Complex32::new(f32::from_le_bytes([c[0], c[1], c[2], c[3]]), f32::from_le_bytes([c[4], c[5], c[6], c[7]]))
        })
        .collect()
}

/// Decodes interleaved little-endian i16 I/Q (unscaled).
pub fn cs16_from_bytes(bytes: &[u8]) -> Vec<Complex32> {
    bytes
        .as_chunks::<4>()
        .0
        .iter()
        .map(|c| Complex32::new(i16::from_le_bytes([c[0], c[1]]) as f32, i16::from_le_bytes([c[2], c[3]]) as f32))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decode_formats() {
        let mut b = Vec::new();
        b.extend_from_slice(&1.5f32.to_le_bytes());
        b.extend_from_slice(&(-2.0f32).to_le_bytes());
        b.push(0); // partial trailing sample
        assert_eq!(cf32_from_bytes(&b), vec![Complex32::new(1.5, -2.0)]);
        let b = [0x01, 0x00, 0xff, 0xff, 0x00, 0x80];
        assert_eq!(cs16_from_bytes(&b), vec![Complex32::new(1.0, -1.0)]);
        assert_eq!(IqFormat::from_path(Path::new("x.cs16")), IqFormat::Cs16);
        assert_eq!(IqFormat::from_path(Path::new("x.cf32")), IqFormat::Cf32);
    }
}
