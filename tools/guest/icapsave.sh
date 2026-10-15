#!/bin/bash
# Save ICAP captures for several 0x83081000 values: card B in native cbw C at 5180 (path 0), ICAP test mode,
# N samples at each tap, files /root/fwre/is_<value>_t<tap>.bin. Rig reset before and after.
# Usage: icapsave.sh <cbw> "<taps>" <value>...   env N (4096)
cd /root/fwre
C=$1 TAPS=$2; shift 2
N=${N:-4096}
./rigreset.sh > /dev/null
PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
python3 chsw.py $PB 149 1 0 > /dev/null; python3 chsw.py $PB 36 1 $C 1 1 > /dev/null
python3 ate.py $PB mode 2 > /dev/null; sleep 2
old=$(python3 memdump.py $PB read 0x83081000 4 | awk '{print $5$4$3$2}')
rm -f is_*.bin
for v in "$@"; do
	python3 memdump.py $PB write 0x83081000 $v > /dev/null
	for t in $TAPS; do python3 icap.py $PB /root/fwre/is_${v}_t$t.bin $t 3 1 $N > /dev/null 2>&1 || echo "fail $v $t"; done
	python3 memdump.py $PB write 0x83081000 0x$old > /dev/null
done
python3 ate.py $PB mode 0 > /dev/null
./rigreset.sh > /dev/null
ls is_*.bin | wc -l
