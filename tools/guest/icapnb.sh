#!/bin/bash
# Card-to-card narrowband through ICAP: card A transmits native narrow frames (CHANNEL_SWITCH cbw CA, path 0,
# 6 Mbps, short payload, back to back unless env GAP=<ms>), card B (cbw CB, path 0) sits in ICAP test mode and captures N rings at
# tap T (40 Msps). Output: /root/fwre/nbcap_<i>.bin (int16 I,Q), decoded on the host with tools/sdr/rx80211.py.
# Usage: icapnb.sh [CA=4] [CB=4] [T=12] [N=3] [freq: ch or outband MHz, default ch 36]
cd /root/fwre
CA=${1:-4} CB=${2:-4} T=${3:-12} N=${4:-3}
./rigreset.sh > /dev/null
PA=$(cat /sys/class/net/wlx00c0cab7962a/phy80211/name); PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
for x in "$PA $CA" "$PB $CB"; do set -- $x; python3 chsw.py $1 149 1 0 > /dev/null; python3 chsw.py $1 36 1 $2 1 1 | cut -c1-90; done
python3 ate.py $PB mode 2 > /dev/null; sleep 2
setsid python3 inject.py mon_a 100000 ${GAP:-0} 12 0 > /dev/null 2>&1 < /dev/null &
sleep 1
rm -f nbcap_*.bin
for i in $(seq $N); do python3 icap.py $PB /root/fwre/nbcap_$i.bin $T 3 2 65536; done
pkill -f '^python3 inject'
python3 ate.py $PB mode 0 > /dev/null
./rigreset.sh > /dev/null	# ICAP mode leaves the packet SRAM unusable for the normal datapath
