#!/bin/sh
# Narrowband RX test: card B configured with a direct CHANNEL_SWITCH (tools/guest/chsw.py), X410 sends 802.11a/p
# 6 Mbps frames, count B's good receptions from the X410 SA.
# Usage: nbtest.sh <freq MHz> <ch> <band 0|1> "<cbw> <tx> <rxmask> <x410 width> [outband MHz] [center ch]" ...
# Pre-extra register writes for B can be passed in env PRE="reg=val reg=val" (applied after the switch, through the
# firmware debug command). NOSTAT=1: firmware without the debug command (stock 2022): status and PRE writes go
# through host register access instead.
cd "$(dirname "$0")/../.."
F=$1 CH=$2 BAND=$3; shift 3
HOP=$([ "$BAND" = 1 ] && echo "149 1 0" || echo "11 0 0")
for row in "$@"; do
	set -- $row
	st=$(tools/g.sh "cd /root/fwre; P=\$(cat /sys/class/net/wlx00c0cab79628/phy80211/name);
	    export NOSTAT=${NOSTAT:-}; python3 chsw.py \$P $HOP > /dev/null; python3 chsw.py \$P $CH $BAND $1 $2 $3 ${5:-0} ${6:-};
	    for kv in ${PRE:-}; do if [ -n \"\$NOSTAT\" ]; then python3 memdump.py \$P write \${kv%%=*} \${kv#*=};
	        else python3 fwdbg.py \$P write \${kv%%=*} \${kv#*=}; fi > /dev/null; done;
	    (setsid timeout 14 tcpdump -i mon_b -nn -e > /tmp/nb.txt 2>/dev/null < /dev/null &)" 2>/dev/null </dev/null | tail -1)
	sleep 2
	timeout 120 tools/h.sh "cd /tmp/mt7921sdr && python3 txframes.py $F $4 200 5 6 40 > /dev/null 2>&1" </dev/null
	sleep 2
	res=$(tools/g.sh "echo good=\$(grep 02:00:00:00:00:02 /tmp/nb.txt | grep -vc bad-fcs)/200 all=\$(grep -c . /tmp/nb.txt)" 2>/dev/null </dev/null)
	echo "cbw=$1 tx=$2 rx=$3 X410=${4}MHz | $res | ${st#*: }"
done
