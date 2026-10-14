#!/bin/bash
# Wide register snapshots on card A (instrumented 2026 fw, mt7921dbg loaded) at 2437 MHz:
#   w20 (normal), w10 + w10b (native 10 MHz via nbset.sh), w5 (native 5 MHz).
# Pages from wide_pages.txt; each page is dumped separately and progress is logged, so a page that faults the
# MCU can be identified and excluded (the run stops at the first failure). Output: /root/fwre/wide_<label>.bin
set -u
cd /root/fwre
A=wlx00c0cab7962a
P=$(cat /sys/class/net/$A/phy80211/name)

snap() { # label
	rm -f "wide_$1.bin"
	for pg in $(cat wide_pages.txt); do
		echo "$1 $pg" > snapwide.progress
		if python3 fwdbg.py "$P" dump /tmp/pg.bin "$pg" > /dev/null 2>&1; then
			cat /tmp/pg.bin >> "wide_$1.bin"
		else
			echo "FAIL $1 $pg" >> snapwide.fail
			echo "read of $pg failed; stopping (an MCU fault triggers a driver reset)"
			exit 1
		fi
	done
	echo "$1 done $(stat -c %s wide_$1.bin)"
}

rm -f snapwide.fail
iw dev mon_a set freq 2412 NOHT; iw dev mon_a set freq 2437 NOHT; sleep 1; snap w20
iw dev mon_a set freq 2412 NOHT; ./nbset.sh mon_a 2437 10; sleep 1; snap w10
iw dev mon_a set freq 2412 NOHT; ./nbset.sh mon_a 2437 10; sleep 1; snap w10b
iw dev mon_a set freq 2412 NOHT; ./nbset.sh mon_a 2437 5; sleep 1; snap w5
iw dev mon_a set freq 2412 NOHT
echo SNAPWIDE_DONE
