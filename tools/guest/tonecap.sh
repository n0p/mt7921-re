#!/bin/bash
# Capture ICAP taps on card B (5180 MHz) in a given mode while an external tone is on air.
# Usage: tonecap.sh <label> <cbw> <extra reg=val or -> <taps...>
cd /root/fwre
L=$1 C=$2 X=$3; shift 3
./rigreset.sh > /dev/null
P=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
python3 chsw.py $P 149 1 0 > /dev/null; python3 chsw.py $P 36 1 $C 1 1 > /dev/null
python3 ate.py $P mode 2 > /dev/null; sleep 2
[ "$X" != - ] && python3 memdump.py $P write ${X%%=*} ${X#*=} > /dev/null
for t in "$@"; do python3 icap.py $P /root/fwre/tone_${L}_t$t.bin $t 3 2 4096 > /dev/null; done
python3 ate.py $P mode 0 > /dev/null
echo "$L done"
