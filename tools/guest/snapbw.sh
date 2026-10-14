#!/bin/bash
# PHY/MAC snapshots on card B at 5180 MHz for 20 / 40 / 80 MHz (wide bandwidth progression) plus a repeat of 20.
# Output: /root/fwre/bw_<label>.bin (wide_pages.txt order). Stops at the first failed page.
set -u
cd /root/fwre
B=wlx00c0cab79628
P=$(cat /sys/class/net/$B/phy80211/name)
snap() {
	rm -f "bw_$1.bin"
	for pg in $(cat wide_pages.txt); do
		python3 fwdbg.py "$P" dump /tmp/pgb.bin "$pg" > /dev/null 2>&1 || { echo "FAIL $1 $pg"; exit 1; }
		cat /tmp/pgb.bin >> "bw_$1.bin"
	done
	echo "$1 done"
}
set_bw() { iw dev mon_b set freq 2412 NOHT; iw dev mon_b set freq 5180 $1; iw dev mon_b info | grep channel; sleep 1; }
set_bw NOHT; snap b20
set_bw HT40+; snap b40
set_bw "80 5210"; snap b80
set_bw NOHT; snap b20b
iw dev mon_b set freq 2412 NOHT
echo SNAPBW_DONE
