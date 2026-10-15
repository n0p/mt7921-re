#!/bin/bash
# Stream IQ from a card through the instrumented firmware (fw/src/iqstream.c: ICAP mode-0 ring -> MCU WFDMA TX ring 0
# -> USB bulk IN 0x84) into mt7921dbg's IQ ring, then save the records (u32 "QIQR", seq, absolute 40 Msps sample
# position, payload bytes; payload = raw ICAP words). The capture overwrites the packet SRAM, so the rig is reset
# before and after.
# Usage: iqstream.sh <A|B> <npkt> [cbw] [MHz] [tap] [out.bin]
#   npkt packets of CHUNK samples (at 40 Msps); cbw/MHz for the native CHANNEL_SWITCH (MHz other than 5180 goes
#   through channel 1 with the frequency override, e.g. 2350); tap = ICAP SEL (4 = front end, chain 0)
# Env: CHUNK (samples per packet, multiple of 4, <= 4080; 4080), INFLIGHT (1), THRESH (energy gate on mean |I|+|Q|,
#   0 = stream everything), HANG (us, 500), PRE (pre-trigger samples, 4096), NSUM (samples per check, 32),
#   SETTLE (us, 200), TMO (stall timeout us, 200000), NORESET=1 (skip the final rig reset), DUR (seconds to wait
#   before reading the stats; default from npkt), GAIN=lna,lpf,dig (manual gain, BirriaSDR's recipe: LNA 0-3,
#   filter 0-8, digital 0-12; without it the AGC hunts between gain states on noise and the energy gate is useless),
#   DURMS (stream wall-clock limit, ms; 2000), X="reg=val ..." (extra register writes after tuning, e.g.
#   0x83081000=0x84000000 to keep the card's own 20 MHz demodulator from locking onto narrow frames)
cd /root/fwre
CARD=$1 NPKT=$2 C=${3:-0} F=${4:-5180} TAP=${5:-4} OUT=${6:-/tmp/iq.bin}
D=/sys/kernel/debug/mt7921dbg
sym() { awk -v s=$1 '$3 == s {print "0x"$1}' /root/fwre/hooks.nm; }
IQ=$(sym hk_iqstream) ST=$(sym iq_stat) CFG=$(sym iq_cfg)
[ -n "$IQ" ] && [ -n "$CFG" ] || { echo "hk_iqstream / iq_cfg not in /root/fwre/hooks.nm"; exit 1; }
./rigreset.sh > /dev/null
IF=$([ "$CARD" = A ] && echo wlx00c0cab7962a || echo wlx00c0cab79628)
P=$(cat /sys/class/net/$IF/phy80211/name)
SLOT=$([ "$CARD" = A ] && echo 0 || echo 1)	# mt7921dbg IQ slot: card A -> iq0, card B -> iq1
if [ "$F" = 5180 ]; then
	python3 chsw.py $P 149 1 0 > /dev/null; python3 chsw.py $P 36 1 $C 1 1 > /dev/null
else
	python3 chsw.py $P 11 0 0 > /dev/null; python3 chsw.py $P 1 0 $C 1 1 $F > /dev/null
fi
for kv in ${X:-}; do python3 fwdbg.py $P write ${kv%%=*} ${kv#*=} > /dev/null; done
rmw() { local v; v=$(python3 fwdbg.py $P read $1 1 | cut -d' ' -f2); python3 fwdbg.py $P write $1 $(printf "0x%x" $(( (0x$v & ~$2) | ($3 & $2) ))) > /dev/null; }
rfw() { python3 fwdbg.py $P call 0x0083a66c $1 $2 $3 > /dev/null; }
if [ -n "$GAIN" ]; then	# analog clamp first (RF 0x768 on both paths), then digital gain, the D force, the A/B/C forces
	IFS=, read lna lpf dig <<< "$GAIN"
	for p in 0 1; do rfw 0x768 $(( 0x80000000 | lna << 27 | lpf << 23 )) $p; done
	rmw 0x83000108 0x3f3f0000 $(( dig << 24 | dig << 16 ))
	rmw 0x83000150 0x8070f000 $(( 0x80000000 | lna << 20 ))
	rmw 0x830000c4 0xf7f7f707 $(( 7 | lna << 8 | lpf << 12 | lna << 16 | lpf << 20 | lna << 24 | lpf << 28 ))
fi
ICAP_SETUP_ONLY=1 python3 icap.py $P /dev/null $TAP 3 > /dev/null
v=$(python3 fwdbg.py $P read 0x820e3000 1 | cut -d' ' -f2)
python3 fwdbg.py $P write 0x820e3000 $(printf "0x%x" $(( (0x$v & ~0x200) | 0x100 ))) > /dev/null	# MAC TX off; RX must stay on (it gates the PHY receive)
python3 fwdbg.py $P write $CFG ${CHUNK:-4080} ${INFLIGHT:-1} ${THRESH:-0} ${HANG:-500} ${PRE:-4096} ${NSUM:-32} 0 ${DURMS:-2000} > /dev/null
echo "$SLOT $P" > $D/iqbind
WAIT=${DUR:-$(python3 -c "print($NPKT * ${CHUNK:-4080} / 40e6 + 0.5)")}
rm -f /tmp/iqdrain.stop
IQSLOT=$SLOT python3 iqdrain.py $OUT $(python3 -c "print($WAIT + 30)") /tmp/iqdrain.stop &	# keep the 64 MiB ring from filling
dpid=$!
python3 fwdbg.py $P calld $IQ $NPKT 0xcc ${SETTLE:-200} ${TMO:-200000} > /dev/null
sleep $WAIT
touch /tmp/iqdrain.stop; wait $dpid
echo "iqstat: $(cat $D/iqstat$SLOT)"
python3 fwdbg.py $P read $ST 8 2>&1 | head -2 | tr '\n' ' ' | sed 's/^/iq_stat (state sent maxbacklog t_us skipped overruns busy checks): /'; echo
echo "saved $(stat -c %s $OUT) bytes to $OUT"
if [ -n "$GAIN" ]; then	# release: the analog override can survive a re-probe
	rmw 0x830000c4 0xf7f7f707 0; rmw 0x83000150 0x8070f000 0; for p in 0 1; do rfw 0x768 0 $p; done
fi
[ -n "$NORESET" ] || ./rigreset.sh > /dev/null
