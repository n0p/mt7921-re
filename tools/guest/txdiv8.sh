#!/bin/bash
# ÷8 (2.5 MHz OFDM) transmit test: card B injects frames with BN_MODE 0x83081000 [30:28] = 7, card A receives them
# through its continuous 10 Msps IQ stream and decodes at 2.5 and 5 MHz.
# Usage: txdiv8.sh <MHz> <cbw of B: 4 (code forced to 7, TICKCR0 0x5) | 5 (native: firmware sets code 7, TICKCR0 0xa)>
#   [frames 100] [rate 12]   env GAIN (A's manual gain, 0,0,0)
cd /root/fwre
F=$1 CB=$2 N=${3:-100} R=${4:-12}
./rigreset.sh > /dev/null
PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
python3 chsw.py $PB 11 0 0 > /dev/null; python3 chsw.py $PB 1 0 $CB 1 1 $F > /dev/null
v=$(python3 fwdbg.py $PB read 0x83081000 1 | cut -d' ' -f2)
python3 fwdbg.py $PB write 0x83081000 $(printf "0x%x" $(( (0x$v & 0x80ffffff) | 0x70000000 ))) > /dev/null
a=$(python3 fwdbg.py $PB read 0x820e3000 1 | cut -d' ' -f2)
python3 fwdbg.py $PB write 0x820e3000 $(printf "0x%x" $(( 0x$a & ~0x100 ))) > /dev/null	# MAC TX on
echo "B: cbw $CB, 0x83081000 $(python3 fwdbg.py $PB read 0x83081000 1 | cut -d' ' -f2) (was $v), TICKCR0 $(python3 fwdbg.py $PB read 0x820b00a0 1 | cut -d' ' -f2)"
for W in 2.5 5; do
	timeout 60 python3 nbrxd.py A $F --pack 2 --gain ${GAIN:-0,0,0} --rx-only --quiet --width $W --seconds $(( N / 10 + 8 )) \
		--pcap /tmp/div8_$W.pcap > /tmp/div8_$W.log 2>&1 &
	sleep 4
	python3 inject.py mon_b $N 80 $R 64 > /dev/null
	wait
	echo "A decoding at $W MHz: $(grep '^# total' /tmp/div8_$W.log); MT7921-NBW frames in pcap: $(grep -ao 'MT7921-NBW-[0-9]*' /tmp/div8_$W.pcap | sort -u | wc -l)"
done
./rigreset.sh > /dev/null
