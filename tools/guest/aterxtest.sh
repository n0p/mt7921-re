#!/bin/bash
# B in ATE RX (J-mode j) at 5180 MHz, A injects in native cbw (monitor); report B's ATE RXOK/RXERR deltas.
# Usage: aterxtest.sh <B jmode> <A cbw>
cd /root/fwre
J=$1 CA=$2
./rigreset.sh > /dev/null
PA=$(cat /sys/class/net/wlx00c0cab7962a/phy80211/name); PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
python3 chsw.py $PA 149 1 0 > /dev/null; python3 chsw.py $PA 36 1 $CA 1 1 > /dev/null
python3 ate.py $PB mode 1; sleep 2
python3 aterx.py $PB start 5180000 $J > /dev/null; sleep 1
q() { python3 -c "
import importlib.util, struct
spec = importlib.util.spec_from_file_location('ate', '/root/fwre/ate.py'); ate = importlib.util.module_from_spec(spec); spec.loader.exec_module(ate)
print(struct.unpack('<II', ate.send('$PB', 2, $1, 0, True))[1])"; }
r0=$(q 0x22); e0=$(q 0x23)
python3 inject.py mon_a 100 20 12 > /dev/null
r1=$(q 0x22); e1=$(q 0x23)
echo "B ATE J$J, A cbw$CA x100: RXOK +$((r1 - r0)) RXERR +$((e1 - e0)) rssi $(q 0x2e)"
python3 aterx.py $PB stop; python3 ate.py $PB mode 0 > /dev/null
