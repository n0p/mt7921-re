#!/bin/bash
# Narrow RX candidate test, card to card (exact clock match): A and B in native narrow (CHANNEL_SWITCH cbw, path 0)
# at 5180 MHz, extra register writes on B (reg=val, comma separated; rf:reg=val for A-die), A injects OFDM,
# count B receptions from A's SA. Each candidate starts from a full rig reset (no leftover writes).
# Usage: rxcand.sh <cbw> <candidate>...      candidate "-" = none
cd /root/fwre
CBW=$1; shift
for cand in "$@"; do
	./rigreset.sh > /dev/null
	PA=$(cat /sys/class/net/wlx00c0cab7962a/phy80211/name); PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
	for P in $PA $PB; do python3 chsw.py $P 149 1 0 > /dev/null; python3 chsw.py $P 36 1 $CBW 1 1 > /dev/null; done
	if [ "$cand" != - ]; then
		for kv in ${cand//,/ }; do
			case $kv in
			rf:*) kv=${kv#rf:}; python3 fwdbg.py $PB call 0x0083a66c ${kv%%=*} ${kv#*=} 0 > /dev/null;;
			*) python3 fwdbg.py $PB write ${kv%%=*} ${kv#*=} > /dev/null;;
			esac
		done
	fi
	timeout 7 tcpdump -i mon_b -nn -e 'wlan addr2 02:00:00:00:00:01' > /tmp/rc.txt 2>/dev/null &
	sleep 1
	python3 inject.py mon_a 80 20 12 > /dev/null
	wait
	echo "cbw $CBW $cand: B lines $(grep -c . /tmp/rc.txt) bad-fcs $(grep -c bad-fcs /tmp/rc.txt)"
done
./rigreset.sh > /dev/null
