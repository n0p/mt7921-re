#!/bin/bash
# Which register halves the RX front-end rate? Card B at 20 MHz (cbw 0) in ICAP mode; apply one candidate write,
# capture at tap 4, report the fraction of samples equal to their predecessor (0.5 => /2, 0.75 => /4).
# Usage: rxdivscan.sh <reg=val>...
cd /root/fwre
./rigreset.sh > /dev/null
P=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
python3 chsw.py $P 149 1 0 > /dev/null; python3 chsw.py $P 36 1 0 1 1 > /dev/null
python3 ate.py $P mode 2 > /dev/null; sleep 2
for kv in "$@"; do
	a=${kv%%=*}; v=${kv#*=}
	old=$(python3 memdump.py $P read $a 4 | awk '{print $5$4$3$2}')
	python3 memdump.py $P write $a $v > /dev/null
	python3 icap.py $P /tmp/rd.bin 4 3 1 1024 > /dev/null
	r=$(python3 -c "
import numpy as np
x = np.fromfile('/tmp/rd.bin', dtype=np.int16).astype(np.int32); iq = x[0::2] + 1j * x[1::2]
print(f'{np.mean(iq[1:] == iq[:-1]):.2f}')")
	python3 memdump.py $P write $a 0x$old > /dev/null
	echo "$kv (was 0x$old): same-fraction $r"
done
python3 ate.py $P mode 0 > /dev/null
