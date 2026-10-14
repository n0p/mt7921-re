#!/bin/bash
# Card B as an SDR for card A's transmissions (no X410 needed): A in native cbw CA plus register writes WA (applied
# after the switch, through the firmware debug command), injecting 6 Mbps frames back to back; B in cbw CB (20 MHz
# by default: front end at 40 Msps, wide enough for 20/10/5 MHz signals) in ICAP test mode, N rings at tap T.
# Output /root/fwre/txcap_<i>.bin (int16 I,Q at 40 Msps); decode on the host: tools/sdr/decmulti.py at 20,10,5.
# Usage: txicap.sh <CA> <WA reg=val,...|-> [CB=0] [T=4] [N=4]
cd /root/fwre
CA=$1 WA=$2 CB=${3:-0} T=${4:-4} N=${5:-4}
./rigreset.sh > /dev/null
PA=$(cat /sys/class/net/wlx00c0cab7962a/phy80211/name); PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
python3 chsw.py $PA 149 1 0 > /dev/null; python3 chsw.py $PA 36 1 $CA 1 1 | cut -c1-80
[ "$WA" = - ] || for kv in ${WA//,/ }; do python3 fwdbg.py $PA write ${kv%%=*} ${kv#*=} > /dev/null; done
python3 chsw.py $PB 149 1 0 > /dev/null; python3 chsw.py $PB 36 1 $CB 1 1 > /dev/null
python3 ate.py $PB mode 2 > /dev/null; sleep 2
setsid python3 inject.py mon_a 100000 0 12 0 > /dev/null 2>&1 < /dev/null &
sleep 1
rm -f txcap_*.bin
for i in $(seq $N); do python3 icap.py $PB /root/fwre/txcap_$i.bin $T 3 2 65536 | cut -c1-60; done
pkill -f '^python3 inject'
python3 ate.py $PB mode 0 > /dev/null
./rigreset.sh > /dev/null
