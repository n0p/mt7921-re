//! nbnode: narrow-band (2.5 / 5 / 10 MHz) IEEE 802.11a/p nodes for MT7921AU cards (std only, no dependencies).
//!
//! The card transmits 5 and 2.5 MHz OFDM itself, but has no demodulator for narrow widths. Its front-end IQ is
//! therefore streamed to the host and demodulated in software. The crate holds the receiver (a port of the Python
//! chain) and the node programs built on it: `nbmesh` (5 / 2.5 MHz mesh node, TUN or TAP, packet FEC), `p11node`
//! (802.11p receiver) and `rxfile` (offline decoder). See README.md.
//!
//! Receiver:
//!
//! | module       | ports                                                                                     |
//! |--------------|-------------------------------------------------------------------------------------------|
//! | [`complex`]  | the complex sample types (numpy complex64 / complex128)                                   |
//! | [`fft`]      | `numpy.fft.fft` / `ifft` for power-of-two sizes                                            |
//! | [`viterbi`]  | `tools/sdr/vit.c` (K=7 soft Viterbi) and `rx80211.depuncture`                              |
//! | [`resample`] | the front end of `tools/guest/nbrxd.py` `decode_segment`: `resample_poly` and the DC high-pass |
//! | [`rx80211`]  | `tools/sdr/rx80211.py`: `decode_all` and `decode_at` with all their helpers                 |
//! | [`iqfile`]   | reading `.cf32` / `.cs16` files, as `rx80211.py`'s command line does                       |
//!
//! The receiver works at the "20 Msps-equivalent" rate, i.e. 64 samples per OFDM FFT period. A 10 MHz (802.11p)
//! capture at 10 Msps, a 5 MHz one at 5 Msps or a 2.5 MHz one at 2.5 Msps decodes unchanged. Captures at other
//! rates go through [`resample::Resampler`] first.
//!
//! Nodes:
//!
//! | module      | role                                                                                          |
//! |-------------|-----------------------------------------------------------------------------------------------|
//! | [`card`]    | every access to the card, the setup steps of tools/guest/nbprep.py, capture slices, TX windows |
//! | [`node`]    | the shared receive engine: slices, overlapping decode blocks, decoder threads, repeat removal  |
//! | [`iqring`]  | mt7921dbg's IQ ring: records and sample unpacking (ICAP packers 2 and 8)                       |
//! | [`frame`]   | 802.11 framing (nbrxd-compatible), the ÷8 inner CRC, radiotap pcap                             |
//! | [`fec`]     | GF(2^8) Cauchy Reed-Solomon packet erasure code                                               |
//! | [`profile`] | the card's PHY coding profiles, airtime, the ÷8 padding model                                 |
//! | [`netdev`]  | TUN / TAP interfaces                                                                          |
//! | [`sys`]     | the few libc calls std lacks (flock, TUNSETIFF, AF_PACKET, signals, CPU affinity)             |

// the doc comments use register bit-field notation ([30:28]) and sample notation (x[n]), which rustdoc would
// otherwise take for links
#![allow(rustdoc::broken_intra_doc_links)]

pub mod complex;
pub mod fft;
pub mod iqfile;
pub mod resample;
pub mod rx80211;
pub mod viterbi;

pub mod card;
pub mod fec;
pub mod frame;
pub mod iqring;
pub mod netdev;
pub mod node;
pub mod profile;
pub mod sys;

pub use complex::{Complex32, Complex64};
pub use rx80211::{decode_all, decode_all_with, DecodeOptions, Frame, Receiver};
