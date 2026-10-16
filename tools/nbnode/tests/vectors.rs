//! Integration tests on the vectors in testdata/ (written by testdata/gen.py; see testdata/manifest.txt).
//!
//! Two kinds of checks:
//! - against the truth (`<name>.truth`, what was transmitted): frames found, FCS, payload bytes, CFO;
//! - against the Python receiver (`<name>.<mode>.pyref`, what tools/sdr/rx80211.decode_all found in the same
//!   samples): the same list of frames (start, rate, length, FCS verdict, PSDU of the good ones), SNR and CFO to
//!   within round-off. The settings come from the file's `options` line, so both sides run the same configuration.
//!
//! The throughput benchmark is `#[ignore]`d: `cargo test --release -- --ignored --nocapture bench`.

use nbnode::iqfile::read_cf32;
use nbnode::resample::{front_end, Resampler};
use nbnode::viterbi::viterbi;
use nbnode::{decode_all, decode_all_with, Complex32, DecodeOptions, Frame, Receiver};
use std::path::PathBuf;

// ---------------------------------------------------------------------------------------------------------------------
// test data parsing
// ---------------------------------------------------------------------------------------------------------------------

fn testdata(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("testdata").join(name)
}

fn load(name: &str) -> Vec<Complex32> {
    read_cf32(&testdata(name)).unwrap_or_else(|e| panic!("{name}: {e} (run testdata/gen.py)"))
}

fn lines(name: &str) -> Vec<String> {
    std::fs::read_to_string(testdata(name))
        .unwrap_or_else(|e| panic!("{name}: {e} (run testdata/gen.py)"))
        .lines()
        .map(str::to_owned)
        .collect()
}

fn unhex(s: &str) -> Vec<u8> {
    (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect()
}

/// A transmitted frame (`.truth` line: frame start rate length cfo snr hex).
struct Sent {
    start: usize,
    rate: u32,
    length: usize,
    cfo: f64,
    mpdu: Vec<u8>,
}

fn read_truth(name: &str) -> Vec<Sent> {
    lines(name)
        .iter()
        .filter(|l| l.starts_with("frame "))
        .map(|l| {
            let f: Vec<&str> = l.split_whitespace().collect();
            Sent {
                start: f[1].parse().unwrap(),
                rate: f[2].parse().unwrap(),
                length: f[3].parse().unwrap(),
                cfo: f[4].parse().unwrap(),
                mpdu: unhex(f[6]),
            }
        })
        .collect()
}

/// A Python decode result (`.pyref` line: frame start rate length fcs snr cfo hex).
struct PyFrame {
    start: usize,
    rate: u32,
    length: usize,
    fcs_ok: bool,
    snr_db: f64,
    cfo: f64,
    mpdu: Vec<u8>,
}

/// A `.pyref` file: the settings the Python ran with, its optional front end and its frames.
struct PyRef {
    opts: DecodeOptions,
    /// (capture rate, decode rate) in Msps when the samples went through nbrxd's front end first.
    frontend: Option<(f64, f64)>,
    frames: Vec<PyFrame>,
}

fn read_pyref(name: &str) -> PyRef {
    let mut r = PyRef { opts: DecodeOptions::default(), frontend: None, frames: Vec::new() };
    for l in lines(name) {
        let mut words = l.split_whitespace();
        match words.next() {
            Some("options") => {
                for kv in words {
                    let (k, v) = kv.split_once('=').unwrap();
                    match k {
                        "max_frames" => r.opts.max_frames = v.parse().unwrap(),
                        "min_snr" => r.opts.min_snr = if v == "none" { None } else { Some(v.parse().unwrap()) },
                        "max_len" => r.opts.max_len = v.parse().unwrap(),
                        "stf_threshold" => r.opts.stf_threshold = v.parse().unwrap(),
                        "h_smooth" => r.opts.h_smooth = v.parse().unwrap(),
                        "ltf_search_derotate" => r.opts.ltf_search_derotate = v == "1",
                        "cfo_offset" => r.opts.cfo_offset = v.parse().unwrap(),
                        _ => panic!("{name}: unknown option {k}"),
                    }
                }
            }
            Some("frontend") => {
                let mut rates = (0.0, 0.0);
                for kv in words {
                    let (k, v) = kv.split_once('=').unwrap();
                    match k {
                        "in_rate" => rates.0 = v.parse().unwrap(),
                        "width_rate" => rates.1 = v.parse().unwrap(),
                        _ => panic!("{name}: unknown front-end setting {k}"),
                    }
                }
                r.frontend = Some(rates);
            }
            Some("frame") => {
                let f: Vec<&str> = l.split_whitespace().collect();
                r.frames.push(PyFrame {
                    start: f[1].parse().unwrap(),
                    rate: f[2].parse().unwrap(),
                    length: f[3].parse().unwrap(),
                    fcs_ok: f[4] == "1",
                    snr_db: f[5].parse().unwrap(),
                    cfo: f[6].parse().unwrap(),
                    mpdu: unhex(f[7]),
                });
            }
            _ => {}
        }
    }
    r
}

/// Decodes `vector` the way the reference was produced (front end + options).
fn decode_like(vector: &str, r: &PyRef) -> Vec<Frame> {
    let raw = load(vector);
    let iq = match r.frontend {
        Some((in_rate, width)) => front_end(&raw, in_rate, width),
        None => raw,
    };
    decode_all_with(&iq, &r.opts)
}

/// Rust and Python must find the same frames. Returns (frames, bad-FCS frames whose PSDU also matched byte for byte).
fn assert_matches_python(vector: &str, pyref: &str) -> (usize, usize) {
    let r = read_pyref(pyref);
    let got = decode_like(vector, &r);
    let mut problems = Vec::new();
    if got.len() != r.frames.len() {
        problems.push(format!("{} frames, Python {}", got.len(), r.frames.len()));
    }
    let mut same_bad_psdu = 0;
    for (i, (g, p)) in got.iter().zip(&r.frames).enumerate() {
        let key = |start, rate, length, ok| format!("start {start} rate {rate} len {length} fcs {ok}");
        let (gk, pk) = (key(g.start, g.rate, g.length, g.fcs_ok), key(p.start, p.rate, p.length, p.fcs_ok));
        if gk != pk {
            problems.push(format!("frame {i}: Rust {gk}, Python {pk}"));
            continue;
        }
        if (g.snr_db as f64 - p.snr_db).abs() > 0.01 {
            problems.push(format!("frame {i}: SNR {} dB, Python {}", g.snr_db, p.snr_db));
        }
        if (g.cfo as f64 - p.cfo).abs() > 1e-6 {
            problems.push(format!("frame {i}: CFO {}, Python {}", g.cfo, p.cfo));
        }
        if g.fcs_ok && g.mpdu != p.mpdu {
            problems.push(format!("frame {i}: good FCS but a different PSDU"));
        }
        if !g.fcs_ok && g.mpdu == p.mpdu {
            same_bad_psdu += 1;
        }
    }
    assert!(problems.is_empty(), "{vector} vs {pyref}:\n  {}", problems.join("\n  "));
    (got.len(), same_bad_psdu)
}

/// The decoded frame that corresponds to a transmitted one: the closest start within 40 samples.
fn find<'a>(frames: &'a [Frame], sent: &Sent) -> Option<&'a Frame> {
    frames.iter().filter(|f| f.start.abs_diff(sent.start) <= 40).min_by_key(|f| f.start.abs_diff(sent.start))
}

// ---------------------------------------------------------------------------------------------------------------------
// Rust vs Python
// ---------------------------------------------------------------------------------------------------------------------

#[test]
fn rates_hi_matches_python() {
    for mode in ["plain", "nbrxd"] {
        let (n, same_bad) = assert_matches_python("rates_hi.cf32", &format!("rates_hi.{mode}.pyref"));
        println!("rates_hi {mode}: {n} frames identical to Python ({same_bad} bad-FCS PSDUs also byte-identical)");
    }
}

#[test]
fn rates_lo_matches_python() {
    for mode in ["plain", "nbrxd"] {
        let (n, same_bad) = assert_matches_python("rates_lo.cf32", &format!("rates_lo.{mode}.pyref"));
        println!("rates_lo {mode}: {n} frames identical to Python ({same_bad} bad-FCS PSDUs also byte-identical)");
    }
}

#[test]
fn wide30_matches_python() {
    assert_matches_python("wide30.cf32", "wide30.nbrxd.pyref");
}

#[test]
fn cw_matches_python() {
    for mode in ["plain", "nbrxd"] {
        assert_matches_python("cw.cf32", &format!("cw.{mode}.pyref"));
    }
}

#[test]
fn cfo_wide_matches_python() {
    assert_matches_python("cfo_wide.cf32", "cfo_wide.nbrxd.pyref");
    assert_matches_python("cfo_wide.cf32", "cfo_wide.nbrxd_offset.pyref");
}

// ---------------------------------------------------------------------------------------------------------------------
// Rust vs the transmitted frames
// ---------------------------------------------------------------------------------------------------------------------

#[test]
fn rates_hi_decodes_every_frame() {
    let truth = read_truth("rates_hi.truth");
    let frames = decode_all_with(&load("rates_hi.cf32"), &DecodeOptions::nbrxd());
    assert_eq!(frames.len(), truth.len(), "one result per transmitted frame");
    for s in &truth {
        let f = find(&frames, s).unwrap_or_else(|| panic!("frame at {} ({} Mbps) not found", s.start, s.rate));
        assert!(f.fcs_ok, "frame at {}: bad FCS", s.start);
        assert_eq!((f.rate, f.length), (s.rate, s.length));
        assert_eq!(f.mpdu, s.mpdu, "frame at {}: payload", s.start);
        assert!((f.cfo as f64 - s.cfo).abs() < 2e-4, "frame at {}: CFO {} vs {}", s.start, f.cfo, s.cfo);
        assert!(f.snr_db > 20.0, "frame at {}: SNR {}", s.start, f.snr_db);
    }
    // The same with the plain settings (no SNR gate). Every transmitted frame still decodes, but extra bad-FCS
    // results appear: candidates inside the DATA of long frames whose random "SIGNAL" passes the checks. Each one
    // moves the scan on by 720 samples, so a frame right after one can be picked up late, from the end of its STF
    // (here the 9 Mbps frame at 49402 is reported at 49476), hence the match by content.
    let plain = decode_all(&load("rates_hi.cf32"), 200, None, 4095);
    for s in &truth {
        assert!(plain.iter().any(|f| f.fcs_ok && f.mpdu == s.mpdu), "plain: frame at {}", s.start);
    }
    assert!(plain.iter().filter(|f| !f.fcs_ok).all(|f| f.snr_db < -1.0), "the extras are what min_snr removes");
}

#[test]
fn rates_lo_good_frames_are_right() {
    let truth = read_truth("rates_lo.truth");
    let frames = decode_all_with(&load("rates_lo.cf32"), &DecodeOptions { max_frames: 200, ..DecodeOptions::nbrxd() });
    let mut good_per_rate = std::collections::BTreeMap::new();
    for f in frames.iter().filter(|f| f.fcs_ok) {
        // a good FCS must mean the transmitted bytes (no false positives)
        let s = truth.iter().find(|s| s.start.abs_diff(f.start) <= 40).expect("good FCS on no transmitted frame");
        assert_eq!(f.mpdu, s.mpdu);
        *good_per_rate.entry(f.rate).or_insert(0) += 1;
    }
    let total: usize = good_per_rate.values().sum();
    println!("rates_lo: good FCS per rate (of 8): {good_per_rate:?}, {total} of 64");
    // near threshold the Python decodes ~70-90% (48 of 64 on this vector); require most of that
    assert!(total >= 40, "{total} good frames of 64");
    assert!(good_per_rate.values().all(|&n| n >= 3), "every rate decodes near its threshold");
}

#[test]
fn wide30_through_the_front_end() {
    let truth = read_truth("wide30.truth");
    let iq = front_end(&load("wide30.cf32"), 30.0, 10.0);
    assert_eq!(iq.len(), 83559 / 3); // 30 -> 10 Msps
    let frames = decode_all_with(&iq, &DecodeOptions::nbrxd());
    assert_eq!(frames.len(), 8);
    for s in &truth {
        let f = find(&frames, s).unwrap_or_else(|| panic!("frame at {} not found", s.start));
        assert!(f.fcs_ok && f.mpdu == s.mpdu, "frame at {}", s.start);
        assert!((f.cfo as f64 - s.cfo).abs() < 2e-4);
    }
    // The high-pass is needed. Without it the 0.4-0.3j DC offset is still there at 10 Msps. Once the frame's CFO is
    // removed it becomes a tone, which differs between the two LTF symbols and biases the CFO, channel and SNR
    // estimates: most frames fail (the Python too: 3 of 8 good).
    let decimated = Resampler::new(1, 3).process(&load("wide30.cf32"));
    let without_hp = decode_all_with(&decimated, &DecodeOptions::nbrxd());
    assert!(without_hp.iter().filter(|f| f.fcs_ok).count() < 8);
}

#[test]
fn cw_and_dc_are_rejected() {
    let truth = read_truth("cw.truth");
    let first_frame = truth[0].start;
    for opts in [DecodeOptions::default(), DecodeOptions::nbrxd()] {
        let frames = decode_all_with(&load("cw.cf32"), &opts);
        assert!(frames.iter().all(|f| f.start + 200 >= first_frame), "a frame inside the CW/DC sections");
        assert_eq!(frames.len(), 3);
        for s in &truth {
            assert!(find(&frames, s).is_some_and(|f| f.fcs_ok && f.mpdu == s.mpdu), "frame at {}", s.start);
        }
    }
}

#[test]
fn cfo_offset_reaches_beyond_the_stf_range() {
    let truth = read_truth("cfo_wide.truth");
    let iq = load("cfo_wide.cf32");
    // 0.045 cycles/sample aliases in the STF estimator (range +-1/32): nothing decodes
    let plain = decode_all_with(&iq, &DecodeOptions::nbrxd());
    assert!(plain.iter().all(|f| !f.fcs_ok));
    // with a static correction of 0.04 the residual is ~0.005 and everything decodes; the reported CFO is the total
    let corrected = decode_all_with(&iq, &DecodeOptions { cfo_offset: 0.04, ..DecodeOptions::nbrxd() });
    assert_eq!(corrected.len(), truth.len());
    for s in &truth {
        let f = find(&corrected, s).unwrap();
        assert!(f.fcs_ok && f.mpdu == s.mpdu);
        assert!((f.cfo as f64 - s.cfo).abs() < 2e-4, "CFO {} vs {}", f.cfo, s.cfo);
    }
}

#[test]
fn ltf_search_without_derotation_loses_large_cfo() {
    // the original rx80211.py (no derotation before the LTF correlation) only acquires |CFO| < ~0.009
    let truth = read_truth("rates_hi.truth");
    let opts = DecodeOptions { ltf_search_derotate: false, ..DecodeOptions::nbrxd() };
    let frames = decode_all_with(&load("rates_hi.cf32"), &opts);
    let (mut small, mut large) = (0, 0);
    for s in &truth {
        let ok = find(&frames, s).is_some_and(|f| f.fcs_ok);
        if s.cfo.abs() < 0.007 {
            assert!(ok, "frame at {} with CFO {} should decode", s.start, s.cfo);
            small += 1;
        } else if s.cfo.abs() > 0.013 {
            assert!(!ok, "frame at {} with CFO {} should not decode", s.start, s.cfo);
            large += 1;
        }
    }
    assert!(small >= 3 && large >= 3, "the vector covers both sides ({small}, {large})");
}

#[test]
fn max_frames_caps_the_result() {
    let iq = load("rates_hi.cf32");
    let all = decode_all_with(&iq, &DecodeOptions::nbrxd());
    let five = decode_all_with(&iq, &DecodeOptions { max_frames: 5, ..DecodeOptions::nbrxd() });
    assert_eq!(five, all[..5]);
    assert!(decode_all(&iq, 0, None, 4095).is_empty());
    // the free function uses the DecodeOptions defaults for everything else
    assert_eq!(decode_all(&iq, 50, None, 4095), decode_all_with(&iq, &DecodeOptions::default()));
}

#[test]
fn max_len_drops_long_frames() {
    let iq = load("rates_hi.cf32");
    let frames = decode_all_with(&iq, &DecodeOptions { max_len: 400, ..DecodeOptions::nbrxd() });
    assert_eq!(frames.iter().filter(|f| f.fcs_ok).count(), 16); // the 1228-byte frames are skipped
    assert!(frames.iter().all(|f| f.length <= 400));
}

// ---------------------------------------------------------------------------------------------------------------------
// building blocks against the Python / scipy / vit.c references
// ---------------------------------------------------------------------------------------------------------------------

#[test]
fn resampler_matches_scipy() {
    let x = load("rs_in.cf32");
    for (up, down) in [(1, 2), (1, 3), (1, 4), (1, 6), (1, 12), (3, 1), (2, 3)] {
        let want = load(&format!("rs_{up}_{down}.cf32"));
        let got = Resampler::new(up, down).process(&x);
        assert_eq!(got.len(), want.len(), "{up}/{down}: length");
        let err = got.iter().zip(&want).map(|(a, b)| (*a - *b).abs()).fold(0.0f32, f32::max);
        println!("resample {up}/{down}: max |error| {err:.2e}");
        assert!(err < 1e-5, "{up}/{down}: max error {err}");
    }
}

#[test]
fn viterbi_matches_vit_c() {
    let llr: Vec<f32> = std::fs::read(testdata("vit_llr.f32"))
        .unwrap()
        .as_chunks::<4>()
        .0
        .iter()
        .map(|c| f32::from_le_bytes(*c))
        .collect();
    let bits = std::fs::read(testdata("vit_bits.u8")).unwrap();
    let mut cases = 0;
    for l in lines("vit_cases.txt").iter().filter(|l| l.starts_with("case ")) {
        let f: Vec<&str> = l.split_whitespace().collect();
        let (name, nbits, lo, bo): (&str, usize, usize, usize) =
            (f[1], f[2].parse().unwrap(), f[3].parse().unwrap(), f[4].parse().unwrap());
        let got = viterbi(&llr[lo..lo + 2 * nbits], nbits);
        assert_eq!(got, &bits[bo..bo + nbits], "case {name}");
        cases += 1;
    }
    assert_eq!(cases, 7);
}

// ---------------------------------------------------------------------------------------------------------------------
// throughput (release build): cargo test --release -- --ignored --nocapture bench
// ---------------------------------------------------------------------------------------------------------------------

/// Repeats `x` until `n` samples.
fn tile(x: &[Complex32], n: usize) -> Vec<Complex32> {
    x.iter().cycle().take(n).copied().collect()
}

/// Decodes `stream` in blocks of `block` samples, as nbrxd does (100 ms blocks); returns (seconds, frames, good).
/// The time is the best of 3 passes (the first may run before the CPU has reached its full clock).
fn timed_decode(stream: &[Complex32], block: usize, opts: &DecodeOptions) -> (f64, usize, usize) {
    let rx = Receiver::new();
    let mut best = f64::INFINITY;
    let (mut n, mut good) = (0, 0);
    for _ in 0..3 {
        let t = std::time::Instant::now();
        (n, good) = (0, 0);
        for b in stream.chunks(block) {
            let frames = rx.decode_all(b, opts);
            n += frames.len();
            good += frames.iter().filter(|f| f.fcs_ok).count();
        }
        best = best.min(t.elapsed().as_secs_f64());
    }
    (best, n, good)
}

/// One benchmark line: throughput in Msps per core and the margin over real time for a 10 MHz channel decoded at
/// 10 Msps and a 5 MHz one at 5 Msps. The receiver does the same work per sample whatever the channel width; only
/// the sample rate it must keep up with changes.
fn report(what: &str, samples: usize, seconds: f64, frames: usize, good: usize) {
    let msps = samples as f64 / seconds / 1e6;
    println!(
        "{what}: {msps:6.1} Msps per core = {:5.1}x real time at 10 Msps, {:5.1}x at 5 Msps ({frames} frames, {good} good)",
        msps / 10.0,
        msps / 5.0
    );
}

#[test]
#[ignore]
fn bench_decode() {
    let n = 20_000_000; // 2 s at 10 Msps, 4 s at 5 Msps
    let block = 1_000_000; // nbrxd decodes 100 ms blocks (at 10 Msps)
    let opts = DecodeOptions { max_frames: usize::MAX, ..DecodeOptions::nbrxd() };

    // (a) rates_hi.cf32 repeated: all 8 rates, 40-1200 byte payloads, ~85% of the air time busy with frames
    let hi = load("rates_hi.cf32");
    let (t, frames, good) = timed_decode(&tile(&hi, n), block, &opts);
    report("all rates, 85% air time  ", n, t, frames, good);

    // (b) only 6 Mbps frames back to back (the Viterbi decoder's worst case per sample at a given rate)
    let six = &hi[..read_truth("rates_hi.truth")[3].start]; // the leading noise and the three 6 Mbps frames
    let (t, frames, good) = timed_decode(&tile(six, n), block, &opts);
    report("6 Mbps frames, 90% air   ", n, t, frames, good);

    // (c) 12 Mbps frames from rates_lo at a third of the air time: a busier-than-typical narrow-band link
    let lo = load("rates_lo.cf32");
    let truth = read_truth("rates_lo.truth");
    let (a, b) = (truth[16].start - 500, truth[24].start - 500); // the eight 12 Mbps frames (4 dB SNR)
    let light = &lo[a..b];
    let quiet = &hi[..1500];
    let mut mix = Vec::new();
    while mix.len() < n {
        mix.extend_from_slice(light);
        for _ in 0..(2 * light.len() / quiet.len()) {
            mix.extend_from_slice(quiet);
        }
    }
    mix.truncate(n);
    let (t, frames, good) = timed_decode(&mix, block, &opts);
    report("12 Mbps at 4 dB, 1/3 air ", n, t, frames, good);

    // (d) noise only: the cost of the preamble detector alone
    let (t, frames, good) = timed_decode(&tile(quiet, n), block, &opts);
    report("noise only               ", n, t, frames, good);

    // (e) 30 Msps capture -> front end (mean, 1/3 resampler, high-pass) -> decode at 10 Msps, as nbrxd --pack 8
    let wide = load("wide30.cf32");
    let raw = tile(&wide, 3 * n / 2); // 1.5 s at 30 Msps
    let rx = Receiver::new();
    let (mut best_front, mut best_total) = (f64::INFINITY, f64::INFINITY);
    let (mut frames, mut good) = (0, 0);
    for _ in 0..3 {
        let t = std::time::Instant::now();
        let mut front = 0.0;
        (frames, good) = (0, 0);
        for b in raw.chunks(3 * block) {
            let t0 = std::time::Instant::now();
            let y = front_end(b, 30.0, 10.0);
            front += t0.elapsed().as_secs_f64();
            let f = rx.decode_all(&y, &opts);
            frames += f.len();
            good += f.iter().filter(|f| f.fcs_ok).count();
        }
        best_total = best_total.min(t.elapsed().as_secs_f64());
        best_front = best_front.min(front);
    }
    println!(
        "30 Msps capture -> 10 Msps: front end {:6.1} Msps in, with the decode {:5.1}x real time ({frames} frames, {good} \
         good)",
        raw.len() as f64 / best_front / 1e6,
        raw.len() as f64 / 30e6 / best_total
    );
}

#[test]
#[ignore]
fn bench_viterbi() {
    // pseudo-random soft values: the decoder's cost does not depend on them
    let nbits = 200_000;
    let mut s = 1u32;
    let llr: Vec<f32> = (0..2 * nbits)
        .map(|_| {
            s = s.wrapping_mul(1664525).wrapping_add(1013904223);
            (s >> 8) as f32 / (1u32 << 24) as f32 - 0.5
        })
        .collect();
    // best of 5 passes: the first ones may run before the CPU has reached its full clock
    let mut dt = f64::INFINITY;
    for _ in 0..5 {
        let t = std::time::Instant::now();
        let bits = viterbi(&llr, nbits);
        dt = dt.min(t.elapsed().as_secs_f64());
        assert_eq!(bits.len(), nbits);
    }
    println!("viterbi: {:.1} ns per decoded bit ({:.1} Mbit/s)", dt / nbits as f64 * 1e9, nbits as f64 / dt / 1e6);
}
