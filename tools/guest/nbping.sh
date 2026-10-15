#!/bin/bash
# Two single-card narrow-band nodes inside the guest: card A and card B each run nbrxd.py as a half-duplex 5 MHz node
# (native cbw 4: hardware ÷4 transmitter in TX windows; firmware IQ stream + software receiver in RX slices, ended
# early when frames are queued), each with a TAP in its own network namespace (nsA 10.99.0.1, nsB 10.99.0.2);
# then ping across. Needs the instrumented firmware with the ICAP pool layout (fw/asm/icap_pool_layout.S).
# Usage: nbping.sh <MHz> <count> [ping interval s] [ping size]
# Env: SLICE (max RX slice ms, 8000), GAIN (0,0,0: the cards sit 10 cm apart), PACK (ICAP packer: 2 = continuous
#      ungated 10 Msps capture), RATIO (adaptive gate, 1.8; 0 with PACK),
#      THRESH (fixed gate floor, none), MINRX (shortest RX slice before TX may end it, ms, 30), DUMP=1 (save each
#      node's IQ records to /tmp/node<A|B>.bin)
cd /root/fwre
F=$1 N=$2 IV=${3:-1} SZ=${4:-56}
./rigreset.sh > /dev/null
ip netns del nsA 2> /dev/null; ip netns del nsB 2> /dev/null
for c in A B; do
	timeout $(( N * ${IV%.*} + 90 )) python3 nbrxd.py $c $F --cbw 4 --gain ${GAIN:-0,0,0} ${THRESH:+--thresh $THRESH} --tx-mode ${TXMODE:-window} \
		${PACK:+--pack $PACK} --ratio ${RATIO:-$([ -n "$PACK" ] && echo 0 || echo 1.8)} --slice-ms ${SLICE:-8000} --min-rx-ms ${MINRX:-30} --tap nb$c --netns ns$c \
		${DUMP:+--dump /tmp/node$c.bin} ${NBARGS:-} > /tmp/node$c.log 2>&1 &
	until ip -n ns$c link show nb$c > /dev/null 2>&1; do sleep 0.3; done
done
until [ $(awk '{print $2}' /sys/kernel/debug/mt7921dbg/iqbind | grep -c phy) -ge 2 ]; do sleep 0.3; done
for c in A B; do ip netns exec ns$c sysctl -qw net.ipv6.conf.all.disable_ipv6=1; done
ip -n nsA addr add 10.99.0.1/24 dev nbA; ip -n nsB addr add 10.99.0.2/24 dev nbB
mac() { ip -n ns$1 -o link show nb$1 | grep -o 'link/ether [0-9a-f:]*' | cut -d' ' -f2; }
ip -n nsA neigh add 10.99.0.2 lladdr $(mac B) dev nbA; ip -n nsB neigh add 10.99.0.1 lladdr $(mac A) dev nbB	# no ARP
sleep 2
ip netns exec nsA ping -c $N -i $IV -s $SZ -W 5 10.99.0.2
pkill -INT -f '^python3 nbrxd.py'; sleep 4
for c in A B; do
	echo "== node $c: $(grep -c ' TX ' /tmp/node$c.log) TX windows, $(grep -c -- '-> tap' /tmp/node$c.log) frames to tap," \
		"$(grep -c 'slice failed' /tmp/node$c.log) failed slices"
	grep '^# total' /tmp/node$c.log
done
