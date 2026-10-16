#!/usr/bin/env python3
"""Narrow-band (5/10 MHz) 802.11 receiver daemon for an MT7921 card running the instrumented firmware.

The card's own PHY cannot demodulate 5/10 MHz frames, so this streams its front-end IQ through the firmware
(fw/src/iqstream.c: ICAP ring -> MCU WFDMA TX ring 0 -> USB bulk IN 0x84, energy-gated) into mt7921dbg's IQ ring and
demodulates in software (sdr/rx80211.py). Stream slices run back to back (the firmware call re-arms in ~25 ms).
Frames with a valid FCS are written to a radiotap pcap and/or a TAP interface (802.11 data -> Ethernet; OCB-style
addressing: addr1 = destination, addr2 = source).

Usage: nbrxd.py <A|B> <MHz> [options]
  --width W          channel width of the frames in MHz (default 5; 10 = 802.11p, 2.5 = the ÷8 transmitter, whose
                     fixed padding here only covers frames up to ~150 bytes: notes/10 §5; tools/nbnode nbmesh pads
                     by length)
  --gain l,f,d       manual gain: LNA 0-3, filter 0-8, digital 0-12 (default 3,4,6)
  --thresh N         fixed energy gate on the mean DC-removed |I|+|Q| per 128 samples (default: none with --ratio,
                     otherwise from the LNA code)
  --ratio R          adaptive gate: open above R x the card's own idle noise floor, tracked by the firmware
                     (default 1.8, or 0 with --pack; 0 = fixed gate only)
  --hits N           consecutive checks above the gate before it opens, rejecting us-long DC steps (default 3)
  --slice-ms N       stream slice length (default 4000; the receiver is off for ~0.1 s between slices)
  --block-ms N       continuous capture: decode in blocks of N ms (default 100; latency vs per-block overhead)
  --show-bad         also print the frames that fail the FCS (rate, length, SNR, CFO): link diagnostics
  --pack 0|2|8       ICAP packer (tools/sdr/iqrec.py PACKS): 0 = 40 Msps (160 MB/s, more than the USB path through
                     QEMU carries, so it needs the gate); 2 = 10 Msps (80 MB/s) and 8 = 30 Msps (120 MB/s) stream
                     continuously, so by default they run ungated: frames below the noise floor, which no energy gate
                     sees, are found by the software preamble detector (long-range mode)
  --seconds N        run time (default: until interrupted)
  --pcap FILE        write decoded frames (radiotap + 802.11, FCS included)
  --tap NAME         TAP interface: received data frames are delivered to it; frames the kernel sends on it are
                     transmitted (802.11 data, OCB addressing, LLC/SNAP) in a TX window after each RX slice
                     (needs --cbw 4 for the hardware 5 MHz transmitter)
  --netns NS         move the TAP into network namespace NS (two nodes in one host)
  --rate R           TX rate in 500 kbps units before the ÷4 (default 12 = 6 Mbps -> 1.5 Mbps on air)
  --tx-mode M        how queued frames are sent once the RX slice has been ended early: window (default): MCU
                     register writes, and MAC TX goes off only once the PLE queues are empty (tx_window); anytime:
                     host-path writes and a fixed airtime wait (tx_now; faster, but a frame still queued when the next
                     slice starts preceded card USB wedges: 3 of 3 two-node 120 s soaks wedged, window 1 of 2). Frames sent while a capture slice runs stop reaching the air
                     after ~100 frames (the firmware cannot service TX completion while the MCU is in the hook), so
                     both modes transmit between slices
  --min-rx-ms N      window mode: shortest RX slice before queued TX frames may end it early (default 30)
  --regain           re-apply the manual gain forces before every slice (a near-field transmitter can trip a
                     receive protection that otherwise stays latched)
  --dump FILE        also save every IQ record read (the format of tools/sdr/iqrec.py) for offline decoding
  --dump-gate X      with --dump: after the first 2000 records (a noise reference), keep only records whose mean
                     power exceeds X times that noise, plus one record on each side (TX scans)
  --rx-only          receive-only node: frames the kernel sends on the TAP are dropped (a streaming card must not
                     transmit: see --tx-only)
  --tx-only          transmit-only node: no capture ever runs on this card (the capture ring overwrites the packet
                     SRAM and breaks this card's TX path), MAC TX stays on, TAP frames go out at once. Pair it with a
                     receive-only card running this daemon without --tap TX traffic.
  --quiet            no per-frame lines
  --cbw 0|4          CHANNEL_SWITCH width of the card (default 0, or 4 with --pack). 4 = the firmware's native
                     narrow mode: its hardware transmitter sends 5 MHz PPDUs (÷4), and its front end is band-limited
                     to +-5 MHz (20 Msps of information), which the packers' plain decimation needs
  --tx-test N        half-duplex test: after every RX slice, enable MAC TX, inject N tagged frames
                     ("NBTX-<slice>-<n>", 6 Mbps OFDM before the ÷4) on the card's monitor interface, then stop TX
"""
import argparse
import fcntl
import os
import signal
import struct
import subprocess
import sys
import threading
import time
import zlib
from fractions import Fraction

import numpy as np
from scipy.signal import lfilter, resample_poly

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "sdr"))
import iqrec  # noqa: E402
import rx80211  # noqa: E402
from fwdbg import request  # noqa: E402

DBG = "/sys/kernel/debug/mt7921dbg/"
SLOT = {"A": 0, "B": 1}  # mt7921dbg IQ slot (own ring per card)
REC = 0x52514951
MIN_SNR = -1.0  # dB of LTF SNR below which a preamble candidate is noise (false alarms sit near -9 dB)
TX_SETTLE = 0.02  # s between the end of a TX burst and the next capture slice
MAX_LEN = 1600  # longest PSDU worth decoding: a 1500-byte MTU plus headers (a false SIGNAL field claims up to 4095)
IFACE = {"A": "wlx00c0cab7962a", "B": "wlx00c0cab79628"}
THRESH = {0: 40, 1: 55, 2: 75, 3: 140}  # ~max of 128-sample noise means x 1.05, filter 4 / digital 6


def sh(cmd):
    return subprocess.run(cmd, shell=True, cwd=HERE, capture_output=True, text=True).stdout


def syms():
    s = {}
    for line in open(os.path.join(HERE, "hooks.nm")):
        a, _, n = line.split()
        s[n] = int(a, 16)
    return s


MON = {"A": "mon_a", "B": "mon_b"}
TX_SA = bytes.fromhex("020000000001")


# The ÷8 transmitter (BN_MODE 0x83081000 [30:28] = 7: 2.5 MHz OFDM) ends every PPDU early, so the hardware FCS never
# survives (measured 2026-10-15 on 119-byte frames: 322 of 322 copies intact up to the last 7 bytes, those always lost).
# In that mode the sender appends its own CRC32 of the body plus DIV8_PAD_SYMS + 1 symbols of padding, and the receiver
# checks that CRC instead of the FCS. The loss grows with the frame (~30 + N/2 of N symbols arrive, notes/10 §5), so this
# fixed padding only covers frames up to ~150 bytes; tools/nbnode nbmesh pads by length.
DIV8_PAD_SYMS = 3
NDBPS = {12: 24, 18: 36, 24: 48, 36: 72, 48: 96, 72: 144, 96: 192, 108: 216}  # rate (500 kbps units) -> bits/symbol


def div8_pad(rate):
    return (DIV8_PAD_SYMS + 1) * NDBPS[rate] // 8


def div8_unwrap(r):
    """A decoded ÷8 frame whose FCS was lost: if the sender's inner CRC holds, rebuild it as a normal frame."""
    m, pad = r["mpdu"], div8_pad(r["rate"] * 2)
    end = len(m) - 4 - pad  # end of the inner CRC
    if end < 24 + 8 + 4:
        return
    if struct.unpack("<I", m[end - 4:end])[0] == zlib.crc32(m[24:end - 4]):
        core = m[:end - 4]
        r["mpdu"], r["fcs_ok"] = core + struct.pack("<I", zlib.crc32(core)), True


def tx_frame(payload, seq=0, rate=12, da=b"\xff" * 6, sa=TX_SA, ethertype=0x88B5, div8=False):
    """Radiotap (rate in 500 kbps units, NOACK) + 802.11 data frame + LLC/SNAP: the hardware's ÷4 turns rate 12
    (6 Mbps) into 1.5 Mbps on air. addr1 is always broadcast and the real destination travels in addr3: injected
    unicast frames are retried despite NOACK, falling back to DSSS rates (undecodable, and the card can never hear an
    ACK anyway). Tap.write maps addr3 back to the Ethernet destination."""
    rt = struct.pack("<BBHI", 0, 0, 12, (1 << 2) | (1 << 15)) + struct.pack("<BxH", rate, 0x0008)
    hdr = struct.pack("<HH", 0x0008, 0) + b"\xff" * 6 + sa + da + struct.pack("<H", (seq & 0xFFF) << 4)
    body = bytes.fromhex("aaaa03000000") + struct.pack(">H", ethertype) + payload
    if div8:
        body += struct.pack("<I", zlib.crc32(body)) + bytes(div8_pad(rate))
    return rt + hdr + body


class Card:
    def __init__(self, card, mhz, gain, code4=True, cbw=0, tx_anytime=False, pack=0, div8=False):
        self.phy = open(f"/sys/class/net/{IFACE[card]}/phy80211/name").read().strip()
        self.mon = MON[card]
        self.cbw = cbw
        self.tx_anytime = tx_anytime
        self.txsock = None
        self.mhz = mhz
        self.gain = gain
        self.code4 = code4
        self.pack = pack
        self.div8 = div8
        self.sym = syms()

    def rd(self, a):
        return request(self.phy, 1, a, 1)[2][0]

    def wr(self, a, *v):
        request(self.phy, 2, a, len(v), data=list(v))

    def rmw(self, a, mask, val):
        self.wr(a, (self.rd(a) & ~mask) | (val & mask))

    def rf(self, reg, val, path):
        request(self.phy, 3, 0x0083A66C, args=[reg, val, path])

    def apply_gain(self):
        lna, lpf, dig = self.gain
        for path in (0, 1):
            self.rf(0x768, 0x80000000 | lna << 27 | lpf << 23, path)
        self.rmw(0x83000108, 0x3F3F0000, dig << 24 | dig << 16)
        self.rmw(0x83000150, 0x8070F000, 0x80000000 | lna << 20)
        self.rmw(0x830000C4, 0xF7F7F707, 7 | lna << 8 | lpf << 12 | lna << 16 | lpf << 20 | lna << 24 | lpf << 28)

    def setup(self):
        p = self.phy
        if self.mhz == 5180:
            sh(f"python3 chsw.py {p} 149 1 0; python3 chsw.py {p} 36 1 {self.cbw} 1 1")
        else:
            sh(f"python3 chsw.py {p} 11 0 0; python3 chsw.py {p} 1 0 {self.cbw} 1 1 {self.mhz}")
        f_khz = request(self.phy, 3, 0x0083A662, args=[0x184, 0])[1]  # RF 0x184: tuned frequency in kHz
        print(f"# {self.phy} tuned to {f_khz / 1000:.3f} MHz (cbw {self.cbw})", file=sys.stderr, flush=True)
        if abs(f_khz - self.mhz * 1000) > 1000:
            raise RuntimeError(f"{self.phy}: tuned to {f_khz} kHz instead of {self.mhz} MHz")
        if self.cbw == 4:
            # MAC tick at 5 MHz timing (MCU write of WFSYS_TICKCR0 [4:0] = 0xa; only these bits of WFSYS_ON may be
            # written): the switch at cbw 4 leaves 0x5 (10 MHz timing), and the card then cuts every 5 MHz frame
            # longer than ~260 bytes (notes/10 §5)
            self.rmw(0x820B00A0, 0x1F, 0xA)
        if self.div8:  # ÷8 transmitter: switch the divider first. Changed later (at the first TX window, after the
            # capture setup), the PA keys an unmodulated carrier instead of the PPDU (measured 2026-10-15)
            self.rmw(0x83081000, 0x70000000, 0x70000000)
        if self.code4:  # keep the card's own 20 MHz demodulator from locking onto narrow frames
            self.rmw(0x83081000, 0x0F000000, 0x04000000)
        # capture setup before the manual-gain forces: the four forces applied first leave the ÷8 transmitter keying an
        # unmodulated carrier (the forces one at a time, or after the capture setup, do not; div8bisect.sh, 2026-10-15)
        sh(f"ICAP_PACK={self.pack} ICAP_SETUP_ONLY=1 python3 icap.py {p} /dev/null 4 3")
        self.apply_gain()
        # MAC RX must stay on (it gates the PHY receive). MAC TX stays off except while frames are being sent: left on
        # through a capture slice, the card emits a ~24 us pulse train (lines at +5 / -15 MHz) that wrecks its own
        # reception (measured 2026-10-15: 0/200 decoded vs 140/200)
        self.rmw(0x820E3000, 0x300, 0x100)
        v = self.rd(0x83081000)
        self.mode_tx = v & ~0x0F000000  # BW code 0: no CCA deferral (code 4 + forced gain holds TX back)
        if self.div8:  # [30:28] = 7: the ÷8 transmitter (2.5 MHz OFDM)
            self.mode_tx = (self.mode_tx & ~0x70000000) | 0x70000000
        self.mode_rx = self.mode_tx | (0x04000000 if self.code4 else 0)

    def release(self):
        self.rmw(0x830000C4, 0xF7F7F707, 0)
        self.rmw(0x83000150, 0x8070F000, 0)
        for path in (0, 1):
            self.rf(0x768, 0, path)

    def hrd(self, a):
        """Host-path register read (USB vendor request, mt76 debugfs regidx/regval): works while the MCU is busy in a
        slice. MCU commands must not be sent then: their packets land in packet SRAM, which the capture overwrites."""
        d = f"/sys/kernel/debug/ieee80211/{self.phy}/mt76/"
        with open(d + "regidx", "w") as f:
            f.write(str(a))
        with open(d + "regval") as f:
            return int(f.read().strip(), 0) & 0xFFFFFFFF

    def hwr(self, a, v):
        d = f"/sys/kernel/debug/ieee80211/{self.phy}/mt76/"
        with open(d + "regidx", "w") as f:
            f.write(str(a))
        with open(d + "regval", "w") as f:
            f.write(str(v & 0xFFFFFFFF))

    def start_slice(self, thresh, dur_ms, ratio16=0, hits=1):
        self.wr(self.sym["iq_cfg"], 4080, 1, thresh, 500, 4096, 128, 0, dur_ms, ratio16, hits)
        self.hwr(self.sym["iq_stat"], 0)  # state 0 until the hook starts (1), then its end state
        # detached call: the response event is sent first; a 3 ms run-in lets it leave the packet SRAM before the
        # capture ring overwrites it
        # 2 s DMA stall limit: a host that stops reading bulk IN for a while costs overruns, not a wedged card
        request(self.phy, 4, self.sym["hk_iqstream"], args=[0xFFFFFFFF, 0xCC, 3000, 2000000])

    def slice_running(self):
        return self.hrd(self.sym["iq_stat"]) in (0, 1)

    def stop_slice(self):
        self.hwr(self.sym["iq_cfg"] + 24, 1)  # iq_cfg.stop

    def stat(self):
        """iq_stat: state, sent, max_backlog, t_us, skipped, overruns, busy_checks, checks, floor (per nsum)."""
        return request(self.phy, 1, self.sym["iq_stat"], 9)[2]

    def tx_now(self, frames, airtime_s):
        """Transmit while a capture slice may be running: host-path writes only (MAC TX on and BW code 0 for the
        frames' airtime, then back to the receive state) and injection; the MCU is not involved."""
        import socket
        if self.txsock is None:
            self.txsock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
            self.txsock.bind((self.mon, 0))
        arb = self.hrd(0x820E3000)
        if self.code4:
            self.hwr(0x83081000, self.mode_tx)
        self.hwr(0x820E3000, arb & ~0x100)  # MAC TX on for the burst only
        for f in frames:
            self.txsock.send(f)
        time.sleep(airtime_s)
        self.hwr(0x820E3000, arb | 0x100)
        if self.code4:
            self.hwr(0x83081000, self.mode_rx)
        # let the firmware finish the TX completion before the next slice takes over MCU TX ring 0: restarting the
        # capture right after a burst preceded most card wedges (vendor requests -110) in the two-node tests
        time.sleep(TX_SETTLE)

    def tx_queued(self):
        """Any frame still in the device's PLE AC queues (MT_PLE_AC_QEMPTY: a cleared bit = a non-empty queue)."""
        return any(self.rd(0x820C0500 + 0x40 * n) != 0xFFFFFFFF for n in range(4))

    def tx_window(self, frames, airtime_s):
        """MAC TX on only while sending, and off only once the device queues are empty: a frame left queued in the
        packet SRAM would be overwritten by the next slice's capture ring (the firmware then hangs on the queue)."""
        import socket
        if self.code4:  # BW code 4 + forced gain makes the MAC defer TX (CCA): code 0 for the TX window
            self.rmw(0x83081000, 0x0F000000, 0)
        self.rmw(0x820E3000, 0x100, 0)
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        s.bind((self.mon, 0))
        for f in frames:
            s.send(f)
        s.close()
        time.sleep(airtime_s)
        t_end = time.time() + 0.5
        while self.tx_queued() and time.time() < t_end:
            time.sleep(0.005)
        time.sleep(0.005)  # the last frame leaves the queue before it leaves the PHY
        self.rmw(0x820E3000, 0x100, 0x100)
        if self.code4:
            self.rmw(0x83081000, 0x0F000000, 0x04000000)


def decode_segment(w, up, down, a, width, maxf=50):
    """Worker: the sample words (iqrec.sample_words) of one contiguous block -> decoded frames (picklable dicts)."""
    q = (w & 0x3FFF).astype(np.int32)
    q -= (q & 0x2000) << 1
    i = ((w >> 16) & 0x3FFF).astype(np.int32)
    i -= (i & 0x2000) << 1
    x = (i + 1j * q).astype(np.complex64)
    y = resample_poly(x - x.mean(), up, down).astype(np.complex64)
    if len(y) < 400:
        return []
    y = lfilter([1, -1], [1, -a], y).astype(np.complex64)
    out = []
    for r in rx80211.decode_all(y, maxf, MIN_SNR, MAX_LEN):
        if width == 2.5 and not r["fcs_ok"]:
            div8_unwrap(r)
        out.append({k: r[k] for k in ("rate", "length", "fcs_ok", "mpdu", "start", "snr", "cfo")})
    return out


class Decoder:
    """Assembles contiguous record runs into segments; each closed segment is decoded in a worker process, so the
    capture slices keep running back to back while decoding (a burst of strong segments can take seconds).
    A continuous (ungated) stream never closes a segment by itself: it is cut into blocks of block_s seconds that
    overlap by overlap_s (longer than any frame, so each frame lies whole in at least one block); a frame decoded
    twice is delivered once, by its absolute ring position."""

    def __init__(self, width, out, workers=3, pack=0, block_s=None, overlap_s=0.016, bad=None):
        from collections import deque
        from concurrent.futures import ProcessPoolExecutor
        self.width = width
        self.pack = pack
        self.fs, per_beat = iqrec.PACKS[pack]
        self.wps = 4 // per_beat  # ring words per sample
        self.block = round(block_s * self.fs) * self.wps if block_s else None
        self.overlap = round(overlap_s * self.fs) * self.wps // 4 * 4  # whole beats
        self.recent = deque(maxlen=256)
        fr = Fraction(width / (self.fs / 1e6)).limit_denominator(1000)
        self.up, self.down = fr.numerator, fr.denominator
        self.a = 1 - 2 * np.pi * 20e3 / (width * 1e6)
        self.pos = None
        self.parts = []
        self.n = 0
        self.out, self.bad = out, bad
        self.frames = self.good = self.segments = 0
        # the workers ignore SIGINT: the daemon's own shutdown still collects the blocks in flight
        self.pool = ProcessPoolExecutor(workers, initializer=signal.signal, initargs=(signal.SIGINT, signal.SIG_IGN))
        self.pending = []

    def add(self, pos, words):
        if self.parts and (pos - self.pos - self.n) % 2**32:
            self.flush()
        if not self.parts:
            self.pos, self.n = pos, 0
        self.parts.append(words)
        self.n += len(words)
        if self.block and self.n >= self.block:
            self.flush(keep=self.overlap)

    def flush(self, keep=0):
        if not self.parts:
            return
        w = np.concatenate(self.parts)
        sw = iqrec.sample_words(w, self.pack)
        self.pending.append((self.pos, time.time(),
                             self.pool.submit(decode_segment, sw, self.up, self.down, self.a, self.width)))
        if keep and len(w) > keep:  # the next block starts with this one's tail
            self.parts, self.pos, self.n = [w[-keep:]], (self.pos + len(w) - keep) % 2**32, keep
        else:
            self.parts, self.n = [], 0
        self.segments += 1

    def collect(self, wait=False):
        """Deliver the frames of every finished segment, in completion order: a slow decode (a long segment of
        calibration bursts or noise) must not hold back the frames of the segments behind it."""
        still = []
        for pos, t_sub, fut in self.pending:
            if wait or fut.done():
                lat = time.time() - t_sub  # block submitted -> frames delivered
                for r in fut.result():
                    p = (pos + round(r["start"] * self.fs / (self.width * 1e6)) * self.wps) % 2**32
                    tol = 400 * self.fs / (self.width * 1e6) * self.wps
                    if any(abs((p - q + 2**31) % 2**32 - 2**31) < tol for q in self.recent):
                        continue  # the same frame in the overlap of two blocks
                    self.recent.append(p)
                    self.frames += 1
                    if r["fcs_ok"]:
                        self.good += 1
                        self.out(r, p, lat)
                    elif self.bad:
                        self.bad(r, p, lat)
            else:
                still.append((pos, t_sub, fut))
        self.pending = still


class Pcap:
    def __init__(self, path, mhz, width):
        self.f = open(path, "wb")
        self.f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 127))  # LINKTYPE_IEEE802_11_RADIOTAP
        self.mhz = mhz
        self.width = width

    def write(self, r):
        # radiotap: FLAGS (FCS at end), RATE, CHANNEL (+ half/quarter-rate flag)
        rate = int(r["rate"] * 2 * self.width // 20)  # 500 kbps units, actual on-air rate
        chflags = (0x0080 if self.mhz < 3000 else 0x0100) | 0x0040 | (
            0x4000 if self.width == 10 else 0x8000 if self.width == 5 else 0)  # band, OFDM, half / quarter rate
        rt = struct.pack("<BBHI", 0, 0, 14, (1 << 1) | (1 << 2) | (1 << 3)) + struct.pack(
            "<BBHH", 0x10, rate, self.mhz, chflags)  # FLAGS (FCS at end), RATE, CHANNEL
        pkt = rt + r["mpdu"]
        t = time.time()
        self.f.write(struct.pack("<IIII", int(t), int((t % 1) * 1e6), len(pkt), len(pkt)) + pkt)
        self.f.flush()


class Tap:
    TUNSETIFF = 0x400454CA
    IFF_TAP, IFF_NO_PI = 0x0002, 0x1000

    def __init__(self, name):
        self.last = {}
        self.fd = os.open("/dev/net/tun", os.O_RDWR)
        fcntl.ioctl(self.fd, self.TUNSETIFF, struct.pack("16sH", name.encode(), self.IFF_TAP | self.IFF_NO_PI))
        subprocess.run(["ip", "link", "set", name, "up"], check=False)
        self.mac = bytes.fromhex(open(f"/sys/class/net/{name}/address").read().strip().replace(":", ""))

    def read_loop(self, q, lock):
        """Frames the kernel sends on the TAP (Ethernet II) are queued for the next TX window."""
        while True:
            eth = os.read(self.fd, 2048)
            if len(eth) >= 14:
                with lock:
                    q.append(eth)

    def write(self, mpdu):
        """802.11 data frame (no QoS, ToDS = FromDS = 0) with LLC/SNAP -> Ethernet II. A broadcast addr1 with a unicast
        addr3 is our own framing (tx_frame) and goes to addr3; repeats of a (source, sequence) pair are dropped."""
        fc = struct.unpack_from("<H", mpdu)[0]
        if (fc >> 2) & 3 != 2 or (fc >> 8) & 3:
            return False
        hdr = 24 + (2 if (fc >> 4) & 0xF == 8 else 0)  # QoS data adds 2 bytes
        body = mpdu[hdr:-4]
        if body[:6] != bytes.fromhex("aaaa03000000"):
            return False
        sa, seq = mpdu[10:16], struct.unpack_from("<H", mpdu, 22)[0] >> 4
        if self.last.get(sa) == seq:  # a repeat of the frame just delivered (÷8 retransmissions carry no Retry bit)
            return False
        self.last[sa] = seq
        da = mpdu[4:10]
        if da == b"\xff" * 6 and mpdu[16:22] != b"\xff" * 6:
            da = mpdu[16:22]
        os.write(self.fd, da + sa + body[6:])
        return True


def tx_only(a, card):
    """Transmit-only node: native narrow CHANNEL_SWITCH (hardware ÷4 = 5 MHz PPDUs), MAC TX on, no capture; every
    frame the kernel sends on the TAP is injected at once as an OCB-style 802.11 data frame."""
    import socket
    p = card.phy
    if a.mhz == 5180:
        sh(f"python3 chsw.py {p} 149 1 0; python3 chsw.py {p} 36 1 4 1 1")
    else:
        sh(f"python3 chsw.py {p} 11 0 0; python3 chsw.py {p} 1 0 4 1 1 {a.mhz}")
    if a.width == 2.5:
        card.rmw(0x83081000, 0x70000000, 0x70000000)  # ÷8 transmitter
        card.rmw(0x820E3000, 0x100, 0)  # MAC TX on
    tap = Tap(a.tap)
    if a.netns:
        subprocess.run(["ip", "netns", "add", a.netns], stderr=subprocess.DEVNULL)
        subprocess.run(["ip", "link", "set", a.tap, "netns", a.netns], check=True)
        subprocess.run(["ip", "-n", a.netns, "link", "set", a.tap, "up"], check=True)
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    s.bind((card.mon, 0))
    t0, seq, end = time.time(), 0, (time.time() + a.seconds) if a.seconds else None
    os.set_blocking(tap.fd, False)
    try:
        while end is None or time.time() < end:
            try:
                eth = os.read(tap.fd, 2048)
            except BlockingIOError:
                time.sleep(0.002)
                continue
            if len(eth) < 14:
                continue
            s.send(tx_frame(eth[14:], seq, a.rate, eth[0:6], eth[6:12], struct.unpack(">H", eth[12:14])[0],
                            a.width == 2.5))
            seq += 1
            if not a.quiet:
                print(f"{time.time() - t0:9.3f} s  TX {len(eth)} bytes  {eth[6:12].hex(':')} -> {eth[0:6].hex(':')}",
                      flush=True)
    except KeyboardInterrupt:
        pass
    print(f"# total: {seq} frames transmitted", file=sys.stderr)


def keep_off_usb_irq_cpus():
    """Run this daemon and its decoder workers (which inherit the mask) away from the CPUs that take the xHCI
    interrupts: streaming 80 MB/s keeps such a vCPU ~98% busy, and a decoder scheduled there delays USB completions
    until the card's DMA stalls and its register requests time out."""
    irq_cpus = set()
    for line in open("/proc/interrupts"):
        if "xhci_hcd" in line:
            irq = line.split(":")[0].strip()
            try:
                txt = open(f"/proc/irq/{irq}/effective_affinity_list").read().strip()
            except OSError:
                continue
            for part in txt.split(","):
                lo, _, hi = part.partition("-")
                irq_cpus.update(range(int(lo), int(hi or lo) + 1))
    cpus = os.sched_getaffinity(0) - irq_cpus
    if cpus:
        os.sched_setaffinity(0, cpus)


class GatedDump:
    """--dump-gate: the first REF records as they come (the noise reference), then only records whose mean power exceeds
    `gate` times the reference median, with one record of context on each side. A loud stretch is cut after MAXRUN
    records (a stuck transmitter would otherwise outrun the disk and the reader): it shows up as a ~5 ms burst."""
    REF = 2000
    MAXRUN = 40
    WINDOW, BUDGET = 7000, 600  # at most BUDGET records written per WINDOW records (~1 s): a TX storm cannot fill the disk

    def __init__(self, f, gate, pack):
        self.f, self.gate, self.pack = f, gate, pack
        self.buf, self.ref, self.noise, self.prev, self.after, self.run = bytearray(), [], None, None, 0, 0
        self.seen, self.written = 0, 0

    def _write(self, rec):
        if self.written < self.BUDGET:
            self.f.write(rec)
            self.written += 1

    def write(self, d):
        self.buf += d
        off = 0
        while off + 16 <= len(self.buf):
            n = struct.unpack_from("<4I", self.buf, off)[3]
            if off + 16 + n > len(self.buf):
                break
            rec = bytes(self.buf[off:off + 16 + n])
            off += 16 + n
            w = np.frombuffer(rec[16:], np.uint32)
            x = iqrec.words_to_iq(w[:len(w) // 32 * 32].reshape(-1, 32)[:, :4].ravel(), self.pack)  # 1/8 subsample
            p = float(np.mean(np.abs(x) ** 2)) if len(x) else 0.0
            if self.noise is None:
                self.f.write(rec)
                self.ref.append(p)
                if len(self.ref) >= self.REF:
                    self.noise = float(np.median(self.ref)) + 1e-9
                continue
            self.seen += 1
            if self.seen >= self.WINDOW:
                self.seen, self.written = 0, 0
            if p > self.gate * self.noise:
                self.run += 1
                if self.run > self.MAXRUN:
                    continue
                if self.prev:
                    self._write(self.prev)
                    self.prev = None
                self._write(rec)
                self.after = 1
            elif self.after:
                self._write(rec)
                self.after, self.run = 0, 0
            else:
                self.prev, self.run = rec, 0
        del self.buf[:off]


def main():
    keep_off_usb_irq_cpus()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("card", choices=["A", "B"])
    ap.add_argument("mhz", type=int)
    ap.add_argument("--width", type=float, default=5)
    ap.add_argument("--gain", default="3,4,6")
    ap.add_argument("--thresh", type=int)
    ap.add_argument("--ratio", type=float)
    ap.add_argument("--hits", type=int, default=3)
    ap.add_argument("--slice-ms", type=int, default=4000)
    ap.add_argument("--pack", type=int, choices=sorted(iqrec.PACKS), default=0)
    ap.add_argument("--block-ms", type=int, default=100)
    ap.add_argument("--show-bad", action="store_true")
    ap.add_argument("--seconds", type=float)
    ap.add_argument("--pcap")
    ap.add_argument("--tap")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--cbw", type=int)
    ap.add_argument("--tx-test", type=int, default=0)
    ap.add_argument("--netns")
    ap.add_argument("--rate", type=int, default=12)
    ap.add_argument("--tx-only", action="store_true")
    ap.add_argument("--rx-only", action="store_true")
    ap.add_argument("--dump")
    ap.add_argument("--dump-gate", type=float)
    ap.add_argument("--regain", action="store_true")
    ap.add_argument("--min-rx-ms", type=int, default=30)
    ap.add_argument("--tx-mode", choices=["anytime", "window"], default="window")
    a = ap.parse_args()
    gain = tuple(int(v) for v in a.gain.split(","))
    if a.ratio is None:
        a.ratio = 0 if a.pack else 1.8
    if a.cbw is None:  # the packers drop samples without filtering: they need the front end narrowed to +-5 MHz
        a.cbw = 4 if a.pack else 0
    thresh = a.thresh if a.thresh is not None else (0 if a.ratio or a.pack else THRESH[gain[0]])
    ratio16 = round(a.ratio * 16)
    gated = bool(thresh or ratio16)

    card = Card(a.card, a.mhz, gain, cbw=a.cbw, tx_anytime=a.tx_mode == "anytime", pack=a.pack, div8=a.width == 2.5)
    if a.width == 2.5:  # one transmission per frame: the MAC takes every truncated ÷8 PPDU for a failure and retries
        with open("/sys/module/mt7921dbg/parameters/tx_count", "w") as f:
            f.write("1")
    if a.tx_only:
        return tx_only(a, card)
    card.setup()
    pcap = Pcap(a.pcap, a.mhz, a.width) if a.pcap else None
    tap = Tap(a.tap) if a.tap else None
    txq, txlock = [], threading.Lock()
    if tap:
        if a.netns:
            subprocess.run(["ip", "netns", "add", a.netns], stderr=subprocess.DEVNULL)
            subprocess.run(["ip", "link", "set", a.tap, "netns", a.netns], check=True)
            subprocess.run(["ip", "-n", a.netns, "link", "set", a.tap, "up"], check=True)
        threading.Thread(target=tap.read_loop, args=(txq, txlock), daemon=True).start()
    t0 = time.time()
    txseq = 0

    own = {tap.mac} if tap else set()  # this node's own TAP frames come back through its own capture

    def deliver(r, pos, lat=0.0):
        m = r["mpdu"]
        if m[10:16] in own:
            return  # our own transmission, captured by our own receiver
        if pcap:
            pcap.write(r)
        sent = tap.write(m) if tap else False
        if not a.quiet:
            print(f"{time.time() - t0:9.3f} s  {r['rate'] * a.width / 20:5.2f} Mbps  len {r['length']:4d}  "
                  f"SA {m[10:16].hex(':')}  DA {m[4:10].hex(':')}{'  -> tap' if sent else ''}  snr {r['snr']:4.1f} dB  "
                  f"+{lat:.2f} s  {m[24:56]!r}", flush=True)

    def show_bad(r, pos, lat):
        print(f"{time.time() - t0:9.3f} s  bad FCS: rate {r['rate']:2d} len {r['length']:4d} snr {r['snr']:5.1f} dB "
              f"cfo {r['cfo'] * a.width * 1e3:7.2f} kHz  +{lat:.2f} s", flush=True)

    dec = Decoder(a.width, deliver, pack=a.pack, block_s=None if gated else a.block_ms / 1000,
                  bad=show_bad if a.show_bad else None)
    with open(DBG + "iqbind", "w") as f:  # route this card's stream to its slot (also empties the ring)
        f.write(f"{SLOT[a.card]} {card.phy}")
    fd = os.open(DBG + f"iq{SLOT[a.card]}", os.O_RDONLY)
    dump = (GatedDump(open(a.dump, "wb"), a.dump_gate, a.pack) if a.dump_gate else open(a.dump, "wb")) if a.dump else None
    buf = bytearray()
    stop = threading.Event()
    end = t0 + a.seconds if a.seconds else None
    def send_pending():
        nonlocal txseq
        with txlock:
            pending, txq[:] = txq[:], []
        frames, air = [], 0.0
        for eth in pending:
            frames.append(tx_frame(eth[14:], txseq, a.rate, eth[0:6], eth[6:12], struct.unpack(">H", eth[12:14])[0],
                                   a.width == 2.5))
            txseq += 1
            air += (len(eth) + 40) * 8 / (a.rate * 0.5e6 * a.width / 20) + 0.0002
        card.tx_now(frames, air * 1.5 + 0.003)
        if not a.quiet:
            print(f"{time.time() - t0:9.3f} s  TX {len(frames)} frames ({air * 1e3:.1f} ms on air)", flush=True)

    def one_slice():
        nonlocal buf, txseq
        if a.regain:
            card.apply_gain()
        card.start_slice(thresh, a.slice_ms, ratio16, a.hits)
        t_start, t_poll, stopping, t_stop = time.time(), 0.0, False, 0.0
        t_data = t_start
        while True:  # no MCU commands until the hook has returned (host-path polling only)
            now = time.time()
            # Host-path reads of iq_stat (packet SRAM) while the hook streams, arms or tears down MCU TX ring 0 preceded
            # most card wedges: read it only once the stream has been quiet for 30 ms (in continuous mode: only after
            # the hook has stopped), and never in a slice's first 50 ms. The stop request is a single write.
            quiet = now - max(t_data, t_stop) > 0.03
            if now - t_start > 0.05 and now - t_poll > (0.004 if stopping else 0.02):
                t_poll = now
                if txq and not a.rx_only and not stopping and now - t_start > a.min_rx_ms / 1000:
                    card.stop_slice()  # frames to send: end the RX slice early (half-duplex turnaround)
                    stopping, t_stop = True, now
                    continue
                if quiet and not card.slice_running():
                    d = os.read(fd, 8 << 20)
                    if dump and d:
                        dump.write(d)
                    buf += d
                    break
            d = os.read(fd, 8 << 20)
            if not d:
                # the gate streams a busy period contiguously; 15 ms without records (USB delivery through QEMU is
                # bursty) means the segment is
                # complete: decode it now instead of when the next segment or the slice end arrives
                if dec.parts and now - t_data > 0.015:
                    dec.flush()
                dec.collect()
                time.sleep(0.002)
                continue
            t_data = now
            if dump:
                dump.write(d)
            dec.collect()
            buf += d
            off = 0
            while off + 16 <= len(buf):
                mark, _, pos, n = struct.unpack_from("<4I", buf, off)
                if mark != REC:
                    raise RuntimeError(f"bad record marker {mark:#x}")
                if off + 16 + n > len(buf):
                    break
                dec.add(pos, np.frombuffer(bytes(buf[off + 16:off + 16 + n]), dtype=np.uint32))
                off += 16 + n
            del buf[:off]
        off = 0
        while off + 16 <= len(buf):  # records that arrived with the final poll
            mark, _, pos, n = struct.unpack_from("<4I", buf, off)
            if mark != REC or off + 16 + n > len(buf):
                break
            dec.add(pos, np.frombuffer(bytes(buf[off + 16:off + 16 + n]), dtype=np.uint32))
            off += 16 + n
        del buf[:off]
        # Slices ended early for TX skip the two MCU round trips (status, tuning check): the half-duplex turnaround is
        # receiver downtime. Every full-length slice still checks that nobody retuned the card.
        st = None
        if not stopping:
            st = card.stat()  # safe now: the hook has returned
            f_khz = request(card.phy, 3, 0x0083A662, args=[0x184, 0])[1]
            if abs(f_khz - a.mhz * 1000) > 1000:  # someone (mac80211 / the driver) retuned the card: take it back
                print(f"# {card.phy} found at {f_khz / 1000:.3f} MHz, 0x83081000 {card.rd(0x83081000):#010x}: "
                      f"re-running the setup", file=sys.stderr, flush=True)
                card.setup()
        dec.flush()
        dec.collect()
        if a.tx_mode == "anytime":
            pending = []
            if txq and not a.rx_only:
                send_pending()
        else:
            with txlock:
                pending, txq[:] = txq[:], []
        if pending and not a.rx_only:
            frames, air = [], 0.0
            for eth in pending:
                frames.append(tx_frame(eth[14:], txseq, a.rate, eth[0:6], eth[6:12],
                                       struct.unpack(">H", eth[12:14])[0], a.width == 2.5))
                txseq += 1
                air += (len(eth) + 40) * 8 / (a.rate * 0.5e6 * a.width / 20) + 0.0002
            card.tx_window(frames, air + 0.01)
            if not a.quiet:
                print(f"{time.time() - t0:9.3f} s  TX {len(frames)} frames ({air * 1e3:.1f} ms on air)", flush=True)
        if a.tx_test:
            k = nslice = getattr(main, "nslice", 0)
            main.nslice = nslice + 1
            fr = [tx_frame(b"NBTX-%03d-%03d" % (k, n) + bytes(64), n, a.rate, div8=a.width == 2.5)
                  for n in range(a.tx_test)]
            if a.tx_mode == "anytime":
                card.tx_now(fr, a.tx_test * 0.0012 + 0.005)
            else:
                card.tx_window(fr, a.tx_test * 0.0012 + 0.02)  # ~1 ms airtime per 110-byte frame at 1.5 Mbps
        if not a.quiet and st:
            print(f"# slice done: {dec.segments} segments, {dec.frames} frames, {dec.good} good FCS | fw state {st[0]}"
                  f" sent {st[1]} overruns {st[5]} busy {st[6]}/{st[7]} floor {st[8] / 128:.0f}",
                  file=sys.stderr, flush=True)

    errors = 0
    try:
        while not stop.is_set() and (end is None or time.time() < end):
            try:
                one_slice()
                errors = 0
            except (SystemExit, OSError, RuntimeError) as e:  # fwdbg exits on a missing or short response
                errors += 1
                print(f"# slice failed ({errors}): {e!r}; re-running the card setup", file=sys.stderr, flush=True)
                if errors >= 5:
                    raise
                time.sleep(1)
                buf = bytearray()
                dec.parts, dec.n = [], 0
                card.setup()
    except KeyboardInterrupt:
        pass
    finally:
        try:  # an interrupted slice would keep streaming into a ring nobody reads
            card.stop_slice()  # harmless without a slice: the next start_slice rewrites iq_cfg
            time.sleep(0.3)  # let the hook finish its teardown before the first host read (see one_slice)
            t = time.time()
            while card.slice_running() and time.time() - t < 2:
                time.sleep(0.05)
        except (OSError, RuntimeError):
            pass
        dec.flush()
        dec.collect(wait=True)
        card.release()
        print(f"# total: {dec.segments} segments, {dec.frames} decoded, {dec.good} with good FCS", file=sys.stderr)


if __name__ == "__main__":
    main()
