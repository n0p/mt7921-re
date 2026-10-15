#!/bin/bash
# Narrow RX screen: for each candidate, card B gets a direct CHANNEL_SWITCH (cbw base, path 0) at 5180 plus register
# writes (fwdbg), then the X410 sends 10, 5 and 20 MHz 802.11a/p frames in turn while B captures; prints how many
# frames B decoded per X410 sample rate (payload tag "X410-OFDM-<Msps>-"; 20 MHz is played at 25 Msps), then
# the total number of frames B captured.
# Env HOPCBW: width of the hop to channel 149 before switching to 36 (default 0 = 20 MHz).
# Usage: nbmulti.sh <cbw> <candidate>...    candidate: reg=val[,reg=val...] or "-"; rf:reg=val writes an A-die RF
# register on path 0 (firmware FUN_83a66c through the debug command), rf1:reg=val on path 1
cd "$(dirname "$0")/../.."
CBW=$1; shift
for cand in "$@"; do
	w=${cand//,/ }; [ "$cand" = - ] && w=""
	tools/g.sh "cd /root/fwre; P=\$(cat /sys/class/net/wlx00c0cab79628/phy80211/name);
	    python3 chsw.py \$P 149 1 ${HOPCBW:-0} > /dev/null; python3 chsw.py \$P 36 1 $CBW 1 1 > /dev/null; python3 fwdbg.py \$P read 0x820b00a0 1 >&2;
	    for kv in $w; do case \$kv in
	        rf:*) kv=\${kv#rf:}; python3 fwdbg.py \$P call 0x0083a66c \${kv%%=*} \${kv#*=} 0 > /dev/null;;
	        rf1:*) kv=\${kv#rf1:}; python3 fwdbg.py \$P call 0x0083a66c \${kv%%=*} \${kv#*=} 1 > /dev/null;;
	        *) python3 fwdbg.py \$P write \${kv%%=*} \${kv#*=} > /dev/null;;
	    esac; done;
	    [ -e /sys/kernel/debug/mt7921dbg/rxbw ] && echo 0 > /sys/kernel/debug/mt7921dbg/rxbw;
	    rm -f /tmp/nbm.pcap; (setsid timeout 40 tcpdump -i mon_b -w /tmp/nbm.pcap > /dev/null 2>&1 < /dev/null &)" </dev/null
	sleep 2
	for wd in 10 5 20; do
		timeout 120 tools/h.sh "cd /tmp/mt7921sdr && python3 txframes.py 5180 $wd 100 4 6 40 > /dev/null 2>&1" </dev/null
	done
	r=$(tools/g.sh "pkill -f '^tcpdump -i mon_b'; sleep 1; tcpdump -r /tmp/nbm.pcap -A 2>/dev/null | grep -a -o 'OFDM-[0-9.]*-' | sort | uniq -c | tr '\n' ' '; tcpdump -r /tmp/nbm.pcap 2>/dev/null | wc -l; grep -E '^bw' /sys/kernel/debug/mt7921dbg/rxbw 2>/dev/null | awk '\$2 > 0 {printf \"%s=%s \", \$1, \$2}'" </dev/null | tr '\n' ' ')
	echo "cbw $CBW $cand: $r"
done
