//! rxfile: decodes the IEEE 802.11a/p frames in an IQ file, one line per frame. The Rust counterpart of
//! `tools/sdr/rx80211.py <file>`, with nbrxd.py's front end (resampling + DC high-pass) as an option.

use nbnode::iqfile::read_iq;
use nbnode::resample::front_end;
use nbnode::{Complex32, DecodeOptions, Frame, Receiver};
use std::path::PathBuf;
use std::process::exit;
use std::time::Instant;

const USAGE: &str = "\
Usage: rxfile <file.cf32|file.cs16> [options]

Decodes the IEEE 802.11a/p OFDM frames in an IQ file and prints one line per frame: start sample, rate, PSDU
length, LTF SNR, CFO, FCS check and the first payload bytes. (.cs16 = interleaved int16, anything else = complex64.)

Front end (tools/guest/nbrxd.py): with --width-rate the samples are resampled from --in-rate to the channel
width's rate (block mean removed first, scipy resample_poly filter), then DC-filtered (one-pole high-pass, 20 kHz).
Without --width-rate they are decoded as they are, like rx80211.py does.

  --width-rate MSPS    decode rate = the channel width in MHz: 10 (802.11p), 5 or 2.5
  --in-rate MSPS       sample rate of the file (default: the width rate, i.e. high-pass only)
  --max-frames N       stop after N frames (default: no limit; rx80211.py stops at 50)
  --min-snr DB         LTF SNR gate (default: none, as rx80211.py; nbrxd uses -1)
  --max-len BYTES      largest PSDU worth decoding (default 4095; nbrxd uses 1600)
  --nbrxd              nbrxd.py's receiver settings: --min-snr -1 --max-len 1600
  --stf-threshold M    STF detection threshold (default 0.6)
  --h-smooth N         channel-estimate smoothing, subcarriers per side (default 1, 0 = off)
  --no-ltf-derotate    search the LTF without removing the STF's CFO first (the original rx80211.py)
  --cfo-offset F       static frequency correction, cycles/sample at the decode rate (default 0)
  --hex                print the first payload bytes in hex instead of text
  --quiet              no per-frame lines, only the summary
  --bench N            decode the file N times and print the throughput instead of the frames
";

struct Args {
    path: PathBuf,
    width_rate: Option<f64>,
    in_rate: Option<f64>,
    opts: DecodeOptions,
    hex: bool,
    quiet: bool,
    bench: Option<usize>,
}

fn fail(msg: &str) -> ! {
    eprintln!("rxfile: {msg}\n\n{USAGE}");
    exit(2);
}

fn parse_args() -> Args {
    let mut args = std::env::args().skip(1);
    let mut path = None;
    let mut a = Args {
        path: PathBuf::new(),
        width_rate: None,
        in_rate: None,
        opts: DecodeOptions { max_frames: usize::MAX, ..DecodeOptions::default() },
        hex: false,
        quiet: false,
        bench: None,
    };
    fn value<T: std::str::FromStr>(args: &mut impl Iterator<Item = String>, flag: &str) -> T {
        let v = args.next().unwrap_or_else(|| fail(&format!("{flag} needs a value")));
        v.parse().unwrap_or_else(|_| fail(&format!("bad value for {flag}: {v}")))
    }
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "-h" | "--help" => {
                print!("{USAGE}");
                exit(0);
            }
            "--width-rate" => a.width_rate = Some(value(&mut args, &arg)),
            "--in-rate" => a.in_rate = Some(value(&mut args, &arg)),
            "--max-frames" => a.opts.max_frames = value(&mut args, &arg),
            "--min-snr" => a.opts.min_snr = Some(value(&mut args, &arg)),
            "--max-len" => a.opts.max_len = value(&mut args, &arg),
            "--nbrxd" => {
                let n = DecodeOptions::nbrxd();
                a.opts.min_snr = n.min_snr;
                a.opts.max_len = n.max_len;
            }
            "--stf-threshold" => a.opts.stf_threshold = value(&mut args, &arg),
            "--h-smooth" => a.opts.h_smooth = value(&mut args, &arg),
            "--no-ltf-derotate" => a.opts.ltf_search_derotate = false,
            "--cfo-offset" => a.opts.cfo_offset = value(&mut args, &arg),
            "--hex" => a.hex = true,
            "--quiet" => a.quiet = true,
            "--bench" => a.bench = Some(value(&mut args, &arg)),
            s if s.starts_with('-') => fail(&format!("unknown option {s}")),
            s => {
                if path.replace(PathBuf::from(s)).is_some() {
                    fail("more than one input file");
                }
            }
        }
    }
    a.path = path.unwrap_or_else(|| fail("no input file"));
    if a.in_rate.is_some() && a.width_rate.is_none() {
        fail("--in-rate needs --width-rate");
    }
    a
}

/// Payload bytes for display: the MPDU body after the 24-byte 802.11 header, without the FCS, at most 40 bytes
/// (the whole MPDU if it is shorter than a header).
fn payload_preview(m: &[u8], hex: bool) -> String {
    let bytes = if m.len() > 24 + 4 { &m[24..(m.len() - 4).min(24 + 40)] } else { &m[..m.len().min(40)] };
    if hex {
        return bytes.iter().map(|b| format!("{b:02x}")).collect();
    }
    let mut s = String::from("\"");
    for &b in bytes {
        match b {
            b'"' => s.push_str("\\\""),
            b'\\' => s.push_str("\\\\"),
            0x20..=0x7e => s.push(b as char),
            _ => s.push_str(&format!("\\x{b:02x}")),
        }
    }
    s.push('"');
    s
}

fn print_frame(f: &Frame, rate_msps: Option<f64>, hex: bool) {
    let (time, on_air, cfo_hz) = match rate_msps {
        // at the decode rate: 1 sample = 1/(W Msps); on-air rate = rate x W / 20; CFO Hz = cycles/sample x W Msps
        Some(w) => (
            format!(" {:12.1} us", f.start as f64 / w),
            format!(" ({:5.2} on air)", f.rate as f64 * w / 20.0),
            format!(" ({:+8.2} kHz)", f.cfo as f64 * w * 1e3),
        ),
        None => (String::new(), String::new(), String::new()),
    };
    println!(
        "start {:9}{time}  rate {:2} Mbps{on_air}  len {:4}  snr {:5.1} dB  cfo {:+.6}{cfo_hz}  FCS {}  {}",
        f.start,
        f.rate,
        f.length,
        f.snr_db,
        f.cfo,
        if f.fcs_ok { "ok " } else { "bad" },
        payload_preview(&f.mpdu, hex)
    );
}

fn main() {
    let a = parse_args();
    let raw = read_iq(&a.path).unwrap_or_else(|e| {
        eprintln!("rxfile: {}: {e}", a.path.display());
        exit(1);
    });
    let in_rate = a.in_rate.or(a.width_rate);
    let rx = Receiver::new();

    if let Some(n) = a.bench {
        bench(&raw, in_rate, a.width_rate, &rx, &a.opts, n.max(1));
        return;
    }

    let iq = match a.width_rate {
        Some(w) => front_end(&raw, in_rate.unwrap_or(w), w),
        None => raw,
    };
    let frames = rx.decode_all(&iq, &a.opts);
    if !a.quiet {
        for f in &frames {
            print_frame(f, a.width_rate, a.hex);
        }
    }
    let good = frames.iter().filter(|f| f.fcs_ok).count();
    println!("# {} frames, {} with a good FCS ({} samples decoded)", frames.len(), good, iq.len());
}

/// Times the front end and the decoder separately over `n` passes and prints throughput figures.
fn bench(raw: &[Complex32], in_rate: Option<f64>, width_rate: Option<f64>, rx: &Receiver, opts: &DecodeOptions, n: usize) {
    let t = Instant::now();
    let mut iq = Vec::new();
    for _ in 0..n {
        iq = match width_rate {
            Some(w) => front_end(raw, in_rate.unwrap_or(w), w),
            None => raw.to_vec(),
        };
    }
    let front = t.elapsed().as_secs_f64() / n as f64;
    let t = Instant::now();
    let mut frames = 0;
    for _ in 0..n {
        frames = rx.decode_all(&iq, opts).len();
    }
    let decode = t.elapsed().as_secs_f64() / n as f64;
    println!("# {} input samples -> {} decoded samples, {} frames, {} passes", raw.len(), iq.len(), frames, n);
    println!(
        "front end: {:8.3} ms/pass  {:8.1} Msps input",
        front * 1e3,
        raw.len() as f64 / front.max(1e-12) / 1e6
    );
    println!("decode:    {:8.3} ms/pass  {:8.1} Msps at the decode rate", decode * 1e3, iq.len() as f64 / decode / 1e6);
    if let (Some(fin), Some(_)) = (in_rate, width_rate) {
        let seconds = raw.len() as f64 / (fin * 1e6);
        // N x real time = seconds of signal processed per second of CPU (below 1: slower than real time)
        println!(
            "real time: {:.3} s of signal at {fin} Msps; front end {:.1}x real time, decode {:.1}x, both {:.1}x",
            seconds,
            seconds / front.max(1e-12),
            seconds / decode,
            seconds / (front + decode)
        );
    }
}
