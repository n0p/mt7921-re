#!/bin/bash
# ContiTx replay on card B: start the band test-signal generator (notes/07 §2, RAM FUN_968f2e) by hand from the TX
# vector of one injected legacy frame, with the generator's bandwidth code (word 0 [10:8]) and BN_MODE chosen freely.
# Card A captures (continuous 30 Msps, ICAP packer 8, gated dump) and tools/sdr/txscan.py measures the OFDM timing.
# Usage: contitx.sh <MHz> <0x83081000 value> <word-0 BW code 0..7 | keep> [seconds on air 1] [rate 12 = 6 Mbps]
#   env W="idx=value ..." overrides generator words (idx 0..11, after the BW code), e.g. W="0=0x012a1400"
cd /root/fwre
F=$1 MODE=$2 BWC=$3 T=${4:-1} RATE=${5:-12}
./rigreset.sh > /dev/null
PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
w() { python3 fwdbg.py $PB write "$@" > /dev/null; }
r() { python3 fwdbg.py $PB read $1 1 | cut -d' ' -f2; }
rmw() { local v; v=$(r $1); w $1 $(printf "0x%x" $(( (0x$v & $2) | $3 ))); }
stop() {	# FUN_968e82 without its RST_CTRL write and register restore; rigreset reloads everything else
	w 0x8308a004 0; w 0x83081044 0; w 0x830ac004 0x16151400; w 0x830ac054 0; w 0x830ac058 0; w 0x8308b024 0
	w 0x8308b000 0xcc000000; w 0x830ad4d8 0; rmw 0x8300c000 0xffffffff 0x80000000; w 0x83080004 0; w 0x83080008 0
	w 0x8308b004 0; w 0x8300d008 0x1d10
}
M=/sys/module/mt7921dbg/parameters
echo 1 > $M/tx_count
trap 'echo 0 > $M/tx_count' EXIT
python3 chsw.py $PB 11 0 0 > /dev/null; python3 chsw.py $PB 1 0 0 1 1 $F 1 > /dev/null
rmw 0x820e3000 0xfffffeff 0	# MAC TX on
python3 inject.py mon_b 1 100 $RATE 64 > /dev/null; sleep 0.3	# the latch 0x8308a830.. now holds this frame's TX vector
L=($(python3 fwdbg.py $PB read 0x8308a830 12 | cut -d' ' -f2-))
[ ${#L[@]} = 12 ] && [ "${L[0]}" != 00000000 ] || { echo "no TX vector latched: ${L[*]}"; exit 1; }
for i in "${!L[@]}"; do L[$i]=0x${L[$i]}; done
[ "$BWC" = keep ] || L[0]=$(printf "0x%08x" $(( (L[0] & ~0x700) | (BWC << 8) )))
for kv in ${W:-}; do L[${kv%%=*}]=${kv#*=}; done
echo "B: 0x83081000 $MODE, generator words ${L[*]}"
DUMP=/root/fwre/ctx.bin
rm -f $DUMP
timeout 60 python3 nbrxd.py A $F --pack 8 --gain 0,0,0 --rx-only --quiet --slice-ms 12000 --seconds $((T + 4)) --dump $DUMP --dump-gate 3 > /tmp/ctx.log 2>&1 &
for i in $(seq 1 120); do	# start once A streams: the gated dump's 2000-record noise reference is on disk
	[ "$(stat -c %s $DUMP 2>/dev/null || echo 0)" -ge $((2000 * 16336)) ] && break; sleep 0.1
done
sleep 0.2
trap 'stop; echo 0 > $M/tx_count' EXIT
# FUN_968f2e, band 0, antenna mask 1, OFDM (0x8308b024 = 0x10); env NOB020 skips the 0x8308b020 write
w 0x830ac004 0x16011400; w 0x8308b000 0x44000000; w 0x830ac058 1
rmw 0x8300d008 0xffffffff 0xc0; rmw 0x8301d008 0xffffff7f 0
w 0x8308b004 0x30000000
[ -n "$NOB020" ] || w 0x8308b020 0x100000	# FUN_968f2e writes it when the rate has no mode bits [9:6]
w 0x83081000 $MODE
w 0x8308a008 ${L[@]:0:8}; w 0x8308a040 ${L[@]:8:4}
w 0x8308b024 0x10; w 0x83081044 0x10; w 0x83081044 0x30; w 0x830ad4d8 0x101f1f10
rmw 0x8300c000 0x7fffffff 0; rmw 0x8301c000 0x7fffffff 0
w 0x83080004 0x38; w 0x83080008 0x38; w 0x830ac054 0x10
w 0x8308a004 0x80000000
echo "running: 0x8308a004 $(r 0x8308a004), 0x83081000 $(r 0x83081000), gen word0 $(r 0x8308a008)"
sleep $T
stop
trap 'echo 0 > $M/tx_count' EXIT
wait
cd sdr && python3 txscan.py $DUMP 8 350
rm -f $DUMP
