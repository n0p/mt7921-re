#!/bin/sh
# Load the stock 2022 RAM firmware on both cards and recreate the monitor interfaces (like rigreset.sh).
# Usage: rig2022.sh            (undo with ./rigreset.sh, which reinstalls the instrumented 2026 image)
cd /root/fwre
./fwswap.sh /root/fw2022/WIFI_RAM_CODE_MT7961_1.bin > /dev/null 2>&1
for x in "wlx00c0cab7962a mon_a" "wlx00c0cab79628 mon_b"; do
	set -- $x
	P=$(cat /sys/class/net/$1/phy80211/name)
	ip link set "$1" down
	iw dev "$2" del 2>/dev/null
	iw phy "$P" interface add "$2" type monitor flags fcsfail control otherbss
	ip link set "$2" up
	iw dev "$2" set txpower fixed 500 2>/dev/null
	iw dev "$2" set freq 5180 NOHT
done
dmesg | grep -i "WM Firmware Version" | tail -2
