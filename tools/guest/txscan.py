#!/usr/bin/env python3
"""Fast TX timing scan on card B: one continuous card-A capture (30 Msps, ICAP packer 8) covers many configurations.
For each configuration card B sends a marker frame at the base, then 3 frames with the configuration's register
writes applied, then undoes the writes. tools/sdr/txscan.py splits the capture at the silences between
configurations and measures every burst (STF / FFT period, decode at the implied width).
Usage: txscan.py <MHz> <config> [config ...]
  config: writes separated by commas: "reg=value", "reg|=bits", "reg&=mask", "reg^=bits", or "p:param=value" for an mt7921dbg
          module parameter (tx_bw, tx_rate, tx_count); "-" = none (a reference group)
  env BASE: writes applied once after the channel switch (same syntax), e.g. BASE=0x820b00a0=0x5
  env CBW: card B's chsw.py bandwidth (default 0 = 20 MHz), CTR its centre channel (default 1; 3 for a 40 MHz PHY),
  TXS / RXM the TX stream count and RX chain mask (default 1 / 1; TXS=2 for STBC);
  env RATE: inject rate in 500 kbps (default 12)
"""
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fwdbg import request  # noqa: E402
from inject import frame  # noqa: E402

D = "/root/fwre"
DUMP = "/root/fwre/scan.bin"


def rd(phy, a):
    return request(phy, 1, a, 1)[2][0]


def wr(phy, a, v):
    request(phy, 2, a, 1, data=[v & 0xffffffff])


PARAMS = "/sys/module/mt7921dbg/parameters/"


def parse(cfg):
    out = []
    for kv in ([] if cfg in ("", "-") else cfg.split(",")):
        if kv.startswith("p:"):
            r, v = kv[2:].split("=")
            out.append((r, "p", int(v, 0)))
            continue
        for op in ("|=", "&=", "^=", "="):
            if op in kv:
                r, v = kv.split(op)
                out.append((int(r, 0), op, int(v, 0)))
                break
    return out


def apply(phy, writes):
    saved = []
    for r, op, v in writes:
        if op == "p":
            old = int(open(PARAMS + r).read())
            saved.append((r, old))
            open(PARAMS + r, "w").write(str(v))
            continue
        old = rd(phy, r)
        saved.append((r, old))
        wr(phy, r, old | v if op == "|=" else old & v if op == "&=" else old ^ v if op == "^=" else v)
    return saved


def main():
    f = int(sys.argv[1])
    cfgs = sys.argv[2:]
    os.chdir(D)
    subprocess.run(["./rigreset.sh"], stdout=subprocess.DEVNULL)
    pb = open("/sys/class/net/wlx00c0cab79628/phy80211/name").read().strip()
    subprocess.run(["python3", "chsw.py", pb, "11", "0", "0"], stdout=subprocess.DEVNULL)
    # CHANNEL_SWITCH: control channel 1 (band 0) with the frequency override; env TXS = TX streams (2 for STBC: both
    # chains), RXM = RX chain mask
    subprocess.run(["python3", "chsw.py", pb, "1", "0", os.environ.get("CBW", "0"), os.environ.get("TXS", "1"),
                    os.environ.get("RXM", "1"), str(f), os.environ.get("CTR", "1")], stdout=subprocess.DEVNULL)
    apply(pb, parse(os.environ.get("BASE", "")))
    wr(pb, 0x820e3000, rd(pb, 0x820e3000) & ~0x100)  # MAC TX on
    m = "/sys/module/mt7921dbg/parameters/tx_count"
    open(m, "w").write("1")
    rate = int(os.environ.get("RATE", "12"))
    secs = int(3 + 1.3 * len(cfgs)) + 2
    if os.path.exists(DUMP):
        os.remove(DUMP)
    cap = subprocess.Popen(["python3", "nbrxd.py", "A", str(f), "--pack", "8", "--gain", "0,0,0", "--rx-only", "--quiet",
                            "--slice-ms", str(secs * 1000 + 5000), "--seconds", str(secs), "--dump", DUMP,
                            "--dump-gate", "3"],
                           stdout=open("/tmp/txscan.log", "w"), stderr=subprocess.STDOUT)
    for _ in range(120):  # the gated dump's 2000-record noise reference (16 KiB records) is on disk: A streams
        if os.path.exists(DUMP) and os.path.getsize(DUMP) >= 2000 * 16336:
            break
        time.sleep(0.1)
    else:
        print("card A's stream did not start")
    time.sleep(0.2)
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    s.bind(("mon_b", 0))
    labels = []
    try:
        for i, c in enumerate(cfgs):
            s.send(frame(1000000 + i, rate, 64))  # marker at the base; payloads carry the config index (txscansum)
            time.sleep(0.06)
            try:
                saved = apply(pb, parse(c))
            except (OSError, SystemExit) as e:  # a wedged MCU: stop here and say where
                print(f"config {i} ({c}) failed: {e}")
                break
            for k in range(3):
                s.send(frame(2000000 + 10 * i + k, rate, 64))
                time.sleep(0.04)
            time.sleep(0.2)
            for r, v in reversed(saved):
                if isinstance(r, str):
                    open(PARAMS + r, "w").write(str(v))
                else:
                    wr(pb, r, v)
            time.sleep(0.5)
            labels.append(c)
            if cap.poll() is not None:
                print(f"capture ended early at config {i}")
                break
    finally:
        for p in ("tx_count", "tx_bw", "tx_rate", "tx_dw6_set", "tx_dw6_clr"):
            open(PARAMS + p, "w").write("0")
    cap.wait()
    open("/tmp/txscan.labels", "w").write("\n".join(labels) + "\n")
    subprocess.run(["python3", "txscan.py", DUMP, "8", "350", "/tmp/txscan.labels"], cwd=D + "/sdr")
    if os.environ.get("KEEP"):  # env KEEP=<path>: keep the gated dump for offline work (e.g. sdr/perawgn.py)
        os.replace(DUMP, os.environ["KEEP"])
    else:
        os.remove(DUMP)


if __name__ == "__main__":
    main()
