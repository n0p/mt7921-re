//! The receive half shared by nbmesh and p11node: capture slices on the card, the IQ ring, decoding in worker
//! threads, and the half-duplex rules that keep the card alive. A port of tools/guest/nbrxd.py's `one_slice` and
//! `Decoder` for the continuous (ungated) packers.
//!
//! The card cannot demodulate narrow frames, so its receive IQ is streamed to the host in capture slices: the
//! firmware hook hk_iqstream runs on the MCU for up to `slice_ms`, and while it runs the MCU serves no commands (the
//! capture ring overwrites the packet SRAM that carries them). Rules learnt from card wedges (notes/09):
//! - during a slice, only host-path register accesses; the stop request is a single host write;
//! - poll the hook's state (a host read of packet SRAM) only after the slice's first 50 ms and once the stream has
//!   been quiet for 30 ms, i.e. once the hook has stopped streaming;
//! - transmit only between slices (`Card::tx_window`).
//!
//! The stream is cut into blocks of `block_ms` that overlap by more than the longest frame, so that every frame lies
//! whole in at least one block; worker threads decode the blocks, and a frame found twice is reported once, by its
//! absolute ring position ([`Dedupe`]).

use std::collections::VecDeque;
use std::io;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, SyncSender, TrySendError};
use std::sync::{Arc, Mutex};
use std::thread::{self, sleep};
use std::time::{Duration, Instant};

use crate::card::{Card, Hooks};
use crate::frame;
use crate::iqring::{IqRing, Packer, Record};
use crate::profile;
use crate::resample;
use crate::rx80211::{DecodeOptions, Frame, Receiver as OfdmReceiver};
use crate::sys;

/// Receive settings.
#[derive(Clone, Debug)]
pub struct RxConfig {
    /// Channel width of the frames in MHz (10 = 802.11p, 5 = ÷4, 2.5 = ÷8); the decoder runs at width Msps.
    pub width: f64,
    pub pack: Packer,
    /// Decode block length.
    pub block_ms: f64,
    /// Block overlap: longer than the longest frame.
    pub overlap_ms: f64,
    /// Decoder threads.
    pub workers: usize,
    pub opts: DecodeOptions,
}

/// A decoded frame, good or bad FCS (at ÷8 a frame whose inner CRC holds counts as good). Overlapping blocks can
/// decode a frame twice: the consumer drops repeats with [`Dedupe`].
pub struct RxFrame {
    /// Absolute ring position of the frame's start (ring words, mod 2^32).
    pub pos: u32,
    pub frame: Frame,
    /// Block cut -> frame decoded.
    pub latency: Duration,
}

#[derive(Default)]
pub struct RxStats {
    pub blocks: AtomicU64,
    /// Blocks dropped because the decoders were behind (the CPU is too slow for the stream).
    pub dropped_blocks: AtomicU64,
    /// Decoder results, repeats from block overlaps included.
    pub decodes: AtomicU64,
}

/// How a slice ended.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SliceEnd {
    /// It ran its full duration (or the firmware ended it).
    Full,
    /// We ended it early to transmit.
    ForTx,
    /// We ended it for shutdown.
    Stopped,
}

struct Block {
    pos: u32,
    words: Vec<u32>,
    cut: Instant,
}

/// Assembles contiguous ring words into blocks (nbrxd Decoder.add / flush).
struct Blocker {
    pos: u32,
    words: Vec<u32>,
    block_words: usize,
    overlap_words: usize,
}

impl Blocker {
    /// Words that continue the current block, or a new block after a gap in positions (the firmware skips ahead
    /// on a ring overrun). Returns the blocks to decode.
    fn add(&mut self, pos: u32, words: &[u32], out: &mut Vec<Block>) {
        if !self.words.is_empty() && pos != self.pos.wrapping_add(self.words.len() as u32) {
            self.flush(0, out);
        }
        if self.words.is_empty() {
            self.pos = pos;
        }
        self.words.extend_from_slice(words);
        if self.words.len() >= self.block_words {
            self.flush(self.overlap_words, out);
        }
    }

    /// Close the current block; the next one starts with its last `keep` words.
    fn flush(&mut self, keep: usize, out: &mut Vec<Block>) {
        if self.words.is_empty() {
            return;
        }
        let tail = if keep > 0 && self.words.len() > keep { self.words[self.words.len() - keep..].to_vec() } else { Vec::new() };
        let next_pos = self.pos.wrapping_add((self.words.len() - tail.len()) as u32);
        let words = std::mem::replace(&mut self.words, tail);
        out.push(Block { pos: self.pos, words, cut: Instant::now() });
        self.pos = next_pos;
    }
}

/// Decode one block: ring words -> samples -> the receiver's front end (mean removal, resampling to the width's
/// rate, DC high-pass) -> OFDM frames. At ÷8 a frame whose FCS was lost with the PPDU's last symbols is checked
/// against the sender's inner CRC instead.
fn decode_block(rx: &OfdmReceiver, cfg: &RxConfig, words: &[u32]) -> Vec<Frame> {
    let mut x = Vec::with_capacity(words.len());
    cfg.pack.samples(words, &mut x);
    let y = resample::front_end(&x, cfg.pack.rate() / 1e6, cfg.width);
    if y.len() < 400 {
        return Vec::new();
    }
    let mut frames = rx.decode_all(&y, &cfg.opts);
    if cfg.width == 2.5 {
        for f in frames.iter_mut().filter(|f| !f.fcs_ok) {
            if let Some(m) = profile::legacy_ndbps(f.rate).and_then(|n| frame::div8_unwrap(&f.mpdu, n)) {
                f.mpdu = m;
                f.fcs_ok = true;
            }
        }
    }
    frames
}

/// The receive engine of one card.
pub struct Rx {
    ring: IqRing,
    slot: u32,
    phy: String,
    records: Vec<Record>,
    blocker: Blocker,
    blocks_tx: SyncSender<Block>,
    pending: Vec<Block>,
    pub stats: Arc<RxStats>,
    /// Ring words per decoded sample (for frame positions).
    pub words_per_decoded_sample: f64,
}

impl Rx {
    /// Bind the card's IQ stream to `slot` and start the decoder threads. Decoded frames arrive on the returned
    /// channel.
    pub fn start(card: &Card, slot: u32, cfg: RxConfig) -> io::Result<(Rx, Receiver<RxFrame>)> {
        let ring = IqRing::bind(slot, &card.phy)?;
        let fs = cfg.pack.rate();
        let wps = cfg.pack.words_per_sample();
        let whole_beats = |w: usize| w / 4 * 4;
        let blocker = Blocker {
            pos: 0,
            words: Vec::new(),
            block_words: whole_beats((cfg.block_ms * 1e-3 * fs) as usize * wps),
            overlap_words: whole_beats((cfg.overlap_ms * 1e-3 * fs) as usize * wps),
        };
        let words_per_decoded_sample = fs / (cfg.width * 1e6) * wps as f64;
        // the queue toward the decoders holds two blocks per worker: beyond that the CPU is not keeping up, and
        // blocks are dropped (counted) instead of stalling the ring reader
        let (blocks_tx, blocks_rx) = mpsc::sync_channel::<Block>(2 * cfg.workers);
        let blocks_rx = Arc::new(Mutex::new(blocks_rx));
        let (frames_tx, frames_rx) = mpsc::channel::<RxFrame>();
        let stats = Arc::new(RxStats::default());
        for _ in 0..cfg.workers {
            let (blocks_rx, frames_tx, stats, cfg) = (blocks_rx.clone(), frames_tx.clone(), stats.clone(), cfg.clone());
            thread::spawn(move || {
                let rx = OfdmReceiver::new();
                loop {
                    let blk = match blocks_rx.lock().unwrap().recv() {
                        Ok(b) => b,
                        Err(_) => return, // the engine is gone
                    };
                    for f in decode_block(&rx, &cfg, &blk.words) {
                        stats.decodes.fetch_add(1, Ordering::Relaxed);
                        let pos = blk.pos.wrapping_add((f.start as f64 * words_per_decoded_sample).round() as u32);
                        let _ = frames_tx.send(RxFrame { pos, frame: f, latency: blk.cut.elapsed() });
                    }
                }
            });
        }
        let rx = Rx {
            ring,
            slot,
            phy: card.phy.clone(),
            records: Vec::new(),
            blocker,
            blocks_tx,
            pending: Vec::new(),
            stats,
            words_per_decoded_sample,
        };
        Ok((rx, frames_rx))
    }

    /// Read what the ring holds and pass the blocks it completes to the decoders. Returns the bytes read.
    fn pump(&mut self) -> io::Result<usize> {
        let n = self.ring.read_records(&mut self.records)?;
        for r in self.records.drain(..) {
            self.blocker.add(r.pos, &r.words, &mut self.pending);
        }
        self.send_pending();
        Ok(n)
    }

    fn send_pending(&mut self) {
        for b in self.pending.drain(..) {
            self.stats.blocks.fetch_add(1, Ordering::Relaxed);
            if let Err(TrySendError::Full(_)) = self.blocks_tx.try_send(b) {
                self.stats.dropped_blocks.fetch_add(1, Ordering::Relaxed);
            }
        }
    }

    /// One capture slice, read and decoded until it ends. `tx_pending` is polled during the slice: once it says
    /// yes and the slice has run `min_rx`, the slice is ended early so that the caller can transmit.
    pub fn slice(&mut self, card: &Card, hooks: &Hooks, slice_ms: u32, min_rx: Duration,
                 tx_pending: &dyn Fn() -> bool) -> io::Result<SliceEnd> {
        card.start_slice(hooks, slice_ms)?;
        let t_start = Instant::now();
        let mut t_poll = t_start;
        let mut t_data = t_start;
        let mut end = SliceEnd::Full;
        let mut t_stop = t_start;
        loop {
            let now = Instant::now();
            let stopping = end != SliceEnd::Full;
            // "quiet": no stream data for 30 ms since the last data and since our stop request
            let quiet = now.duration_since(t_data.max(t_stop)) > Duration::from_millis(30);
            let poll_every = Duration::from_millis(if stopping { 4 } else { 20 });
            if now - t_start > Duration::from_millis(50) && now - t_poll > poll_every {
                t_poll = now;
                if !stopping && (sys::stop_requested() || (now - t_start > min_rx && tx_pending())) {
                    // end the slice early: one host-path write of iq_cfg.stop
                    card.stop_slice(hooks)?;
                    end = if sys::stop_requested() { SliceEnd::Stopped } else { SliceEnd::ForTx };
                    t_stop = now;
                    continue;
                }
                // host-path read of iq_stat.state, only once the stream is quiet
                if quiet && !card.slice_running(hooks)? {
                    self.pump()?; // the records that arrived with the final poll
                    break;
                }
            }
            if self.pump()? == 0 {
                sleep(Duration::from_millis(2));
                continue;
            }
            t_data = now;
        }
        // a slice boundary is a gap in the stream: decode what is left
        self.blocker.flush(0, &mut self.pending);
        self.send_pending();
        Ok(end)
    }

    /// After an error: make sure no slice is left running (host-path stop request, then polls of the hook's state
    /// for up to 2 s), and empty the ring by binding the slot again, to `card` (which may be the same card
    /// re-attached under a new phy after the kernel reset it).
    pub fn recover(&mut self, card: &Card, hooks: &Hooks) {
        let _ = card.stop_slice(hooks);
        sleep(Duration::from_millis(300)); // let the hook finish its teardown before the first host read
        let t = Instant::now();
        while card.slice_running(hooks).unwrap_or(false) && t.elapsed() < Duration::from_secs(2) {
            sleep(Duration::from_millis(50));
        }
        self.blocker.words.clear();
        self.ring.reset();
        self.phy = card.phy.clone();
        if let Ok(r) = IqRing::bind(self.slot, &self.phy) {
            self.ring = r;
        }
    }

    /// Unbind the IQ slot.
    pub fn unbind(&self) {
        let _ = self.ring.unbind();
    }
}

/// Reports a frame once although overlapping blocks decode it twice: positions within `tolerance` ring words of a
/// recent one are repeats.
pub struct Dedupe {
    recent: VecDeque<u32>,
    tolerance: u32,
}

impl Dedupe {
    /// `tolerance`: 400 decoded samples (a preamble's worth), in ring words.
    pub fn new(words_per_decoded_sample: f64) -> Dedupe {
        Dedupe { recent: VecDeque::with_capacity(256), tolerance: (400.0 * words_per_decoded_sample) as u32 }
    }

    pub fn first_time(&mut self, pos: u32) -> bool {
        // distance on the 2^32 circle
        if self.recent.iter().any(|&q| pos.wrapping_sub(q).min(q.wrapping_sub(pos)) < self.tolerance) {
            return false;
        }
        if self.recent.len() == 256 {
            self.recent.pop_front();
        }
        self.recent.push_back(pos);
        true
    }
}

/// Simple xorshift generator for timing jitter (std has no random numbers).
pub struct Jitter(u64);

impl Jitter {
    pub fn new(seed: u64) -> Jitter {
        let t = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default();
        Jitter(seed ^ t.as_nanos() as u64 | 1)
    }

    /// Uniform in [0, max).
    pub fn below(&mut self, max: Duration) -> Duration {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        max.mul_f64((self.0 >> 11) as f64 / (1u64 << 53) as f64)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blocks_overlap_and_split_on_gaps() {
        let mut b = Blocker { pos: 0, words: Vec::new(), block_words: 8, overlap_words: 4 };
        let mut out = Vec::new();
        b.add(100, &[1; 6], &mut out);
        assert!(out.is_empty());
        b.add(106, &[2; 6], &mut out); // 12 words: one block of 12, the next starts with its last 4
        assert_eq!((out.len(), out[0].pos, out[0].words.len()), (1, 100, 12));
        assert_eq!((b.pos, b.words.len()), (108, 4));
        b.add(500, &[3; 2], &mut out); // a gap: the 4 carried words are flushed as a block, a new one starts
        assert_eq!((out.len(), out[1].pos, out[1].words.len(), b.pos), (2, 108, 4, 500));
    }

    #[test]
    fn dedupe_wraps() {
        let mut d = Dedupe::new(2.0); // tolerance 800 words
        assert!(d.first_time(u32::MAX - 100));
        assert!(!d.first_time(300)); // 401 words later, across the wrap
        assert!(d.first_time(5000));
    }
}
