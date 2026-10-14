#!/bin/sh
# Apply register values on card A (via fwdbg), capture the emission with the X410, restore.
# Usage: regtry.sh <freq MHz> <width 20|10|5> <tx on|off> [reg=val ...]
# Prints: label obw20 obw99 peak median-burst-us bursts duty%
set -u
cd "$(dirname "$0")/../.."
F=$1 W=$2 TX=$3; shift 3
G=tools/g.sh
H="tools/h.sh"
GAP=${GAP_MS:-10}
ND=$([ "$W" = 20 ] && echo NOHT || echo "${W}MHz")

$G "cd /root/fwre; A=wlx00c0cab7962a; P=\$(cat /sys/class/net/\$A/phy80211/name); pkill -f 'inject.py mon_a';
    iw dev mon_a del 2>/dev/null; ip link set \$A down; iw phy \$P interface add mon_a type monitor && ip link set mon_a up;
    iw dev mon_a set freq $F $ND; iw dev mon_a set txpower fixed 500" > /dev/null 2>&1 < /dev/null

save=""
for kv in "$@"; do
	a=${kv%%=*} v=${kv#*=}
	old=$($G "cd /root/fwre; P=\$(cat /sys/class/net/wlx00c0cab7962a/phy80211/name); python3 fwdbg.py \$P read $a 1 | cut -d' ' -f2;
	          python3 fwdbg.py \$P write $a $v > /dev/null" 2>/dev/null < /dev/null)
	save="$save $a=0x$old"
done
[ "$TX" = on ] && $G "cd /root/fwre; setsid python3 inject.py mon_a 100000 $GAP 12 > /dev/null 2>&1 < /dev/null &" > /dev/null 2>&1 < /dev/null
sleep 2
$H "cd /tmp/mt7921sdr && python3 -I psd.py $F 0.5 31.25 40 TX/RX0 x" 2>/dev/null < /dev/null | grep '^{' | python3 -c "
import sys, json, statistics
d = json.loads(sys.stdin.readline())
t = d['timeline_db']
runs, cur = [], 0
for v in t:
    if v > 10: cur += 1
    elif cur: runs.append(cur); cur = 0
med = round(statistics.median(runs) * 1024 / 31.25) if runs else 0
duty = round(100 * sum(v > 10 for v in t) / len(t))
print(f\"tx=$TX $* obw20={d['obw20_mhz']} obw99={d['obw99_mhz']} peak={d['peak_db']} burst_us={med} bursts={len(runs)} duty={duty}%\")"
$G "pkill -f 'inject.py mon_a'" > /dev/null 2>&1 < /dev/null
for kv in $save; do
	$G "cd /root/fwre; P=\$(cat /sys/class/net/wlx00c0cab7962a/phy80211/name); python3 fwdbg.py \$P write ${kv%%=*} ${kv#*=} > /dev/null" > /dev/null 2>&1 < /dev/null
done
