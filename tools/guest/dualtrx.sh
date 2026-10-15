#!/bin/bash
# Card-wedge reproducer: rig reset, optional wait, then both cards run as half-duplex nodes (continuous 10 Msps RX
# slices of 300 ms, each followed by a 2-frame TX burst) for SECS seconds. Reports slices / failures per card and the
# new vendor-request timeouts (from the persistent kernel log /root/dmesg_boot_*.log).
# Usage: dualtrx.sh [wait s after the rig reset, 0] [secs 120]
cd /root/fwre
W=${1:-0} SECS=${2:-120}
L=$(ls -t /root/dmesg_boot_*.log | head -1); T0=$(grep -c "failed:-110" $L)
./rigreset.sh > /dev/null
sleep $W
for c in A B; do
	timeout $((SECS + 60)) python3 nbrxd.py $c 2350 --pack 2 --gain 0,0,0 --slice-ms 300 --tx-test 2 --seconds $SECS \
		> /tmp/dtrx_$c.log 2>&1 &
	sleep 1
done
wait
for c in A B; do
	echo "$c: $(grep -c 'slice done' /tmp/dtrx_$c.log) slices, $(grep -c 'slice failed' /tmp/dtrx_$c.log) failed" \
		"$(grep -m1 -n 'slice failed' /tmp/dtrx_$c.log | cut -d: -f1 | sed 's/^/(first failure at log line /;s/$/)/')"
done
echo "new vendor timeouts: $(( $(grep -c 'failed:-110' $L) - T0 )); first: $(grep 'failed:-110' $L | tail -n +$((T0 + 1)) | head -1 | cut -c1-80)"
