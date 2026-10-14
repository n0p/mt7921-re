#!/bin/sh
# For each RX BW code c (0x83081000 bits 26:24) on card B (cbw 0 base, path 0), play X410 OFDM frames at several
# sample rates r (Msps; 20 = exact 802.11a via resampling) and count B's receptions. Rig reset per code.
# Usage: codesweep.sh "<code values 0x9c000000...>" "<rates>"
cd "$(dirname "$0")/../.."
for v in $1; do
	tools/g.sh "cd /root/fwre; ./rigreset.sh > /dev/null; P=\$(cat /sys/class/net/wlx00c0cab79628/phy80211/name);
	    python3 chsw.py \$P 149 1 0 > /dev/null; python3 chsw.py \$P 36 1 0 1 1 > /dev/null; python3 fwdbg.py \$P write 0x83081000 $v > /dev/null" </dev/null
	line="$v:"
	for r in $2; do
		tools/g.sh "(setsid timeout 8 tcpdump -i mon_b -nn -e > /tmp/cs.txt 2>/dev/null < /dev/null &)" </dev/null
		sleep 1.5
		timeout 60 tools/h.sh "cd /tmp/mt7921sdr && python3 txframes.py 5180 $r 100 3 6 40 > /dev/null 2>&1" </dev/null
		sleep 1.5
		n=$(tools/g.sh "grep 02:00:00:00:00:02 /tmp/cs.txt | grep -vc bad-fcs" </dev/null)
		line="$line r$r=$n"
	done
	echo "$line"
done
tools/g.sh "/root/fwre/rigreset.sh > /dev/null" </dev/null
