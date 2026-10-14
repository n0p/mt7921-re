#!/bin/sh
# Card B receive matrix against X410 reference frames (802.11a/p OFDM, 6 Mbps, 300 frames, 2437 MHz).
# Each row: B mode (set + verified with nbset.sh), X410 width, X410 send status, B good/bad-fcs/all frames.
# Usage: rxmatrix.sh "<bmode> <x410width>" ...     bmode: 20 | 10 | 5 (native, via nbset.sh)
cd "$(dirname "$0")/../.."
for row in "$@"; do
	set -- $row
	st=$(tools/g.sh "cd /root/fwre; iw dev mon_b set freq 2412 NOHT; ./nbset.sh mon_b 2437 $1; (setsid timeout 15 tcpdump -i mon_b -nn -e -tt > /tmp/x410all.txt 2>/dev/null < /dev/null &)" 2>/dev/null </dev/null | tail -1)
	sleep 2
	tx=$(timeout 120 tools/h.sh "cd /tmp/mt7921sdr && python3 txframes.py 2437 $2 300 5 6 40 2>&1 | grep -c '^sent'" 2>/dev/null </dev/null)
	sleep 2
	res=$(tools/g.sh "echo good=\$(grep 02:00:00:00:00:02 /tmp/x410all.txt | grep -vc bad-fcs) bad=\$(grep 02:00:00:00:00:02 /tmp/x410all.txt | grep -c bad-fcs) all=\$(grep -c . /tmp/x410all.txt)" 2>/dev/null </dev/null)
	echo "B=$1 X410=$2 tx_ok=$tx | $res | ${st#*: }"
done
