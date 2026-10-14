#!/bin/bash
# Card-to-card with independent modes: A (TX) native cbw, B (RX) native cbw + extra writes. Rig reset each run.
# Usage: rxpair.sh <A cbw> <B cbw> <candidate>...   (reg=val[,reg=val]; rf:reg=val; "-" = none)
cd /root/fwre
CA=$1 CB=$2; shift 2
for cand in "$@"; do
	./rigreset.sh > /dev/null
	PA=$(cat /sys/class/net/wlx00c0cab7962a/phy80211/name); PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
	python3 chsw.py $PA 149 1 0 > /dev/null; python3 chsw.py $PA 36 1 $CA 1 1 > /dev/null
	python3 chsw.py $PB 149 1 0 > /dev/null; python3 chsw.py $PB 36 1 $CB 1 1 > /dev/null
	if [ "$cand" != - ]; then
		for kv in ${cand//,/ }; do
			case $kv in
			rf:*) kv=${kv#rf:}; python3 fwdbg.py $PB call 0x0083a66c ${kv%%=*} ${kv#*=} 0 > /dev/null;;
			*) python3 fwdbg.py $PB write ${kv%%=*} ${kv#*=} > /dev/null;;
			esac
		done
	fi
	timeout 7 tcpdump -i mon_b -nn -e 'wlan addr2 02:00:00:00:00:01' > /tmp/rp.txt 2>/dev/null &
	sleep 1
	python3 inject.py mon_a 80 20 12 > /dev/null
	wait
	echo "A cbw$CA -> B cbw$CB + $cand: lines $(grep -c . /tmp/rp.txt) bad-fcs $(grep -c bad-fcs /tmp/rp.txt) $(head -1 /tmp/rp.txt | grep -o -E '[0-9.]+ Mb/s|-[0-9]+dBm' | tr '\n' ' ')"
done
./rigreset.sh > /dev/null
