#!/bin/sh
# Install a WIFI_RAM_CODE image and re-probe all mt7921u interfaces so the firmware is downloaded again.
# Usage: fwswap.sh <image.bin>     install image and reprobe
#        fwswap.sh --restore       reinstall the stock 2026 image (md5 e19555f0...) and reprobe
set -eu
FW=/lib/firmware/mediatek/WIFI_RAM_CODE_MT7961_1.bin
STOCK=/lib/firmware/mediatek/WIFI_RAM_CODE_MT7961_1.bin.2026
STOCK_MD5=e19555f0c440cbbc53fb072db5c8eabd

[ "$(md5sum < "$STOCK" | cut -d' ' -f1)" = "$STOCK_MD5" ] || { echo "stock backup $STOCK missing or altered"; exit 1; }
if [ "$1" = --restore ]; then
	cp "$STOCK" "$FW"
else
	cp "$1" "$FW"
fi
echo "installed $(md5sum < "$FW" | cut -d' ' -f1)"

drv=/sys/bus/usb/drivers/mt7921u
for i in $(ls "$drv" | grep -E '^[0-9]+-[0-9.]+:1\.3$'); do
	echo "$i" > "$drv/unbind"
	sleep 1
	echo "$i" > "$drv/bind"
done
sleep 8
dmesg | tail -12 | grep -E "mt7921u|Firmware|firmware|semaphore|Timeout|failed" || true
iw dev | grep -E "phy|Interface" || echo "no wiphy"
