#!/bin/bash
# Back-to-back IQ streams without a rig reset in between: tune card C once (iqstream.sh-style setup), then run
# hk_iqstream K times (DURMS each), draining every run to <out>.<k>.bin. Tests whether the streamer can run
# continuously. Usage: iqloop.sh <A|B> <K> <MHz> <out prefix>   env as iqstream.sh (GAIN, THRESH, NSUM, X, DURMS)
cd /root/fwre
CARD=$1 K=$2 F=$3 PFX=$4
D=/sys/kernel/debug/mt7921dbg
sym() { awk -v s=$1 '$3 == s {print "0x"$1}' /root/fwre/hooks.nm; }
IQ=$(sym hk_iqstream) ST=$(sym iq_stat) CFG=$(sym iq_cfg)
./rigreset.sh > /dev/null
IF=$([ "$CARD" = A ] && echo wlx00c0cab7962a || echo wlx00c0cab79628)
P=$(cat /sys/class/net/$IF/phy80211/name)
SLOT=$([ "$CARD" = A ] && echo 0 || echo 1)	# mt7921dbg IQ slot: card A -> iq0, card B -> iq1
python3 chsw.py $P 11 0 0 > /dev/null; python3 chsw.py $P 1 0 0 1 1 $F > /dev/null
for kv in ${X:-}; do python3 fwdbg.py $P write ${kv%%=*} ${kv#*=} > /dev/null; done
rmw() { local v; v=$(python3 fwdbg.py $P read $1 1 | cut -d' ' -f2); python3 fwdbg.py $P write $1 $(printf "0x%x" $(( (0x$v & ~$2) | ($3 & $2) ))) > /dev/null; }
rfw() { python3 fwdbg.py $P call 0x0083a66c $1 $2 $3 > /dev/null; }
IFS=, read lna lpf dig <<< "${GAIN:-3,4,6}"
for p in 0 1; do rfw 0x768 $(( 0x80000000 | lna << 27 | lpf << 23 )) $p; done
rmw 0x83000108 0x3f3f0000 $(( dig << 24 | dig << 16 ))
rmw 0x83000150 0x8070f000 $(( 0x80000000 | lna << 20 ))
rmw 0x830000c4 0xf7f7f707 $(( 7 | lna << 8 | lpf << 12 | lna << 16 | lpf << 20 | lna << 24 | lpf << 28 ))
ICAP_SETUP_ONLY=1 python3 icap.py $P /dev/null 4 3 > /dev/null
v=$(python3 fwdbg.py $P read 0x820e3000 1 | cut -d' ' -f2)
python3 fwdbg.py $P write 0x820e3000 $(printf "0x%x" $(( (0x$v & ~0x200) | 0x100 ))) > /dev/null
DURMS=${DURMS:-3000}
for k in $(seq 1 $K); do
	t0=$(date +%s.%N)
	python3 fwdbg.py $P write $CFG ${CHUNK:-4080} 1 ${THRESH:-135} ${HANG:-500} ${PRE:-4096} ${NSUM:-128} 0 $DURMS > /dev/null || { echo "run $k: cfg write failed"; break; }
	echo "$SLOT $P" > $D/iqbind; rm -f /tmp/iqdrain.stop
	IQSLOT=$SLOT python3 iqdrain.py $PFX.$k.bin $(( DURMS / 1000 + 30 )) /tmp/iqdrain.stop > /dev/null &
	dpid=$!
	python3 fwdbg.py $P calld $IQ 1000000 0xcc 200 200000 > /dev/null || { echo "run $k: calld failed"; touch /tmp/iqdrain.stop; wait $dpid; break; }
	t1=$(date +%s.%N)
	sleep $(python3 -c "print($DURMS / 1000 + 0.2)")
	until s=$(python3 fwdbg.py $P read $ST 8 2>/dev/null) && [ "$(echo $s | awk '{print $2}')" != 00000001 ]; do sleep 0.05; done
	touch /tmp/iqdrain.stop; wait $dpid
	echo "run $k: call latency $(python3 -c "print(round($t1 - $t0, 3))") s; $(cat $D/iqstat$SLOT | cut -d' ' -f1-8); iq_stat $(echo $s | cut -d' ' -f2-9)"
done
rmw 0x830000c4 0xf7f7f707 0; rmw 0x83000150 0x8070f000 0; for p in 0 1; do rfw 0x768 0 $p; done
./rigreset.sh > /dev/null
