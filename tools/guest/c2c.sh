#!/bin/bash
# Card-to-card narrow RX screen without an SDR. Card A transmits native narrow frames (CHANNEL_SWITCH cbw CA,
# path 0, 6 Mbps, 55-byte MPDU "MT7921-NBW-n" from 02:00:00:00:00:01); card B sits in native cbw CB (path 0) and
# gets candidate register writes through the firmware debug command, restored after each candidate.
# Per candidate prints: frames from A's SA (good / bad FCS), all bad-FCS frames and non-beacon frames (a
# demodulator that locks but mis-decodes still shows up here) and all frames B reported.
# Usage: c2c.sh <CA> <CB> <candidate>...    candidate: reg=val[,reg=val...] or "-"; env NF=frames (40), GAP=ms (25)
# Env BOTH=1: apply (and restore) each candidate on card A too (symmetric link tests).
cd /root/fwre
CA=$1 CB=$2; shift 2
NF=${NF:-40} GAP=${GAP:-25}
./rigreset.sh > /dev/null
PA=$(cat /sys/class/net/wlx00c0cab7962a/phy80211/name); PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
python3 chsw.py $PA 149 1 0 > /dev/null; python3 chsw.py $PA 36 1 $CA 1 1 > /dev/null
python3 chsw.py $PB 149 1 0 > /dev/null; python3 chsw.py $PB 36 1 $CB 1 1 > /dev/null
for cand in "$@"; do
	saved=""
	if [ "$cand" != - ]; then
		for P in $PB ${BOTH:+$PA}; do
			for kv in ${cand//,/ }; do
				a=${kv%%=*}; v=${kv#*=}
				old=$(python3 fwdbg.py $P read $a 1 | awk '{print $2}')
				saved="$saved $P:$a=0x$old"
				python3 fwdbg.py $P write $a $v > /dev/null
			done
		done
	fi
	timeout $(( NF * GAP / 1000 + 4 )) tcpdump -i mon_b -nn -e -v > /tmp/c2c.txt 2>/dev/null &
	sleep 0.7
	python3 inject.py mon_a $NF $GAP 12 0 > /dev/null
	wait
	sa=$(grep -c "SA:02:00:00:00:00:01\|02:00:00:00:00:01 " /tmp/c2c.txt)
	good=$(grep "02:00:00:00:00:01" /tmp/c2c.txt | grep -vc bad-fcs)
	len=$(grep -c bad-fcs /tmp/c2c.txt)
	nb=$(grep -v -c -E "Beacon|Probe" /tmp/c2c.txt)
	all=$(grep -c . /tmp/c2c.txt)
	for pkv in $saved; do P=${pkv%%:*}; kv=${pkv#*:}; python3 fwdbg.py $P write ${kv%%=*} ${kv#*=} > /dev/null; done
	echo "A$CA->B$CB $cand: A-frames $sa (good $good) bad-fcs $len non-beacon $nb all $all"
done
