//! nbmesh: a narrow-band (5 or 2.5 MHz) mesh node on one MT7921AU card, serving a TUN or a TAP interface.
//!
//! The card's own transmitter sends ÷4 (5 MHz) or ÷8 (2.5 MHz) OFDM PPDUs with the card's PHY coding (`--profile`,
//! notes/10). The card cannot demodulate narrow frames, so its receive IQ is streamed to the host and decoded here
//! (the Rust port of tools/sdr/rx80211.py). The node is half-duplex: it receives in capture slices and transmits in
//! TX windows between them. A slice ends early, after `--min-rx-ms`, when packets wait.
//!
//! What the host adds to the frames before the card sends them (the reason this is a program and not a script):
//! - `--fec K,M`: packet-level Reed-Solomon erasure coding (src/fec.rs). Every K packets get M parity frames, and any
//!   K of the K + M frames rebuild them. No MAC acknowledgement exists at narrow widths, so this replaces
//!   retransmissions.
//! - at 2.5 MHz, an inner CRC plus padding, because the ÷8 transmitter ends every PPDU early (notes/10 §5);
//! - 802.11 data framing (src/frame.rs), compatible with tools/guest/nbrxd.py nodes.
//!
//! `--mode tun`: IP packets, one shared subnet (`--addr`). `--mode tap`: Ethernet frames, for ARP, bridging and
//! layer-2 or layer-3 mesh routing on top (see src/netdev.rs).
//!
//! The setup is that of tools/guest/nbprep.py: channel switch at cbw 4 with the frequency override, ÷8 for 2.5 MHz,
//! receive class 4, IQ capture registers, manual gain, MAC TX off, TXD overrides, monitor interface. The card needs
//! the instrumented firmware (fw/, `make install`) and the mt7921dbg module (tools/guest/mt7921dbg).
//!
//! The node transmits on `--freq` in any band the card tunes (2300-2500, 4900-5925, 5925-7125 MHz): ISM spectrum, or
//! amateur allocations for a licensed operator. It imposes no band rules of its own: using a frequency, width and
//! power that are allowed where it runs is up to the operator. `--rx-only` makes a node that never transmits.

use std::collections::{HashMap, VecDeque};
use std::io;
use std::process::exit;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, sleep};
use std::time::{Duration, Instant};

use nbnode::card::{self, Card, Hooks};
use nbnode::fec;
use nbnode::frame::{self, Pcap, BROADCAST};
use nbnode::iqring::Packer;
use nbnode::netdev::{self, Mode, NetDev};
use nbnode::node::{Dedupe, Jitter, Rx, RxConfig, SliceEnd};
use nbnode::profile::{self, Profile};
use nbnode::rx80211::DecodeOptions;
use nbnode::sys;

const USAGE: &str = "\
usage: nbmesh <iface> [--freq MHz] [options]
  <iface>              the card's managed interface (e.g. wlx00c0cab79628); it is taken down
  --freq MHz           centre frequency: 2300-2500, 4900-5925 or 5925-7125 MHz (the card's RF bands). Default 2450:
                       the 2.4 GHz ISM band's nominal centre, between Wi-Fi channels 6 and 11 and clear of the BLE
                       advertising channels (2402, 2426, 2480)
  --width 5|2.5        channel width in MHz (default 5: the ÷4 transmitter; 2.5: ÷8)
  --mode tun|tap       network interface type (default tun)
  --dev NAME           interface name (default nb0)
  --addr ADDR/PREFIX   address for the interface (repeatable), e.g. 10.42.0.1/24
  --mtu N              interface MTU (default 1400)
  --netns NS           move the interface into network namespace NS (two nodes on one host)
  --profile P          the card's PHY coding (default legacy:6; syntax of tools/guest/nbprep.py --fec)
  --fec K,M|off        packet erasure code: K data + M parity frames per block (default 4,2)
  --txpower DBM        transmit power (default 5)
  --monitor NAME       monitor interface used for injection (default nbmon0)
  --pack 2|8           ICAP sample packer: 2 = 10 Msps (default), 8 = 30 Msps
  --gain L,F,D         manual receive gain: LNA 0-3, filter 0-8, digital 0-12 (default 3,4,6)
  --slot 0|1           mt7921dbg IQ slot (default 0; one per card)
  --hooks PATH         hook addresses of the installed firmware (default /root/fwre/hooks.nm)
  --slice-ms N         capture slice length (default 4000)
  --block-ms N         decode block length (default 100: latency vs per-block overhead)
  --min-rx-ms N        shortest slice before waiting packets may end it (default 30; +0-20 ms random jitter)
  --max-tx-ms N        airtime budget of one TX window (default 150)
  --queue N            packets that may wait for a TX window; the oldest are dropped beyond (default 64)
  --rx-only            never transmit
  --cfo-khz X          static frequency correction of the receiver (default 0)
  --workers N          decoder threads (default 2)
  --pcap FILE          also write every decoded frame (radiotap)
  --show-bad           print frames that fail the FCS (link diagnostics)
  --quiet              no per-frame lines (the status lines stay)
  --status-s N         a status line at least every N seconds (default 5)
  --stall-ms N         a TX window whose frames have not left the device queues N ms after their airtime has
                       stalled (default 60); the node then tunes again, which clears it
  --no-retune          do not tune again after a stalled TX window
  --tickcr0 V          WFSYS_TICKCR0 [4:0] written after each channel switch: the MAC timing, which decides when
                       a TX PPDU ends (default 0xa, 5 MHz timing: 5 MHz frames of any length arrive whole. The
                       switch's own 0x5 cuts every 5 MHz frame longer than ~260 bytes; notes/10 §5)
  --regain             re-apply the manual gain forces before every slice (a near-field transmitter can trip a
                       receive protection that otherwise stays latched)
  --seconds N          run time (default: until SIGINT / SIGTERM)";

/// Longest ÷8 PPDU that was seen to leave the card: a 1608-byte PSDU at 6 Mbps (17.3 ms). A 2167-byte one (23 ms)
/// never did (notes/10 §5).
const DIV8_MAX_AIRTIME: Duration = Duration::from_micros(17_500);

/// Default centre frequency (MHz): 2.4 GHz ISM band, see `--freq` in USAGE.
const DEFAULT_FREQ: u32 = 2450;

/// Transparent Ethernet Bridging: in TAP mode, an FEC data unit carries a whole Ethernet frame.
const ETH_BRIDGED: u16 = 0x6558;

struct Args {
    iface: String,
    freq: u32,
    width: f64,
    mode: Mode,
    dev: String,
    addrs: Vec<String>,
    mtu: usize,
    netns: Option<String>,
    profile: Profile,
    fec: Option<(usize, usize)>,
    txpower: f64,
    monitor: String,
    pack: Packer,
    gain: (u32, u32, u32),
    slot: u32,
    hooks: String,
    slice_ms: u32,
    block_ms: f64,
    min_rx_ms: u64,
    max_tx_ms: u64,
    queue: usize,
    rx_only: bool,
    cfo_khz: f64,
    workers: usize,
    pcap: Option<String>,
    show_bad: bool,
    quiet: bool,
    seconds: Option<f64>,
    regain: bool,
    status_s: f64,
    retune: bool,
    stall_ms: u64,
    tickcr0: Option<u32>,
}

impl Args {
    fn parse() -> Result<Args, String> {
        let mut it = std::env::args().skip(1);
        let mut a = Args {
            iface: String::new(),
            freq: DEFAULT_FREQ,
            width: 5.0,
            mode: Mode::Tun,
            dev: "nb0".into(),
            addrs: Vec::new(),
            mtu: 1400,
            netns: None,
            profile: Profile::parse("legacy:6").unwrap(),
            fec: Some((4, 2)),
            txpower: 5.0,
            monitor: "nbmon0".into(),
            pack: Packer(2),
            gain: (3, 4, 6),
            slot: 0,
            hooks: "/root/fwre/hooks.nm".into(),
            slice_ms: 4000,
            block_ms: 100.0,
            min_rx_ms: 30,
            max_tx_ms: 150,
            queue: 64,
            rx_only: false,
            cfo_khz: 0.0,
            workers: 2,
            pcap: None,
            show_bad: false,
            quiet: false,
            seconds: None,
            regain: false,
            status_s: 5.0,
            retune: true,
            stall_ms: 60,
            tickcr0: Some(0xA),
        };
        fn num<T: std::str::FromStr>(o: &str, v: Option<String>) -> Result<T, String> {
            let v = v.ok_or(format!("{o} needs a value"))?;
            v.parse().map_err(|_| format!("bad value {v:?} for {o}"))
        }
        while let Some(o) = it.next() {
            match o.as_str() {
                "-h" | "--help" => return Err(String::new()),
                "--freq" => a.freq = num(&o, it.next())?,
                "--width" => a.width = num(&o, it.next())?,
                "--mode" => a.mode = num(&o, it.next())?,
                "--dev" => a.dev = num(&o, it.next())?,
                "--addr" => a.addrs.push(num(&o, it.next())?),
                "--mtu" => a.mtu = num(&o, it.next())?,
                "--netns" => a.netns = Some(num(&o, it.next())?),
                "--profile" => a.profile = Profile::parse(&num::<String>(&o, it.next())?)?,
                "--fec" => {
                    let v: String = num(&o, it.next())?;
                    a.fec = match v.as_str() {
                        "off" | "0" => None,
                        _ => {
                            let (k, m) = v.split_once(',').ok_or("--fec wants K,M or off")?;
                            let (k, m): (usize, usize) = (num(&o, Some(k.into()))?, num(&o, Some(m.into()))?);
                            if k == 0 || k + m > 255 {
                                return Err("--fec: K >= 1 and K + M <= 255".into());
                            }
                            Some((k, m))
                        }
                    }
                }
                "--txpower" => a.txpower = num(&o, it.next())?,
                "--monitor" => a.monitor = num(&o, it.next())?,
                "--pack" => a.pack = Packer(num(&o, it.next())?),
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
                "--min-rx-ms" => a.min_rx_ms = num(&o, it.next())?,
                "--max-tx-ms" => a.max_tx_ms = num(&o, it.next())?,
                "--queue" => a.queue = num(&o, it.next())?,
                "--rx-only" => a.rx_only = true,
                "--cfo-khz" => a.cfo_khz = num(&o, it.next())?,
                "--workers" => a.workers = num::<usize>(&o, it.next())?.max(1),
                "--pcap" => a.pcap = Some(num(&o, it.next())?),
                "--show-bad" => a.show_bad = true,
                "--quiet" => a.quiet = true,
                "--seconds" => a.seconds = Some(num(&o, it.next())?),
                "--regain" => a.regain = true,
                "--no-retune" => a.retune = false,
                "--tickcr0" => {
                    let v: String = num(&o, it.next())?;
                    a.tickcr0 = Some(nbnode::card::parse_u32(&v).filter(|&t| t <= 0x1F).ok_or("--tickcr0 wants 0..0x1f")?);
                }
                "--stall-ms" => a.stall_ms = num(&o, it.next())?,
                "--status-s" => a.status_s = num(&o, it.next())?,
                _ if o.starts_with('-') => return Err(format!("unknown option {o}")),
                _ if a.iface.is_empty() => a.iface = o,
                _ => return Err(format!("unexpected argument {o}")),
            }
        }
        if a.iface.is_empty() {
            return Err("the interface is required".into());
        }
        if a.width != 5.0 && a.width != 2.5 {
            return Err("--width must be 5 or 2.5 (the card has no ÷2 for 10 MHz, notes/07)".into());
        }
        if a.pack.0 != 2 && a.pack.0 != 8 {
            return Err("--pack must be 2 or 8".into());
        }
        if a.slot > 1 {
            return Err("--slot must be 0 or 1".into());
        }
        Ok(a)
    }

    /// The transmit divider of the width.
    fn divider(&self) -> u32 {
        if self.width == 2.5 {
            8
        } else {
            4
        }
    }
}

fn mac_str(m: &[u8; 6]) -> String {
    m.iter().map(|b| format!("{b:02x}")).collect::<Vec<_>>().join(":")
}

/// Tune and configure the radio (nbprep.py steps 1-5, in its order: the MAC tick and the ÷8 divider before the
/// capture setup, the capture setup before the gain forces). Returns the tuned frequency in kHz.
fn setup_radio(card: &Card, a: &Args) -> io::Result<u32> {
    let streams = a.profile.tx_streams() as u8;
    let tuned = card.tune(a.freq, streams, if streams == 2 { 3 } else { 1 })?;
    if let Some(t) = a.tickcr0 {
        card.set_tickcr0(t)?; // the MAC timing (TX end) for the narrow PPDU
    }
    card.set_width(a.divider())?;
    card.set_rx_class()?;
    card.icap_setup(a.pack.0)?;
    card.set_gain(a.gain.0, a.gain.1, a.gain.2)?;
    Ok(tuned)
}

/// Counters the delivery thread shares with the main loop's reports.
#[derive(Default)]
struct Delivered {
    /// Distinct frames decoded (block-overlap repeats removed), and those with a good FCS.
    frames: AtomicU64,
    good: AtomicU64,
    /// Packets written to the interface, and how many of them the FEC rebuilt.
    packets: AtomicU64,
    recovered: AtomicU64,
}

/// One received packet toward the host's IP stack. `da` / `sa` come from the 802.11 header, `ethertype` / `payload`
/// from the frame body or a rebuilt FEC unit. Returns whether it was written.
fn to_host(dev: &mut NetDev, da: [u8; 6], sa: [u8; 6], ethertype: u16, payload: &[u8]) -> bool {
    let r = match (dev.mode, ethertype) {
        (Mode::Tap, ETH_BRIDGED) if payload.len() >= 14 => dev.write(payload), // a whole Ethernet frame
        (Mode::Tap, ETH_BRIDGED) => return false,
        (Mode::Tap, et) => {
            let mut eth = Vec::with_capacity(14 + payload.len());
            eth.extend_from_slice(&da);
            eth.extend_from_slice(&sa);
            eth.extend_from_slice(&et.to_be_bytes());
            eth.extend_from_slice(payload);
            dev.write(&eth)
        }
        (Mode::Tun, ETH_BRIDGED) => {
            // a TAP node's Ethernet frame: pass its IP packet on
            let et = payload.get(12..14).map(|b| u16::from_be_bytes([b[0], b[1]]));
            match et {
                Some(0x0800 | 0x86DD) if dev.wants(&payload[14..]) => dev.write(&payload[14..]),
                _ => return false,
            }
        }
        (Mode::Tun, 0x0800 | 0x86DD) if dev.wants(payload) => dev.write(payload),
        (Mode::Tun, _) => return false,
    };
    match r {
        Ok(()) => true,
        Err(e) => {
            eprintln!("# {}: write failed: {e}", dev.name);
            false
        }
    }
}

fn main() {
    let a = match Args::parse() {
        Ok(a) => a,
        Err(e) => {
            if !e.is_empty() {
                eprintln!("nbmesh: {e}");
            }
            eprintln!("{USAGE}");
            exit(2);
        }
    };
    if let Err(e) = run(a) {
        eprintln!("nbmesh: {e}");
        exit(1);
    }
}

fn run(mut a: Args) -> io::Result<()> {
    sys::catch_stop_signals();
    let avoided = sys::keep_off_usb_irq_cpus(); // before any thread starts: they inherit the CPU mask
    if !avoided.is_empty() {
        eprintln!("# running off the USB interrupt CPUs {avoided:?}");
    }
    let t0 = Instant::now();
    let mut card = Card::open(&a.iface)?;
    let hooks = Hooks::load(&a.hooks)?;
    let tx_on = !a.rx_only;
    if tx_on && !a.profile.host_decodable() {
        eprintln!("# warning: the receivers decode legacy 6-24/48 Mbps only (notes/10); other nodes will not decode {:?}",
                  a.profile.format);
    }

    // ÷8: the padding nearly doubles long frames. An injected frame (radiotap + 802.11) is at most MONITOR_MTU
    // bytes, and PPDUs longer than DIV8_MAX_AIRTIME were not seen to leave the card (notes/10 §5): lower the MTU
    // until the longest frame fits both. Below 1280 the kernel runs no IPv6 on the interface.
    if a.divider() == 8 {
        let n = a.profile.ndbps() as usize;
        let fits = |mtu: usize| {
            let inner_end = mtu + 14 + 24 + 8 + fec::HEADER_LEN + 4 + 4;
            let psdu = inner_end + profile::div8_pad_bytes(inner_end, n) + 4;
            12 + psdu - 4 <= card::MONITOR_MTU && a.profile.airtime(psdu, 8) <= DIV8_MAX_AIRTIME
        };
        let fit = (256..=a.mtu).rev().find(|&m| fits(m)).unwrap_or(256);
        if fit < a.mtu {
            eprintln!("# MTU {} -> {fit} at 2.5 MHz: longer ÷8 frames (with their padding) cannot be sent", a.mtu);
            a.mtu = fit;
        }
    }

    // --- the card (tools/guest/nbprep.py) ---
    card.set_tx_overrides(&a.profile)?; // TXD rate code, coding fields, one attempt per frame
    card.make_monitor(&a.monitor, a.txpower)?; // monitor interface for injection, TX power
    let tuned = setup_radio(&card, &a)?;
    eprintln!("# {}: tuned {:.3} MHz, {} MHz PPDUs, profile {:?} MCS {} (rate code {:#x}), IQ packer {}, {}",
              card.phy, tuned as f64 / 1000.0, a.width, a.profile.format, a.profile.mcs, a.profile.rate_code(),
              a.pack.0, if tx_on { format!("transmitting at {} dBm", a.txpower) } else { "receive only".into() });

    // --- the network interface ---
    let dev = NetDev::open(&a.dev, a.mode, a.mtu, &a.addrs, a.netns.as_deref())?;
    // the node's address on the air: the TAP's MAC (it is the source of the frames the kernel sends), or the card's
    let node_addr = dev.mac.unwrap_or(card.mac);
    eprintln!("# {} ({:?}) up, node address {}, FEC {}", a.dev, a.mode, mac_str(&node_addr),
              a.fec.map_or("off".into(), |(k, m)| format!("{k} data + {m} parity")));

    // packets the kernel sends on the interface wait here for the next TX window
    let txq: Arc<Mutex<VecDeque<Vec<u8>>>> = Arc::new(Mutex::new(VecDeque::new()));
    let queue_drops = Arc::new(AtomicU64::new(0));
    {
        let (txq, drops, max, mut rd) = (txq.clone(), queue_drops.clone(), a.queue, dev.try_clone()?);
        let to_air = tx_on;
        thread::spawn(move || {
            let mut buf = vec![0u8; 65536];
            loop {
                match rd.read(&mut buf) {
                    Ok(n) if n > 0 && to_air => {
                        let mut q = txq.lock().unwrap();
                        if q.len() >= max {
                            q.pop_front();
                            drops.fetch_add(1, Ordering::Relaxed);
                        }
                        q.push_back(buf[..n].to_vec());
                    }
                    Ok(_) => {} // receive-only: the kernel's packets are dropped
                    Err(e) => {
                        eprintln!("# {}: read failed: {e}", rd.name);
                        return;
                    }
                }
            }
        });
    }

    // --- the receiver ---
    // ÷8: the frames carry an inner CRC and padding, sized from the rate's data bits per symbol
    let div8_ndbps = if a.divider() == 8 { Some(a.profile.ndbps() as usize) } else { None };
    // the longest PSDU a node of this mesh sends: MTU + Ethernet header (TAP) + 802.11 header, LLC/SNAP, FEC
    // header and unit header, ÷8 inner CRC and padding, FCS
    let inner_end = a.mtu + 14 + 24 + 8 + fec::HEADER_LEN + 4 + 4;
    let max_psdu = inner_end + div8_ndbps.map_or(0, |n| profile::div8_pad_bytes(inner_end, n)) + 4;
    // blocks overlap by more than the longest frame at the slowest rate any node uses (6 Mbps)
    let longest = Profile::parse("legacy:6").unwrap().airtime(max_psdu, a.divider());
    let cfg = RxConfig {
        width: a.width,
        pack: a.pack,
        block_ms: a.block_ms,
        overlap_ms: longest.as_secs_f64() * 1e3 * 1.3 + 1.0,
        workers: a.workers,
        opts: DecodeOptions {
            max_len: max_psdu.max(1600) + 32,
            // cycles per sample at the decode rate (width Msps)
            cfo_offset: a.cfo_khz * 1e3 / (a.width * 1e6),
            ..DecodeOptions::nbrxd()
        },
    };
    let (mut rx, frames) = Rx::start(&card, a.slot, cfg)?;
    let delivered = Arc::new(Delivered::default());
    {
        // the delivery thread: decoded frames -> packets on the interface
        let (mut dev, delivered, wpds) = (dev.try_clone()?, delivered.clone(), rx.words_per_decoded_sample);
        let mut pcap = match &a.pcap {
            Some(p) => Some(Pcap::create(p, a.freq, a.width)?),
            None => None,
        };
        let (quiet, width, show_bad) = (a.quiet, a.width, a.show_bad);
        thread::spawn(move || {
            let mut dedupe = Dedupe::new(wpds);
            let mut last_seq: HashMap<[u8; 6], u16> = HashMap::new();
            let mut fec_rx = fec::Decoder::default();
            for rf in frames {
                if !dedupe.first_time(rf.pos) {
                    continue; // the same frame in the overlap of two blocks
                }
                let f = &rf.frame;
                let t = t0.elapsed().as_secs_f64();
                delivered.frames.fetch_add(1, Ordering::Relaxed);
                if !f.fcs_ok {
                    if show_bad {
                        eprintln!("{t:9.3} s  bad FCS: rate {:2} len {:4} snr {:5.1} dB cfo {:7.2} kHz", f.rate,
                                  f.length, f.snr_db, f.cfo as f64 * width * 1e3);
                    }
                    continue;
                }
                delivered.good.fetch_add(1, Ordering::Relaxed);
                if let Some(p) = pcap.as_mut() {
                    let _ = p.write(&f.mpdu, f.rate as f64);
                }
                let Some(d) = frame::parse_data(&f.mpdu) else { continue };
                if d.sa == node_addr || last_seq.get(&d.sa) == Some(&d.seq) {
                    continue; // our own frame, or a repeat of the frame just delivered
                }
                last_seq.insert(d.sa, d.seq);
                let mut n = 0;
                if d.ethertype == fec::ETHERTYPE {
                    let before = fec_rx.recovered;
                    for (et, p) in fec_rx.receive(d.sa, &d.payload) {
                        n += to_host(&mut dev, BROADCAST, d.sa, et, &p) as u64;
                    }
                    delivered.recovered.fetch_add(fec_rx.recovered - before, Ordering::Relaxed);
                } else {
                    n += to_host(&mut dev, d.da, d.sa, d.ethertype, &d.payload) as u64;
                }
                delivered.packets.fetch_add(n, Ordering::Relaxed);
                if !quiet {
                    println!("{t:9.3} s  {:5.2} Mbps  len {:4}  SA {}  DA {}  type {:#06x}{}  snr {:4.1} dB  +{:.2} s",
                             f.rate as f64 * width / 20.0, f.length, mac_str(&d.sa), mac_str(&d.da), d.ethertype,
                             if n > 0 { format!("  -> {n} pkt") } else { String::new() }, f.snr_db,
                             rf.latency.as_secs_f64());
                }
            }
        });
    }

    // --- the half-duplex loop: capture slices, TX windows between them ---
    let mut encoder = a.fec.map(|(k, m)| fec::Encoder::new(k, m));
    let mut seq: u16 = 0;
    let mut jitter = Jitter::new(u64::from_le_bytes([node_addr[0], node_addr[1], node_addr[2], node_addr[3],
                                                    node_addr[4], node_addr[5], 0, 0]));
    let end = a.seconds.map(|s| t0 + Duration::from_secs_f64(s));
    let mut errors = 0;
    let mut last_check = Instant::now();
    let mut last_line = Instant::now();
    let (mut tx_frames, mut tx_windows, mut tx_stalls) = (0u64, 0u64, 0u64);
    while !sys::stop_requested() && end.map_or(true, |e| Instant::now() < e) {
        let slice_ms = match end {
            Some(e) => (e.saturating_duration_since(Instant::now()).as_millis() as u32).clamp(100, a.slice_ms),
            None => a.slice_ms,
        };
        let min_rx = Duration::from_millis(a.min_rx_ms) + jitter.below(Duration::from_millis(20));
        let txq_ref = &txq;
        let step = (|| -> io::Result<()> {
            if a.regain {
                // re-apply the manual gain forces (MCU / RF writes, between slices): a near-field transmitter can trip
                // a receive protection that otherwise stays latched
                card.set_gain(a.gain.0, a.gain.1, a.gain.2)?;
            }
            let ended = rx.slice(&card, &hooks, slice_ms, min_rx, &|| tx_on && !txq_ref.lock().unwrap().is_empty())?;
            // the hook has returned: MCU commands are safe again. Check after every full slice, and at least every
            // 10 s, that nobody (mac80211, the driver) retuned the card
            let mut fw = String::new();
            if ended == SliceEnd::Full || last_check.elapsed() > Duration::from_secs(10) {
                last_check = Instant::now();
                let st = card.iq_stat(&hooks)?;
                let khz = card.tuned_khz()?;
                if (khz as i64 - a.freq as i64 * 1000).abs() > 1000 {
                    eprintln!("# {} found at {:.3} MHz: setting it up again", card.phy, khz as f64 / 1000.0);
                    setup_radio(&card, &a)?;
                }
                fw = format!(" | fw state {} sent {} overruns {}", st.state, st.sent, st.overruns);
            }
            // a status line with each firmware check, and at least every --status-s (host counters only)
            if !fw.is_empty() || last_line.elapsed().as_secs_f64() >= a.status_s {
                last_line = Instant::now();
                let s = &rx.stats;
                eprintln!("# {:.1} s: {} blocks ({} dropped), {} frames ({} good), {} packets delivered ({} rebuilt by FEC), \
                           TX {tx_frames} frames in {tx_windows} windows ({tx_stalls} stalled, {} queue drops){fw}",
                          t0.elapsed().as_secs_f64(), s.blocks.load(Ordering::Relaxed),
                          s.dropped_blocks.load(Ordering::Relaxed), delivered.frames.load(Ordering::Relaxed),
                          delivered.good.load(Ordering::Relaxed), delivered.packets.load(Ordering::Relaxed),
                          delivered.recovered.load(Ordering::Relaxed), queue_drops.load(Ordering::Relaxed));
            }
            if !tx_on || ended == SliceEnd::Stopped {
                return Ok(());
            }
            // take packets for this window, within its airtime budget (FEC parity included)
            let overhead = a.fec.map_or(1.0, |(k, m)| (k + m) as f64 / k as f64);
            let budget = Duration::from_millis(a.max_tx_ms);
            let mut pending = Vec::new();
            let mut air = Duration::ZERO;
            {
                let mut q = txq.lock().unwrap();
                while let Some(p) = q.front() {
                    let psdu = p.len() + 50 + div8_ndbps.map_or(0, |n| profile::div8_pad_bytes(p.len() + 46, n));
                    let t = a.profile.airtime(psdu, a.divider()).mul_f64(overhead);
                    if !pending.is_empty() && air + t > budget {
                        break;
                    }
                    air += t;
                    pending.push(q.pop_front().unwrap());
                }
            }
            if pending.is_empty() {
                return Ok(());
            }
            let frames = build_frames(&pending, &a, node_addr, card.mac, encoder.as_mut(), &mut seq, div8_ndbps);
            // on-air time plus the card's per-frame injection overhead (~3.5 ms: USB, firmware, MAC access at ÷4
            // timing; measured 4.2 ms between the frames of one window for 0.8 ms of airtime)
            let air: Duration = frames.iter().map(|f| a.profile.airtime(frame::psdu_len(f), a.divider())
                + Duration::from_micros(3500)).sum();
            let rep = card.tx_window(&frames, air + Duration::from_millis(5), Duration::from_millis(a.stall_ms))?;
            if rep.refused > 0 {
                eprintln!("# {:.1} s: the kernel refused {} of {} frames", t0.elapsed().as_secs_f64(), rep.refused,
                          frames.len());
            }
            if !rep.drained() {
                // The MAC kept deferring (seen after the other card's bursts at 10 cm: one queue stays non-empty
                // until the PHY is reset). A new channel switch (with the capture and gain setup) clears it at once;
                // the frames still queued are lost to the FEC.
                tx_stalls += 1;
                let t = Instant::now();
                if a.retune {
                    setup_radio(&card, &a)?;
                }
                eprintln!("# {:.1} s: TX window of {} frames stalled ({:08x?}){}", t0.elapsed().as_secs_f64(),
                          frames.len(), rep.qempty,
                          if a.retune { format!("; re-tuned in {:.0} ms", t.elapsed().as_secs_f64() * 1e3) }
                          else { String::new() });
            }
            tx_frames += frames.len() as u64;
            tx_windows += 1;
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
                // (the managed interface keeps its name), with a new monitor interface
                if let Ok(c) = Card::open(&a.iface) {
                    if c.phy != card.phy {
                        eprintln!("# {} is back as {}: attaching again", a.iface, c.phy);
                        card = c;
                        if let Err(e) = card.make_monitor(&a.monitor, a.txpower) {
                            eprintln!("# monitor interface: {e}");
                        }
                    }
                }
                rx.recover(&card, &hooks);
                if let Err(e) = setup_radio(&card, &a) {
                    eprintln!("# setup failed: {e}");
                }
            }
        }
    }

    // --- shutdown: the last slice has returned (or recover() stopped it); undo the gain forces and TXD overrides ---
    sleep(Duration::from_millis(300)); // let the decoders deliver the last blocks
    rx.unbind();
    card.release()?;
    eprintln!("# total: {} frames ({} good), {} packets delivered ({} rebuilt by FEC), TX {tx_frames} frames in \
               {tx_windows} windows ({tx_stalls} stalled)",
              delivered.frames.load(Ordering::Relaxed), delivered.good.load(Ordering::Relaxed),
              delivered.packets.load(Ordering::Relaxed), delivered.recovered.load(Ordering::Relaxed));
    Ok(())
}

/// The frames of one TX window. Without FEC, one 802.11 frame per packet: in TAP mode with the Ethernet frame's own
/// addresses and ethertype (as nbrxd sends), in TUN mode from the card's address to broadcast. With FEC, the packets
/// become data units (TAP: the whole Ethernet frame, ethertype 0x6558; TUN: the IP packet), coded into blocks, and
/// every shard goes from the node's address to broadcast.
fn build_frames(pending: &[Vec<u8>], a: &Args, node_addr: [u8; 6], card_mac: [u8; 6],
                encoder: Option<&mut fec::Encoder>, seq: &mut u16, div8_ndbps: Option<usize>) -> Vec<Vec<u8>> {
    let rate = a.profile.radiotap_rate();
    let mut next_seq = || {
        let s = *seq;
        *seq = seq.wrapping_add(1);
        s
    };
    match encoder {
        None => pending
            .iter()
            .filter_map(|p| match a.mode {
                Mode::Tap if p.len() >= 14 => {
                    let mac = |o: usize| -> [u8; 6] { p[o..o + 6].try_into().unwrap() };
                    let et = u16::from_be_bytes([p[12], p[13]]);
                    Some(frame::tx_frame(mac(6), mac(0), next_seq(), et, &p[14..], rate, div8_ndbps))
                }
                Mode::Tap => None,
                Mode::Tun => netdev::ip_ethertype(p)
                    .map(|et| frame::tx_frame(card_mac, BROADCAST, next_seq(), et, p, rate, div8_ndbps)),
            })
            .collect(),
        Some(enc) => {
            let units: Vec<(u16, Vec<u8>)> = pending
                .iter()
                .filter_map(|p| match a.mode {
                    Mode::Tap if p.len() >= 14 => Some((ETH_BRIDGED, p.clone())),
                    Mode::Tap => None,
                    Mode::Tun => netdev::ip_ethertype(p).map(|et| (et, p.clone())),
                })
                .collect();
            if units.is_empty() {
                return Vec::new();
            }
            enc.encode(&units)
                .iter()
                .map(|body| frame::tx_frame(node_addr, BROADCAST, next_seq(), fec::ETHERTYPE, body, rate, div8_ndbps))
                .collect()
        }
    }
}
