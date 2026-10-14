#!/bin/sh
# SDR measurement helper: card A transmits 6 Mbps OFDM frames continuously on FREQ, alternating phases:
#   OFF (silence) -> STOCK (2026 registers) -> PROFILE (narrow register profile) -> OFF
# Each phase lasts PHASE seconds and is announced on stdout with a timestamp.
# Usage: narrow_tx.sh <width 10|5> [freq] [phase seconds] [txpower mBm]
set -u
W=$1 FREQ=${2:-5180} PHASE=${3:-15} PWR=${4:-500}
A=wlx00c0cab7962a
cd /root/fwre
PA=$(cat /sys/class/net/$A/phy80211/name)

iw dev mon_a del 2>/dev/null
ip link set "$A" down
iw phy "$PA" interface add mon_a type monitor && ip link set mon_a up
iw dev mon_a set freq "$FREQ" "${W}MHz"
iw dev mon_a set txpower fixed "$PWR" 2>/dev/null
n=$((PHASE * 500))	# frames at 2 ms spacing

phase() { echo "$(date +%T) PHASE $1 ($FREQ MHz, ${W}MHz declared, ${PHASE}s)"; }

phase OFF; sleep "$PHASE"
phase STOCK; python3 inject.py mon_a "$n" 2 12 > /dev/null
python3 - "$PA" "$W" > saved_tx.txt <<'EOF'
import json, subprocess, sys
phy, w = sys.argv[1], sys.argv[2]
for a, v in json.load(open("narrow_profiles.json"))[w].items():
    old = subprocess.run(["python3", "fwdbg.py", phy, "read", a, "1"], capture_output=True, text=True).stdout.split()[1]
    subprocess.run(["python3", "fwdbg.py", phy, "write", a, v], capture_output=True)
    print(a, "0x" + old)
EOF
phase PROFILE; python3 inject.py mon_a "$n" 2 12 > /dev/null
while read -r a v; do python3 fwdbg.py "$PA" write "$a" "$v" > /dev/null; done < saved_tx.txt
phase OFF; sleep "$PHASE"
echo "$(date +%T) DONE (registers restored)"
