#!/usr/bin/env python3
"""Narrow-band (5/10 MHz) 802.11 receiver daemon for an MT7921 card running the instrumented firmware.

The card's own PHY cannot demodulate 5/10 MHz frames, so this streams its front-end IQ through the firmware
(fw/src/iqstream.c: ICAP ring -> MCU WFDMA TX ring 0 -> USB bulk IN 0x84, energy-gated) into mt7921dbg's IQ ring and
demodulates in software (sdr/rx80211.py). Stream slices run back to back (the firmware call re-arms in ~25 ms).
Frames with a valid FCS are written to a radiotap pcap and/or a TAP interface (802.11 data -> Ethernet; OCB-style
addressing: addr1 = destination, addr2 = source).

Usage: nbrxd.py <A|B> <MHz> [options]
  --width 5|10       channel width of the frames (default 5)
  --gain l,f,d       manual gain: LNA 0-3, filter 0-8, digital 0-12 (default 3,4,6)
  --thresh N         fixed energy gate on the mean DC-removed |I|+|Q| per 128 samples (default: none with --ratio,
                     otherwise from the LNA code)
  --ratio R          adaptive gate: open above R x the card's own idle noise floor, tracked by the firmware
                     (default 1.8; 0 = fixed gate only)
  --hits N           consecutive checks above the gate before it opens, rejecting us-long DC steps (default 3)
  --slice-ms N       stream slice length (default 4000; the receiver is off for ~0.1 s between slices)
  --seconds N        run time (default: until interrupted)
  --pcap FILE        write decoded frames (radiotap + 802.11, FCS included)
  --tap NAME         TAP interface: received data frames are delivered to it; frames the kernel sends on it are
                     transmitted (802.11 data, OCB addressing, LLC/SNAP) in a TX window after each RX slice
                     (needs --cbw 4 for the hardware 5 MHz transmitter)
  --netns NS         move the TAP into network namespace NS (two nodes in one host)
  --rate R           TX rate in 500 kbps units before the ÷4 (default 12 = 6 Mbps -> 1.5 Mbps on air)
  --regain           re-apply the manual gain forces before every slice (a near-field transmitter can trip a
                     receive protection that otherwise stays latched)
  --dump FILE        also save every IQ record read (the format of tools/sdr/iqrec.py) for offline decoding
  --rx-only          receive-only node: frames the kernel sends on the TAP are dropped (a streaming card must not
                     transmit: see --tx-only)
  --tx-only          transmit-only node: no capture ever runs on this card (the capture ring overwrites the packet
                     SRAM and breaks this card's TX path), MAC TX stays on, TAP frames go out at once. Pair it with a
                     receive-only card running this daemon without --tap TX traffic.
  --quiet            no per-frame lines
  --cbw 0|4          CHANNEL_SWITCH width of the card (default 0). 4 = the firmware's native narrow mode, whose
                     hardware transmitter sends 5 MHz PPDUs (÷4): use it on a card that also transmits
  --tx-test N        half-duplex test: after every RX slice, enable MAC TX, inject N tagged frames
                     ("NBTX-<slice>-<n>", 6 Mbps OFDM before the ÷4) on the card's monitor interface, then stop TX
"""
import argparse
import fcntl
import os
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
import rx80211  # noqa: E402
from fwdbg import request  # noqa: E402

DBG = "/sys/kernel/debug/mt7921dbg/"
SLOT = {"A": 0, "B": 1}  # mt7921dbg IQ slot (own ring per card)
REC = 0x52514951
FS = 40e6
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


def tx_frame(payload, seq=0, rate=12, da=b"\xff" * 6, sa=TX_SA, ethertype=0x88B5):
    """Radiotap (rate in 500 kbps units, NOACK) + 802.11 data frame + LLC/SNAP: the hardware's ÷4 turns rate 12
    (6 Mbps) into 1.5 Mbps on air. addr1 is always broadcast and the real destination travels in addr3: injected
    unicast frames are retried despite NOACK, falling back to DSSS rates (undecodable, and the card can never hear an
    ACK anyway). Tap.write maps addr3 back to the Ethernet destination."""
    rt = struct.pack("<BBHI", 0, 0, 12, (1 << 2) | (1 << 15)) + struct.pack("<BxH", rate, 0x0008)
    hdr = struct.pack("<HH", 0x0008, 0) + b"\xff" * 6 + sa + da + struct.pack("<H", (seq & 0xFFF) << 4)
    return rt + hdr + bytes.fromhex("aaaa03000000") + struct.pack(">H", ethertype) + payload


class Card:
    def __init__(self, card, mhz, gain, code4=True, cbw=0):
        self.phy = open(f"/sys/class/net/{IFACE[card]}/phy80211/name").read().strip()
        self.mon = MON[card]
        self.cbw = cbw
        self.mhz = mhz
        self.gain = gain
        self.code4 = code4
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
        if self.code4:  # keep the card's own 20 MHz demodulator from locking onto narrow frames
            self.rmw(0x83081000, 0x0F000000, 0x04000000)
        self.apply_gain()
        sh(f"ICAP_SETUP_ONLY=1 python3 icap.py {p} /dev/null 4 3")
        self.rmw(0x820E3000, 0x300, 0x100)  # MAC TX off; RX stays on (it gates the PHY receive)

    def release(self):
        self.rmw(0x830000C4, 0xF7F7F707, 0)
        self.rmw(0x83000150, 0x8070F000, 0)
        for path in (0, 1):
            self.rf(0x768, 0, path)

    def start_slice(self, thresh, dur_ms, ratio16=0, hits=1):
        self.wr(self.sym["iq_cfg"], 4080, 1, thresh, 500, 4096, 128, 0, dur_ms, ratio16, hits)
        request(self.phy, 4, self.sym["hk_iqstream"], args=[0xFFFFFFFF, 0xCC, 200, 200000])

    def stat(self):
        """iq_stat: state, sent, max_backlog, t_us, skipped, overruns, busy_checks, checks, floor (per nsum)."""
        return request(self.phy, 1, self.sym["iq_stat"], 9)[2]

    def tx_queued(self):
        """Any frame still in the device's PLE AC queues (MT_PLE_AC_QEMPTY: a cleared bit = a non-empty queue)."""
        return any(self.rd(0x820C0500 + 0x40 * n) != 0xFFFFFFFF for n in range(4))

    def tx_window(self, frames, airtime_s):
        """MAC TX on only while sending, and off only once the device queues are empty: a frame left queued in the
        packet SRAM would be overwritten by the next slice's capture ring (the firmware then hangs on the queue)."""
        import socket
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


def decode_segment(w, up, down, a, width, maxf=50):
    """Worker: raw ICAP words of one contiguous segment -> decoded frames (picklable dicts)."""
    q = (w & 0x3FFF).astype(np.int32)
    q -= (q & 0x2000) << 1
    i = ((w >> 16) & 0x3FFF).astype(np.int32)
    i -= (i & 0x2000) << 1
    x = (i + 1j * q).astype(np.complex64)
    y = resample_poly(x - x.mean(), up, down).astype(np.complex64)
    if len(y) < 400:
        return []
    y = lfilter([1, -1], [1, -a], y).astype(np.complex64)
    return [{k: r[k] for k in ("rate", "length", "fcs_ok", "mpdu", "start")} for r in rx80211.decode_all(y, maxf)]


class Decoder:
    """Assembles contiguous record runs into segments; each closed segment is decoded in a worker process, so the
    capture slices keep running back to back while decoding (a burst of strong segments can take seconds)."""

    def __init__(self, width, out, workers=3):
        from concurrent.futures import ProcessPoolExecutor
        self.width = width
        fr = Fraction(width / (FS / 1e6)).limit_denominator(1000)
        self.up, self.down = fr.numerator, fr.denominator
        self.a = 1 - 2 * np.pi * 20e3 / (width * 1e6)
        self.pos = None
        self.parts = []
        self.n = 0
        self.out = out
        self.frames = self.good = self.segments = 0
        self.pool = ProcessPoolExecutor(workers)
        self.pending = []

    def add(self, pos, words):
        if self.parts and (pos - self.pos - self.n) % 2**32:
            self.flush()
        if not self.parts:
            self.pos, self.n = pos, 0
        self.parts.append(words)
        self.n += len(words)

    def flush(self):
        if not self.parts:
            return
        w = np.concatenate(self.parts)
        self.pending.append((self.pos, self.pool.submit(decode_segment, w, self.up, self.down, self.a, self.width)))
        self.parts, self.n = [], 0
        self.segments += 1

    def collect(self, wait=False):
        """Deliver the results of finished segments (in submission order)."""
        while self.pending and (wait or self.pending[0][1].done()):
            pos, fut = self.pending.pop(0)
            for r in fut.result():
                self.frames += 1
                if r["fcs_ok"]:
                    self.good += 1
                    self.out(r, pos + round(r["start"] * FS / (self.width * 1e6)))


class Pcap:
    def __init__(self, path, mhz, width):
        self.f = open(path, "wb")
        self.f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 127))  # LINKTYPE_IEEE802_11_RADIOTAP
        self.mhz = mhz
        self.width = width

    def write(self, r):
        # radiotap: FLAGS (FCS at end), RATE, CHANNEL (+ half/quarter-rate flag)
        rate = r["rate"] * 2 * self.width // 20  # 500 kbps units, actual on-air rate
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
        if self.last.get(sa) == seq and fc & 0x0800:  # retry of a frame already delivered
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
            s.send(tx_frame(eth[14:], seq, a.rate, eth[0:6], eth[6:12], struct.unpack(">H", eth[12:14])[0]))
            seq += 1
            if not a.quiet:
                print(f"{time.time() - t0:9.3f} s  TX {len(eth)} bytes  {eth[6:12].hex(':')} -> {eth[0:6].hex(':')}",
                      flush=True)
    except KeyboardInterrupt:
        pass
    print(f"# total: {seq} frames transmitted", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("card", choices=["A", "B"])
    ap.add_argument("mhz", type=int)
    ap.add_argument("--width", type=int, default=5)
    ap.add_argument("--gain", default="3,4,6")
    ap.add_argument("--thresh", type=int)
    ap.add_argument("--ratio", type=float, default=1.8)
    ap.add_argument("--hits", type=int, default=3)
    ap.add_argument("--slice-ms", type=int, default=4000)
    ap.add_argument("--seconds", type=float)
    ap.add_argument("--pcap")
    ap.add_argument("--tap")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--cbw", type=int, default=0)
    ap.add_argument("--tx-test", type=int, default=0)
    ap.add_argument("--netns")
    ap.add_argument("--rate", type=int, default=12)
    ap.add_argument("--tx-only", action="store_true")
    ap.add_argument("--rx-only", action="store_true")
    ap.add_argument("--dump")
    ap.add_argument("--regain", action="store_true")
    a = ap.parse_args()
    gain = tuple(int(v) for v in a.gain.split(","))
    thresh = a.thresh if a.thresh is not None else (0 if a.ratio else THRESH[gain[0]])
    ratio16 = round(a.ratio * 16)

    card = Card(a.card, a.mhz, gain, cbw=a.cbw)
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

    def deliver(r, pos):
        m = r["mpdu"]
        if pcap:
            pcap.write(r)
        sent = tap.write(m) if tap else False
        if not a.quiet:
            print(f"{time.time() - t0:9.3f} s  {r['rate'] * a.width / 20:5.2f} Mbps  len {r['length']:4d}  "
                  f"SA {m[10:16].hex(':')}  DA {m[4:10].hex(':')}{'  -> tap' if sent else ''}  {m[24:56]!r}",
                  flush=True)

    dec = Decoder(a.width, deliver)
    with open(DBG + "iqbind", "w") as f:  # route this card's stream to its slot (also empties the ring)
        f.write(f"{SLOT[a.card]} {card.phy}")
    fd = os.open(DBG + f"iq{SLOT[a.card]}", os.O_RDONLY)
    dump = open(a.dump, "wb") if a.dump else None
    buf = bytearray()
    stop = threading.Event()
    end = t0 + a.seconds if a.seconds else None
    def one_slice():
        nonlocal buf, txseq
        if a.regain:
            card.apply_gain()
        card.start_slice(thresh, a.slice_ms, ratio16, a.hits)
        t_slice = time.time() + a.slice_ms / 1000
        while time.time() < t_slice + 0.05:  # the MCU cannot answer commands during the slice
            d = os.read(fd, 8 << 20)
            if not d:
                time.sleep(0.003)
                continue
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
        st = card.stat()
        while st[0] == 1:  # slice still running (late): wait for the hook to return
            time.sleep(0.02)
            st = card.stat()
        dec.flush()
        dec.collect()
        with txlock:
            pending, txq[:] = txq[:], []
        if pending and not a.rx_only:
            frames, air = [], 0.0
            for eth in pending:
                frames.append(tx_frame(eth[14:], txseq, a.rate, eth[0:6], eth[6:12],
                                       struct.unpack(">H", eth[12:14])[0]))
                txseq += 1
                air += (len(eth) + 40) * 8 / (a.rate * 0.5e6 * a.width / 20) + 0.0002
            card.tx_window(frames, air + 0.01)
            if not a.quiet:
                print(f"{time.time() - t0:9.3f} s  TX {len(frames)} frames ({air * 1e3:.1f} ms on air)", flush=True)
        if a.tx_test:
            k = nslice = getattr(main, "nslice", 0)
            main.nslice = nslice + 1
            fr = [tx_frame(b"NBTX-%03d-%03d" % (k, n) + bytes(64), n) for n in range(a.tx_test)]
            card.tx_window(fr, a.tx_test * 0.0012 + 0.02)  # ~1 ms airtime per 110-byte frame at 1.5 Mbps
        if not a.quiet:
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
        dec.flush()
        dec.collect(wait=True)
        card.release()
        print(f"# total: {dec.segments} segments, {dec.frames} decoded, {dec.good} with good FCS", file=sys.stderr)


if __name__ == "__main__":
    main()
