#!/bin/bash
# ICAP capture-rate and format survey: card B in native cbw C at 5180 (path 0), ICAP ring mode 1 (64 KiB at 0xe0280000,
# no test mode, so the debug command keeps working); for each domain-4
# MODE byte (0x830ad440[7:0]) capture N raw words at tap T while the firmware hook hk_icaprate measures the producer
# advance over 200 us (the real capture rate), then print the run-length histogram of identical raw words.
# Rig reset before and after. Usage: icaprate.sh <cbw> <tap> <mode>...   env N (1024), HOOK (hk_icaprate address)
cd /root/fwre
C=$1 T=$2; shift 2
N=${N:-1024} HOOK=${HOOK:-$(awk '$3 == "hk_icaprate" {print "0x"$1}' /root/fwre/hooks.nm)}
./rigreset.sh > /dev/null
PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
python3 chsw.py $PB 149 1 0 > /dev/null; python3 chsw.py $PB 36 1 $C 1 1 > /dev/null
for m in "$@"; do
	r=$(ICAP_RING=1 ICAP_MODE=$m ICAP_RATE_HOOK=$HOOK ICAP_RAW=1 timeout 120 python3 icap.py $PB /tmp/icr.bin $T 3 1 $N 2>&1 | grep rate:)
	python3 - "$m" "$r" <<'PY'
import sys, numpy as np
w = np.fromfile("/tmp/icr.bin", dtype=np.uint32)
runs, r = [], 1
for a, b in zip(w[:-1], w[1:]):
    if a == b: r += 1
    else: runs.append(r); r = 1
h = np.bincount(runs, minlength=9)[1:9]
flags = sorted({int(x) for x in (w >> 30)} | {int(x) << 2 for x in ((w >> 14) & 3)})
q = (w & 0x3fff).astype(np.int32); q -= (q & 0x2000) << 1
i = ((w >> 16) & 0x3fff).astype(np.int32); i -= (i & 0x2000) << 1
iq = i + 1j * q
ri, r2 = [], 1
for a, b in zip(iq[:-1], iq[1:]):
    if a == b: r2 += 1
    else: ri.append(r2); r2 = 1
hi = np.bincount(ri, minlength=9)[1:9]
print(f"mode {sys.argv[1]}: {sys.argv[2].replace('rate: producer advance per ', '')} runs[1..8] {list(map(int, h))} "
      f"distinct {len(set(w.tolist()))} flags {flags} |iq| {np.abs(iq).mean():.0f} iq-runs {list(map(int, hi))}")
PY
done
./rigreset.sh > /dev/null
