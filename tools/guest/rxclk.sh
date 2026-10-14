#!/bin/bash
# Does the TX clock-divider bit also clock the receiver? A and B at 20 MHz (cbw 0, path 0); B gets extra writes.
# Usage: rxclk.sh <candidate>...   (reg=val[,reg=val]; "-" = none)
cd /root/fwre
for cand in "$@"; do
	./rigreset.sh > /dev/null
	PA=$(cat /sys/class/net/wlx00c0cab7962a/phy80211/name); PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
	for P in $PA $PB; do python3 chsw.py $P 149 1 0 > /dev/null; python3 chsw.py $P 36 1 0 1 1 > /dev/null; done
	[ "$cand" != - ] && for kv in ${cand//,/ }; do python3 fwdbg.py $PB write ${kv%%=*} ${kv#*=} > /dev/null; done
	timeout 7 tcpdump -i mon_b -nn -e 'wlan addr2 02:00:00:00:00:01' > /tmp/rk.txt 2>/dev/null &
	sleep 1
	python3 inject.py mon_a 80 20 12 > /dev/null
	wait
	echo "A20 -> B20 + $cand: B lines $(grep -c . /tmp/rk.txt) bad-fcs $(grep -c bad-fcs /tmp/rk.txt)"
done
./rigreset.sh > /dev/null
