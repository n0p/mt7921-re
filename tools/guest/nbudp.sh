#!/bin/bash
# One-way IP over the narrow link: card B is a transmit-only node (hardware 5 MHz, TAP nbB in netns nsB,
# 10.99.0.2), card A a receive-only node (firmware IQ stream + software receiver, TAP nbA in nsA, 10.99.0.1).
# nsB sends N UDP datagrams to 10.99.0.1:5555 (static ARP: nothing can answer); nsA counts what arrives.
# Usage: nbudp.sh <MHz> <N> [interval s]   env GAIN (0,0,0: the cards sit 10 cm apart), CBW_RX (0)
cd /root/fwre
F=$1 N=$2 IV=${3:-0.2}
./rigreset.sh > /dev/null
ip netns del nsA 2> /dev/null; ip netns del nsB 2> /dev/null
timeout 120 python3 nbrxd.py A $F --cbw ${CBW_RX:-0} --gain ${GAIN:-0,0,0} --tap nbA --netns nsA --rx-only ${DUMP:+--dump $DUMP} \
	> /tmp/udpA.log 2>&1 &
until ip -n nsA link show nbA > /dev/null 2>&1 && grep -q phy /sys/kernel/debug/mt7921dbg/iqbind; do sleep 0.3; done
timeout 120 python3 nbrxd.py B $F --tx-only --tap nbB --netns nsB > /tmp/udpB.log 2>&1 &
until ip -n nsB link show nbB > /dev/null 2>&1; do sleep 0.3; done
for n in A B; do ip netns exec ns$n sysctl -qw net.ipv6.conf.all.disable_ipv6=1; done
ip -n nsA addr add 10.99.0.1/24 dev nbA; ip -n nsB addr add 10.99.0.2/24 dev nbB
MAC_A=$(ip -n nsA -o link show nbA | grep -o 'link/ether [0-9a-f:]*' | cut -d' ' -f2)
ip -n nsB neigh add 10.99.0.1 lladdr $MAC_A dev nbB
ip netns exec nsA timeout $(python3 -c "print(int($N * $IV + 15))") python3 -c '
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("10.99.0.1", 5555)); got = set()
s.settimeout(1)
import time; end = time.time() + float(sys.argv[1])
while time.time() < end:
    try: d, a = s.recvfrom(2048)
    except socket.timeout: continue
    got.add(d.split(b" ")[1]); print("rx", a[0], d[:40], flush=True)
print(f"received {len(got)} distinct datagrams")' $(python3 -c "print($N * $IV + 12)") > /tmp/udp_rx.txt 2>&1 &
sleep 3
ip netns exec nsB python3 -c '
import socket, sys, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for n in range(int(sys.argv[1])):
    s.sendto(b"NB-UDP %04d over a 5 MHz MT7921 link" % n, ("10.99.0.1", 5555)); time.sleep(float(sys.argv[2]))
print("sent", sys.argv[1])' $N $IV
sleep 10
pkill -INT -f '^python3 nbrxd.py'; sleep 2
tail -3 /tmp/udp_rx.txt; echo "A: $(tail -1 /tmp/udpA.log)"; echo "B: $(tail -1 /tmp/udpB.log)"
