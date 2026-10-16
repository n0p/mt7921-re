#!/bin/sh
# Two-node nbmesh test on one host: card A in network namespace nsa (10.42.0.1), card B in nsb (10.42.0.2), both
# running tools/nbnode nbmesh, then pings from A to B through the narrow-band link.
# Usage: nbmeshtest.sh [MHz 2350] [pings 40] [extra nbmesh options, e.g. "--width 2.5 --fec off --mode tap"]
# Env: PSIZE (ping payload bytes, default 56). The cards sit 10 cm apart on this rig: pass "--gain 0,0,0".
# Logs: /tmp/nbm_a.log, /tmp/nbm_b.log. Both cards transmit at MHz: the default 2350 is this rig's test frequency,
# inside the operator's amateur licence (nbmesh itself defaults to 2450 MHz, 2.4 GHz ISM).
cd "$(dirname "$0")"
MHZ=${1:-2350}
N=${2:-40}
EXTRA=${3:-}
A=wlx00c0cab7962a
B=wlx00c0cab79628
./nbmesh $A --freq "$MHZ" --dev nba --netns nsa --addr 10.42.0.1/24 --slot 0 --monitor nbmon0 $EXTRA \
    >/tmp/nbm_a.out 2>/tmp/nbm_a.log &
PA=$!
./nbmesh $B --freq "$MHZ" --dev nbb --netns nsb --addr 10.42.0.2/24 --slot 1 --monitor nbmon1 $EXTRA \
    >/tmp/nbm_b.out 2>/tmp/nbm_b.log &
PB=$!
sleep 8  # card setup (two channel switches, capture and gain registers) and the first slices
ip netns exec nsa ping -c "$N" -i 0.5 -W 3 -s "${PSIZE:-56}" 10.42.0.2 | tail -3
kill -INT $PA $PB
wait $PA $PB
echo "--- A"; tail -3 /tmp/nbm_a.log
echo "--- B"; tail -3 /tmp/nbm_b.log
