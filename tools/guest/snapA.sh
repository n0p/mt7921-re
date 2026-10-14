#!/bin/bash
# Wide snapshots on card A at 5180 MHz via direct CHANNEL_SWITCH: a20 (cbw 0), a10 + a10b (cbw 4). Path 0 only.
set -u
cd /root/fwre
P=$(cat /sys/class/net/wlx00c0cab7962a/phy80211/name)
pkill -f '^python3 inject'
snap() {
	rm -f "A_$1.bin"
	for pg in $(cat wide_pages.txt); do
		python3 fwdbg.py "$P" dump /tmp/pga.bin "$pg" > /dev/null 2>&1 || { echo "FAIL $1 $pg"; exit 1; }
		cat /tmp/pga.bin >> "A_$1.bin"
	done
	echo "$1 done"
}
python3 chsw.py $P 149 1 0 > /dev/null; python3 chsw.py $P 36 1 0 1 1; sleep 1; snap a20
python3 chsw.py $P 149 1 0 > /dev/null; python3 chsw.py $P 36 1 4 1 1; sleep 1; snap a10
python3 chsw.py $P 149 1 0 > /dev/null; python3 chsw.py $P 36 1 4 1 1; sleep 1; snap a10b
echo SNAPA_DONE
