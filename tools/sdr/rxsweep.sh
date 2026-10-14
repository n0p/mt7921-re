#!/bin/sh
# Narrow-RX register sweep: card B in native WIDTH mode (1x1, path 0), X410 sends 802.11p frames at WIDTH,
# for each candidate set of "reg=val[,reg=val...]" B's good/bad-FCS receptions from the X410 SA are counted.
# Registers are restored after each candidate. Candidate "-" = no extra writes (baseline).
# Usage: rxsweep.sh <width 10|5> <candidate>...
cd "$(dirname "$0")/../.."
W=$1; shift
G=tools/g.sh
$G "cd /root/fwre; P=\$(cat /sys/class/net/wlx00c0cab79628/phy80211/name); ip link set mon_b down; iw phy \$P set antenna 1 1;
    iw dev mon_b del; iw phy \$P interface add mon_b type monitor flags fcsfail control otherbss; ip link set mon_b up;
    iw dev mon_b set freq 2412 NOHT; ./nbset.sh mon_b 2437 $W" 2>/dev/null </dev/null | tail -1
for cand in "$@"; do
	writes=$(echo "$cand" | tr ',' ' ')
	[ "$cand" = - ] && writes=""
	$G "cd /root/fwre; P=\$(cat /sys/class/net/wlx00c0cab79628/phy80211/name); rm -f saved_sweep.txt;
	    for kv in $writes; do a=\${kv%%=*}; v=\${kv#*=}; echo \"\$a 0x\$(python3 fwdbg.py \$P read \$a 1 | cut -d' ' -f2)\" >> saved_sweep.txt;
	    python3 fwdbg.py \$P write \$a \$v > /dev/null; done;
	    (setsid timeout 12 tcpdump -i mon_b -nn -e > /tmp/sweep.txt 2>/dev/null < /dev/null &)" >/dev/null 2>&1 </dev/null
	sleep 2
	timeout 120 tools/h.sh "cd /tmp/mt7921sdr && python3 txframes.py 2437 $W 200 5 6 40 > /dev/null 2>&1" </dev/null
	sleep 2
	res=$($G "P=\$(cat /sys/class/net/wlx00c0cab79628/phy80211/name); echo good=\$(grep 02:00:00:00:00:02 /tmp/sweep.txt | grep -vc bad-fcs) bad=\$(grep 02:00:00:00:00:02 /tmp/sweep.txt | grep -c bad-fcs) all=\$(grep -c . /tmp/sweep.txt);
	    cd /root/fwre; [ -f saved_sweep.txt ] && while read -r a v; do python3 fwdbg.py \$P write \$a \$v > /dev/null; done < saved_sweep.txt" 2>/dev/null </dev/null)
	echo "$cand | $res"
done
