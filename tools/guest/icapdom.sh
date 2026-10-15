#!/bin/bash
# ICAP source survey: card B in native cbw C at 5180 (path 0), ICAP ring mode 1 (no test mode); for each spec
# dom:sel:rate capture N samples while the firmware hook hk_icaprate measures the real capture rate (producer advance
# over 200 us), then print the rate, the sample statistics and the run-length (hold) histogram.
#   dom 4: BirriaSDR node, sel = 0x830ad448[15:8], rate = divider [19:18] (3 = 40 Msps)
#   dom 1: MODE 0x14, sel = 0x830ad448[15:8], rate = divider [19:18]
#   dom 2: MODE 0x21, sel = 0x830ad44c[7:0], rate = 0x830ad44c[18:16]
# Rig reset before and after. Usage: icapdom.sh <cbw> <dom:sel:rate>...   env N (512)
cd /root/fwre
C=$1; shift
N=${N:-512} HOOK=$(awk '$3 == "hk_icaprate" {print "0x"$1}' /root/fwre/hooks.nm)
./rigreset.sh > /dev/null
PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
python3 chsw.py $PB 149 1 0 > /dev/null; python3 chsw.py $PB 36 1 $C 1 1 > /dev/null
for spec in "$@"; do
	IFS=: read dom sel rate <<< "$spec"
	rm -f /tmp/icd.bin
	r=$(ICAP_RING=1 ICAP_DOM=$dom ICAP_RATE2=$rate ICAP_RATE_HOOK=$HOOK ICAP_RAW=1 timeout 120 \
		python3 icap.py $PB /tmp/icd.bin $sel $rate 1 $N 2>&1 | grep rate:)
	[ -s /tmp/icd.bin ] || { echo "$spec: capture failed"; continue; }
	python3 - "$spec" "$r" <<'PY'
import sys, numpy as np
w = np.fromfile("/tmp/icd.bin", dtype=np.uint32)
q = (w & 0x3fff).astype(np.int32); q -= (q & 0x2000) << 1
i = ((w >> 16) & 0x3fff).astype(np.int32); i -= (i & 0x2000) << 1
iq = i + 1j * q
runs, r = [], 1
for a, b in zip(w[:-1], w[1:]):
    if a == b: r += 1
    else: runs.append(r); r = 1
h = np.bincount(runs, minlength=9)[1:9]
print(f"{sys.argv[1]:>10}: {sys.argv[2].replace('rate: producer advance per ', '')} distinct {len(set(w.tolist()))} "
      f"|iq| med {np.median(np.abs(iq)):.0f} hi {int(((w >> 30) != 0).sum() + (((w >> 14) & 3) != 0).sum())} runs {list(map(int, h))}")
PY
done
./rigreset.sh > /dev/null
