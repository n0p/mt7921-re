#!/bin/bash
# Card-to-card narrow link: card B transmits hardware 5 MHz frames (native CHANNEL_SWITCH cbw 4 = the firmware's
# "10 MHz", a ÷4 = 5 MHz PPDU on air; inject.py "MT7921-NBW-n" from 02:00:00:00:00:01) while card A receives them
# through the firmware IQ stream (tools/guest/iqstream.sh, gated, manual gain); the records are decoded at 5 MHz.
# Usage: c2cstream.sh <MHz> <nframes> <interval ms> [out.bin]
# Env: X (card A register writes; default BW code 4 so its own 20 MHz demodulator stays blind: 0x83081000=0x84000000),
#   GAIN (1,4,6: the cards sit side by side), THRESH (130), NSUM (128), DURMS (20000), LEAD (s, 16), BCBW (4),
#   IRATE (inject rate_500k, 12 = 6 Mbps OFDM before the ÷4)
cd "$(dirname "$0")/../.."
F=$1 N=$2 IV=$3 OUT=${4:-work/icap/stream/c2c_${1}.bin}
DURMS=${DURMS:-20000}
HOP=$([ "$F" = 5180 ] && echo "149 1 0" || echo "11 0 0"); CH=$([ "$F" = 5180 ] && echo "36 1" || echo "1 0")
tools/g.sh "cd /root/fwre; X=\"${X-0x83081000=0x84000000}\" GAIN=${GAIN:-1,4,6} THRESH=${THRESH:-130} NSUM=${NSUM:-128} HANG=${HANG:-500} PRE=${PRE:-4096} \
	DURMS=$DURMS DUR=$(( DURMS / 1000 + 1 )) ./iqstream.sh A 1000000 0 $F 4 /tmp/c2c.bin" </dev/null > /tmp/c2c.log 2>&1 &
gpid=$!
sleep ${LEAD:-16}
tools/g.sh "cd /root/fwre; P=\$(cat /sys/class/net/wlx00c0cab79628/phy80211/name); python3 chsw.py \$P $HOP > /dev/null;
	python3 chsw.py \$P $CH ${BCBW:-4} 1 1 $([ "$F" = 5180 ] || echo $F) | tail -1; python3 inject.py mon_b $N $IV ${IRATE:-12} 2>&1 | tail -1" </dev/null
wait $gpid
grep -E "iqstat|iq_stat" /tmp/c2c.log
tools/g.sh 'cat /tmp/c2c.bin' > $OUT
cd tools/sdr && python3 iqdec.py ../../$OUT 5 0 > /tmp/c2c_dec.txt
tail -1 /tmp/c2c_dec.txt
echo "card B frames with good FCS: $(grep 'fcs ok' /tmp/c2c_dec.txt | grep -c 020000000001)"
grep 'fcs ok' /tmp/c2c_dec.txt | grep 020000000001 | head -3
