#!/bin/bash
# ATE receive test at 2.4 GHz with PHY counter readout (host register reads; the debug command is not dispatched in
# test mode). Card B: rig reset, ATE test mode, RX started at F MHz with J-mode J, optional host writes W; snapshot
# 0x83081010..0x83081024 and ATE RXOK/RXERR, X410 sends 802.11a/p frames of width X, snapshot again.
# Usage: aterxcnt.sh <F MHz> <J 0|1|2> <X410 width 20|10|5> [W reg=val,...|-]
cd "$(dirname "$0")/../.."
F=$1 J=$2 X=$3 W=${4:--}
snap() { tools/g.sh "cd /root/fwre; PB=\$(cat /sys/class/net/wlx00c0cab79628/phy80211/name);
	for a in 0x83081010 0x83081014 0x83081018 0x8308101c 0x83081020 0x83081024; do python3 memdump.py \$PB read \$a 4 | awk '{print \$5\$4\$3\$2}'; done | tr '\n' ' ';
	python3 aterx.py \$PB stats | tr '\n' ' '" </dev/null; }
tools/g.sh "cd /root/fwre; ./rigreset.sh > /dev/null; PB=\$(cat /sys/class/net/wlx00c0cab79628/phy80211/name);
	python3 ate.py \$PB mode 1 > /dev/null; sleep 2; python3 aterx.py \$PB start $((F * 1000)) $J;
	[ '$W' = - ] || for kv in \$(echo '$W' | tr ',' ' '); do python3 memdump.py \$PB write \${kv%%=*} \${kv#*=} > /dev/null; done;
	python3 memdump.py \$PB read 0x83081000 4" </dev/null
echo "before: $(snap)"
timeout 120 tools/h.sh "cd /tmp/mt7921sdr && python3 txframes.py $F $X 200 5 6 40 > /dev/null 2>&1" </dev/null
echo "after : $(snap)"
tools/g.sh "cd /root/fwre; PB=\$(cat /sys/class/net/wlx00c0cab79628/phy80211/name); python3 aterx.py \$PB stop; python3 ate.py \$PB mode 0 > /dev/null; ./rigreset.sh > /dev/null" </dev/null
