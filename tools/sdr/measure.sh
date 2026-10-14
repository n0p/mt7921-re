#!/bin/sh
# Capture the X410 spectrum while card A (guest) injects frames, for one or more register phases.
# Usage: measure.sh <freq MHz> <width 20|10|5> <phase>...   phase: off | stock | profile
#   off     : no TX
#   stock   : 6 Mbps injection with the stock 2026 registers
#   profile : same, with work/phydump/narrow_profiles.json[<width>] applied (restored afterwards)
# Output: one JSON line per phase (tools/sdr/psd.py) on stdout. Env GAP_MS: frame spacing (default 2 ms).
set -u
cd "$(dirname "$0")/../.."
F=$1 W=$2; shift 2
G=tools/g.sh
H="tools/h.sh"
ND=$([ "$W" = 20 ] && echo NOHT || echo "${W}MHz")

$G "cd /root/fwre; A=wlx00c0cab7962a; P=\$(cat /sys/class/net/\$A/phy80211/name); iw dev mon_a del 2>/dev/null;
    ip link set \$A down; iw phy \$P interface add mon_a type monitor && ip link set mon_a up;
    iw dev mon_a set freq $F $ND; iw dev mon_a set txpower fixed 500" > /dev/null 2>&1

for ph in "$@"; do
	if [ "$ph" = profile ]; then
		$G "cd /root/fwre; P=\$(cat /sys/class/net/wlx00c0cab7962a/phy80211/name); python3 - \$P $W > saved_meas.txt <<'EOF'
import json, subprocess, sys
phy, w = sys.argv[1], sys.argv[2]
for a, v in json.load(open('narrow_profiles.json'))[w].items():
    old = subprocess.run(['python3', 'fwdbg.py', phy, 'read', a, '1'], capture_output=True, text=True).stdout.split()[1]
    subprocess.run(['python3', 'fwdbg.py', phy, 'write', a, v], capture_output=True)
    print(a, '0x' + old)
EOF" > /dev/null 2>&1
	fi
	if [ "$ph" != off ]; then
		$G "cd /root/fwre; setsid python3 inject.py mon_a $((2500 * 2 / ${GAP_MS:-2})) ${GAP_MS:-2} 12 > /dev/null 2>&1 < /dev/null &" > /dev/null 2>&1
		sleep 2
	fi
	$H "cd /tmp/mt7921sdr && python3 -I psd.py $F 0.5 31.25 40 TX/RX0 ${ph}_${W}" 2>/dev/null | grep '^{'
	$G "pkill -f 'inject.py mon_a'" > /dev/null 2>&1
	if [ "$ph" = profile ]; then
		$G "cd /root/fwre; P=\$(cat /sys/class/net/wlx00c0cab7962a/phy80211/name); while read -r a v; do python3 fwdbg.py \$P write \$a \$v > /dev/null; done < saved_meas.txt" > /dev/null 2>&1
	fi
	sleep 1
done
