//! p11node: an IEEE 802.11p receiving node (ITS-G5 / DSRC: 10 MHz OCB channels in the 5.9 GHz band) on one
//! MT7921AU card.
//!
//! 802.11p frames are 10 MHz OFDM. The MT7961 silicon cannot demodulate them, because its demodulator has fixed
//! 20 MHz numerology (notes/03). It cannot transmit them either: its transmitter divides by 1, 4 or 8 only (20, 5 or
//! 2.5 MHz PPDUs), and no ÷2 exists (notes/07). So this node receives through the IQ stream:
//! - the card is tuned to the ITS channel in the firmware's native 10 MHz mode (cbw 4, front end band-limited to
//!   ±5 MHz) and streams 30 Msps (ICAP packer 8) to the host;
//! - the host resamples to 10 Msps and decodes (the Rust port of tools/sdr/rx80211.py).
//!
//! Received frames go to:
//! - a TAP interface, as Ethernet frames (GeoNetworking 0x8947, WSMP 0x88dc, IPv6 ...), for an ITS stack such as
//!   Vanetza; or a TUN interface (IP packets only);
//! - a radiotap pcap (Wireshark decodes GeoNetworking, BTP, CAM and DENM);
//! - stdout, one line per frame.
//!
//! The node never transmits, so the kernel's packets on its interface are dropped. The card's own narrow
//! transmitter (5 MHz, tools/nbnode nbmesh) is not 802.11p: 10 MHz ITS stations cannot receive it.
//!
//! The setup follows tools/guest/nbprep.py (without the transmit steps). It needs the instrumented firmware (fw/,
//! `make install`) and the mt7921dbg module.

use std::collections::HashMap;
use std::io;
use std::process::exit;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::thread::{self, sleep};
use std::time::{Duration, Instant};

use nbnode::card::{self, Card, Hooks};
use nbnode::frame::{self, Pcap};
use nbnode::iqring::Packer;
use nbnode::netdev::{Mode, NetDev};
use nbnode::node::{Dedupe, Rx, RxConfig, SliceEnd};
use nbnode::profile::Profile;
use nbnode::rx80211::DecodeOptions;
use nbnode::sys;

const USAGE: &str = "\
usage: p11node <iface> [--channel N] [options]
  <iface>              the card's managed interface (e.g. wlx00c0cab7962a); it is taken down
  --channel N          ITS channel, 10 MHz: 172 174 176 178 180 182 184 (5860-5920 MHz; default 180, ETSI G5-CCH
                       5900 MHz; US DSRC CCH is 178)
  --test-freq MHz      lab test at another centre frequency instead (not an ITS channel), e.g. 2350 with an SDR
                       sending 802.11p frames there
  --mode tun|tap       interface type for the received frames (default tap: Ethernet, any ethertype; tun: IP only)
  --dev NAME           interface name (default p11)
  --netns NS           move the interface into network namespace NS
  --no-dev             no interface: pcap and/or stdout only
  --pcap FILE          write every decoded frame (radiotap, half-rate channel flag)
  --gain L,F,D         manual receive gain: LNA 0-3, filter 0-8, digital 0-12 (default 3,4,6)
  --slot 0|1           mt7921dbg IQ slot (default 0; one per card)
  --hooks PATH         hook addresses of the installed firmware (default /root/fwre/hooks.nm)
  --slice-ms N         capture slice length (default 4000; the receiver is off for ~0.1 s between slices)
  --block-ms N         decode block length (default 100)
  --workers N          decoder threads (default 3: the 30 Msps front end is the heaviest part)
  --cfo-khz X          static frequency correction of the receiver (default 0)
  --show-bad           print frames that fail the FCS
  --quiet              no per-frame lines (the status lines stay)
  --status-s N         a status line at least every N seconds (default 10)
  --seconds N          run time (default: until SIGINT / SIGTERM)";

/// ITS channels (IEEE 802.11 channel numbers in the 5.9 GHz band, 10 MHz wide): f = 5000 + 5 n MHz.
const ITS_CHANNELS: [u32; 7] = [172, 174, 176, 178, 180, 182, 184];
/// Channel width of 802.11p frames, MHz: the decoder runs at 10 Msps.
const WIDTH: f64 = 10.0;
/// ICAP packer 8: 30 Msps, three samples per channel sample (packer 2's 10 Msps leaves no guard band at 10 MHz).
const PACK: Packer = Packer(8);

struct Args {
    iface: String,
    freq: u32,
    its: bool,
    mode: Mode,
    dev: Option<String>,
    netns: Option<String>,
    pcap: Option<String>,
    gain: (u32, u32, u32),
    slot: u32,
    hooks: String,
    slice_ms: u32,
    block_ms: f64,
    workers: usize,
    cfo_khz: f64,
    show_bad: bool,
    quiet: bool,
    status_s: f64,
    seconds: Option<f64>,
}

impl Args {
    fn parse() -> Result<Args, String> {
        let mut it = std::env::args().skip(1);
        let mut a = Args {
            iface: String::new(),
            freq: 5000 + 5 * 180,
            its: true,
            mode: Mode::Tap,
            dev: Some("p11".into()),
            netns: None,
            pcap: None,
            gain: (3, 4, 6),
            slot: 0,
            hooks: "/root/fwre/hooks.nm".into(),
            slice_ms: 4000,
            block_ms: 100.0,
            workers: 3,
            cfo_khz: 0.0,
            show_bad: false,
            quiet: false,
            status_s: 10.0,
            seconds: None,
        };
        fn num<T: std::str::FromStr>(o: &str, v: Option<String>) -> Result<T, String> {
            let v = v.ok_or(format!("{o} needs a value"))?;
            v.parse().map_err(|_| format!("bad value {v:?} for {o}"))
        }
        while let Some(o) = it.next() {
            match o.as_str() {
                "-h" | "--help" => return Err(String::new()),
                "--channel" => {
                    let ch: u32 = num(&o, it.next())?;
                    if !ITS_CHANNELS.contains(&ch) {
                        return Err(format!("{ch} is not a 10 MHz ITS channel ({ITS_CHANNELS:?})"));
                    }
                    a.freq = 5000 + 5 * ch;
                    a.its = true;
                }
                "--test-freq" => {
                    a.freq = num(&o, it.next())?;
                    a.its = false;
                }
                "--mode" => a.mode = num(&o, it.next())?,
                "--dev" => a.dev = Some(num(&o, it.next())?),
                "--no-dev" => a.dev = None,
                "--netns" => a.netns = Some(num(&o, it.next())?),
                "--pcap" => a.pcap = Some(num(&o, it.next())?),
                "--gain" => {
                    let v: String = num(&o, it.next())?;
                    let g: Vec<u32> = v.split(',').filter_map(|x| x.parse().ok()).collect();
                    match g[..] {
                        [l, f, d] if l <= 3 && f <= 8 && d <= 12 => a.gain = (l, f, d),
                        _ => return Err("--gain wants L,F,D: LNA 0-3, filter 0-8, digital 0-12".into()),
                    }
                }
                "--slot" => a.slot = num(&o, it.next())?,
                "--hooks" => a.hooks = num(&o, it.next())?,
                "--slice-ms" => a.slice_ms = num(&o, it.next())?,
                "--block-ms" => a.block_ms = num(&o, it.next())?,
                "--workers" => a.workers = num::<usize>(&o, it.next())?.max(1),
                "--cfo-khz" => a.cfo_khz = num(&o, it.next())?,
                "--show-bad" => a.show_bad = true,
                "--quiet" => a.quiet = true,
                "--status-s" => a.status_s = num(&o, it.next())?,
                "--seconds" => a.seconds = Some(num(&o, it.next())?),
                _ if o.starts_with('-') => return Err(format!("unknown option {o}")),
                _ if a.iface.is_empty() => a.iface = o,
                _ => return Err(format!("unexpected argument {o}")),
            }
        }
        if a.iface.is_empty() {
            return Err("the interface is required".into());
        }
        if a.slot > 1 {
            return Err("--slot must be 0 or 1".into());
        }
        Ok(a)
    }
}

fn mac_str(m: &[u8; 6]) -> String {
    m.iter().map(|b| format!("{b:02x}")).collect::<Vec<_>>().join(":")
}

/// Tune and configure the receiver (nbprep.py steps 1, 3, 4, 5; nothing for transmitting). Returns the tuned
/// frequency in kHz.
fn setup_radio(card: &Card, a: &Args) -> io::Result<u32> {
    // channel switch at cbw 4, the firmware's native 10 MHz mode, with the frequency override; it narrows the
    // receive front end to ±5 MHz, which the packers' plain decimation needs (notes/08)
    let tuned = card.tune(a.freq, 1, 1)?;
    // bandwidth class 4: the card's own 20 MHz demodulator stays off the narrow frames (notes/03)
    card.set_rx_class()?;
    // IQ capture registers, packer 8 = 30 Msps (host-path writes); MAC RX on, TX off
    card.icap_setup(PACK.0)?;
    // manual gain after the capture setup (the AGC hunts on noise)
    card.set_gain(a.gain.0, a.gain.1, a.gain.2)?;
    Ok(tuned)
}

#[derive(Default)]
struct Counters {
    frames: AtomicU64,
    good: AtomicU64,
    delivered: AtomicU64,
}

fn main() {
    let a = match Args::parse() {
        Ok(a) => a,
        Err(e) => {
            if !e.is_empty() {
                eprintln!("p11node: {e}");
            }
            eprintln!("{USAGE}");
            exit(2);
        }
    };
    if let Err(e) = run(a) {
        eprintln!("p11node: {e}");
        exit(1);
    }
}

fn run(a: Args) -> io::Result<()> {
    sys::catch_stop_signals();
    let avoided = sys::keep_off_usb_irq_cpus(); // before any thread starts: they inherit the CPU mask
    if !avoided.is_empty() {
        eprintln!("# running off the USB interrupt CPUs {avoided:?}");
    }
    let t0 = Instant::now();
    if !a.its {
        eprintln!("# test frequency {} MHz: not an ITS channel (802.11p uses 5.9 GHz only)", a.freq);
    }
    let mut card = Card::open(&a.iface)?;
    let hooks = Hooks::load(&a.hooks)?;

    // --- the card: managed interface down (mac80211 would retune it), then the receiver setup ---
    card::run(&["ip", "link", "set", &a.iface, "down"], true)?;
    let tuned = setup_radio(&card, &a)?;
    let ch = if a.its { format!("ITS channel {}", (a.freq - 5000) / 5) } else { "test frequency".into() };
    eprintln!("# {}: tuned {:.3} MHz ({ch}), 10 MHz 802.11p, IQ 30 Msps -> 10 Msps", card.phy, tuned as f64 / 1e3);

    // --- the interface for received frames ---
    let dev = match &a.dev {
        Some(name) => Some(NetDev::open(name, a.mode, 1500, &[], a.netns.as_deref())?),
        None => None,
    };
    if let Some(d) = &dev {
        eprintln!("# {} ({:?}) up: received frames are delivered there; packets sent on it are dropped", d.name, d.mode);
        let mut rd = d.try_clone()?;
        // the node does not transmit: read and drop what the kernel sends, so that its queue does not fill
        thread::spawn(move || {
            let mut buf = vec![0u8; 65536];
            while rd.read(&mut buf).is_ok() {}
        });
    }

    // --- the receiver ---
    // blocks overlap by more than the longest frame: a 2400-byte PSDU at 3 Mbps (6 Mbps at 20 MHz timing), 6.5 ms
    let longest = Profile::parse("legacy:6").unwrap().airtime(2400, 2);
    let cfg = RxConfig {
        width: WIDTH,
        pack: PACK,
        block_ms: a.block_ms,
        overlap_ms: longest.as_secs_f64() * 1e3 * 1.3 + 1.0,
        workers: a.workers,
        opts: DecodeOptions {
            max_len: 2400,
            cfo_offset: a.cfo_khz * 1e3 / (WIDTH * 1e6), // cycles per sample at 10 Msps
            ..DecodeOptions::nbrxd()
        },
    };
    let (mut rx, frames) = Rx::start(&card, a.slot, cfg)?;
    let counters = Arc::new(Counters::default());
    {
        let (counters, wpds) = (counters.clone(), rx.words_per_decoded_sample);
        let mut dev = match &dev {
            Some(d) => Some(d.try_clone()?),
            None => None,
        };
        let mut pcap = match &a.pcap {
            Some(p) => Some(Pcap::create(p, a.freq, WIDTH)?),
            None => None,
        };
        let (quiet, show_bad) = (a.quiet, a.show_bad);
        thread::spawn(move || {
            let mut dedupe = Dedupe::new(wpds);
            let mut last_seq: HashMap<[u8; 6], u16> = HashMap::new();
            for rf in frames {
                if !dedupe.first_time(rf.pos) {
                    continue; // the same frame in the overlap of two blocks
                }
                let f = &rf.frame;
                let t = t0.elapsed().as_secs_f64();
                let cfo_khz = f.cfo as f64 * WIDTH * 1e3;
                counters.frames.fetch_add(1, Ordering::Relaxed);
                if !f.fcs_ok {
                    if show_bad {
                        eprintln!("{t:9.3} s  bad FCS: {:4.1} Mbps len {:4} snr {:5.1} dB cfo {cfo_khz:7.2} kHz",
                                  f.rate as f64 / 2.0, f.length, f.snr_db);
                    }
                    continue;
                }
                counters.good.fetch_add(1, Ordering::Relaxed);
                if let Some(p) = pcap.as_mut() {
                    let _ = p.write(&f.mpdu, f.rate as f64);
                }
                let fc = u16::from_le_bytes([f.mpdu[0], f.mpdu[1]]);
                let data = frame::parse_data(&f.mpdu);
                let mut to_dev = false;
                if let (Some(d), Some(dv)) = (&data, dev.as_mut()) {
                    // a repeat of the frame just delivered from this source (same sequence number) is dropped
                    if last_seq.get(&d.sa) != Some(&d.seq) {
                        last_seq.insert(d.sa, d.seq);
                        to_dev = deliver(dv, d);
                        if to_dev {
                            counters.delivered.fetch_add(1, Ordering::Relaxed);
                        }
                    }
                }
                if !quiet {
                    let what = match &data {
                        Some(d) => format!("SA {}  DA {}  type {:#06x}", mac_str(&d.sa), mac_str(&d.da), d.ethertype),
                        None => format!("frame control {fc:#06x}"),
                    };
                    println!("{t:9.3} s  {:4.1} Mbps  len {:4}  {what}{}  snr {:4.1} dB  cfo {cfo_khz:6.1} kHz",
                             f.rate as f64 / 2.0, f.length, if to_dev { "  -> dev" } else { "" }, f.snr_db);
                }
            }
        });
    }

    // --- capture slices back to back (receive only) ---
    let end = a.seconds.map(|s| t0 + Duration::from_secs_f64(s));
    let mut errors = 0;
    let mut last_line = Instant::now();
    while !sys::stop_requested() && end.map_or(true, |e| Instant::now() < e) {
        let slice_ms = match end {
            Some(e) => (e.saturating_duration_since(Instant::now()).as_millis() as u32).clamp(100, a.slice_ms),
            None => a.slice_ms,
        };
        let step = (|| -> io::Result<()> {
            let ended = rx.slice(&card, &hooks, slice_ms, Duration::MAX, &|| false)?;
            if ended == SliceEnd::Stopped {
                return Ok(());
            }
            // the hook has returned: MCU commands are safe again. Check that nobody retuned the card
            let st = card.iq_stat(&hooks)?;
            let khz = card.tuned_khz()?;
            if (khz as i64 - a.freq as i64 * 1000).abs() > 1000 {
                eprintln!("# {} found at {:.3} MHz: setting it up again", card.phy, khz as f64 / 1e3);
                setup_radio(&card, &a)?;
            }
            if last_line.elapsed().as_secs_f64() >= a.status_s {
                last_line = Instant::now();
                let s = &rx.stats;
                eprintln!("# {:.1} s: {} blocks ({} dropped), {} frames ({} good), {} delivered | fw state {} sent {} \
                           overruns {}", t0.elapsed().as_secs_f64(), s.blocks.load(Ordering::Relaxed),
                          s.dropped_blocks.load(Ordering::Relaxed), counters.frames.load(Ordering::Relaxed),
                          counters.good.load(Ordering::Relaxed), counters.delivered.load(Ordering::Relaxed),
                          st.state, st.sent, st.overruns);
            }
            Ok(())
        })();
        match step {
            Ok(()) => errors = 0,
            Err(e) => {
                errors += 1;
                eprintln!("# slice failed ({errors}): {e}; setting the card up again");
                if errors >= 5 {
                    rx.unbind();
                    return Err(io::Error::new(e.kind(), format!("{e} (5 failures in a row: is the card wedged? \
                                                                 tools/rigrecover.sh)")));
                }
                sleep(Duration::from_secs(1));
                // a card that the kernel reset and probed again comes back under a new phy: attach to it again
                if let Ok(c) = Card::open(&a.iface) {
                    if c.phy != card.phy {
                        eprintln!("# {} is back as {}: attaching again", a.iface, c.phy);
                        card = c;
                        let _ = card::run(&["ip", "link", "set", &a.iface, "down"], false);
                    }
                }
                rx.recover(&card, &hooks);
                if let Err(e) = setup_radio(&card, &a) {
                    eprintln!("# setup failed: {e}");
                }
            }
        }
    }
    sleep(Duration::from_millis(300)); // let the decoders deliver the last blocks
    rx.unbind();
    card.release()?;
    eprintln!("# total: {} frames ({} good), {} delivered", counters.frames.load(Ordering::Relaxed),
              counters.good.load(Ordering::Relaxed), counters.delivered.load(Ordering::Relaxed));
    Ok(())
}

/// A received data frame toward the host: TAP gets an Ethernet frame with the 802.11 addresses (OCB: addr1 is the
/// destination, addr2 the source), TUN the IP packets. Returns whether it was written.
fn deliver(dev: &mut NetDev, d: &frame::RxData) -> bool {
    let r = match dev.mode {
        Mode::Tap => {
            let mut eth = Vec::with_capacity(14 + d.payload.len());
            eth.extend_from_slice(&d.da);
            eth.extend_from_slice(&d.sa);
            eth.extend_from_slice(&d.ethertype.to_be_bytes());
            eth.extend_from_slice(&d.payload);
            dev.write(&eth)
        }
        Mode::Tun if matches!(d.ethertype, 0x0800 | 0x86DD) => dev.write(&d.payload),
        Mode::Tun => return false,
    };
    r.is_ok()
}
