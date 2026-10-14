#!/bin/sh
# PHY RX counter deltas on card B (0x83081010..0x83081024, path 0) around an X410 burst.
# Usage: phycnt.sh <freq MHz> <x410 width|idle> [label]   (B must already be configured, e.g. via chsw.py)
cd "$(dirname "$0")/../.."
F=$1 W=$2 L=${3:-}
rd() { tools/g.sh 'P=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name); python3 /root/fwre/fwdbg.py $P read 0x83081010 6' 2>/dev/null </dev/null | sed 's/^[0-9a-f]*: //' | tr '\n' ' '; }
a=$(rd)
if [ "$W" = idle ]; then sleep 3; else
	timeout 120 tools/h.sh "cd /tmp/mt7921sdr && python3 txframes.py $F $W 200 5 6 40 > /dev/null 2>&1" </dev/null
fi
b=$(rd)
python3 -c "
a = [int(x, 16) for x in '$a'.split()]; b = [int(x, 16) for x in '$b'.split()]
names = ['+10', '+14', '+18', '+1c', '+20', '+24']
out = []
for n, x, y in zip(names, a, b):
    lo = ((y & 0xffff) - (x & 0xffff)) & 0xffff; hi = ((y >> 16) - (x >> 16)) & 0xffff
    out.append(f'{n}: hi {hi:5d} lo {lo:5d}')
print('$L X410=$W | ' + ' | '.join(out))"
