#!/bin/bash
# Put a monitor interface on a native 5/10 MHz channel (stock 2026 firmware, no register hacks).
# The driver sends the UNI SNIFFER config with the narrow bw, but the BSS_INFO RLM TLV sent after it maps
# CMD_CBW_5/10 back to 20 MHz. Re-sending the sniffer config last leaves the firmware in its native narrow mode.
# Usage: nbset.sh <monitor iface> <freq MHz> <width 5|10|20>   (20 forces the sniffer bw back to 20 MHz)
set -eu
IF=$1 F=$2 W=$3
P=$(cat /sys/class/net/$IF/phy80211/name)
echo "$P" > /sys/kernel/debug/mt7921dbg/target

iw dev "$IF" set freq "$F" "$([ "$W" = 20 ] && echo NOHT || echo "${W}MHz")"
if [ "$F" -lt 3000 ]; then band=1; ch=$(( F == 2484 ? 14 : (F - 2407) / 5 )); else band=2; ch=$(( (F - 5000) / 5 )); fi
case $W in 10) bw=5;; 5) bw=4;; *) bw=0;; esac	# sniffer encoding: 0 = 20 MHz, 4 = 5 MHz, 5 = 10 MHz
# u32 cmd (UNI 0x24) | hdr{band_idx,pad[3]} | tlv{tag=1,len=16,aid,ch_band,bw,ctrl,sco,center,center2,drop_err,pad[3]}
printf "$(printf '\\x%02x' 0x24 0 2 0  0 0 0 0  1 0 16 0  0 0 $band $bw $ch 0 $ch 0 1 0 0 0)" > /sys/kernel/debug/mt7921dbg/raw
echo "$IF ($P): $F MHz ch $ch, sniffer bw=$bw; fw cached bw=$(python3 /root/fwre/fwdbg.py $P call 0x0094252e 3 0 | cut -d' ' -f4)" \
     "clk0=$(python3 /root/fwre/fwdbg.py $P read 0x83081000 1 | cut -d' ' -f2)" \
     "clk1=$(python3 /root/fwre/fwdbg.py $P read 0x83091000 1 | cut -d' ' -f2)" \
     "ant=$(iw phy $P info | grep 'Configured Antennas' | sed 's/.*TX //;s/ RX /\//')"
