//! Transmit coding profiles: which 802.11 format, MCS and FEC options the card's own transmitter applies to the
//! frames we inject (notes/10 §1). The syntax is that of tools/guest/nbprep.py `--fec`:
//!
//! | profile                                            | card transmits                                        |
//! |----------------------------------------------------|-------------------------------------------------------|
//! | `legacy:R`                                         | 802.11a OFDM at R Mbps (20 MHz timing), BCC           |
//! | `ht:N[+ldpc][+stbc][+sgi]`                         | HT mixed format MCS N; LDPC; STBC over 2 chains; SGI  |
//! | `he:N[+dcm][+ldpc][+gi1.6\|+gi3.2][+ltf2\|+ltf4]`   | HE SU MCS N                                           |
//! | `he-er:N[+dcm][+ldpc][+ru106][+gi..][+ltf..]`      | HE extended-range SU                                  |
//!
//! The card applies the profile through mt7921dbg's TXD overrides (`tx_rate`, `tx_dw6_set`, `tx_dw6_clr`). Our
//! receivers decode legacy only so far, so the other profiles are for SDR measurements until the host receiver
//! learns HT/HE (notes/10 §4).

use std::time::Duration;

/// 802.11 PPDU format of a profile.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Format {
    Legacy,
    Ht,
    HeSu,
    HeEr,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Profile {
    pub format: Format,
    /// Legacy: the rate in Mbps at 20 MHz timing (6, 9, ... 54). HT / HE: the MCS index.
    pub mcs: u32,
    pub ldpc: bool,
    pub stbc: bool,
    pub sgi: bool,
    pub dcm: bool,
    pub ru106: bool,
    /// HE guard interval: 0, 1, 2 = 0.8, 1.6, 3.2 us (at 20 MHz timing; 4x / 8x longer on air at ÷4 / ÷8).
    pub he_gi: u32,
    /// HE-LTF size: 0, 1, 2 = 1x, 2x, 4x.
    pub he_ltf: u32,
}

/// Legacy rate (Mbps at 20 MHz timing) -> L-SIG RATE field, which is also the rate index of the TXD rate code.
const LEGACY_RATE: [(u32, u32); 8] = [(6, 0xB), (9, 0xF), (12, 0xA), (18, 0xE), (24, 0x9), (36, 0xD), (48, 0x8), (54, 0xC)];
/// Data bits per OFDM symbol (NDBPS) of the legacy rates.
const LEGACY_NDBPS: [(u32, u32); 8] = [(6, 24), (9, 36), (12, 48), (18, 72), (24, 96), (36, 144), (48, 192), (54, 216)];
/// HT MCS 0-7, one spatial stream, 20 MHz (52 data tones): NDBPS.
const HT_NDBPS: [u32; 8] = [26, 52, 78, 104, 156, 208, 234, 260];
/// HE MCS 0-11, one spatial stream, 242-tone RU (234 data tones): NDBPS.
const HE_NDBPS: [u32; 12] = [117, 234, 351, 468, 702, 936, 1053, 1170, 1404, 1560, 1755, 1950];

// TXD DW6 rate code (mt76 MT_TX_RATE_*): [5:0] rate index / MCS, [9:6] PHY mode, [12:10] NSS - 1, [13] STBC,
// [4] HE DCM, [5] HE-ER 106-tone RU
const MODE_OFDM: u32 = 1;
const MODE_HT: u32 = 2;
const MODE_HE_SU: u32 = 8;
const MODE_HE_ER: u32 = 9;
const RATE_STBC: u32 = 1 << 13;
const RATE_DCM: u32 = 1 << 4;
const RATE_ER106: u32 = 1 << 5;
// TXD DW6 coding fields: [11] LDPC, [15:14] guard interval, [13:12] HE-LTF size
const DW6_LDPC: u32 = 1 << 11;
const DW6_GI_SHIFT: u32 = 14;
const DW6_LTF_SHIFT: u32 = 12;

/// The ÷8 transmitter ends every PPDU early (notes/10 §5, measured with the X410 on legacy 6 Mbps PPDUs): it stops
/// about 1 ms after the end the MAC computes with its own timing, and the slowest MAC timing there is, 5 MHz
/// (WFSYS_TICKCR0 = 0xa, set by the nodes), runs twice as fast as the ÷8 PHY. Of a PPDU of N symbols about
/// 30 + N / 2 arrive (with the channel switch's TICKCR0 = 0x5 only ~27 + N / 4). The hardware FCS never survives a
/// long frame, so ÷8 senders put their own CRC32 after the body, followed by enough padding that the part which
/// arrives covers it. This is the measured base of "30 + N / 2", minus 2 symbols of margin.
pub const DIV8_KEPT_BASE: usize = 28;
/// Fewest padding symbols after the inner CRC (short frames lose ~3 symbols: notes/09 §4).
pub const DIV8_MIN_PAD_SYMBOLS: usize = 4;

impl Profile {
    pub fn parse(spec: &str) -> Result<Profile, String> {
        let (kind, rest) = spec.split_once(':').unwrap_or((spec, "6"));
        let mut parts = rest.split('+');
        let n: u32 = parts.next().unwrap_or("").parse().map_err(|_| format!("bad rate/MCS in profile {spec:?}"))?;
        let mut p = Profile {
            format: match kind {
                "legacy" => Format::Legacy,
                "ht" => Format::Ht,
                "he" => Format::HeSu,
                "he-er" => Format::HeEr,
                _ => return Err(format!("unknown profile kind {kind:?} (legacy, ht, he, he-er)")),
            },
            mcs: n,
            ldpc: false,
            stbc: false,
            sgi: false,
            dcm: false,
            ru106: false,
            he_gi: 0,
            he_ltf: 0,
        };
        let he = matches!(p.format, Format::HeSu | Format::HeEr);
        for flag in parts {
            match flag {
                "ldpc" if p.format != Format::Legacy => p.ldpc = true,
                "stbc" if p.format == Format::Ht => p.stbc = true,
                "sgi" if p.format == Format::Ht => p.sgi = true,
                "dcm" if he => p.dcm = true,
                "ru106" if p.format == Format::HeEr => p.ru106 = true,
                "gi1.6" if he => p.he_gi = 1,
                "gi3.2" if he => p.he_gi = 2,
                "ltf2" if he => p.he_ltf = 1,
                "ltf4" if he => p.he_ltf = 2,
                _ => return Err(format!("option {flag:?} does not apply to {kind}")),
            }
        }
        let ok = match p.format {
            Format::Legacy => LEGACY_RATE.iter().any(|&(r, _)| r == n),
            Format::Ht => n < 8,
            Format::HeSu => (n as usize) < HE_NDBPS.len(),
            Format::HeEr => n < 3,
        };
        if !ok {
            return Err(format!("rate/MCS {n} is not valid for {kind}"));
        }
        Ok(p)
    }

    /// TXD DW6 rate code (mt7921dbg `tx_rate`).
    pub fn rate_code(&self) -> u32 {
        match self.format {
            Format::Legacy => MODE_OFDM << 6 | LEGACY_RATE.iter().find(|&&(r, _)| r == self.mcs).unwrap().1,
            Format::Ht => MODE_HT << 6 | self.mcs | if self.stbc { RATE_STBC } else { 0 },
            Format::HeSu | Format::HeEr => {
                let mode = if self.format == Format::HeSu { MODE_HE_SU } else { MODE_HE_ER };
                mode << 6
                    | self.mcs
                    | if self.dcm { RATE_DCM } else { 0 }
                    | if self.ru106 { RATE_ER106 } else { 0 }
            }
        }
    }

    /// DW6 bits mt7921dbg sets (`tx_dw6_set`) after the rate code.
    pub fn dw6_set(&self) -> u32 {
        let mut v = if self.ldpc { DW6_LDPC } else { 0 };
        match self.format {
            Format::Ht if self.sgi => v |= 1 << DW6_GI_SHIFT,
            Format::HeSu | Format::HeEr => v |= self.he_gi << DW6_GI_SHIFT | self.he_ltf << DW6_LTF_SHIFT,
            _ => {}
        }
        v
    }

    /// DW6 bits mt7921dbg clears (`tx_dw6_clr`) before setting `dw6_set`.
    pub fn dw6_clr(&self) -> u32 {
        match self.format {
            Format::Legacy => 0,
            Format::Ht => if self.sgi { 3 << DW6_GI_SHIFT } else { 0 },
            Format::HeSu | Format::HeEr => 3 << DW6_GI_SHIFT | 3 << DW6_LTF_SHIFT,
        }
    }

    /// TX chains the profile needs: STBC sends Alamouti pairs over both.
    pub fn tx_streams(&self) -> u32 {
        if self.stbc {
            2
        } else {
            1
        }
    }

    /// Whether our software receivers (tools/sdr/rx80211.py, this crate) can decode the profile's PPDUs.
    pub fn host_decodable(&self) -> bool {
        self.format == Format::Legacy && self.mcs != 36 && self.mcs != 54 // 36 / 54 never decode at ÷4 (notes/10)
    }

    /// Radiotap rate in 500 kbps units. mt7921dbg's `tx_rate` overrides it for every injected frame; it still makes
    /// mac80211 pick an OFDM rate, never a DSSS one.
    pub fn radiotap_rate(&self) -> u8 {
        if self.format == Format::Legacy {
            (self.mcs * 2) as u8
        } else {
            12
        }
    }

    /// Data bits per OFDM symbol.
    pub fn ndbps(&self) -> u32 {
        match self.format {
            Format::Legacy => LEGACY_NDBPS.iter().find(|&&(r, _)| r == self.mcs).unwrap().1,
            Format::Ht => HT_NDBPS[self.mcs as usize],
            Format::HeSu | Format::HeEr => {
                // the 106-tone RU carries 102 data tones instead of 234; DCM sends every bit twice
                let full = HE_NDBPS[self.mcs as usize];
                let ru = if self.ru106 { full * 102 / 234 } else { full };
                if self.dcm {
                    ru / 2
                } else {
                    ru
                }
            }
        }
    }

    /// Duration of one data OFDM symbol at 20 MHz timing, in microseconds.
    pub fn symbol_us(&self) -> f64 {
        match self.format {
            Format::Legacy => 4.0,
            Format::Ht => {
                if self.sgi {
                    3.6
                } else {
                    4.0
                }
            }
            Format::HeSu | Format::HeEr => 12.8 + [0.8, 1.6, 3.2][self.he_gi as usize],
        }
    }

    /// Preamble duration at 20 MHz timing, in microseconds (an upper bound for HE: one HE-LTF of the chosen size
    /// plus the longest packet extension).
    pub fn preamble_us(&self) -> f64 {
        let he_ltf = |gi: f64| [3.2, 6.4, 12.8][self.he_ltf as usize] + gi;
        let gi = [0.8, 1.6, 3.2][self.he_gi as usize];
        match self.format {
            Format::Legacy => 20.0,                                         // L-STF, L-LTF, L-SIG
            Format::Ht => 36.0 + if self.stbc { 4.0 } else { 0.0 },         // + HT-SIG, HT-STF, HT-LTF(s)
            Format::HeSu => 20.0 + 4.0 + 8.0 + 4.0 + he_ltf(gi) + 16.0,     // + RL-SIG, HE-SIG-A, HE-STF, PE
            Format::HeEr => 20.0 + 4.0 + 16.0 + 4.0 + he_ltf(gi) + 16.0,    // HE-SIG-A takes 4 symbols
        }
    }

    /// On-air duration of a PPDU carrying `psdu_len` bytes, with the transmit divider (4 = 5 MHz, 8 = 2.5 MHz).
    pub fn airtime(&self, psdu_len: usize, divider: u32) -> Duration {
        // SERVICE (16 bits) + PSDU + tail (6 bits), in whole symbols
        let bits = 16 + 8 * psdu_len as u64 + 6;
        let nsym = bits.div_ceil(self.ndbps() as u64);
        let us = (self.preamble_us() + nsym as f64 * self.symbol_us()) * divider as f64;
        Duration::from_secs_f64(us * 1e-6)
    }

}

/// Data bits per symbol of a legacy rate (Mbps at 20 MHz timing), as the receiver decodes it.
pub fn legacy_ndbps(mbps: u32) -> Option<usize> {
    LEGACY_NDBPS.iter().find(|&&(r, _)| r == mbps).map(|&(_, n)| n as usize)
}

/// ÷8 padding in bytes after an inner CRC that ends `inner_end` bytes into the PSDU, at `ndbps` data bits per
/// symbol: d symbols reach the end of the inner CRC (SERVICE field included), and a PPDU of N = d + p symbols keeps
/// DIV8_KEPT_BASE + N / 2 of them, so p >= d - 2 DIV8_KEPT_BASE (and at least DIV8_MIN_PAD_SYMBOLS). Long frames
/// therefore take nearly twice their airtime at ÷8.
pub fn div8_pad_bytes(inner_end: usize, ndbps: usize) -> usize {
    let d = (16 + 8 * inner_end).div_ceil(ndbps);
    let p = d.saturating_sub(2 * DIV8_KEPT_BASE).max(DIV8_MIN_PAD_SYMBOLS);
    (p * ndbps).div_ceil(8)
}

/// The receiver's inverse of div8_pad_bytes: where the inner CRC of a received PSDU of `psdu_len` bytes (FCS
/// included) ends, or None if no inner-CRC position gives that length. psdu_len = inner_end + pad + 4 grows strictly
/// with inner_end (by 1 byte, or by 1 + a symbol's bytes), so at most one position fits.
pub fn div8_inner_end(psdu_len: usize, ndbps: usize) -> Option<usize> {
    let mut x = psdu_len.checked_sub(4 + (DIV8_MIN_PAD_SYMBOLS * ndbps).div_ceil(8))?;
    loop {
        let len = x + div8_pad_bytes(x, ndbps) + 4;
        if len == psdu_len {
            return Some(x);
        }
        if len < psdu_len || x == 0 {
            return None;
        }
        x -= 1;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The rate codes and DW6 fields nbprep.py writes for the same profiles (checked on the card, notes/10).
    #[test]
    fn matches_nbprep() {
        let p = Profile::parse("legacy:6").unwrap();
        assert_eq!((p.rate_code(), p.dw6_set(), p.dw6_clr(), p.tx_streams()), (0x4B, 0, 0, 1));
        let p = Profile::parse("ht:0+ldpc+stbc").unwrap();
        assert_eq!((p.rate_code(), p.dw6_set(), p.dw6_clr(), p.tx_streams()), (0x2080, 0x800, 0, 2));
        let p = Profile::parse("ht:7+sgi").unwrap();
        assert_eq!((p.rate_code(), p.dw6_set(), p.dw6_clr()), (0x87, 0x4000, 0xC000));
        let p = Profile::parse("he-er:0+dcm+ldpc+ru106+gi3.2+ltf4").unwrap();
        assert_eq!((p.rate_code(), p.dw6_set(), p.dw6_clr()), (0x270, 0x800 | 2 << 14 | 2 << 12, 0xF000));
        assert!(Profile::parse("legacy:6+ldpc").is_err());
        assert!(Profile::parse("ht:8").is_err());
    }

    #[test]
    fn airtime_and_padding() {
        let p = Profile::parse("legacy:6").unwrap();
        // 100 bytes at 6 Mbps: 20 us + ceil(822 / 24) = 35 symbols of 4 us = 160 us, x4 at 5 MHz
        assert_eq!(p.airtime(100, 4), Duration::from_micros(640));
        // short ÷8 frames: 4 symbols of padding, as nbrxd's fixed div8_pad (12 bytes at 6 Mbps)
        assert_eq!(div8_pad_bytes(100, 24), 12);
        // a 1400-byte inner end at 6 Mbps: d = ceil(11216 / 24) = 468 symbols, p = 412 symbols = 1236 bytes
        assert_eq!(div8_pad_bytes(1400, 24), 1236);
        // the receiver finds the inner CRC again from the length alone, for every length
        for n in [24, 36, 48, 72, 96, 144, 192, 216] {
            for x in 40..1600 {
                let len = x + div8_pad_bytes(x, n) + 4;
                assert_eq!(div8_inner_end(len, n), Some(x), "ndbps {n} inner end {x}");
            }
        }
    }

    /// The padding keeps the inner CRC inside the part of the PPDU that the ÷8 transmitter sends, with the
    /// measured model (30 + N / 2 symbols kept).
    #[test]
    fn div8_padding_covers_the_inner_crc() {
        for x in 40usize..1600 {
            let d = (16 + 8 * x).div_ceil(24);
            let n = (16 + 8 * (x + div8_pad_bytes(x, 24) + 4) + 6).div_ceil(24);
            assert!(30 + n / 2 >= d + 2, "inner end {x}: {d} data symbols, {n} in all");
        }
    }
}
