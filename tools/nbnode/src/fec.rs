//! Packet-level erasure code (notes/10 §4): systematic Reed-Solomon over GF(2^8) with a Cauchy generator.
//!
//! The card can apply its PHY FEC (BCC/LDPC) to each frame, but a frame lost to fading, a collision or the
//! half-duplex turnaround is lost whole, and at narrow widths no MAC acknowledgement or retransmission exists (the
//! receiving card cannot decode narrow frames). So the sender groups up to k packets into a block and adds m parity
//! packets. Any k of the k + m frames of a block rebuild all of it, and a data frame that arrives is delivered at
//! once (systematic code: no added latency when nothing is lost).
//!
//! On the air, every frame of a block is an 802.11 data frame with ethertype [`ETHERTYPE`] and this body:
//!
//! | bytes | field                                                                      |
//! |-------|----------------------------------------------------------------------------|
//! | 1     | magic 0x4E ('N')                                                           |
//! | 1     | version 1                                                                  |
//! | 2     | block id, little endian (per sender, incrementing)                         |
//! | 1     | shard index: 0..k-1 data, k..k+m-1 parity                                  |
//! | 1     | k, data shards in this block (1..=255 - m)                                 |
//! | 1     | m, parity shards in this block                                             |
//! | 1     | reserved, 0                                                                |
//! | ...   | data shard: unit = u16 ethertype (BE), u16 payload length (LE), payload    |
//! |       | parity shard: the RS parity of the block's units, each zero-padded to the longest |
//!
//! Data shards travel unpadded; the receiver pads them with zeros to the parity length before decoding.

use std::collections::HashMap;
use std::time::{Duration, Instant};

/// IEEE 802 local experimental ethertype 1: nbmesh FEC shards.
pub const ETHERTYPE: u16 = 0x88B5;
const MAGIC: u8 = 0x4E;
const VERSION: u8 = 1;
pub const HEADER_LEN: usize = 8;
/// Data unit header: ethertype + payload length.
const UNIT_HDR: usize = 4;

/// GF(2^8) arithmetic with the primitive polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11d) and generator 2.
struct Gf {
    exp: [u8; 512],
    log: [u8; 256],
}

impl Gf {
    fn get() -> &'static Gf {
        static GF: std::sync::OnceLock<Gf> = std::sync::OnceLock::new();
        GF.get_or_init(|| {
            let mut g = Gf { exp: [0; 512], log: [0; 256] };
            let mut x: u16 = 1;
            for i in 0..255 {
                g.exp[i] = x as u8;
                g.log[x as usize] = i as u8;
                x <<= 1;
                if x & 0x100 != 0 {
                    x ^= 0x11D;
                }
            }
            for i in 255..512 {
                g.exp[i] = g.exp[i - 255]; // so that exp[log a + log b] needs no modulo
            }
            g
        })
    }

    fn mul(&self, a: u8, b: u8) -> u8 {
        if a == 0 || b == 0 {
            0
        } else {
            self.exp[self.log[a as usize] as usize + self.log[b as usize] as usize]
        }
    }

    fn inv(&self, a: u8) -> u8 {
        assert!(a != 0);
        self.exp[255 - self.log[a as usize] as usize]
    }

    /// dst ^= c * src, byte by byte.
    fn mul_add(&self, dst: &mut [u8], c: u8, src: &[u8]) {
        if c == 0 {
            return;
        }
        let lc = self.log[c as usize] as usize;
        for (d, &s) in dst.iter_mut().zip(src) {
            if s != 0 {
                *d ^= self.exp[lc + self.log[s as usize] as usize];
            }
        }
    }
}

/// Generator row of shard `index` in a block of k data shards: a unit row for data shards, and for parity shard
/// i = index - k the Cauchy row 1 / (x_i + y_j) with x_i = k + i, y_j = j. The x and y values are all distinct, so
/// every square submatrix of the Cauchy part is invertible and any k rows of the generator are independent.
fn generator_row(gf: &Gf, k: usize, index: usize) -> Vec<u8> {
    if index < k {
        let mut r = vec![0; k];
        r[index] = 1;
        r
    } else {
        (0..k).map(|j| gf.inv((index as u8) ^ (j as u8))).collect()
    }
}

/// Parity shards for the given data units (each zero-padded to the longest).
pub fn encode(units: &[Vec<u8>], m: usize) -> Vec<Vec<u8>> {
    let gf = Gf::get();
    let k = units.len();
    assert!(k >= 1 && k + m <= 256);
    let len = units.iter().map(|u| u.len()).max().unwrap_or(0);
    (k..k + m)
        .map(|index| {
            let row = generator_row(gf, k, index);
            let mut p = vec![0u8; len];
            for (c, u) in row.iter().zip(units) {
                gf.mul_add(&mut p, *c, u);
            }
            p
        })
        .collect()
}

/// Rebuild the missing data units of a block from any k shards: `shards[index]` holds the shards received (data
/// units unpadded, parity at full length). Returns the k data units, padded, or None with fewer than k shards.
pub fn decode(k: usize, shards: &[Option<Vec<u8>>]) -> Option<Vec<Vec<u8>>> {
    let gf = Gf::get();
    let have: Vec<usize> = (0..shards.len()).filter(|&i| shards[i].is_some()).take(k).collect();
    if have.len() < k {
        return None;
    }
    let len = shards.iter().flatten().map(|s| s.len()).max().unwrap();
    let padded = |i: usize| {
        let mut v = shards[i].clone().unwrap();
        v.resize(len, 0);
        v
    };
    if have.iter().all(|&i| i < k) {
        return Some(have.iter().map(|&i| padded(i)).collect()); // every data shard arrived
    }
    // invert the k x k matrix of the received shards' generator rows (Gauss-Jordan elimination over GF(2^8))
    let mut a: Vec<Vec<u8>> = have.iter().map(|&i| generator_row(gf, k, i)).collect();
    let mut inv: Vec<Vec<u8>> = (0..k).map(|i| generator_row(gf, k, i)).collect(); // identity
    for col in 0..k {
        let piv = (col..k).find(|&r| a[r][col] != 0)?; // never fails for an MDS code
        a.swap(col, piv);
        inv.swap(col, piv);
        let s = gf.inv(a[col][col]);
        for j in 0..k {
            a[col][j] = gf.mul(a[col][j], s);
            inv[col][j] = gf.mul(inv[col][j], s);
        }
        for r in 0..k {
            let f = a[r][col];
            if r != col && f != 0 {
                for j in 0..k {
                    a[r][j] ^= gf.mul(f, a[col][j]);
                    inv[r][j] ^= gf.mul(f, inv[col][j]);
                }
            }
        }
    }
    // data unit j = sum over the received shards r of inv[j][r] * shard r (only the missing units are computed)
    let rx: Vec<Vec<u8>> = have.iter().map(|&i| padded(i)).collect();
    Some(
        (0..k)
            .map(|j| match &shards[j] {
                Some(_) => padded(j),
                None => {
                    let mut u = vec![0u8; len];
                    for (r, s) in rx.iter().enumerate() {
                        gf.mul_add(&mut u, inv[j][r], s);
                    }
                    u
                }
            })
            .collect(),
    )
}

/// A data unit: the packet's ethertype and length in front of it, so that a rebuilt (zero-padded) unit can be cut
/// back to the packet.
pub fn unit(ethertype: u16, payload: &[u8]) -> Vec<u8> {
    let mut u = Vec::with_capacity(UNIT_HDR + payload.len());
    u.extend_from_slice(&ethertype.to_be_bytes());
    u.extend_from_slice(&(payload.len() as u16).to_le_bytes());
    u.extend_from_slice(payload);
    u
}

/// A unit (possibly zero-padded) -> (ethertype, payload).
pub fn ununit(u: &[u8]) -> Option<(u16, Vec<u8>)> {
    if u.len() < UNIT_HDR {
        return None;
    }
    let n = u16::from_le_bytes([u[2], u[3]]) as usize;
    let payload = u.get(UNIT_HDR..UNIT_HDR + n)?;
    Some((u16::from_be_bytes([u[0], u[1]]), payload.to_vec()))
}

/// Sender side: packets -> the bodies of the frames of one or more blocks.
pub struct Encoder {
    k: usize,
    m: usize,
    next_block: u16,
}

impl Encoder {
    pub fn new(k: usize, m: usize) -> Encoder {
        assert!(k >= 1 && k + m <= 255);
        Encoder { k, m, next_block: 0 }
    }

    /// Split `packets` (ethertype, payload) into blocks of at most k and return every frame body (shard header +
    /// shard), data shards first within each block. A short block of j < k packets gets ceil(m * j / k) parity
    /// shards, so the overhead stays at m / k (a lone packet with m > 0 is sent twice).
    pub fn encode(&mut self, packets: &[(u16, Vec<u8>)]) -> Vec<Vec<u8>> {
        let mut out = Vec::new();
        for block in packets.chunks(self.k) {
            let j = block.len();
            let m = (self.m * j).div_ceil(self.k);
            let units: Vec<Vec<u8>> = block.iter().map(|(et, p)| unit(*et, p)).collect();
            let parity = encode(&units, m);
            let id = self.next_block;
            self.next_block = self.next_block.wrapping_add(1);
            for (index, shard) in units.iter().chain(parity.iter()).enumerate() {
                let mut b = Vec::with_capacity(HEADER_LEN + shard.len());
                b.extend_from_slice(&[MAGIC, VERSION]);
                b.extend_from_slice(&id.to_le_bytes());
                b.extend_from_slice(&[index as u8, j as u8, m as u8, 0]);
                b.extend_from_slice(shard);
                out.push(b);
            }
        }
        out
    }
}

struct Block {
    k: usize,
    m: usize,
    created: Instant,
    shards: Vec<Option<Vec<u8>>>,
    delivered: Vec<bool>,
}

/// Receiver side: shard frames from all senders -> packets, each delivered once.
pub struct Decoder {
    blocks: HashMap<([u8; 6], u16), Block>,
    /// Blocks are forgotten after this long (a block id comes back after 65536 blocks).
    pub max_age: Duration,
    pub recovered: u64,
}

impl Default for Decoder {
    fn default() -> Self {
        Decoder { blocks: HashMap::new(), max_age: Duration::from_secs(5), recovered: 0 }
    }
}

impl Decoder {
    /// One received shard body from sender `sa` -> the packets (ethertype, payload) it makes deliverable: itself if
    /// it is a new data shard, plus every data unit it lets the decoder rebuild.
    pub fn receive(&mut self, sa: [u8; 6], body: &[u8]) -> Vec<(u16, Vec<u8>)> {
        let now = Instant::now();
        let max_age = self.max_age;
        self.blocks.retain(|_, b| now - b.created < max_age);
        if body.len() < HEADER_LEN || body[0] != MAGIC || body[1] != VERSION {
            return Vec::new();
        }
        let id = u16::from_le_bytes([body[2], body[3]]);
        let (index, k, m) = (body[4] as usize, body[5] as usize, body[6] as usize);
        if k == 0 || index >= k + m {
            return Vec::new();
        }
        let shard = body[HEADER_LEN..].to_vec();
        let b = self.blocks.entry((sa, id)).or_insert_with(|| Block {
            k,
            m,
            created: now,
            shards: vec![None; k + m],
            delivered: vec![false; k],
        });
        if b.k != k || b.m != m || b.shards[index].is_some() {
            return Vec::new(); // a stale block with a reused id, or a repeat
        }
        let mut out = Vec::new();
        if index < k {
            out.extend(ununit(&shard));
            b.delivered[index] = true;
        }
        b.shards[index] = Some(shard);
        let missing = b.delivered.iter().any(|d| !d);
        if missing && b.shards.iter().flatten().count() >= k {
            if let Some(units) = decode(k, &b.shards) {
                for (j, u) in units.iter().enumerate() {
                    if !b.delivered[j] {
                        b.delivered[j] = true;
                        if let Some(p) = ununit(u) {
                            out.push(p);
                            self.recovered += 1;
                        }
                    }
                }
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn packets(n: usize) -> Vec<(u16, Vec<u8>)> {
        (0..n).map(|i| (0x0800, (0..(40 + 37 * i)).map(|b| (b * 7 + i) as u8).collect())).collect()
    }

    /// Every way of losing at most m frames of a block still delivers every packet, exactly once.
    #[test]
    fn any_k_of_n() {
        let (k, m) = (4, 2);
        let pk = packets(k);
        let frames = Encoder::new(k, m).encode(&pk);
        assert_eq!(frames.len(), k + m);
        for lost in 0u32..(1 << (k + m)) {
            if lost.count_ones() as usize > m {
                continue;
            }
            let mut d = Decoder::default();
            let mut got = Vec::new();
            for (i, f) in frames.iter().enumerate() {
                if lost & (1 << i) == 0 {
                    got.extend(d.receive([2, 0, 0, 0, 0, 9], f));
                }
            }
            got.sort();
            let mut want = pk.clone();
            want.sort();
            assert_eq!(got, want, "lost mask {lost:#b}");
        }
    }

    #[test]
    fn short_blocks_and_too_many_losses() {
        let mut e = Encoder::new(4, 2);
        // 5 packets: a block of 4 (+2 parity) and a block of 1 (+1 parity, i.e. sent twice)
        let frames = e.encode(&packets(5));
        assert_eq!(frames.len(), 6 + 2);
        let mut d = Decoder::default();
        // the lone packet's data frame is lost, its parity copy rebuilds it
        let got = d.receive([2, 0, 0, 0, 0, 9], &frames[7]);
        assert_eq!(got, vec![packets(5)[4].clone()]);
        // three of the first block's six frames lost: only the data frames that arrived come out
        let mut d = Decoder::default();
        let got: Vec<_> = [0usize, 4, 5].iter().flat_map(|&i| d.receive([2, 0, 0, 0, 0, 9], &frames[i])).collect();
        assert_eq!(got.len(), 1);
    }
}
