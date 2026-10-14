#!/usr/bin/env python3
"""Estimate the NDS32 $gp value used by the MT7961 RAM code.

GP is set by the boot ROM, so it never appears in the RAM image. We score each
candidate by how many `addi.gp rX, imm` targets land exactly on the start of a
printable C string in the loaded regions.
Usage: find_gp.py <regions.json dir> <disasm .txt>...
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

rdir = Path(sys.argv[1])
info = json.loads((rdir / "regions.json").read_text())
mem = []
for r in info["ram"]["regions"] + info["patch"]["sections"]:
    if r["file"]:
        mem.append((int(r["addr"], 16), (rdir / r["file"]).read_bytes()))

starts = set()
for base, blob in mem:
    for m in re.finditer(rb"[\x20-\x7e\t\n]{6,}\x00", blob):
        starts.add(base + m.start())

offs = Counter()
for d in sys.argv[2:]:
    for m in re.finditer(r"addi\.gp \w+, (-?\d+)", Path(d).read_text()):
        offs[int(m.group(1))] += 1
print(f"{len(starts)} string starts, {len(offs)} distinct addi.gp offsets")

scores = Counter()
for s in starts:
    for o in offs:
        g = s - o
        if g % 4 == 0:
            scores[g] += 1
for g, n in scores.most_common(8):
    print(f"gp={g:#010x} hits={n}")
