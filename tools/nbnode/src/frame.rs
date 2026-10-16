//! Frames as the node puts them on the air, and the reverse mapping of received ones.
//!
//! Transmit: radiotap + 802.11 data frame + LLC/SNAP + payload, exactly as tools/guest/nbrxd.py tx_frame builds
//! them, so nbmesh and nbrxd nodes understand each other. addr1 is always broadcast and the real destination travels
//! in addr3: injected unicast frames would be retried despite NOACK, and no ACK can come back at narrow widths.
//! The ÷8 transmitter ends every PPDU early, the more the longer the frame (notes/10 §5), so in that mode the body
//! carries its own CRC32 followed by length-dependent padding, and the receiver checks that CRC instead of the (lost)
//! FCS.

/// LLC/SNAP header in front of the ethertype.
pub const SNAP: [u8; 6] = [0xAA, 0xAA, 0x03, 0x00, 0x00, 0x00];
pub const BROADCAST: [u8; 6] = [0xFF; 6];
/// 802.11 data frame header length (no QoS, ToDS = FromDS = 0).
const HDR_LEN: usize = 24;

/// zlib's CRC-32 (the 802.11 FCS and the ÷8 inner CRC).
pub fn crc32(data: &[u8]) -> u32 {
    static TABLE: std::sync::OnceLock<[u32; 256]> = std::sync::OnceLock::new();
    let t = TABLE.get_or_init(|| {
        let mut t = [0u32; 256];
        for (i, e) in t.iter_mut().enumerate() {
            let mut c = i as u32;
            for _ in 0..8 {
                c = if c & 1 != 0 { 0xEDB8_8320 ^ (c >> 1) } else { c >> 1 };
            }
            *e = c;
        }
        t
    });
    !data.iter().fold(!0u32, |c, &b| t[((c ^ b as u32) & 0xFF) as usize] ^ (c >> 8))
}

/// One frame to inject: radiotap (rate in 500 kbps units, NOACK) + 802.11 data + LLC/SNAP + ethertype + payload,
/// then, at ÷8 (`div8_ndbps` = Some(data bits per symbol of the rate)), the inner CRC32 of the body and the padding
/// of profile::div8_pad_bytes.
pub fn tx_frame(sa: [u8; 6], da: [u8; 6], seq: u16, ethertype: u16, payload: &[u8], rate_500k: u8,
                div8_ndbps: Option<usize>) -> Vec<u8> {
    let mut f = Vec::with_capacity(12 + HDR_LEN + 8 + payload.len() + 4);
    // radiotap: version 0, pad, length 12, present = RATE (bit 2) | TX_FLAGS (bit 15); rate, pad, TX flags NOACK
    f.extend_from_slice(&[0, 0, 12, 0]);
    f.extend_from_slice(&((1u32 << 2) | (1 << 15)).to_le_bytes());
    f.extend_from_slice(&[rate_500k, 0]);
    f.extend_from_slice(&0x0008u16.to_le_bytes());
    // 802.11: frame control 0x0008 (data), duration 0, addr1 broadcast, addr2 source, addr3 destination, sequence
    let body_start = f.len() + HDR_LEN;
    f.extend_from_slice(&0x0008u16.to_le_bytes());
    f.extend_from_slice(&0u16.to_le_bytes());
    f.extend_from_slice(&BROADCAST);
    f.extend_from_slice(&sa);
    f.extend_from_slice(&da);
    f.extend_from_slice(&((seq & 0xFFF) << 4).to_le_bytes());
    f.extend_from_slice(&SNAP);
    f.extend_from_slice(&ethertype.to_be_bytes());
    f.extend_from_slice(payload);
    if let Some(ndbps) = div8_ndbps {
        let c = crc32(&f[body_start..]);
        f.extend_from_slice(&c.to_le_bytes());
        let inner_end = f.len() - 12; // in the PSDU: the radiotap header is not sent
        f.resize(f.len() + crate::profile::div8_pad_bytes(inner_end, ndbps), 0);
    }
    f
}

/// PSDU length on air of a frame from `tx_frame` (radiotap removed, FCS added).
pub fn psdu_len(frame: &[u8]) -> usize {
    frame.len() - 12 + 4
}

/// A received frame whose FCS failed at ÷8: if the sender's inner CRC holds, return the MPDU rebuilt as a normal
/// frame (body up to the inner CRC, then a valid FCS). `mpdu` is the whole decoded PSDU (FCS position included);
/// `ndbps`: data bits per symbol of the decoded rate (profile::legacy_ndbps). The inner CRC's position follows from
/// the length (profile::div8_inner_end).
pub fn div8_unwrap(mpdu: &[u8], ndbps: usize) -> Option<Vec<u8>> {
    let end = crate::profile::div8_inner_end(mpdu.len(), ndbps)?; // end of the inner CRC
    if end < HDR_LEN + 8 + 4 {
        return None;
    }
    let inner = u32::from_le_bytes(mpdu[end - 4..end].try_into().unwrap());
    if inner != crc32(&mpdu[HDR_LEN..end - 4]) {
        return None;
    }
    let mut core = mpdu[..end - 4].to_vec();
    let fcs = crc32(&core);
    core.extend_from_slice(&fcs.to_le_bytes());
    Some(core)
}

/// The fields of a received 802.11 data frame that the node uses.
#[derive(Clone, Debug, PartialEq)]
pub struct RxData {
    pub sa: [u8; 6],
    /// Destination: addr3 when addr1 is broadcast and addr3 unicast (our framing), else addr1.
    pub da: [u8; 6],
    pub seq: u16,
    pub ethertype: u16,
    pub payload: Vec<u8>,
}

/// An 802.11 data frame (with its FCS at the end, already checked) with LLC/SNAP -> its fields. Other frames
/// (management, control, frames through a DS, other encapsulations) give None.
pub fn parse_data(mpdu: &[u8]) -> Option<RxData> {
    if mpdu.len() < HDR_LEN + 8 + 4 {
        return None;
    }
    let fc = u16::from_le_bytes([mpdu[0], mpdu[1]]);
    if (fc >> 2) & 3 != 2 || (fc >> 8) & 3 != 0 {
        return None; // not a data frame, or ToDS / FromDS set
    }
    let hdr = HDR_LEN + if (fc >> 4) & 0xF == 8 { 2 } else { 0 }; // QoS data adds 2 bytes
    let body = mpdu.get(hdr..mpdu.len() - 4)?;
    if body.len() < 8 || body[..6] != SNAP {
        return None;
    }
    let addr = |o: usize| -> [u8; 6] { mpdu[o..o + 6].try_into().unwrap() };
    let (a1, sa, a3) = (addr(4), addr(10), addr(16));
    let da = if a1 == BROADCAST && a3 != BROADCAST { a3 } else { a1 };
    Some(RxData {
        sa,
        da,
        seq: u16::from_le_bytes([mpdu[22], mpdu[23]]) >> 4,
        ethertype: u16::from_be_bytes([body[6], body[7]]),
        payload: body[8..].to_vec(),
    })
}

/// Radiotap pcap writer for decoded frames (LINKTYPE_IEEE802_11_RADIOTAP), as nbrxd's --pcap.
pub struct Pcap {
    f: std::io::BufWriter<std::fs::File>,
    mhz: u16,
    width: f64,
}

impl Pcap {
    pub fn create(path: &str, mhz: u32, width: f64) -> std::io::Result<Pcap> {
        use std::io::Write;
        let mut f = std::io::BufWriter::new(std::fs::File::create(path)?);
        // magic, version 2.4, zone 0, sigfigs 0, snaplen 65535, link type 127
        f.write_all(&0xA1B2_C3D4u32.to_le_bytes())?;
        f.write_all(&2u16.to_le_bytes())?;
        f.write_all(&4u16.to_le_bytes())?;
        for v in [0u32, 0, 65535, 127] {
            f.write_all(&v.to_le_bytes())?;
        }
        f.flush()?;
        Ok(Pcap { f, mhz: mhz as u16, width })
    }

    /// One frame: radiotap FLAGS (FCS at end), RATE (the on-air rate), CHANNEL (+ half / quarter-rate flag).
    pub fn write(&mut self, mpdu: &[u8], rate_mbps_20: f64) -> std::io::Result<()> {
        use std::io::Write;
        let rate = (rate_mbps_20 * 2.0 * self.width / 20.0) as u8;
        let chflags: u16 = (if self.mhz < 3000 { 0x0080 } else { 0x0100 })
            | 0x0040
            | if self.width == 10.0 { 0x4000 } else if self.width == 5.0 { 0x8000 } else { 0 };
        let mut rt = vec![0u8, 0, 14, 0];
        rt.extend_from_slice(&((1u32 << 1) | (1 << 2) | (1 << 3)).to_le_bytes());
        rt.extend_from_slice(&[0x10, rate]);
        rt.extend_from_slice(&self.mhz.to_le_bytes());
        rt.extend_from_slice(&chflags.to_le_bytes());
        let t = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default();
        let len = (rt.len() + mpdu.len()) as u32;
        for v in [t.as_secs() as u32, t.subsec_micros(), len, len] {
            self.f.write_all(&v.to_le_bytes())?;
        }
        self.f.write_all(&rt)?;
        self.f.write_all(mpdu)?;
        self.f.flush()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc_matches_zlib() {
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
    }

    /// A ÷8 frame as transmitted, with its last bytes (FCS and part of the padding) lost, still unwraps.
    #[test]
    fn div8_roundtrip() {
        let f = tx_frame([2, 0, 0, 0, 0, 1], [2, 0, 0, 0, 0, 2], 7, 0x0800, b"hello world", 12, Some(24));
        let mut mpdu = f[12..].to_vec();
        mpdu.extend_from_slice(&[0xEE; 4]); // the FCS position, garbage
        for b in mpdu.iter_mut().rev().take(10) {
            *b ^= 0x5A; // the lost symbols
        }
        let core = div8_unwrap(&mpdu, 24).expect("inner CRC");
        let r = parse_data(&core).unwrap();
        assert_eq!((r.sa, r.da, r.seq, r.ethertype, &r.payload[..]), ([2, 0, 0, 0, 0, 1], [2, 0, 0, 0, 0, 2], 7, 0x0800, &b"hello world"[..]));
        assert_eq!(crc32(&core[..core.len() - 4]).to_le_bytes(), core[core.len() - 4..]);
        // a long frame: most of its padding is lost
        let payload = vec![0x5Au8; 900];
        let f = tx_frame([2, 0, 0, 0, 0, 1], BROADCAST, 8, 0x0800, &payload, 12, Some(24));
        let mut mpdu = f[12..].to_vec();
        mpdu.extend_from_slice(&[0; 4]);
        let keep = 24 + 8 + 900 + 4 + 2; // the inner CRC and 2 bytes of padding arrive
        for b in mpdu[keep..].iter_mut() {
            *b = 0xA5;
        }
        assert_eq!(parse_data(&div8_unwrap(&mpdu, 24).expect("inner CRC")).unwrap().payload, payload);
    }
}
