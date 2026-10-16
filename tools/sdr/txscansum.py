#!/usr/bin/env python3
"""Summarize a tools/guest/txscan.py log by time: configuration k's marker goes out at about t0 + k * period, so
every burst is assigned to floor((t - t0 + slack) / period) whatever the silence-based grouping did (a configuration
that keys the transmitter for a second swallows the next marker). Prints one line per configuration: the label and
the widths of its bursts after the marker (20 / 10 / 5 / 2.5 MHz, ? = not OFDM timing, LONG = > 4 ms).
Usage: txscansum.py <log> <labels: one per line, or a file with space-separated configs; "-" is prepended>"""
import re
import sys

import numpy as np


def main():
    log, labf = sys.argv[1], sys.argv[2]
    labels = ["-"] + open(labf).read().split()  # txscan.py runs a "-" reference first
    rows = []
    for line in open(log):
        m = re.match(r"\s+t\s+([\d.]+) s\s+([\d.]+) us .*-> +(\S+) MHz", line)
        if m:
            rows.append((float(m[1]), float(m[2]), m[3]))
    starts = []
    for line in open(log):
        m = re.match(r"group \d+", line)
        if m:
            starts.append(None)
    # group starts from the first burst after each "group" header
    gs, cur = [], False
    for line in open(log):
        if line.startswith("group "):
            cur = True
        else:
            m = re.match(r"\s+t\s+([\d.]+) s", line)
            if m and cur:
                gs.append(float(m[1]))
                cur = False
    ids = {}
    for line in open(log):
        m = re.match(r"\s+t\s+([\d.]+) s.* id=(\d+)", line)
        if m:
            ids[float(m[1])] = int(m[2])
    per = {}
    if ids:
        # frames carry their configuration (tools/guest/txscan.py): marker 1000000 + i, test frame 2000000 + 10 i + k;
        # an undecoded burst belongs to the last decoded one before it, if that is under 0.8 s earlier
        last = None
        for t, dur, w in rows:
            if t in ids:
                v = ids[t]
                last = (t, v - 1000000 if v < 2000000 else (v - 2000000) // 10)
                kind = "M" if v < 2000000 else "f"
            elif last and t - last[0] < 0.8:
                kind = "?"
            else:
                continue
            per.setdefault(last[1], []).append((kind, dur, w))
        print(f"{len(labels)} labels, {len(rows)} bursts printed, {len(ids)} carry a config id")
    else:
        d = np.diff(gs)
        period = float(np.median(d[(d > 0.5) & (d < 2.0)]))
        t0 = gs[0]
        for t, dur, w in rows:
            k = int(np.floor((t - t0 + 0.15) / period))
            per.setdefault(k, []).append(("M" if t - (t0 + k * period) <= 0.03 else "f", dur, w))
        print(f"period {period:.3f} s, {len(labels)} labels, {len(rows)} bursts printed")
    for k, lab in enumerate(labels):
        bs = per.get(k, [])
        cfg = [b for b in bs if b[0] != "M"]
        mk = "M" if any(b[0] == "M" for b in bs) else "-"
        ws = " ".join(("LONG" if dur > 4000 else w) for _, dur, w in cfg) or "silent"
        print(f"{k:3d} {mk} {lab:45s} {ws}")


if __name__ == "__main__":
    main()
