#!/bin/bash
# MIB counter diff on card B around a burst of card A's frames: dump B's WF_MIB page (0x820ed000, 4 KB) before
# and after A injects, print the words that changed (counters are mostly clear-on-read, so "after" is the delta).
# Usage: mibdiff.sh <CA> <CB> [B writes reg=val,...|-]      (rig reset first)
cd /root/fwre
CA=$1 CB=$2 W=${3:--}
./rigreset.sh > /dev/null
PA=$(cat /sys/class/net/wlx00c0cab7962a/phy80211/name); PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
python3 chsw.py $PA 149 1 0 > /dev/null; python3 chsw.py $PA 36 1 $CA 1 1 > /dev/null
python3 chsw.py $PB 149 1 0 > /dev/null; python3 chsw.py $PB 36 1 $CB 1 1 > /dev/null
[ "$W" = - ] || for kv in ${W//,/ }; do python3 fwdbg.py $PB write ${kv%%=*} ${kv#*=} > /dev/null; done
python3 fwdbg.py $PB dump /tmp/mib0.bin 0x820ed000 > /dev/null
sleep 2
python3 fwdbg.py $PB dump /tmp/mib1.bin 0x820ed000 > /dev/null
python3 inject.py mon_a 100 10 12 0 > /dev/null
python3 fwdbg.py $PB dump /tmp/mib2.bin 0x820ed000 > /dev/null
python3 - "$CA" "$CB" "$W" <<PY
import struct, sys
r = lambda f: struct.unpack("<1024I", open(f, "rb").read())
idle, tx = r("/tmp/mib1.bin"), r("/tmp/mib2.bin")
print(f"A{sys.argv[1]}->B{sys.argv[2]} {sys.argv[3]}:")
for i, (a, b) in enumerate(zip(idle, tx)):
    if a or b:
        print(f"  0x{0x820ed000 + 4*i:08x} idle2s {a:#010x} txburst {b:#010x}")
PY
