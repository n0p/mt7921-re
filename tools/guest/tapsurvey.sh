#!/bin/bash
# ICAP tap survey on card B at 5180 MHz in CHANNEL_SWITCH mode cbw (0 = 20 MHz, 4 = native narrow).
# Usage: tapsurvey.sh <cbw> <taps...>    output: /root/fwre/tap_c<cbw>_t<tap>.bin
cd /root/fwre
C=$1; shift
./rigreset.sh > /dev/null
P=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
python3 chsw.py $P 149 1 0 > /dev/null; python3 chsw.py $P 36 1 $C 1 1 | cut -c1-70
python3 ate.py $P mode 2 > /dev/null; sleep 2
for t in "$@"; do
	python3 icap.py $P /root/fwre/tap_c${C}_t$t.bin $t 3 3 4096 | sed "s/^/tap $t: /"
done
python3 ate.py $P mode 0 > /dev/null
