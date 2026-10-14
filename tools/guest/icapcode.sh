#!/bin/bash
# Rate meter for the receive chain: card B in native cbw C (path 0), ICAP test mode; for each value v of 0x83081000
# (host write, restored afterwards) capture N samples at each tap and print the hold histogram (runs of identical
# samples at the 40 Msps capture clock: runs of 2 = data at 20 Msps, 4 = 10 Msps) and the mean amplitude.
# Rig reset before and after (ICAP mode leaves the packet SRAM unusable).
# Usage: icapcode.sh <cbw> "<taps>" <value>...       env N (1024)
cd /root/fwre
C=$1 TAPS=$2; shift 2
N=${N:-1024}
./rigreset.sh > /dev/null
PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
python3 chsw.py $PB 149 1 0 > /dev/null; python3 chsw.py $PB 36 1 $C 1 1 > /dev/null
python3 ate.py $PB mode 2 > /dev/null; sleep 2
old=$(python3 memdump.py $PB read 0x83081000 4 | awk '{print $5$4$3$2}')
for v in "$@"; do
	[ "$v" = - ] || python3 memdump.py $PB write 0x83081000 $v > /dev/null
	for t in $TAPS; do
		python3 icap.py $PB /tmp/ic.bin $t 3 1 $N > /dev/null 2>&1 || { echo "$v tap $t: capture failed"; continue; }
		python3 - "$v" "$t" <<PY
import sys, numpy as np
x = np.fromfile("/tmp/ic.bin", dtype=np.int16).astype(np.int32); iq = x[0::2] + 1j * x[1::2]
runs, r = [], 1
for a, b in zip(iq[:-1], iq[1:]):
    if a == b: r += 1
    else: runs.append(r); r = 1
h = np.bincount(runs, minlength=9)[1:9]
print(f"{sys.argv[1]} tap {sys.argv[2]:>2}: mean|x| {np.abs(iq).mean():7.1f} runs[1..8] {list(map(int, h))}")
PY
	done
	python3 memdump.py $PB write 0x83081000 0x$old > /dev/null
done
python3 ate.py $PB mode 0 > /dev/null
./rigreset.sh > /dev/null
