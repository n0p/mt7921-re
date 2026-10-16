#!/usr/bin/env python3
"""nbprep.py: prepare an MT7921AU card for narrow-band (5 or 2.5 MHz) operation.

The card's own transmitter sends 5 MHz (÷4) or 2.5 MHz (÷8) OFDM PPDUs with the card's FEC. The card cannot
demodulate narrow frames (its demodulator is fixed at 20 MHz numerology, notes/03 and 07), so it streams its receive
IQ to the host, where a software receiver decodes them (tools/guest/nbrxd.py, or tools/nbnode nbmesh). This script
leaves the card in the half-duplex node's idle state, ready for such a daemon or for manual injection tests:

  1. channel switch at the firmware's native narrow width (cbw 4) with the frequency override (any frequency in the
     2.3/2.4, 5 or 6 GHz RF bands; RF readback checked)
  2. the MAC tick at 5 MHz timing (WFSYS_TICKCR0 = 0xa): with the switch's 0x5 the card cuts every 5 MHz frame
     longer than ~260 bytes (notes/10 §5); then the ÷8 divider for 2.5 MHz (before the capture setup: the order
     matters, notes/09)
  3. bandwidth class 4 for reception (keeps the card's own 20 MHz demodulator from locking onto narrow frames)
  4. IQ capture (ICAP) registers: front-end tap, rate, ring, sample packer
  5. manual receive gain (the AGC hunts on noise)
  6. MAC: receive on, transmit off (a daemon opens TX windows between capture slices)
  7. transmit coding profile (FEC) for injected frames, through mt7921dbg's TXD overrides; one attempt per frame
     (there are no MAC acknowledgements at narrow widths)
  8. a monitor interface for injection, and the TX power

Requires: the instrumented firmware (fw/, `make install`: debug command CE 0xcb and the hk_iqstream hook) and the
mt7921dbg kernel module (tools/guest/mt7921dbg). This script imposes no band rules: using a frequency that is allowed
where the card runs (ISM, or amateur bands for a licensed operator) is up to the operator.

Usage:
  nbprep.py <iface> [--freq MHz] [--width 5|2.5] [--fec PROFILE] [--txpower dBm] [--monitor NAME]
                    [--pack 2|8] [--gain L,F,D] [--rx-test SECONDS]
  nbprep.py <iface> --status          print the card's state (read back)
  nbprep.py <iface> --release         undo the gain forces and the TXD overrides
<iface> is the card's managed interface (e.g. wlx00c0cab79628); it is taken down (mac80211 would retune it).

FEC profiles (notes/10; the card transmits all of them, the host receivers decode legacy only so far):
  legacy:R            802.11a rate R at 20 MHz timing (6 9 12 18 24 36 48 54); BCC. Default legacy:6 (BPSK 1/2)
  ht:N[+ldpc][+stbc][+sgi]          HT-MF MCS N (0-7); LDPC instead of BCC; STBC over both chains; short GI
  he:N[+dcm][+ldpc][+gi1.6|+gi3.2][+ltf2|+ltf4]   HE-SU MCS N
  he-er:N[+dcm][+ldpc][+ru106][+gi...][+ltf...]    HE extended-range SU
"""
import argparse
import fcntl
import struct
import subprocess
import time

MT7921DBG = "/sys/kernel/debug/mt7921dbg/"  # mt7921dbg's debugfs directory
PARAMS = "/sys/module/mt7921dbg/parameters/"  # mt7921dbg module parameters (TXD overrides)
LOCK = "/tmp/fwdbg.lock"  # the lock fwdbg.py / chsw.py also take (tools/guest/dbglock.py)
DBG_MAGIC = 0x4D543739  # "MT79": first word of every debug-command answer (fw/src/dbgcmd.c)
MCU_RXD_LEN = 0x24  # the MCU event header the driver may leave in front of the answer

# Debug command operations (fw/src/dbgcmd.c): read / write MCU-side memory or registers, call a function
OP_READ, OP_WRITE, OP_CALL, OP_CALL_DETACH = 1, 2, 3, 4
# mt76 MCU command id encoding for EXT commands: MCU_EXT_PREFIX (0xed) | ext id << 8
MCU_EXT = 0xED
EXT_CHANNEL_SWITCH, EXT_SET_RX_PATH = 0x08, 0x4E

# Boot ROM helpers (notes/04; tools/ghidra/annotations/gen_rom.py): A-die RF register read / write through the MCU
ROM_RF_READ, ROM_RF_WRITE = 0x0083A662, 0x0083A66C
RF_TUNED_KHZ = 0x184  # A-die RF register holding the frequency the synthesizer is tuned to, in kHz

# Registers (names from tools/ghidra/annotations; meaning and source in the comments where they are used)
PHY_BN_MODE = 0x83081000
WFSYS_TICKCR0 = 0x820B00A0
TICK_5MHZ = 0xA  # WFSYS_TICKCR0 [1:0] WLAN_CLK_MODE = 2, [4:2] LMAC_1X_MODE = 2: the firmware's own 5 MHz MAC timing
ARB_SCR = 0x820E3000

# Legacy OFDM rate (Mbps at 20 MHz timing) -> the L-SIG RATE field, which is also the index the TXD rate code uses
LEGACY_RATE = {6: 0xB, 9: 0xF, 12: 0xA, 18: 0xE, 24: 0x9, 36: 0xD, 48: 0x8, 54: 0xC}
# TXD DW6 rate code (mt76 MT_TX_RATE_*): [5:0] index, [9:6] PHY mode, [12:10] NSS - 1, [13] STBC, [4] HE DCM,
# [5] HE-ER 106-tone RU
MODE_OFDM, MODE_HT, MODE_HE_SU, MODE_HE_ER = 1, 2, 8, 9
RATE_STBC, RATE_DCM, RATE_ER106 = 1 << 13, 1 << 4, 1 << 5
# TXD DW6 coding fields: [11] LDPC, [15:14] guard interval, [13:12] HE-LTF size
DW6_LDPC, DW6_GI_SHIFT, DW6_LTF_SHIFT = 1 << 11, 14, 12


class Card:
    """One MT7921 card, addressed through its managed interface. Every access to the card goes through the methods
    below; each says which path it uses."""

    def __init__(self, iface):
        self.iface = iface
        # the wiphy name (phyN) mt7921dbg uses to select the card
        self.phy = open(f"/sys/class/net/{iface}/phy80211/name").read().strip()
        # mt76's own debugfs: host register access over USB vendor requests (regidx / regval)
        self.mt76 = f"/sys/kernel/debug/ieee80211/{self.phy}/mt76/"

    # --- access paths -------------------------------------------------------------------------------------------
    def _with_target(self, fn):
        """mt7921dbg holds one "target" card for all tools: select ours and issue the command under the shared lock,
        so another tool cannot redirect it to the other card in between."""
        with open(LOCK, "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            with open(MT7921DBG + "target", "w") as f:
                f.write(self.phy)
            return fn()

    def debug_cmd(self, op, addr=0, n=0, args=(), data=()):
        """The instrumented firmware's debug command (MCU CE cid 0xcb, fw/src/dbgcmd.c), sent by mt7921dbg's "cmd"
        file. Request: op, addr, count, four call arguments, then data words. Answer: magic, op, status, return
        value, word count, words. The MCU executes it, so it reaches MCU-only memory (DLM, ILM) and every register."""
        a = list(args) + [0] * (4 - len(args))
        req = struct.pack("<7I", op, addr, n, *a) + b"".join(struct.pack("<I", w & 0xFFFFFFFF) for w in data)

        def go():
            with open(MT7921DBG + "cmd", "wb", buffering=0) as f:
                f.write(req)
            with open(MT7921DBG + "cmd", "rb") as f:
                return f.read()
        raw = self._with_target(go)
        off = 0 if raw[:4] == struct.pack("<I", DBG_MAGIC) else MCU_RXD_LEN
        magic, _op, status, ret, cnt = struct.unpack_from("<5I", raw, off)
        if magic != DBG_MAGIC:
            raise RuntimeError(f"{self.phy}: no debug-command answer (instrumented firmware loaded?)")
        return status, ret, list(struct.unpack_from(f"<{cnt}I", raw, off + 20))

    def mcu_read(self, addr):
        """Register or memory word read by the MCU (debug command op 1)."""
        return self.debug_cmd(OP_READ, addr, 1)[2][0]

    def mcu_write(self, addr, value):
        """Register or memory word write by the MCU (debug command op 2)."""
        self.debug_cmd(OP_WRITE, addr, 1, data=[value])

    def mcu_rmw(self, addr, mask, value):
        """Read-modify-write through the MCU: only the bits in mask change."""
        self.mcu_write(addr, (self.mcu_read(addr) & ~mask) | (value & mask))

    def mcu_call(self, fn, *args):
        """Call a firmware/ROM function with up to 4 arguments (debug command op 3); returns its return value."""
        return self.debug_cmd(OP_CALL, fn, args=args)[1]

    def host_read(self, addr):
        """Register read over the USB register path (mt76 debugfs regidx/regval). Works while the MCU is busy (e.g.
        inside the IQ stream hook), unlike the debug command."""
        with open(self.mt76 + "regidx", "w") as f:
            f.write(str(addr))
        with open(self.mt76 + "regval") as f:
            return int(f.read().strip(), 0) & 0xFFFFFFFF

    def host_write(self, addr, value):
        """Register write over the USB register path (mt76 debugfs regidx/regval)."""
        with open(self.mt76 + "regidx", "w") as f:
            f.write(str(addr))
        with open(self.mt76 + "regval", "w") as f:
            f.write(str(value & 0xFFFFFFFF))

    def host_update(self, addr, clear, bits):
        """Read-modify-write over the USB register path: clear the bits in `clear`, then OR in `bits` (icap.py's
        upd(); `bits` may lie outside `clear`)."""
        self.host_write(addr, (self.host_read(addr) & ~clear) | bits)

    def ext_cmd(self, ext_id, payload):
        """Send an MCU EXT command as the driver would (mt7921dbg "raw": u32 mt76 command id, then the payload) and
        wait for the firmware's answer."""
        self._with_target(lambda: open(MT7921DBG + "raw", "wb", buffering=0).write(
            struct.pack("<I", MCU_EXT | ext_id << 8) + payload))

    def rf_read(self, reg, path=0):
        """A-die RF register read through the boot ROM's rfRead(reg, path)."""
        return self.mcu_call(ROM_RF_READ, reg, path)

    def rf_write(self, reg, value, path):
        """A-die RF register write through the boot ROM's rfWrite(reg, value, path)."""
        self.mcu_call(ROM_RF_WRITE, reg, value, path)


# --- setup steps -------------------------------------------------------------------------------------------------
def rf_band(freq):
    """Frequency (MHz) -> (CHANNEL_SWITCH band, a channel to hop through first, the channel to switch to). The
    frequency override replaces the channel's frequency, but the firmware skips a switch to the channel it is
    already on (notes/04), so every tune hops through another channel of the same band first."""
    if 2300 <= freq <= 2500:
        return 0, 11, 1  # 2.4 GHz RF (also covers the 2.3 GHz amateur band)
    if 4900 <= freq <= 5925:
        return 1, 149, 36  # 5 GHz RF (includes 5.9 GHz)
    if 5925 < freq <= 7125:
        return 2, 5, 1  # 6 GHz RF
    raise ValueError(f"{freq} MHz is outside the card's RF bands")


def tune(card, freq, tx_streams=1, rx_mask=1):
    """EXT CHANNEL_SWITCH (then SET_RX_PATH, as the driver sends both) at cbw 4, the firmware's native 10 MHz mode.
    At cbw 4 the firmware itself sets the ÷4 transmit divider (PHY_BN_MODE [30:28] = 0x5 -> 5 MHz PPDUs, notes/07)
    and WFSYS_TICKCR0 = 0x5 (MAC tick: without it the transmitter never ends a narrow PPDU, notes/03), and it narrows
    the receive front end to +-5 MHz, which the IQ packers need (notes/08)."""
    band, hop, ch = rf_band(freq)

    def payload(channel, cbw, outband_mhz):
        # the driver's struct (mt76 mt7921_mcu_set_chan_info): control channel, centre channel, cbw (driver
        # CMD_CBW_*: 0 = 20, 4 = 10, 5 = 5 MHz), TX path count, RX path mask, switch reason, band index (DBDC),
        # centre channel 2 (80+80), u16 CAC case, channel band (0/1/2 = 2.4/5/6 GHz), reserved, u32 outband_freq:
        # the frequency override in MHz (0 = the channel's own frequency; the firmware tunes the RF to it,
        # notes/04), TX power drop, AP bandwidth, AP centre channel, 57 reserved bytes
        return struct.pack("<BBBBBBBBHBBIBBB57x", channel, channel, cbw, tx_streams, rx_mask, 0, 0, 0, 0, band, 0,
                           outband_mhz, 0, 0, 0)
    # hop at 20 MHz (as nbrxd and chsw.py do), then the real switch at cbw 4 with the override
    for channel, cbw, outband in ((hop, 0, 0), (ch, 4, freq)):
        card.ext_cmd(EXT_CHANNEL_SWITCH, payload(channel, cbw, outband))
        card.ext_cmd(EXT_SET_RX_PATH, payload(channel, cbw, outband))
    tuned = card.rf_read(RF_TUNED_KHZ)  # what the synthesizer is really tuned to
    if abs(tuned - freq * 1000) > 1000:
        raise RuntimeError(f"tuned to {tuned} kHz instead of {freq} MHz")
    return tuned


def set_mac_tick(card):
    """WFSYS_TICKCR0 [4:0] = 0xa (MCU write; only these bits of WFSYS_ON may be written): the MAC's timing, which
    decides when a transmitted PPDU ends, at 5 MHz. The channel switch at cbw 4 leaves 0x5 (10 MHz timing), and the
    card then stops a 5 MHz PPDU about 1 ms after the end computed at that timing: frames longer than ~260 bytes
    lose their end (measured with the X410: 1364-byte frames kept 290 of 456 symbols at 0x5, all of them at 0xa)."""
    card.mcu_rmw(WFSYS_TICKCR0, 0x1F, TICK_5MHZ)


def set_width(card, width):
    """5 MHz: the ÷4 divider the channel switch already set. 2.5 MHz: PHY_BN_MODE [30:28] = 7, the ÷8 divider. This
    must come before the capture setup: written later (at the first TX window), the PA keys an unmodulated carrier
    instead of the PPDU (measured, notes/09). Even with the 5 MHz MAC tick, the ÷8 transmitter keeps only about
    30 + N/2 of a PPDU's N symbols (notes/10 §5), so senders append their own CRC and enough padding (tools/nbnode
    nbmesh: profile::div8_pad_bytes)."""
    if width == 2.5:
        card.mcu_rmw(PHY_BN_MODE, 0x70000000, 0x70000000)
    elif width != 5:
        raise ValueError("width must be 5 or 2.5 MHz (no ÷2 exists for 10 MHz, notes/07)")


def set_rx_class(card):
    """PHY_BN_MODE [27:24] = 4 while receiving: the chain behind the front end runs at the narrow rate and the card's
    own 20 MHz demodulator stops locking onto the narrow frames (notes/03). A TX window puts class 0 back: class 4
    with forced gain makes the MAC defer transmissions (CCA)."""
    card.mcu_rmw(PHY_BN_MODE, 0x0F000000, 0x04000000)


def icap_setup(card, pack):
    """Program the internal IQ capture (ICAP, 0x80021000; MT7996's ICAPn register for register, notes/08), as
    BirriaSDR and tools/guest/icap.py do, without starting it: the firmware hook hk_iqstream starts each capture
    slice. Host-path writes, like icap.py."""
    # MAC: receive on (ARB_SCR bit 9 clear; with MAC RX off the PHY front end freezes), transmit off (bit 8 set)
    card.host_update(ARB_SCR, 0x200, 0x100)
    # band clock forces 0x83080004/08 |= 0x3840: keep the capture path's clocks running (BirriaSDR's sequence)
    card.host_update(0x83080004, 0, 0x3840)
    card.host_update(0x83080008, 0, 0x3840)
    # PHY-top capture routing (BirriaSDR): 0x830a3008 [22:21] = 0, 0x830a1000 = 0x04090100, 0x830a1004 [7:0] = 0x10,
    # 0x830a1008 = 0
    card.host_update(0x830A3008, 3 << 21, 0)
    card.host_write(0x830A1000, 0x04090100)
    card.host_update(0x830A1004, 0xFF, 0x10)
    card.host_write(0x830A1008, 0)
    # ICAP_CR0 control = 0xf1: enabled, not capturing yet ([0] EN, [7:4] = 0xf as the firmware writes)
    card.host_write(0x80021090, 0xF1)
    # ICAP_CR2 / CR3: the ring, 64 K samples from the start of the capture SRAM (0xe0200000), end inclusive
    card.host_write(0x80021098, 0)
    card.host_write(0x8002109C, 65536 - 4)
    # ICAP_CR10: [19:16] sample packer (2 = uniform 10 Msps, 8 = 30 Msps; notes/09) and the SRAM-interface selects
    # 0x9c000000 that BirriaSDR uses
    card.host_update(0x800210B8, 0xF0000, 0x9C000000 | (pack & 0xF) << 16)
    # capture node, domain 4 (BirriaSDR's): mode byte 0x13 in 0x830ad440 [7:0] with the enable bit 31;
    # 0x830ad448: [23] node select, [19:18] rate 3 = 40 Msps, [15:8] tap 4 = the receive front end's output
    card.host_update(0x830AD440, 0xFF, 0x80000013)
    card.host_update(0x830AD448, 0x0FFFFFFF, 0x00800000 | 3 << 18 | 4 << 8)
    # PSE_ICAP_GRANT 0x88009004 [27:24] = 0xf: lend the SRAM banks to the capture. HAZARD: never touch [23:0]
    card.host_update(0x88009004, 0, 0x0F000000)
    # ICAP_CR0 = 0x4f1: ring mode with the soft reset released ([10] SW_RST_B), still not capturing
    card.host_write(0x80021090, 0x4F1)


def set_gain(card, lna, lpf, dig):
    """Manual receive gain (BirriaSDR's recipe, notes/03): the AGC hunts between gain states on noise, which an IQ
    receiver sees as level jumps. LNA 0-3, filter 0-8, digital 0-12."""
    for path in (0, 1):  # A-die RF 0x768 per chain: [31] force, [28:27] LNA, [26:23] filter gain
        card.rf_write(0x768, 0x80000000 | lna << 27 | lpf << 23, path)
    card.mcu_rmw(0x83000108, 0x3F3F0000, dig << 24 | dig << 16)  # digital gain, both halves
    card.mcu_rmw(0x83000150, 0x8070F000, 0x80000000 | lna << 20)  # D-die LNA force
    card.mcu_rmw(0x830000C4, 0xF7F7F707, 7 | lna << 8 | lpf << 12 | lna << 16 | lpf << 20 | lna << 24 | lpf << 28)


def fec_profile(spec):
    """Profile string -> (TXD rate code, DW6 bits to set, DW6 bits to clear, TX streams needed). See the module doc."""
    kind, _, rest = spec.partition(":")
    parts = rest.split("+") if rest else ["6"]
    n, flags = int(parts[0]), set(parts[1:])
    dw6_set, dw6_clr, streams = 0, 0, 1
    if kind == "legacy":
        if flags:
            raise ValueError("legacy rates take no options")
        return MODE_OFDM << 6 | LEGACY_RATE[n], 0, 0, 1
    if kind == "ht":
        code = MODE_HT << 6 | n
        if "stbc" in flags:
            code |= RATE_STBC
            streams = 2  # Alamouti over both TX chains
        if "sgi" in flags:
            dw6_set |= 1 << DW6_GI_SHIFT
            dw6_clr |= 3 << DW6_GI_SHIFT
    elif kind in ("he", "he-er"):
        code = (MODE_HE_SU if kind == "he" else MODE_HE_ER) << 6 | n
        if "dcm" in flags:
            code |= RATE_DCM
        if "ru106" in flags:
            code |= RATE_ER106
        gi = {"gi1.6": 1, "gi3.2": 2}.get(next((f for f in flags if f.startswith("gi")), ""), 0)
        ltf = {"ltf2": 1, "ltf4": 2}.get(next((f for f in flags if f.startswith("ltf")), ""), 0)
        dw6_set |= gi << DW6_GI_SHIFT | ltf << DW6_LTF_SHIFT
        dw6_clr |= 3 << DW6_GI_SHIFT | 3 << DW6_LTF_SHIFT
    else:
        raise ValueError(f"unknown FEC profile kind {kind}")
    if "ldpc" in flags:
        dw6_set |= DW6_LDPC
    return code, dw6_set, dw6_clr, streams


def set_fec(spec):
    """mt7921dbg rewrites the TXD of every injected frame (kretprobe on mt76_connac2_mac_write_txwi): the rate code,
    the coding fields, and one transmission attempt (tx_count = 1). At narrow widths no acknowledgement can come back
    (the receiving card cannot decode the frame), so mt76's 15 attempts would only waste air time."""
    code, dw6_set, dw6_clr, streams = fec_profile(spec)
    for name, value in (("tx_rate", code), ("tx_dw6_set", dw6_set), ("tx_dw6_clr", dw6_clr), ("tx_count", 1)):
        with open(PARAMS + name, "w") as f:
            f.write(str(value))
    return code, dw6_set, streams


def make_monitor(card, name, txpower_dbm):
    """A monitor interface to inject frames on (raw 802.11 + radiotap), and the TX power. The managed interface
    goes down: mac80211 would otherwise scan and retune the card."""
    subprocess.run(["ip", "link", "set", card.iface, "down"], check=True)
    subprocess.run(["iw", "dev", name, "del"], stderr=subprocess.DEVNULL)
    subprocess.run(["iw", "phy", card.phy, "interface", "add", name, "type", "monitor", "flags", "fcsfail",
                    "control", "otherbss"], check=True)
    subprocess.run(["ip", "link", "set", name, "up"], check=True)
    subprocess.run(["iw", "dev", name, "set", "txpower", "fixed", str(int(txpower_dbm * 100))],
                   stderr=subprocess.DEVNULL)


def rx_test(card, seconds, hooks, slot):
    """Run one IQ capture slice through the firmware hook hk_iqstream and report the stream counters: checks that
    samples reach the host (mt7921dbg collects them into /sys/kernel/debug/mt7921dbg/iqN)."""
    sym = {}
    for line in open(hooks):  # hook addresses of the installed instrumented build (fw/build/hooks.nm)
        a, _, n = line.split()
        sym[n] = int(a, 16)
    with open(MT7921DBG + "iqbind", "w") as f:  # route this card's stream to the IQ slot (also empties its ring)
        f.write(f"{slot} {card.phy}")
    # iq_cfg (fw/src/iqstream.c): 4080 samples per USB packet, 1 descriptor in flight, no energy gate (thresh 0),
    # hang 500 us, 4096 pre-trigger samples, 128 samples per energy check, stop flag 0, duration, ratio gate off, 1 hit
    for i, v in enumerate((4080, 1, 0, 500, 4096, 128, 0, int(seconds * 1000), 0, 1)):
        card.mcu_write(sym["iq_cfg"] + 4 * i, v)
    card.host_write(sym["iq_stat"], 0)  # iq_stat.state = 0 until the hook runs
    # detached call (op 4): the firmware answers first, then runs the hook, because the capture ring overwrites the
    # packet SRAM that carries answers. Arguments: no packet limit, event id 0xcc (what mt7921dbg collects), 3 ms
    # run-in so the answer leaves the SRAM first, 2 s DMA stall limit
    card.debug_cmd(OP_CALL_DETACH, sym["hk_iqstream"], args=(0xFFFFFFFF, 0xCC, 3000, 2000000))
    time.sleep(seconds + 0.3)
    stat = open(MT7921DBG + f"iqstat{slot}").read().split()
    with open(MT7921DBG + "iqbind", "w") as f:
        f.write(f"{slot} -")  # unbind the slot
    return " ".join(stat[:10])


def status(card):
    """Read back what defines the narrow mode."""
    bn = card.mcu_read(PHY_BN_MODE)
    out = {
        "tuned_khz": card.rf_read(RF_TUNED_KHZ),
        "PHY_BN_MODE": f"{bn:#010x} (divider [30:28] = {bn >> 28 & 7}: 5 = 5 MHz, 7 = 2.5 MHz; class [27:24] = "
                       f"{bn >> 24 & 0xF})",
        "WFSYS_TICKCR0": f"{card.mcu_read(WFSYS_TICKCR0):#x}",
        "ARB_SCR": f"{card.mcu_read(ARB_SCR):#010x} (bit 8 = MAC TX off, bit 9 = MAC RX off)",
    }
    for p in ("tx_rate", "tx_dw6_set", "tx_dw6_clr", "tx_count"):
        out[p] = open(PARAMS + p).read().strip()
    return out


def release(card):
    """Undo what outlives this script: the manual gain forces and the TXD overrides. A channel switch or a firmware
    reload restores the PHY registers."""
    card.mcu_rmw(0x830000C4, 0xF7F7F707, 0)
    card.mcu_rmw(0x83000150, 0x8070F000, 0)
    for path in (0, 1):
        card.rf_write(0x768, 0, path)
    for p in ("tx_rate", "tx_dw6_set", "tx_dw6_clr", "tx_count"):
        with open(PARAMS + p, "w") as f:
            f.write("0")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("iface")
    # default 2450 MHz: the 2.4 GHz ISM band's nominal centre, between Wi-Fi channels 6 and 11 and clear of the BLE
    # advertising channels (2402, 2426, 2480)
    ap.add_argument("--freq", type=int, default=2450)
    ap.add_argument("--width", type=float, default=5, choices=(5, 2.5))
    ap.add_argument("--fec", default="legacy:6")
    ap.add_argument("--txpower", type=float, default=5)
    ap.add_argument("--monitor", default="nbmon0")
    ap.add_argument("--pack", type=int, default=2, choices=(2, 8))
    ap.add_argument("--gain", default="3,4,6")
    ap.add_argument("--rx-test", type=float, default=0)
    ap.add_argument("--hooks", default="/root/fwre/hooks.nm")
    ap.add_argument("--slot", default="0", choices=("0", "1"), help="mt7921dbg IQ slot for --rx-test")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--release", action="store_true")
    a = ap.parse_args()
    card = Card(a.iface)
    if a.status:
        for k, v in status(card).items():
            print(f"{k:14s} {v}")
        return
    if a.release:
        release(card)
        print("released")
        return
    code, dw6, streams = set_fec(a.fec)
    make_monitor(card, a.monitor, a.txpower)
    tuned = tune(card, a.freq, tx_streams=streams, rx_mask=3 if streams == 2 else 1)
    set_mac_tick(card)
    set_width(card, a.width)
    set_rx_class(card)
    icap_setup(card, a.pack)
    set_gain(card, *(int(x) for x in a.gain.split(",")))
    print(f"{card.phy}: tuned {tuned / 1000:.3f} MHz, {a.width} MHz PPDUs, FEC {a.fec} (rate code {code:#x}, DW6 "
          f"{dw6:#x}), monitor {a.monitor}, IQ packer {a.pack}")
    if a.rx_test:
        print("rx test:", rx_test(card, a.rx_test, a.hooks, a.slot))


if __name__ == "__main__":
    main()
