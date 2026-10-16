//! One MT7921AU card running the instrumented firmware (fw/, `make install`) with the mt7921dbg kernel module: every
//! access the narrow-band node makes to the card. The setup steps are those of tools/guest/nbprep.py, in the same
//! order and for the same reasons; the capture-slice and TX-window logic is that of tools/guest/nbrxd.py.
//!
//! Access paths. Each method says which one it uses:
//! - **debug command**: mt7921dbg's `cmd` file, which becomes MCU CE command 0xcb (fw/src/dbgcmd.c). The MCU itself
//!   reads or writes memory and registers, or calls a firmware/ROM function. It reaches everything, but the MCU must
//!   be free: never use it while a capture slice runs (the capture overwrites the packet SRAM that carries commands).
//! - **host register path**: mt76's debugfs `regidx` / `regval`, i.e. USB vendor requests. Register access without
//!   the MCU: the only path that is safe while the IQ stream hook runs.
//! - **EXT command**: mt7921dbg's `raw` file: a driver MCU command (channel switch), as mt76 would send it.
//! - **mt7921dbg module parameters**: TXD overrides applied to every injected frame (rate, coding, attempts).
//! - **monitor interface**: frames to transmit, as radiotap + 802.11 ("injection").
//!
//! Hazards (notes/03, 07; never do these from here): MCU reads of 0x8103xxxx or unmapped memory hang the MCU; host
//! reads at or after 0xe0300000 reset the card; never write 0x88009000, 0x88009004 [23:0] or 0x88009008; leave the
//! CONN AFE (0x7c003xxx) alone.

use std::collections::HashMap;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::os::unix::io::AsRawFd;
use std::process::{Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use crate::profile::Profile;
use crate::sys::{self, PacketSocket};

/// mt7921dbg's debugfs directory.
pub const MT7921DBG: &str = "/sys/kernel/debug/mt7921dbg/";
/// mt7921dbg's module parameters (TXD overrides).
const PARAMS: &str = "/sys/module/mt7921dbg/parameters/";
/// The lock every tool that uses mt7921dbg's single "target" takes (tools/guest/dbglock.py).
const LOCK: &str = "/tmp/fwdbg.lock";
/// First word of every debug-command answer ("MT79", fw/src/dbgcmd.c).
const DBG_MAGIC: u32 = 0x4D54_3739;
/// The MCU event header the driver may leave in front of the answer.
const MCU_RXD_LEN: usize = 0x24;

// Debug command operations (fw/src/dbgcmd.c)
const OP_READ: u32 = 1;
const OP_WRITE: u32 = 2;
const OP_CALL: u32 = 3;
/// Answer first, then call: for calls that keep the MCU busy (the IQ stream hook).
const OP_CALL_DETACH: u32 = 4;
/// At most this many words per read or write command.
const DBG_MAX_WORDS: usize = 64;

/// mt76 MCU command id of an EXT command: MCU_EXT_PREFIX (0xed) | ext id << 8.
const MCU_EXT: u32 = 0xED;
const EXT_CHANNEL_SWITCH: u32 = 0x08;
const EXT_SET_RX_PATH: u32 = 0x4E;

// Boot ROM helpers (notes/04): A-die RF register read / write through the MCU
const ROM_RF_READ: u32 = 0x0083_A662;
const ROM_RF_WRITE: u32 = 0x0083_A66C;
/// A-die RF register holding the frequency the synthesizer is tuned to, in kHz.
const RF_TUNED_KHZ: u32 = 0x184;

/// PHY band mode: [30:28] transmit divider (5 = ÷4 = 5 MHz, 7 = ÷8 = 2.5 MHz), [27:24] bandwidth class.
pub const PHY_BN_MODE: u32 = 0x8308_1000;
/// MAC tick configuration: the channel switch at cbw 4 sets 0x5.
pub const WFSYS_TICKCR0: u32 = 0x820B_00A0;
/// MAC arbiter: bit 8 = MAC TX off, bit 9 = MAC RX off.
pub const ARB_SCR: u32 = 0x820E_3000;
const ARB_TX_OFF: u32 = 0x100;
const ARB_RX_OFF: u32 = 0x200;
/// MTU of the monitor interface: the largest frame (radiotap + 802.11, without FCS) that can be injected.
pub const MONITOR_MTU: usize = 2304;
/// PLE AC queue-empty bitmaps (MT_PLE_AC_QEMPTY), 4 registers 0x40 apart: a cleared bit is a non-empty queue.
const PLE_AC_QEMPTY: u32 = 0x820C_0500;

/// Addresses of the instrumented firmware's IQ stream hook and its control blocks (fw/build/hooks.nm, installed as
/// /root/fwre/hooks.nm). They change with every firmware build.
#[derive(Clone, Debug)]
pub struct Hooks {
    /// struct iq_cfg (fw/src/iqstream.c): chunk, inflight, thresh, hang_us, pre, nsum, stop, dur_ms, ratio16, hits.
    pub iq_cfg: u32,
    /// struct iq_stat: state, sent, max_backlog, t_us, skipped, overruns, busy_checks, checks, floor.
    pub iq_stat: u32,
    /// u32 hk_iqstream(npkt, eid, settle_us, timeout_us).
    pub hk_iqstream: u32,
}

impl Hooks {
    /// Parse an `nm` listing ("address type name" per line).
    pub fn load(path: &str) -> io::Result<Hooks> {
        let mut sym = HashMap::new();
        for line in fs::read_to_string(path)?.lines() {
            let f: Vec<&str> = line.split_whitespace().collect();
            if f.len() == 3 {
                if let Ok(a) = u32::from_str_radix(f[0], 16) {
                    sym.insert(f[2].to_string(), a);
                }
            }
        }
        let get = |n: &str| sym.get(n).copied().ok_or_else(|| other(format!("{path}: no symbol {n}")));
        Ok(Hooks { iq_cfg: get("iq_cfg")?, iq_stat: get("iq_stat")?, hk_iqstream: get("hk_iqstream")? })
    }
}

/// The state word of iq_stat: 0 until the hook starts, 1 while it streams, then how it ended.
pub const IQ_STATE_RUNNING: [u32; 2] = [0, 1];

pub fn other<E: Into<Box<dyn std::error::Error + Send + Sync>>>(e: E) -> io::Error {
    io::Error::new(io::ErrorKind::Other, e)
}

/// The card's RF band for a frequency: (CHANNEL_SWITCH band, a channel to hop through first, the channel to switch
/// to). The frequency override replaces the channel's frequency, but the firmware skips a switch to the channel it
/// is already on (notes/04), so every tune hops through another channel of the same band first.
pub fn rf_band(mhz: u32) -> io::Result<(u8, u8, u8)> {
    match mhz {
        2300..=2500 => Ok((0, 11, 1)),  // 2.4 GHz RF (also covers the 2.3 GHz amateur band)
        4900..=5925 => Ok((1, 149, 36)), // 5 GHz RF (includes the 5.9 GHz ITS band)
        5926..=7125 => Ok((2, 5, 1)),   // 6 GHz RF
        _ => Err(other(format!("{mhz} MHz is outside the card's RF bands"))),
    }
}

pub struct Card {
    /// The card's managed interface (e.g. wlx00c0cab79628). It is taken down: mac80211 would retune the card.
    pub iface: String,
    /// The wiphy name (phyN) that mt7921dbg uses to select the card.
    pub phy: String,
    /// The card's MAC address.
    pub mac: [u8; 6],
    /// mt76's own debugfs directory of this card (host register path).
    mt76: String,
    /// Raw socket on the monitor interface, once `make_monitor` has run.
    tx: Option<PacketSocket>,
}

impl Card {
    pub fn open(iface: &str) -> io::Result<Card> {
        let phy = fs::read_to_string(format!("/sys/class/net/{iface}/phy80211/name"))?.trim().to_string();
        let mac = parse_mac(fs::read_to_string(format!("/sys/class/net/{iface}/address"))?.trim())
            .ok_or_else(|| other(format!("{iface}: unreadable MAC address")))?;
        Ok(Card { iface: iface.to_string(), mt76: format!("/sys/kernel/debug/ieee80211/{phy}/mt76/"), phy, mac, tx: None })
    }

    // --- access paths -------------------------------------------------------------------------------------------

    /// mt7921dbg holds one "target" card for all tools: select ours and run `f` under the shared lock, so that
    /// another tool cannot redirect the target to the other card in between.
    fn with_target<T>(&self, f: impl FnOnce() -> io::Result<T>) -> io::Result<T> {
        let lock = OpenOptions::new().create(true).write(true).open(LOCK)?;
        sys::lock_exclusive(lock.as_raw_fd())?;
        let r = fs::write(format!("{MT7921DBG}target"), &self.phy).and_then(|_| f());
        sys::unlock(lock.as_raw_fd());
        r
    }

    /// The instrumented firmware's debug command (MCU CE cid 0xcb, fw/src/dbgcmd.c), through mt7921dbg's "cmd" file.
    /// Request: op, address, word count, four call arguments, then the data words. Answer: magic, op, status, return
    /// value, word count, words. Returns (return value, words).
    fn debug_cmd(&self, op: u32, addr: u32, n: usize, args: &[u32], data: &[u32]) -> io::Result<(u32, Vec<u32>)> {
        let mut a = [0u32; 4];
        a[..args.len()].copy_from_slice(args);
        let mut req = Vec::with_capacity(28 + 4 * data.len());
        for w in [op, addr, n as u32, a[0], a[1], a[2], a[3]].iter().chain(data) {
            req.extend_from_slice(&w.to_le_bytes());
        }
        let raw = self.with_target(|| {
            // one write sends the command and waits for the MCU's answer, which the next read returns
            OpenOptions::new().write(true).open(format!("{MT7921DBG}cmd"))?.write_all(&req)?;
            fs::read(format!("{MT7921DBG}cmd"))
        })?;
        let word = |o: usize| raw.get(o..o + 4).map(|b| u32::from_le_bytes(b.try_into().unwrap()));
        let off = if word(0) == Some(DBG_MAGIC) { 0 } else { MCU_RXD_LEN };
        if word(off) != Some(DBG_MAGIC) {
            return Err(other(format!("{}: no debug-command answer (instrumented firmware loaded?)", self.phy)));
        }
        let (status, ret, cnt) = (word(off + 8).unwrap_or(1), word(off + 12).unwrap_or(0), word(off + 16).unwrap_or(0));
        if status != 0 {
            return Err(other(format!("{}: debug command op {op} refused (status {status})", self.phy)));
        }
        let words: Option<Vec<u32>> = (0..cnt as usize).map(|i| word(off + 20 + 4 * i)).collect();
        Ok((ret, words.ok_or_else(|| other("short debug-command answer"))?))
    }

    /// Register or memory words read by the MCU (debug command op 1).
    pub fn mcu_read_words(&self, addr: u32, n: usize) -> io::Result<Vec<u32>> {
        assert!(n <= DBG_MAX_WORDS);
        let w = self.debug_cmd(OP_READ, addr, n, &[], &[])?.1;
        if w.len() != n {
            return Err(other("short MCU read"));
        }
        Ok(w)
    }

    /// Register or memory word read by the MCU (debug command op 1).
    pub fn mcu_read(&self, addr: u32) -> io::Result<u32> {
        Ok(self.mcu_read_words(addr, 1)?[0])
    }

    /// Consecutive register or memory words written by the MCU (debug command op 2), in one command.
    pub fn mcu_write_words(&self, addr: u32, words: &[u32]) -> io::Result<()> {
        assert!(words.len() <= DBG_MAX_WORDS);
        self.debug_cmd(OP_WRITE, addr, words.len(), &[], words).map(|_| ())
    }

    pub fn mcu_write(&self, addr: u32, value: u32) -> io::Result<()> {
        self.mcu_write_words(addr, &[value])
    }

    /// Read-modify-write through the MCU: only the bits in `mask` change.
    pub fn mcu_rmw(&self, addr: u32, mask: u32, value: u32) -> io::Result<()> {
        let v = self.mcu_read(addr)?;
        self.mcu_write(addr, (v & !mask) | (value & mask))
    }

    /// Call a firmware/ROM function with up to 4 arguments (debug command op 3); returns its return value.
    pub fn mcu_call(&self, func: u32, args: &[u32]) -> io::Result<u32> {
        Ok(self.debug_cmd(OP_CALL, func, 0, args, &[])?.0)
    }

    /// Call a function after the answer has been sent (debug command op 4): the MCU stays in the function after
    /// this returns.
    pub fn mcu_call_detached(&self, func: u32, args: &[u32]) -> io::Result<()> {
        self.debug_cmd(OP_CALL_DETACH, func, 0, args, &[]).map(|_| ())
    }

    /// Register read over the USB register path (mt76 debugfs regidx/regval). Works while the MCU is busy.
    pub fn host_read(&self, addr: u32) -> io::Result<u32> {
        fs::write(format!("{}regidx", self.mt76), addr.to_string())?;
        let s = fs::read_to_string(format!("{}regval", self.mt76))?;
        parse_u32(s.trim()).ok_or_else(|| other(format!("regval: unexpected {s:?}")))
    }

    /// Register write over the USB register path (mt76 debugfs regidx/regval).
    pub fn host_write(&self, addr: u32, value: u32) -> io::Result<()> {
        fs::write(format!("{}regidx", self.mt76), addr.to_string())?;
        fs::write(format!("{}regval", self.mt76), value.to_string())
    }

    /// Read-modify-write over the USB register path: clear the bits in `clear`, then OR in `bits` (`bits` may lie
    /// outside `clear`, as in tools/guest/icap.py's upd()).
    pub fn host_update(&self, addr: u32, clear: u32, bits: u32) -> io::Result<()> {
        let v = self.host_read(addr)?;
        self.host_write(addr, (v & !clear) | bits)
    }

    /// Send an MCU EXT command as the driver would (mt7921dbg "raw": u32 mt76 command id, then the payload) and wait
    /// for the firmware's answer.
    fn ext_cmd(&self, ext_id: u32, payload: &[u8]) -> io::Result<()> {
        let mut msg = (MCU_EXT | ext_id << 8).to_le_bytes().to_vec();
        msg.extend_from_slice(payload);
        self.with_target(|| OpenOptions::new().write(true).open(format!("{MT7921DBG}raw"))?.write_all(&msg))
    }

    /// A-die RF register read through the boot ROM's rfRead(reg, path).
    pub fn rf_read(&self, reg: u32, path: u32) -> io::Result<u32> {
        self.mcu_call(ROM_RF_READ, &[reg, path])
    }

    /// A-die RF register write through the boot ROM's rfWrite(reg, value, path).
    pub fn rf_write(&self, reg: u32, value: u32, path: u32) -> io::Result<()> {
        self.mcu_call(ROM_RF_WRITE, &[reg, value, path]).map(|_| ())
    }

    /// The frequency the synthesizer is tuned to, in kHz (RF 0x184).
    pub fn tuned_khz(&self) -> io::Result<u32> {
        self.rf_read(RF_TUNED_KHZ, 0)
    }

    // --- setup steps (tools/guest/nbprep.py) --------------------------------------------------------------------

    /// EXT CHANNEL_SWITCH, then SET_RX_PATH (the driver sends both), at cbw 4, the firmware's native 10 MHz mode. At
    /// cbw 4 the firmware itself sets the ÷4 transmit divider (PHY_BN_MODE [30:28] = 5: 5 MHz PPDUs, notes/07) and
    /// WFSYS_TICKCR0 = 0x5 (without that MAC tick the transmitter never ends a narrow PPDU, notes/03), and it narrows
    /// the receive front end to +-5 MHz, which the IQ packers need (notes/08). Returns the tuned frequency in kHz.
    pub fn tune(&self, mhz: u32, tx_streams: u8, rx_mask: u8) -> io::Result<u32> {
        let (band, hop, ch) = rf_band(mhz)?;
        // the driver's struct (mt76 mt7921_mcu_set_chan_info): control channel, centre channel, cbw (driver CMD_CBW_*:
        // 0 = 20, 4 = 10 MHz), TX path count, RX path mask, switch reason, band index (DBDC), centre channel 2
        // (80+80), u16 CAC case, channel band (0/1/2 = 2.4/5/6 GHz), reserved, u32 outband_freq (the frequency
        // override in MHz, 0 = the channel's own frequency; the firmware tunes the RF to it, notes/04), TX power
        // drop, AP bandwidth, AP centre channel, 57 reserved bytes
        let payload = |channel: u8, cbw: u8, outband_mhz: u32| {
            let mut p = vec![channel, channel, cbw, tx_streams, rx_mask, 0, 0, 0];
            p.extend_from_slice(&0u16.to_le_bytes());
            p.extend_from_slice(&[band, 0]);
            p.extend_from_slice(&outband_mhz.to_le_bytes());
            p.extend_from_slice(&[0, 0, 0]);
            p.extend_from_slice(&[0; 57]);
            p
        };
        // hop at 20 MHz (as nbrxd and chsw.py do), then the real switch at cbw 4 with the override
        for (channel, cbw, outband) in [(hop, 0, 0), (ch, 4, mhz)] {
            let p = payload(channel, cbw, outband);
            self.ext_cmd(EXT_CHANNEL_SWITCH, &p)?;
            self.ext_cmd(EXT_SET_RX_PATH, &p)?;
        }
        let tuned = self.tuned_khz()?; // what the synthesizer is really tuned to
        if (tuned as i64 - mhz as i64 * 1000).abs() > 1000 {
            return Err(other(format!("{}: tuned to {tuned} kHz instead of {mhz} MHz", self.phy)));
        }
        Ok(tuned)
    }

    /// 5 MHz: the ÷4 divider the channel switch already set. 2.5 MHz: PHY_BN_MODE [30:28] = 7, the ÷8 divider.
    /// Must come before the capture setup: written later (at the first TX window), the PA keys an unmodulated
    /// carrier instead of the PPDU (measured, notes/09).
    pub fn set_width(&self, divider: u32) -> io::Result<()> {
        match divider {
            4 => Ok(()),
            8 => self.mcu_rmw(PHY_BN_MODE, 0x7000_0000, 0x7000_0000),
            _ => Err(other("the transmitter divides by 4 or 8 only (no ÷2 exists for 10 MHz, notes/07)")),
        }
    }

    /// WFSYS_TICKCR0 [4:0] (MCU write; the only bits of WFSYS_ON that may be written): [1:0] WLAN_CLK_MODE,
    /// [4:2] LMAC_1X_MODE. It scales the MAC's timing (when a TX PPDU ends, slot, SIFS; notes/03, 05). The channel
    /// switch at cbw 4 writes 0x5 (10 MHz MAC timing); the firmware's own 5 MHz mode writes 0xa.
    pub fn set_tickcr0(&self, v: u32) -> io::Result<()> {
        self.mcu_rmw(WFSYS_TICKCR0, 0x1F, v & 0x1F)
    }

    /// PHY_BN_MODE [27:24] = 4 while receiving (MCU write): the chain behind the front end runs at the narrow rate and
    /// the card's own 20 MHz demodulator stops locking onto narrow frames (notes/03). A TX window puts class 0 back.
    pub fn set_rx_class(&self) -> io::Result<()> {
        self.mcu_rmw(PHY_BN_MODE, 0x0F00_0000, 0x0400_0000)
    }

    /// Program the internal IQ capture (ICAP, 0x80021000; MT7996's ICAPn register for register, notes/08) without
    /// starting it: the firmware hook hk_iqstream starts each capture slice. Host-path writes, like tools/guest/icap.py.
    /// `pack`: ICAP_CR10 [19:16] sample packer, 2 = uniform 10 Msps, 8 = 30 Msps (notes/09).
    pub fn icap_setup(&self, pack: u32) -> io::Result<()> {
        // MAC: receive on (ARB_SCR bit 9 clear; with MAC RX off the PHY front end freezes), transmit off (bit 8 set)
        self.host_update(ARB_SCR, ARB_RX_OFF, ARB_TX_OFF)?;
        // band clock forces 0x83080004/08 |= 0x3840: keep the capture path's clocks running (BirriaSDR's sequence)
        self.host_update(0x8308_0004, 0, 0x3840)?;
        self.host_update(0x8308_0008, 0, 0x3840)?;
        // PHY-top capture routing (BirriaSDR): 0x830a3008 [22:21] = 0, 0x830a1000 = 0x04090100,
        // 0x830a1004 [7:0] = 0x10, 0x830a1008 = 0
        self.host_update(0x830A_3008, 3 << 21, 0)?;
        self.host_write(0x830A_1000, 0x0409_0100)?;
        self.host_update(0x830A_1004, 0xFF, 0x10)?;
        self.host_write(0x830A_1008, 0)?;
        // ICAP_CR0 control = 0xf1: enabled, not capturing yet ([0] EN, [7:4] = 0xf as the firmware writes)
        self.host_write(0x8002_1090, 0xF1)?;
        // ICAP_CR2 / CR3: the ring, 64 K samples from the start of the capture SRAM (0xe0200000), end inclusive
        self.host_write(0x8002_1098, 0)?;
        self.host_write(0x8002_109C, 65536 - 4)?;
        // ICAP_CR10: [19:16] sample packer, plus the SRAM-interface selects 0x9c000000 that BirriaSDR uses
        self.host_update(0x8002_10B8, 0xF_0000, 0x9C00_0000 | (pack & 0xF) << 16)?;
        // capture node, domain 4 (BirriaSDR's): mode byte 0x13 in 0x830ad440 [7:0] with the enable bit 31;
        // 0x830ad448: [23] node select, [19:18] rate 3 = 40 Msps, [15:8] tap 4 = the receive front end's output
        self.host_update(0x830A_D440, 0xFF, 0x8000_0013)?;
        self.host_update(0x830A_D448, 0x0FFF_FFFF, 0x0080_0000 | 3 << 18 | 4 << 8)?;
        // PSE_ICAP_GRANT 0x88009004 [27:24] = 0xf: lend the SRAM banks to the capture. HAZARD: never touch [23:0]
        self.host_update(0x8800_9004, 0, 0x0F00_0000)?;
        // ICAP_CR0 = 0x4f1: ring mode with the soft reset released ([10] SW_RST_B), still not capturing
        self.host_write(0x8002_1090, 0x4F1)
    }

    /// Manual receive gain (BirriaSDR's recipe, notes/03): the AGC hunts between gain states on noise, which an IQ
    /// receiver sees as level jumps. LNA 0-3, filter 0-8, digital 0-12. After the capture setup: the forces applied
    /// first leave the ÷8 transmitter keying an unmodulated carrier (notes/09).
    pub fn set_gain(&self, lna: u32, lpf: u32, dig: u32) -> io::Result<()> {
        for path in 0..2 {
            // A-die RF 0x768 per chain: [31] force, [28:27] LNA, [26:23] filter gain
            self.rf_write(0x768, 0x8000_0000 | lna << 27 | lpf << 23, path)?;
        }
        self.mcu_rmw(0x8300_0108, 0x3F3F_0000, dig << 24 | dig << 16)?; // digital gain, both halves
        self.mcu_rmw(0x8300_0150, 0x8070_F000, 0x8000_0000 | lna << 20)?; // D-die LNA force
        self.mcu_rmw(
            0x8300_00C4,
            0xF7F7_F707,
            7 | lna << 8 | lpf << 12 | lna << 16 | lpf << 20 | lna << 24 | lpf << 28,
        )
    }

    /// mt7921dbg rewrites the TXD of every injected frame (kretprobe on mt76_connac2_mac_write_txwi): the rate code,
    /// the coding fields and one transmission attempt (tx_count = 1). At narrow widths no acknowledgement can come
    /// back (the receiving card cannot decode the frame), so mt76's 15 attempts would only waste air time.
    pub fn set_tx_overrides(&self, p: &Profile) -> io::Result<()> {
        for (name, value) in [
            ("tx_rate", p.rate_code()),
            ("tx_dw6_set", p.dw6_set()),
            ("tx_dw6_clr", p.dw6_clr()),
            ("tx_count", 1),
        ] {
            fs::write(format!("{PARAMS}{name}"), value.to_string())?;
        }
        Ok(())
    }

    /// A monitor interface to inject frames on (raw 802.11 + radiotap), with the TX power, through `ip` and `iw`.
    /// The managed interface goes down: mac80211 would otherwise scan and retune the card.
    pub fn make_monitor(&mut self, name: &str, txpower_dbm: f64) -> io::Result<()> {
        self.tx = None;
        run(&["ip", "link", "set", &self.iface, "down"], true)?;
        run(&["iw", "dev", name, "del"], false)?; // a leftover from an earlier run
        run(&["iw", "phy", &self.phy, "interface", "add", name, "type", "monitor", "flags", "fcsfail", "control",
              "otherbss"], true)?;
        // the largest MPDU mac80211 accepts (IEEE80211_MAX_DATA_LEN): ÷8 frames carry up to ~2x their payload
        run(&["ip", "link", "set", name, "mtu", &MONITOR_MTU.to_string()], false)?;
        run(&["ip", "link", "set", name, "up"], true)?;
        let mbm = ((txpower_dbm * 100.0).round() as i64).to_string();
        run(&["iw", "dev", name, "set", "txpower", "fixed", &mbm], false)?;
        self.tx = Some(PacketSocket::bind_send_only(name)?);
        Ok(())
    }

    /// Undo what outlives the node: the manual gain forces and the TXD overrides. A channel switch or a firmware
    /// reload restores the other PHY registers.
    pub fn release(&self) -> io::Result<()> {
        self.mcu_rmw(0x8300_00C4, 0xF7F7_F707, 0)?;
        self.mcu_rmw(0x8300_0150, 0x8070_F000, 0)?;
        for path in 0..2 {
            self.rf_write(0x768, 0, path)?;
        }
        for name in ["tx_rate", "tx_dw6_set", "tx_dw6_clr", "tx_count"] {
            fs::write(format!("{PARAMS}{name}"), "0")?;
        }
        Ok(())
    }

    /// Read back what defines the narrow mode (MCU reads; not during a capture slice).
    pub fn status(&self) -> io::Result<String> {
        let bn = self.mcu_read(PHY_BN_MODE)?;
        let mut s = format!(
            "tuned {} kHz, PHY_BN_MODE {bn:#010x} (divider code [30:28] = {}: 5 = 5 MHz, 7 = 2.5 MHz; class [27:24] = {}), \
             WFSYS_TICKCR0 {:#x}, ARB_SCR {:#010x}",
            self.tuned_khz()?,
            bn >> 28 & 7,
            bn >> 24 & 0xF,
            self.mcu_read(WFSYS_TICKCR0)?,
            self.mcu_read(ARB_SCR)?
        );
        for name in ["tx_rate", "tx_dw6_set", "tx_dw6_clr", "tx_count"] {
            s += &format!(", {name} {}", fs::read_to_string(format!("{PARAMS}{name}"))?.trim());
        }
        Ok(s)
    }

    // --- IQ capture slices (fw/src/iqstream.c, tools/guest/nbrxd.py) ---------------------------------------------

    /// Start one capture slice: the firmware hook hk_iqstream streams the ICAP ring to the host until `dur_ms`
    /// elapses or `stop_slice` is called. From here until `slice_running` says it ended, use host-path accesses only.
    pub fn start_slice(&self, hooks: &Hooks, dur_ms: u32) -> io::Result<()> {
        // iq_cfg (MCU write, one command): 4080 samples per USB packet, 1 descriptor in flight (three corrupted the
        // stream), no energy gate (thresh 0: the packers stream continuously, and frames below the noise floor are
        // found by the software preamble detector), hang 500 us, 4096 pre-trigger samples, 128 samples per energy
        // check, stop flag 0, the slice duration, adaptive gate off, 1 hit
        self.mcu_write_words(hooks.iq_cfg, &[4080, 1, 0, 500, 4096, 128, 0, dur_ms, 0, 1])?;
        // iq_stat.state = 0 until the hook runs (host path: the next poll must not see the last slice's end state)
        self.host_write(hooks.iq_stat, 0)?;
        // detached call (op 4): the firmware answers first, then runs the hook, because the capture ring overwrites
        // the packet SRAM that carries answers. Arguments: no packet limit, event id 0xcc (what mt7921dbg collects),
        // 3 ms run-in so that the answer leaves the SRAM first, 2 s DMA stall limit (a host that stops reading for a
        // while costs overruns, not a wedged card)
        self.mcu_call_detached(hooks.hk_iqstream, &[0xFFFF_FFFF, 0xCC, 3000, 2_000_000])
    }

    /// Whether the hook still runs (host-path read of iq_stat.state). The read goes to packet SRAM: call it only
    /// once the stream has been quiet for 30 ms, and never in a slice's first 50 ms (`Slice` enforces both).
    pub fn slice_running(&self, hooks: &Hooks) -> io::Result<bool> {
        Ok(IQ_STATE_RUNNING.contains(&self.host_read(hooks.iq_stat)?))
    }

    /// Ask the hook to end the slice: one host-path write of iq_cfg.stop.
    pub fn stop_slice(&self, hooks: &Hooks) -> io::Result<()> {
        self.host_write(hooks.iq_cfg + 24, 1)
    }

    /// The slice's end statistics (MCU read; only after the hook has returned).
    pub fn iq_stat(&self, hooks: &Hooks) -> io::Result<IqStat> {
        let w = self.mcu_read_words(hooks.iq_stat, 9)?;
        Ok(IqStat { state: w[0], sent: w[1], overruns: w[5], busy: w[6], checks: w[7], floor: w[8] })
    }

    /// Transmit `frames` (radiotap + 802.11, see frame.rs) between two capture slices. MAC TX is on only while
    /// sending, and goes off only once the device queues are empty: a frame left queued in the packet SRAM would be
    /// overwritten by the next slice's capture ring, and the firmware then hangs on the queue.
    /// `airtime`: how long the frames take to leave (waited in full); `drain_limit`: how long to wait after that for
    /// the queues to empty. A queue that does not drain is reported (`TxReport::drained`): the MAC kept deferring.
    pub fn tx_window(&self, frames: &[Vec<u8>], airtime: Duration, drain_limit: Duration) -> io::Result<TxReport> {
        let sock = self.tx.as_ref().ok_or_else(|| other("no monitor interface"))?;
        let t0 = Instant::now();
        // class 0 for the window (MCU write): class 4 with forced gain makes the MAC defer transmissions (CCA)
        self.mcu_rmw(PHY_BN_MODE, 0x0F00_0000, 0)?;
        // MAC TX on (MCU write of ARB_SCR bit 8): left on through a capture slice, the card emits a ~24 us pulse
        // train that wrecks its own reception (notes/09)
        self.mcu_rmw(ARB_SCR, ARB_TX_OFF, 0)?;
        // inject; a frame the kernel refuses (e.g. too long) is counted and skipped, the window goes on
        let mut refused = 0;
        for f in frames {
            if sock.send(f).is_err() {
                refused += 1;
            }
        }
        sleep(airtime);
        // wait for the PLE queues to drain (MCU reads), at most drain_limit
        let t_drain = Instant::now();
        let limit = t_drain + drain_limit;
        let mut qempty = self.ple_qempty()?;
        while qempty.iter().any(|&w| w != 0xFFFF_FFFF) && Instant::now() < limit {
            sleep(Duration::from_millis(5));
            qempty = self.ple_qempty()?;
        }
        let drain = t_drain.elapsed();
        sleep(Duration::from_millis(5)); // the last frame leaves the queue before it leaves the PHY
        self.mcu_rmw(ARB_SCR, ARB_TX_OFF, ARB_TX_OFF)?; // MAC TX off
        self.mcu_rmw(PHY_BN_MODE, 0x0F00_0000, 0x0400_0000)?; // back to the receive class
        Ok(TxReport { total: t0.elapsed(), drain, qempty, refused })
    }

    /// The PLE AC queue-empty bitmaps (MCU reads): a cleared bit is a non-empty queue.
    pub fn ple_qempty(&self) -> io::Result<[u32; 4]> {
        let mut q = [0u32; 4];
        for (n, w) in q.iter_mut().enumerate() {
            *w = self.mcu_read(PLE_AC_QEMPTY + 0x40 * n as u32)?;
        }
        Ok(q)
    }
}

/// How a TX window went.
#[derive(Clone, Copy, Debug)]
pub struct TxReport {
    /// The whole window, MCU commands included.
    pub total: Duration,
    /// Time spent waiting for the device queues after the airtime.
    pub drain: Duration,
    /// The queue-empty bitmaps at the end: all ones = drained.
    pub qempty: [u32; 4],
    /// Frames the kernel refused to inject.
    pub refused: usize,
}

impl TxReport {
    pub fn drained(&self) -> bool {
        self.qempty.iter().all(|&w| w == 0xFFFF_FFFF)
    }
}

/// iq_stat words worth reporting.
#[derive(Clone, Copy, Debug, Default)]
pub struct IqStat {
    /// 2 = packet count reached, 3 = stopped (duration or host request), 4+ = an error (4 busy, 5 not idle,
    /// 6 source timeout, 7 DMA timeout).
    pub state: u32,
    pub sent: u32,
    pub overruns: u32,
    pub busy: u32,
    pub checks: u32,
    /// Idle noise floor the firmware tracks, per nsum (128) samples.
    pub floor: u32,
}

/// Run a configuration command (`ip`, `iw`); `check`: fail if it fails.
pub fn run(argv: &[&str], check: bool) -> io::Result<()> {
    let st = Command::new(argv[0]).args(&argv[1..]).stdout(Stdio::null()).stderr(Stdio::null()).status()?;
    if check && !st.success() {
        return Err(other(format!("{} failed ({st})", argv.join(" "))));
    }
    Ok(())
}

pub fn parse_u32(s: &str) -> Option<u32> {
    match s.strip_prefix("0x").or_else(|| s.strip_prefix("0X")) {
        Some(h) => u32::from_str_radix(h, 16).ok(),
        None => s.parse().ok(),
    }
}

pub fn parse_mac(s: &str) -> Option<[u8; 6]> {
    let v: Vec<u8> = s.split(':').filter_map(|x| u8::from_str_radix(x, 16).ok()).collect();
    v.try_into().ok()
}
