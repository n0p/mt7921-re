#!/bin/sh
# Clean rig state: re-download the instrumented firmware on both cards (resets PHY/RF registers), recreate
# monitor interfaces mon_a (card A) / mon_b (card B) with fcsfail, managed interfaces down, 5 dBm TX power.
cd /root/fwre
./fwswap.sh /root/fwre/WIFI_RAM_CODE_MT7961_1.instr.bin > /dev/null 2>&1
lsmod | grep -q mt7921dbg || insmod /root/fwre/mt7921dbg/mt7921dbg.ko
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
echo "rig reset: A=$(cat /sys/class/net/wlx00c0cab7962a/phy80211/name) B=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)"
