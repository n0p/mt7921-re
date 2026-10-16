#!/bin/bash
# TX rate meter run: card B transmits injected legacy frames (6 Mbps, one transmission each) in a given configuration;
# card A captures them (continuous 30 Msps, ICAP packer 8, native cbw 4) and tools/sdr/txmeter.py reports the OFDM
# timing (STF period, FFT period) and the widths at which the frames decode.
# Usage: txmeter.sh <MHz> <B base cbw[:center ch]> <0x83081000 value | keep> [TICKCR0 0x820b00a0 value | keep] [frames 40]
#   (cbw: chsw.py encoding; a 40 MHz PHY needs the center channel, e.g. 1:3; env BWCR sets 0x820e2004 [3:2])
cd /root/fwre
F=$1 CB=$2 MODE=$3 TICK=${4:-keep} N=${5:-40}
./rigreset.sh > /dev/null
M=/sys/module/mt7921dbg/parameters
echo ${TXC:-0} > $M/tx_count	# env TXC: TXD attempts (1 for /8, whose MAC retries never end)
echo ${TXBW:-0} > $M/tx_bw	# env TXBW: TX vector bandwidth 1..4 = 20/40/80/160 MHz (0 = mt76's)
echo ${TXRATE:-0} > $M/tx_rate	# env TXRATE: DW6 rate code (0x4b OFDM 6 Mbps, 0x80 HT MCS0; 0 = mt76's)
trap 'echo 0 > $M/tx_bw; echo 0 > $M/tx_rate; echo 0 > $M/tx_count' EXIT
PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
r() { python3 fwdbg.py $PB read $1 1 | cut -d' ' -f2; }
CTR=1; case $CB in *:*) CTR=${CB#*:}; CB=${CB%%:*} ;; esac	# "cbw:center channel", e.g. 1:3 for a 40 MHz PHY
python3 chsw.py $PB 11 0 0 > /dev/null; python3 chsw.py $PB 1 0 $CB 1 1 $F $CTR > /dev/null
[ "$MODE" = keep ] || python3 fwdbg.py $PB write 0x83081000 $MODE > /dev/null
[ "$TICK" = keep ] || python3 fwdbg.py $PB write 0x820b00a0 $TICK > /dev/null
if [ -n "$BWCR" ]; then	# env BWCR: the MAC bandwidth class, AGG 0x820e2004 [3:2] (MT7996's RATE BWCR; its 0x820ee008 page does not exist here)
	v=$(r 0x820e2004); python3 fwdbg.py $PB write 0x820e2004 $(printf "0x%x" $(( (0x$v & ~0xc) | (BWCR << 2) ))) > /dev/null
fi
for kv in ${X:-}; do	# env X="reg=value reg|=bits reg&=mask ...": extra writes after the mode
	case $kv in
	*'|='*) v=$(r ${kv%%|=*}); python3 fwdbg.py $PB write ${kv%%|=*} $(printf "0x%x" $(( 0x$v | ${kv#*|=} ))) > /dev/null ;;
	*'&='*) v=$(r ${kv%%&=*}); python3 fwdbg.py $PB write ${kv%%&=*} $(printf "0x%x" $(( 0x$v & ${kv#*&=} ))) > /dev/null ;;
	*) python3 fwdbg.py $PB write ${kv%%=*} ${kv#*=} > /dev/null ;;
	esac
done
a=$(r 0x820e3000); python3 fwdbg.py $PB write 0x820e3000 $(printf "0x%x" $(( 0x$a & ~0x100 ))) > /dev/null	# MAC TX on
echo "B: txbw ${TXBW:-0} txrate ${TXRATE:-0}, cbw $CB, 0x83081000 $(r 0x83081000), 0x83080000 $(r 0x83080000), TICKCR0 $(r 0x820b00a0), AGG 0x820e2004 $(r 0x820e2004), RF 0x184 $(python3 fwdbg.py $PB call 0x0083a662 0x184 0 | cut -d' ' -f4)"
rm -f /tmp/txm.bin
timeout 60 python3 nbrxd.py A $F --pack 8 --gain 0,0,0 --rx-only --quiet --slice-ms 12000 --seconds 10 --dump /tmp/txm.bin > /tmp/txm.log 2>&1 &
for i in $(seq 1 40); do	# inject only once A's stream runs
	[ "$(cut -d' ' -f2 /sys/kernel/debug/mt7921dbg/iqstat0)" -gt 1000 ] 2>/dev/null && break; sleep 0.25
done
python3 inject.py mon_b $N 100 12 64 > /dev/null
wait
cd sdr && python3 txmeter.py /tmp/txm.bin 8
rm -f /tmp/txm.bin
