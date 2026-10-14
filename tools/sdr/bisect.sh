#!/bin/sh
# Per-register effect of the narrow profile on card A's emission (X410).
# Card A injects 6 Mbps frames every GAP_MS ms for the whole run; for each profile register, write the 2022
# value alone, capture, restore. Prints: reg value obw20 obw99 peak median-burst-us bursts
# Usage: bisect.sh <freq MHz> <width 10|5>
set -u
cd "$(dirname "$0")/../.."
F=$1 W=$2 GAP=${GAP_MS:-10}
G=tools/g.sh
H="tools/h.sh"
ND=$([ "$W" = 20 ] && echo NOHT || echo "${W}MHz")

$G "cd /root/fwre; A=wlx00c0cab7962a; P=\$(cat /sys/class/net/\$A/phy80211/name); iw dev mon_a del 2>/dev/null;
    ip link set \$A down; iw phy \$P interface add mon_a type monitor && ip link set mon_a up;
    iw dev mon_a set freq $F $ND; iw dev mon_a set txpower fixed 500;
    setsid python3 inject.py mon_a 1000000 $GAP 12 > /dev/null 2>&1 < /dev/null &" > /dev/null 2>&1
sleep 2

summ() { # label
	python3 -c "
import sys, json, statistics
d = json.loads(sys.stdin.readline())
runs, cur = [], 0
for v in d['timeline_db']:
    if v > 10: cur += 1
    elif cur: runs.append(cur); cur = 0
med = round(statistics.median(runs) * 1024 / 31.25) if runs else 0
print(f\"$1 obw20={d['obw20_mhz']:<6} obw99={d['obw99_mhz']:<6} peak={d['peak_db']:<5} burst_us={med:<6} bursts={len(runs)}\")"
}
cap() { $H "cd /tmp/mt7921sdr && python3 -I psd.py $F 0.5 31.25 40 TX/RX0 x" 2>/dev/null < /dev/null | grep '^{'; }

cap | summ "stock"
python3 -c "import json; [print(a, v) for a, v in json.load(open('work/phydump/narrow_profiles.json'))['$W'].items()]" |
while read -r a v; do
	old=$($G "cd /root/fwre; P=\$(cat /sys/class/net/wlx00c0cab7962a/phy80211/name); python3 fwdbg.py \$P read $a 1 | cut -d' ' -f2;
	          python3 fwdbg.py \$P write $a $v > /dev/null" 2>/dev/null < /dev/null)
	cap | summ "$a=$v(was 0x$old)"
	$G "cd /root/fwre; P=\$(cat /sys/class/net/wlx00c0cab7962a/phy80211/name); python3 fwdbg.py \$P write $a 0x$old > /dev/null" > /dev/null 2>&1 < /dev/null
done
cap | summ "stock-end"
$G "pkill -f 'inject.py mon_a'" > /dev/null 2>&1
