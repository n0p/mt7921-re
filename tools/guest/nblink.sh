#!/bin/bash
# Two single-card narrow-band nodes (as nbping.sh), then numbered UDP datagrams in both directions at RATE per second
# for SECS seconds (Poisson send times); each receiver logs arrival time and sequence, so loss per direction and its pattern over time are
# visible. Usage: nblink.sh <MHz> [secs 30] [rate 5]   env as nbping.sh (GAIN, RATIO, SLICE, TXMODE, DUMP)
cd /root/fwre
F=$1 SECS=${2:-30} RATE=${3:-5}
./rigreset.sh > /dev/null
ip netns del nsA 2> /dev/null; ip netns del nsB 2> /dev/null
for c in A B; do
	timeout $(( SECS + 90 )) python3 nbrxd.py $c $F --cbw 4 --gain ${GAIN:-0,0,0} ${THRESH:+--thresh $THRESH} \
		--tx-mode ${TXMODE:-window} ${PACK:+--pack $PACK} --ratio ${RATIO:-$([ -n "$PACK" ] && echo 0 || echo 1.8)} --slice-ms ${SLICE:-8000} --tap nb$c --netns ns$c \
		${DUMP:+--dump /tmp/node$c.bin} ${NBARGS:-} > /tmp/node$c.log 2>&1 &
	until ip -n ns$c link show nb$c > /dev/null 2>&1; do sleep 0.3; done
done
until [ $(awk '{print $2}' /sys/kernel/debug/mt7921dbg/iqbind | grep -c phy) -ge 2 ]; do sleep 0.3; done
for c in A B; do ip netns exec ns$c sysctl -qw net.ipv6.conf.all.disable_ipv6=1; done
ip -n nsA addr add 10.99.0.1/24 dev nbA; ip -n nsB addr add 10.99.0.2/24 dev nbB
mac() { ip -n ns$1 -o link show nb$1 | grep -o 'link/ether [0-9a-f:]*' | cut -d' ' -f2; }
ip -n nsA neigh add 10.99.0.2 lladdr $(mac B) dev nbA; ip -n nsB neigh add 10.99.0.1 lladdr $(mac A) dev nbB
sleep 2
for pair in "A 10.99.0.1 10.99.0.2" "B 10.99.0.2 10.99.0.1"; do
	set -- $pair
	ip netns exec ns$1 python3 -c '
import random, socket, sys, time
me, peer, secs, rate = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); rx.bind((me, 6000)); rx.setblocking(False)
tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
t0, n, got = time.time(), 0, []
t_next = random.expovariate(rate)  # Poisson sends: two senders on a fixed cadence would transmit in lockstep
while time.time() - t0 < secs + 3:
    if time.time() - t0 < secs and time.time() - t0 >= t_next:
        tx.sendto(b"%06d %.3f" % (n, time.time()), (peer, 6000)); n += 1; t_next += random.expovariate(rate)
    try:
        d = rx.recv(64); got.append((time.time() - t0, int(d.split()[0]), time.time() - float(d.split()[1])))
    except BlockingIOError:
        time.sleep(0.005)
print(f"{me}: sent {n}; received {len(got)} from {peer}")
print("  arrivals (t s, seq, one-way-ish latency s):", [(round(a, 1), b, round(c, 2)) for a, b, c in got])
' $2 $3 $SECS $RATE > /tmp/link$1.txt &
done
wait %3 %4 2>/dev/null; sleep $(( SECS + 6 ))
pkill -INT -f '^python3 nbrxd.py'; sleep 3
cat /tmp/linkA.txt /tmp/linkB.txt
