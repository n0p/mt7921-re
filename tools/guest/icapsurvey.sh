#!/bin/bash
# ICAP tap survey for one capture domain: card B in native cbw C (path 0) plus optional writes W, ICAP test mode;
# for each SEL capture 512 samples and print liveness (zero / constant / live), hold histogram and mean amplitude.
# Usage: icapsurvey.sh <cbw> <W reg=val,...|-> <dom 1|2|4> <sel>...    (rig reset before and after)
cd /root/fwre
C=$1 W=$2 D=$3; shift 3
./rigreset.sh > /dev/null
PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
python3 chsw.py $PB 149 1 0 > /dev/null; python3 chsw.py $PB 36 1 $C 1 1 > /dev/null
[ "$W" = - ] || for kv in ${W//,/ }; do python3 fwdbg.py $PB write ${kv%%=*} ${kv#*=} > /dev/null; done
python3 ate.py $PB mode 2 > /dev/null; sleep 2
for t in "$@"; do
	ICAP_DOM=$D timeout 60 python3 icap.py $PB /tmp/sv.bin $t 3 1 512 > /dev/null 2>&1 || { echo "dom $D sel $t: capture failed"; continue; }
	python3 - $D $t <<PY
import sys, numpy as np
x = np.fromfile("/tmp/sv.bin", dtype=np.int16).astype(np.int32); iq = x[0::2] + 1j * x[1::2]
d = len(set(iq.tolist()))
runs, r = [], 1
for a, b in zip(iq[:-1], iq[1:]):
    if a == b: r += 1
    else: runs.append(r); r = 1
h = np.bincount(runs, minlength=9)[1:9]
state = "zero" if not np.any(iq) else ("const" if d == 1 else "live")
print(f"dom {sys.argv[1]} sel {sys.argv[2]:>3}: {state:5} distinct {d:4} mean|x| {np.abs(iq).mean():8.1f} runs[1..8] {list(map(int, h))}")
PY
done
python3 ate.py $PB mode 0 > /dev/null
./rigreset.sh > /dev/null
