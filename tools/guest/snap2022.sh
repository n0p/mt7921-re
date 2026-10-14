#!/bin/sh
# Register snapshots of the PHY/RF pages under the 2022 firmware (host reads via debugfs regval; no TX).
# Loads fw2022, takes NOHT / 10MHz / 10MHz(repeat) / 5MHz snapshots on 5180 MHz monitor, then reinstalls
# the instrumented 2026 image. Output: /root/fwre/phy22_*.bin (pages from phy_pages.txt, concatenated).
set -u
cd /root/fwre
./fwswap.sh /root/fw2022/WIFI_RAM_CODE_MT7961_1.bin
IF=${IF:-wlx00c0cab7962a}	# card A
P=$(cat /sys/class/net/$IF/phy80211/name)
ip link set "$IF" down
iw phy "$P" interface add mon0 type monitor && ip link set mon0 up

snap() { # width out
	iw dev mon0 set freq 5180 "$1"
	iw dev mon0 info | grep channel
	sleep 1
	rm -f "$2"
	for pg in $(cat phy_pages.txt); do
		python3 memdump.py "$P" read "$pg" 4096 /tmp/pg.bin 2>/dev/null && cat /tmp/pg.bin >> "$2"
	done
	ls -la "$2"
}
snap NOHT phy22_20.bin
snap 10MHz phy22_10.bin
iw dev mon0 set freq 5180 NOHT
snap 10MHz phy22_10b.bin
snap 5MHz phy22_5.bin
./fwswap.sh /root/fwre/WIFI_RAM_CODE_MT7961_1.instr.bin
echo SNAP2022_DONE
