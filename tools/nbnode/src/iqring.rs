//! The card's receive IQ stream as it reaches the host: fw/src/iqstream.c sends ICAP ring words over USB, and
//! mt7921dbg collects them into a 32 MiB ring per slot (`/sys/kernel/debug/mt7921dbg/iqN`) as records:
//! u32 "QIQR", u32 sequence, u32 absolute ring position (mod 2^32), u32 payload bytes, then the payload words
//! (Q in [13:0], I in [29:16], 14-bit two's complement). This module reads that ring and turns the words into
//! samples (tools/sdr/iqrec.py).

use std::fs::{self, File};
use std::io::{self, Read};

use crate::card::{other, MT7921DBG};
use crate::complex::Complex32;

/// Record header marker "QIQR".
const REC_MARK: u32 = 0x5251_4951;

/// One record: the payload words and the ring position of the first one.
pub struct Record {
    pub seq: u32,
    pub pos: u32,
    pub words: Vec<u32>,
}

/// A bound mt7921dbg IQ slot.
pub struct IqRing {
    slot: u32,
    file: File,
    buf: Vec<u8>,
    chunk: Vec<u8>,
}

impl IqRing {
    /// Route card `phy`'s IQ stream to `slot` (0 or 1) and open its ring. Writing iqbind also empties the ring.
    pub fn bind(slot: u32, phy: &str) -> io::Result<IqRing> {
        fs::write(format!("{MT7921DBG}iqbind"), format!("{slot} {phy}"))?;
        let file = File::open(format!("{MT7921DBG}iq{slot}"))?;
        Ok(IqRing { slot, file, buf: Vec::new(), chunk: vec![0; 8 << 20] })
    }

    /// Unbind the slot: the card's later stream packets are dropped by mt7921dbg.
    pub fn unbind(&self) -> io::Result<()> {
        fs::write(format!("{MT7921DBG}iqbind"), format!("{} -", self.slot))
    }

    /// Read what the ring holds now (never blocks) and append its complete records to `out`. Returns the number of
    /// bytes read.
    pub fn read_records(&mut self, out: &mut Vec<Record>) -> io::Result<usize> {
        let n = self.file.read(&mut self.chunk)?;
        self.buf.extend_from_slice(&self.chunk[..n]);
        let word = |b: &[u8], o: usize| u32::from_le_bytes(b[o..o + 4].try_into().unwrap());
        let mut off = 0;
        while off + 16 <= self.buf.len() {
            let (mark, seq, pos, len) =
                (word(&self.buf, off), word(&self.buf, off + 4), word(&self.buf, off + 8), word(&self.buf, off + 12));
            if mark != REC_MARK {
                self.buf.clear(); // out of step: the caller rebinds (which empties the ring)
                return Err(other(format!("IQ ring {}: bad record marker {mark:#x}", self.slot)));
            }
            let len = len as usize;
            if off + 16 + len > self.buf.len() {
                break; // the rest of this record comes with the next read
            }
            let p = &self.buf[off + 16..off + 16 + len];
            out.push(Record { seq, pos, words: p.chunks_exact(4).map(|c| word(c, 0)).collect() });
            off += 16 + len;
        }
        self.buf.drain(..off);
        Ok(n)
    }

    /// Forget a partial record (after a slice error).
    pub fn reset(&mut self) {
        self.buf.clear();
    }
}

/// The ICAP sample packer (ICAP_CR10 [19:16], notes/09): how ring words carry samples.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Packer(pub u32);

impl Packer {
    /// Samples per second.
    pub fn rate(self) -> f64 {
        match self.0 {
            2 => 10e6,
            8 => 30e6,
            _ => 40e6,
        }
    }

    /// Ring words per sample: packer 2 fills half of each 4-word beat.
    pub fn words_per_sample(self) -> usize {
        if self.0 == 2 {
            2
        } else {
            1
        }
    }

    /// Ring words (beat-aligned) -> samples, in time order (iqrec.words_to_iq). Packer 2 normally puts its pair of
    /// samples in words 0-1 of a beat with words 2-3 zero, but runs of beats (~2%) carry it in words 2-3, and the
    /// beat where it moves holds the same pair in both halves: take the half with data, once per beat.
    pub fn samples(self, words: &[u32], out: &mut Vec<Complex32>) {
        if self.0 == 2 {
            for b in words.chunks_exact(4) {
                let hi = (b[2] != 0 || b[3] != 0) && b[0] == 0 && b[1] == 0;
                let pair = if hi { &b[2..4] } else { &b[0..2] };
                out.extend(pair.iter().map(|&w| word_to_iq(w)));
            }
        } else {
            out.extend(words.iter().map(|&w| word_to_iq(w)));
        }
    }
}

/// One ring word -> a sample: I in bits [29:16], Q in bits [13:0], both 14-bit two's complement.
#[inline]
pub fn word_to_iq(w: u32) -> Complex32 {
    let sext14 = |v: u32| ((v << 18) as i32 >> 18) as f32;
    Complex32::new(sext14(w >> 16 & 0x3FFF), sext14(w & 0x3FFF))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unpack() {
        assert_eq!(word_to_iq(0x1FFF_2000), Complex32::new(8191.0, -8192.0));
        assert_eq!(word_to_iq(0x3FFF_0001), Complex32::new(-1.0, 1.0));
        let mut v = Vec::new();
        // beat 1: pair in words 0-1; beat 2: pair moved to words 2-3
        Packer(2).samples(&[1, 2, 0, 0, 0, 0, 3, 4], &mut v);
        let q: Vec<f32> = v.iter().map(|c| c.im).collect();
        assert_eq!(q, [1.0, 2.0, 3.0, 4.0]);
    }
}
