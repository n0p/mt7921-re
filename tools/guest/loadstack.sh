#!/bin/sh
# (Re)load the user's working mt76 stack from /root/stackok in dependency order. /lib/modules holds mixed builds
# that fail with "disagrees about version of symbol", so modprobe cannot be used.
# Usage: loadstack.sh [stack dir]
D=${1:-/root/stackok}
for m in mt7921dbg mt7921u mt7921_common mt792x_usb mt792x_lib mt76_connac_lib mt76_usb mt76; do
	rmmod "$m" 2>/dev/null
done
for m in mt76 mt76-usb mt76-connac-lib mt792x-lib mt792x-usb mt7921-common mt7921u; do
	insmod "$D/$m.ko" || { echo "insmod $m failed"; exit 1; }
done
sleep 8
iw dev | grep -E "phy#|Interface"
