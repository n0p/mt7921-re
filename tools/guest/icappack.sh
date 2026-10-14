#!/bin/bash
# ICAP packing field survey: card B native cbw (path 0), ICAP test mode, 2048 samples at tap T for each value of
# 0x800210b8[19:16]; prints raw word statistics (distinct words, repeats, top/bottom halves). Rig reset around.
# Usage: icappack.sh [cbw=4] [tap=4] [values...]
cd /root/fwre
C=${1:-4} T=${2:-4}; shift 2
./rigreset.sh > /dev/null
PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
python3 chsw.py $PB 149 1 0 > /dev/null; python3 chsw.py $PB 36 1 $C 1 1 > /dev/null
python3 ate.py $PB mode 2 > /dev/null; sleep 2
for v in "$@"; do ICAP_PACK=$v python3 icap.py $PB /tmp/pack$v.bin $T 3 2 2048 | cut -c1-50; done
./rigreset.sh > /dev/null
for v in "$@"; do python3 - $v <<PY
import sys, numpy as np
v = sys.argv[1]
x = np.fromfile(f"/tmp/pack{v}.bin", dtype=np.int16).astype(np.int32); iq = x[0::2] + 1j * x[1::2]
runs, r = [], 1
for a, b in zip(iq[:-1], iq[1:]):
    if a == b: r += 1
    else: runs.append(r); r = 1
h = np.bincount(runs, minlength=9)[1:9]
print(f"pack {v}: mean|x| {np.abs(iq).mean():7.1f} distinct {len(set(iq.tolist()))} runs[1..8] {list(map(int, h))}")
PY
done
