#!/bin/bash
# MCU bus speed for a would-be IQ DAC: how fast can the WM MCU store to (and load from) the tone generator's DC word
# 0x8308fc08 and other PHY words, compared with SRAM? fw hk_wrspeed (stores alternating 0 / 0x00010001) and hk_bench
# (loads). The tone is not running, so the DC word changes nothing on air.
# Usage: busspeed.sh [card A|B, default B] [n 200000]
cd /root/fwre
C=${1:-B} N=${2:-200000}
IF=$([ "$C" = A ] && echo wlx00c0cab7962a || echo wlx00c0cab79628)
P=$(cat /sys/class/net/$IF/phy80211/name)
sym() { awk -v s=$1 '$3 == s {print "0x"$1}' /root/fwre/hooks.nm; }
WR=$(sym hk_wrspeed) BE=$(sym hk_bench) SINK=$(sym bench_sink)
[ -n "$WR" ] && [ -n "$BE" ] || { echo "hk_wrspeed / hk_bench not in /root/fwre/hooks.nm"; exit 1; }
us() { python3 fwdbg.py $P call "$@" | sed 's/.*ret //'; }
rate() { python3 -c "import sys; n, t = int(sys.argv[1]), int(sys.argv[2], 0); print(f'{n} in {t} us = {n / max(t, 1):.2f} M/s, {1e3 * t / n:.0f} ns each')" $N $1; }
for a in 0x8308fc08 0x8308a008 0x83081044; do
	[ $a = 0x83081044 ] && continue	# a TX control word: loads only
	echo "store $a: $(rate $(us $WR $a $N 0 0x00010001))"
done
echo "store bench_sink ($SINK, hook SRAM): $(rate $(us $WR $SINK $N 0 1))"
for a in 0x8308fc08 0x83081000; do
	echo "load  $a: $(rate $(us $BE $a $N 0 0))"
done
