#!/bin/bash
# Two-node narrow-band IP link inside the guest: card A and card B each run nbrxd.py as a half-duplex 5 MHz node
# (hardware ÷4 transmitter in TX windows, firmware IQ stream + software receiver in RX slices), with a TAP in its
# own network namespace (nsA 10.99.0.1, nsB 10.99.0.2); then ping across.
# Usage: nbping.sh <MHz> <count> [ping interval s]   env SLICE (ms, 300), GAIN (0,4,6), RATIO (adaptive gate, 1.8),
#   THRESH (fixed gate floor, none)
cd /root/fwre
F=$1 N=$2 IV=${3:-1}
./rigreset.sh > /dev/null
ip netns del nsA 2> /dev/null; ip netns del nsB 2> /dev/null
for c in A B; do
	timeout $(( N * ${IV%.*} + 60 )) python3 nbrxd.py $c $F --cbw 4 --gain ${GAIN:-0,4,6} ${THRESH:+--thresh $THRESH} --ratio ${RATIO:-1.8} \
		--slice-ms ${SLICE:-300} --tap nb$c --netns ns$c > /tmp/node$c.log 2>&1 &
	sleep 4
done
until ip -n nsA link show nbA > /dev/null 2>&1 && ip -n nsB link show nbB > /dev/null 2>&1 &&
	[ $(awk '{print $2}' /sys/kernel/debug/mt7921dbg/iqbind | grep -c phy) -ge 2 ]; do sleep 0.3; done
ip -n nsA addr add 10.99.0.1/24 dev nbA; ip -n nsB addr add 10.99.0.2/24 dev nbB
sleep 2
ip netns exec nsA ping -c $N -i $IV -W 4 10.99.0.2
pkill -INT -f '^python3 nbrxd.py'; sleep 3
for c in A B; do echo "== node $c: $(grep -c 'TX ' /tmp/node$c.log) TX windows, $(grep -c -- '-> tap' /tmp/node$c.log) frames to tap"; tail -1 /tmp/node$c.log; done
