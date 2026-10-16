#!/usr/bin/env python3
"""Match a pdscan.py log (candidate windows t0..t1, env SCORE=good) against a tcpdump -tt capture of the reference
transmitter's frames on the monitor interface: print every candidate (and baseline window) with good-FCS frames inside
its window. Usage: rx10match.py <pdscan log> <tcpdump -e -tt output>"""
import ast
import re
import sys

good = [float(l.split()[0]) for l in open(sys.argv[2]) if l[:1].isdigit() and "bad-fcs" not in l]
bad = [float(l.split()[0]) for l in open(sys.argv[2]) if l[:1].isdigit() and "bad-fcs" in l]
print(f"{len(good)} good-FCS and {len(bad)} bad-FCS frames from the reference SA in the capture")
wins = []
for line in open(sys.argv[1]):
    if line.startswith("baseline"):
        for d in ast.literal_eval(line[line.index("["):line.index("]") + 1]):
            wins.append(("baseline", d))
        continue
    m = re.match(r"(HIT )?\s*(\d+) (.*): (\{.*\})", line.rstrip())
    if m:
        wins.append((f"{m[2]} {m[3]}", ast.literal_eval(m[4])))
hits = 0
for label, d in wins:
    n = sum(d["t0"] <= t <= d["t1"] for t in good)
    if n:
        hits += 1
        print(f"{n:3d} good  {label}  {d}")
print(f"{len(wins)} windows, {hits} with good-FCS reference frames")
