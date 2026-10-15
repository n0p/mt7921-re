#!/bin/bash
# End-to-end narrow RX test through the firmware IQ stream: card A streams (tools/guest/iqstream.sh: gated, manual
# gain) while the X410 sends 802.11a/p frames; the records are fetched and decoded with iqdec.py at the frame width.
# Usage: streamtest.sh <MHz> <width 5|10|20> <nframes> <gap ms> <X410 gain> [out.bin]
# Env: GAIN (3,4,6), THRESH (130), NSUM (128), HANG (500), PRE (4096), DURMS (20000), LEAD (s between starting the
#   stream and the X410; the stream setup incl. rig reset takes ~14 s), CARD (A)
cd "$(dirname "$0")/../.."
F=$1 W=$2 N=$3 GAP=$4 XG=$5 OUT=${6:-work/icap/stream/st_${1}_${2}.bin}
DURMS=${DURMS:-20000}
tools/g.sh "cd /root/fwre; X=\"${X:-}\" GAIN=${GAIN:-3,4,6} THRESH=${THRESH:-130} NSUM=${NSUM:-128} HANG=${HANG:-500} PRE=${PRE:-4096} \
	DURMS=$DURMS DUR=$(( DURMS / 1000 + 1 )) ./iqstream.sh ${CARD:-A} 1000000 0 $F 4 /tmp/st.bin" </dev/null > /tmp/st.log 2>&1 &
gpid=$!
sleep ${LEAD:-17}
tools/h.sh "cd /tmp/mt7921sdr && python3 txframes.py $F $W $N $GAP 6 $XG 2>&1 | tail -1" </dev/null
wait $gpid
grep -E "iqstat|iq_stat" /tmp/st.log
tools/g.sh 'cat /tmp/st.bin' > $OUT
cd tools/sdr && python3 iqdec.py ../../$OUT $W 0 > /tmp/st_dec.txt
tail -1 /tmp/st_dec.txt
echo "distinct X410 payloads with good FCS: $(grep 'fcs ok' /tmp/st_dec.txt | grep -c X410)"
