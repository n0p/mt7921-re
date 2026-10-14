#!/bin/sh
# Two-card self test of the narrowband register profile (instrumented 2026 firmware must be running).
# Card A injects 6 Mbps OFDM frames on FREQ at WIDTH; card B counts them in monitor mode.
#   case base : stock 2026 registers on both cards
#   case txonly: profile applied on A only   (narrow TX should NOT be decoded by a stock receiver)
#   case both : profile applied on A and B   (narrow TX should be decoded by a narrow receiver)
# Usage: narrow_test.sh <width 10|5> [freq] [frames]
set -u
W=$1 FREQ=${2:-5180} N=${3:-300}
A=wlx00c0cab7962a B=wlx00c0cab79628
cd /root/fwre
PA=$(cat /sys/class/net/$A/phy80211/name) PB=$(cat /sys/class/net/$B/phy80211/name)

mon() { # phy ifname
	iw dev "$2" del 2>/dev/null
	iw phy "$1" interface add "$2" type monitor flags fcsfail control otherbss && ip link set "$2" up
	iw dev "$2" set freq "$FREQ" "${W}MHz"
	iw dev "$2" set txpower fixed 500 2>/dev/null	# 5 dBm
}

# apply <phy> <tag>: write the profile for $W, saving previous values to saved_<tag>.txt
apply() {
	python3 - "$1" "$W" > "saved_$2.txt" <<'EOF'
import json, subprocess, sys
phy, w = sys.argv[1], sys.argv[2]
prof = json.load(open("narrow_profiles.json"))[w]
for a, v in prof.items():
    old = subprocess.run(["python3", "fwdbg.py", phy, "read", a, "1"], capture_output=True, text=True).stdout.split()[1]
    subprocess.run(["python3", "fwdbg.py", phy, "write", a, v], capture_output=True)
    print(a, "0x" + old)
EOF
	echo "applied $(wc -l < saved_$2.txt) regs on $1"
}

restore() { # phy tag
	while read -r a v; do python3 fwdbg.py "$1" write "$a" "$v" > /dev/null; done < "saved_$2.txt"
}

run() { # label
	timeout 12 tcpdump -i mon_b -nn -e -tt > /tmp/nt_rx.txt 2>/dev/null &
	sleep 1
	python3 inject.py mon_a "$N" 10 12 > /dev/null
	wait
	ok=$(grep 02:00:00:00:00:01 /tmp/nt_rx.txt | grep -vc "bad-fcs")
	bad=$(grep -c "bad-fcs" /tmp/nt_rx.txt)
	echo "RESULT $1 width=$W freq=$FREQ: B good=$ok/$N bad-fcs=$bad total=$(grep -c . /tmp/nt_rx.txt)"
	grep "bad-fcs" /tmp/nt_rx.txt | head -2 | cut -c1-150
}

ip link set "$A" down; ip link set "$B" down
mon "$PA" mon_a
mon "$PB" mon_b
run base
apply "$PA" A
run txonly
apply "$PB" B
run both
restore "$PA" A
restore "$PB" B
run restored
dmesg | tail -3 | cut -c1-120
