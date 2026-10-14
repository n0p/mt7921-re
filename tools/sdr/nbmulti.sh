#!/bin/bash
# Narrow RX screen: for each candidate, card B gets a direct CHANNEL_SWITCH (cbw base, path 0) at 5180 plus register
# writes (fwdbg), then the X410 sends 10, 5 and 20 MHz 802.11a/p frames in turn while B captures; prints how many
# frames B decoded per X410 sample rate (payload tag "X410-OFDM-<Msps>-"; 20 MHz is played at 25 Msps), then
# the total number of frames B captured.
# Usage: nbmulti.sh <cbw> <candidate>...    candidate: reg=val[,reg=val...] or "-"
cd "$(dirname "$0")/../.."
CBW=$1; shift
for cand in "$@"; do
	w=${cand//,/ }; [ "$cand" = - ] && w=""
	tools/g.sh "cd /root/fwre; P=\$(cat /sys/class/net/wlx00c0cab79628/phy80211/name);
	    python3 chsw.py \$P 149 1 0 > /dev/null; python3 chsw.py \$P 36 1 $CBW 1 1 > /dev/null;
	    for kv in $w; do python3 fwdbg.py \$P write \${kv%%=*} \${kv#*=} > /dev/null; done;
	    rm -f /tmp/nbm.pcap; (setsid timeout 40 tcpdump -i mon_b -w /tmp/nbm.pcap > /dev/null 2>&1 < /dev/null &)" </dev/null
	sleep 2
	for wd in 10 5 20; do
		timeout 120 tools/h.sh "cd /tmp/mt7921sdr && python3 txframes.py 5180 $wd 100 4 6 40 > /dev/null 2>&1" </dev/null
	done
	r=$(tools/g.sh "pkill -f '^tcpdump -i mon_b'; sleep 1; tcpdump -r /tmp/nbm.pcap -A 2>/dev/null | grep -a -o 'OFDM-[0-9.]*-' | sort | uniq -c | tr '\n' ' '; tcpdump -r /tmp/nbm.pcap 2>/dev/null | wc -l" </dev/null | tr '\n' ' ')
	echo "cbw $CBW $cand: $r"
done
