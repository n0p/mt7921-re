#!/bin/bash
# Which ICAP sample rate does each divider value (0x830ad448[19:18]) give? Card B in native cbw (path 0), ICAP test
# mode, 2048 samples at tap T per divider, report the run-length histogram of repeated samples. Rig reset before
# and after (ICAP mode leaves the packet SRAM unusable for the normal datapath).
# Usage: icapdiv.sh [cbw=4] [tap=4]
cd /root/fwre
C=${1:-4} T=${2:-4}
./rigreset.sh > /dev/null
PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
python3 chsw.py $PB 149 1 0 > /dev/null; python3 chsw.py $PB 36 1 $C 1 1 > /dev/null
python3 ate.py $PB mode 2 > /dev/null; sleep 2
for d in 3 2 1 0; do python3 icap.py $PB /tmp/div$d.bin $T $d 2 2048 | cut -c1-60; done
./rigreset.sh > /dev/null
python3 - <<PY
import numpy as np
for d in (3, 2, 1, 0):
    x = np.fromfile(f"/tmp/div{d}.bin", dtype=np.int16).astype(np.int32); iq = x[0::2] + 1j * x[1::2]
    runs, r = [], 1
    for a, b in zip(iq[:-1], iq[1:]):
        if a == b: r += 1
        else: runs.append(r); r = 1
    h = np.bincount(runs, minlength=9)[1:9]
    print(f"div {d}: mean|x| {np.abs(iq).mean():6.1f} runs[1..8] {list(map(int, h))}")
PY
