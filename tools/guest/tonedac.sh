#!/bin/bash
# Is the band tone generator's DC I/Q word (0x8308fc08: I [11:0], Q [27:16], 12-bit two's complement) a live DAC?
# Card TXC (env, default B) starts the firmware's TxTone at DC (FUN_9691d2: chain mask 1, type 0, FREQ 0 -> fc08 =
# I = Q = -1024), then rewrites fc08 every second (without, then with the fc0c [0] latch pulse); the other card dumps
# its continuous 10 Msps stream, unless OBS=none (an X410 capture observes instead: tools/sdr/capture.py on RX1).
# The tone is stopped (FUN_969126) before the final rig reset. tools/sdr/toneana.py reads the phase steps.
# Usage: tonedac.sh [MHz 2350] [out /root/iq/tonedac.bin]   env TXC (A|B), OBS (card|none), DELAY (s before the tone)
cd /root/fwre
F=${1:-2350} OUT=${2:-/root/iq/tonedac.bin}
./rigreset.sh > /dev/null
TXC=${TXC:-B}; RXC=$([ $TXC = A ] && echo B || echo A)
PB=$(cat /sys/class/net/$([ $TXC = A ] && echo wlx00c0cab7962a || echo wlx00c0cab79628)/phy80211/name)
w() { python3 fwdbg.py $PB write $1 $2 > /dev/null; }
r() { python3 fwdbg.py $PB read $1 1 | cut -d' ' -f2; }
python3 chsw.py $PB 11 0 0 > /dev/null; python3 chsw.py $PB 1 0 0 1 1 $F > /dev/null
rm -f $OUT
[ "${OBS:-card}" = none ] || timeout 60 python3 nbrxd.py $RXC $F --pack 2 --gain 0,0,0 --rx-only --quiet --seconds 16 \
	--dump $OUT > /tmp/tonedac.log 2>&1 &
sleep ${DELAY:-4}
python3 fwdbg.py $PB call 0x009691d2 1 0 0 0 > /dev/null
echo "tone on: fc00 $(r 0x8308fc00) fc08 $(r 0x8308fc08) fc0c $(r 0x8308fc0c)"
date +%s.%N > /tmp/tonedac.times
for v in 0x04000400 0x00000400 0x04000000 0x0c000c00; do	# +45, 0, 90 deg, back to -135 deg
	sleep 1; w 0x8308fc08 $v; date +%s.%N >> /tmp/tonedac.times
done
c=$(r 0x8308fc0c)
for v in 0x04000400 0x00000400 0x04000000 0x0c000c00; do	# the same, each followed by a latch pulse
	sleep 1; w 0x8308fc08 $v; w 0x8308fc0c $(printf "0x%x" $(( 0x$c | 1 ))); w 0x8308fc0c 0x$c; date +%s.%N >> /tmp/tonedac.times
done
sleep 1
python3 fwdbg.py $PB call 0x00969126 0 0 > /dev/null
echo "tone off: fc00 $(r 0x8308fc00)"
wait
./rigreset.sh > /dev/null
[ "${OBS:-card}" = none ] || echo "dump $(stat -c %s $OUT) bytes"
echo "write times:"; cat /tmp/tonedac.times
