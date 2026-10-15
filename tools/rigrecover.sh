#!/bin/sh
# Full rig recovery after a card wedge (vendor requests failing with -110, MCU message timeouts, mt76u oops):
# USB-reset both cards on the lab host (a VM reset alone leaves a wedged card wedged), reset the guest through the QEMU
# monitor (tmux session vm23, Ctrl-a c), wait for SSH, reload the mt76 stack and mt7921dbg, reset the rig.
# Also streams the guest kernel log to /root/dmesg_boot_<HHMM>.log so the next oops is kept.
H=$(dirname "$0")/h.sh G=$(dirname "$0")/g.sh
$G 'pkill -INT -f "^python3 nbrxd.py"; sleep 2; pkill -9 -f "^python3 nbrxd.py"; sync' 2>/dev/null
$H 'for b in 002 006; do d=$(lsusb | awk -v b=$b "\$2 == b && /0e8d:7961/ {print \$4}" | tr -d :); [ -n "$d" ] && sudo -n usbreset $b/$d; done
sleep 2; sudo -n tmux send-keys -t vm23 C-a c; sleep 1; sudo -n tmux send-keys -t vm23 "system_reset" Enter; sleep 1
sudo -n tmux send-keys -t vm23 C-a c' || exit 1
sleep 20
i=0
until $G true 2>/dev/null; do i=$((i + 1)); [ $i -gt 30 ] && { echo "guest did not come back"; exit 1; }; sleep 5; done
$G 'setsid nohup sh -c "dmesg -wT > /root/dmesg_boot_$(date +%H%M).log" > /dev/null 2>&1 < /dev/null &
cd /root/fwre && ./loadstack.sh > /dev/null 2>&1; insmod mt7921dbg/mt7921dbg.ko; ./rigreset.sh | tail -1
for i in wlx00c0cab7962a wlx00c0cab79628; do P=$(cat /sys/class/net/$i/phy80211/name); D=/sys/kernel/debug/ieee80211/$P/mt76
	echo 0x820e3000 > $D/regidx; echo "$i $P alive: $(timeout 5 cat $D/regval)"; done'
