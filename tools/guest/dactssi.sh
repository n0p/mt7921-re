#!/bin/bash
# Why does the tone-DAC output collapse? Card TXC (default A) repeats the same playback (+15.6 kHz at 250 ksps, 0.5 s;
# fw hk_dacplay) in separate tone sessions under three variants, while an X410 capture records (tools/sdr/capture.py,
# RX1; analyse with tools/sdr/dacsess.py): plain; 0x8300c000/0x8301c000 back to the stock TSSI control 0xc0c80806 after the tone
# starts (TxTone writes 0x00274027); the tone power override 0x8300c508 [31]/[15] cleared.
# Usage: dactssi.sh [MHz 2350]   env TXC, DELAY (s before the first tone, 8), REPS (2)
cd /root/fwre
F=${1:-2350}
./rigreset.sh > /dev/null
P=$(cat /sys/class/net/$([ "${TXC:-A}" = A ] && echo wlx00c0cab7962a || echo wlx00c0cab79628)/phy80211/name)
HK=$(awk '$3 == "hk_dacplay" {print "0x"$1}' hooks.nm)
w() { python3 fwdbg.py $P write $1 $2 > /dev/null; }
r() { python3 fwdbg.py $P read $1 1 | cut -d' ' -f2; }
python3 chsw.py $P 11 0 0 > /dev/null; python3 chsw.py $P 1 0 0 1 1 $F > /dev/null
sleep ${DELAY:-8}
session() {	# variant
	python3 fwdbg.py $P call 0x009691d2 1 0 0 0 > /dev/null
	case $1 in
	tssi) w 0x8300c000 0xc0c80806; w 0x8301c000 0xc0c80806 ;;
	nopwr) w 0x8300c508 $(printf "0x%x" $(( 0x$(r 0x8300c508) & 0x7fff7fff ))) ;;
	esac
	sleep 0.3
	echo "$(date +%s.%N) $1: $(python3 fwdbg.py $P call $HK 4 4 125000 1024)"
	sleep 0.3
	python3 fwdbg.py $P call 0x00969126 0 0 > /dev/null
	sleep 0.4
}
for i in $(seq 1 ${REPS:-2}); do for v in plain tssi nopwr; do session $v; done; done
./rigreset.sh > /dev/null
