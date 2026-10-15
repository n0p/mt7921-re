#!/bin/bash
# Bandwidth of the tone generator's DC I/Q word as a DAC (fw/src/dac.c hk_dacplay): card TXC (default A) keys the DC
# tone, then plays complex tones into 0x8308fc08 at several rates while an X410 capture (tools/sdr/capture.py, RX1)
# records; tools/sdr/dacana.py reads the lines. Usage: dacbw.sh [MHz 2350]   env TXC, DELAY (s before the tone, 8)
cd /root/fwre
F=${1:-2350}
./rigreset.sh > /dev/null
P=$(cat /sys/class/net/$([ "${TXC:-A}" = A ] && echo wlx00c0cab7962a || echo wlx00c0cab79628)/phy80211/name)
HK=$(awk '$3 == "hk_dacplay" {print "0x"$1}' hooks.nm)
python3 chsw.py $P 11 0 0 > /dev/null; python3 chsw.py $P 1 0 0 1 1 $F > /dev/null
sleep ${DELAY:-8}
play() {	# step period_us nsamp amp label: each tone in its own tone session (a trip of the DC path, seen after a
	# 90-degree-per-sample run, then cannot carry over)
	python3 fwdbg.py $P call 0x009691d2 1 0 0 0 > /dev/null
	sleep 0.3
	echo "$(date +%s.%N) start $5"
	echo "  $5: $(python3 fwdbg.py $P call $HK $1 $2 $3 $4)"
	sleep 0.3
	python3 fwdbg.py $P call 0x00969126 0 0 > /dev/null
	sleep 0.4
}
play 2 2 250000 1024 "+15.6 kHz at 500 ksps (11.25 deg/sample)"
play 4 2 250000 1024 "+31.25 kHz at 500 ksps (22.5 deg)"
play 8 2 250000 1024 "+62.5 kHz at 500 ksps (45 deg)"
play 12 2 250000 1024 "+93.75 kHz at 500 ksps (67.5 deg)"
play 16 4 125000 1024 "+62.5 kHz at 250 ksps (90 deg)"
play 2 2 250000 1024 "+15.6 kHz at 500 ksps again"
play 1 0 700000 1024 "unpaced, step 1 (5.6 deg)"
echo "$(date +%s.%N) done"
./rigreset.sh > /dev/null
