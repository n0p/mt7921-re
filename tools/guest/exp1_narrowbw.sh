#!/bin/sh
# Experiment 1: does the stock MT7961 firmware really narrow the PHY when sent CMD_CBW_10MHZ/5MHZ?
# Needs patches/0001-mt7921-force_cbw-experiment.patch applied (param: mt7921_common.force_cbw).
#
# Usage: exp1_narrowbw.sh <tx phy> <rx phy> <freq MHz> <cbw: -1|4|5> [frames] [rx cbw]
#   e.g. exp1_narrowbw.sh phy0 phy1 5180 4          (both at 10 MHz: should still receive)
#        exp1_narrowbw.sh phy0 phy1 5180 4 2000 -1  (TX 10 MHz, RX 20 MHz: should mostly fail if PHY narrowed)
# Creates mon_tx / mon_rx monitor interfaces, applies the forced width, injects from TX, and counts frames on RX.
# Measure the TX occupied bandwidth with the SDR while frames are being sent:
#   20 MHz OFDM ~16.6 MHz wide, 10 MHz ~8.3 MHz, 5 MHz ~4.2 MHz.
set -eu
TXPHY=$1 RXPHY=$2 FREQ=$3 CBW=$4 N=${5:-2000} RXCBW=${6:-$4}
HERE=$(dirname "$0")
P=/sys/module/mt7921_common/parameters/force_cbw

[ -w "$P" ] || { echo "force_cbw param missing: patched driver not loaded?"; exit 1; }

setup() { # phy name
	iw dev "$2" del 2>/dev/null || true
	iw phy "$1" interface add "$2" type monitor flags none
	ip link set "$2" up
}

setup "$TXPHY" mon_tx
setup "$RXPHY" mon_rx
# The param is read when a channel is set, so each card can be tuned with a different forced width.
# Hop to another channel first so the set-freq below always re-sends CHANNEL_SWITCH.
echo "$CBW" > "$P"
iw dev mon_tx set freq 2412 20MHz; iw dev mon_tx set freq "$FREQ" 20MHz
echo "$RXCBW" > "$P"
iw dev mon_rx set freq 2412 20MHz; iw dev mon_rx set freq "$FREQ" 20MHz
echo -1 > "$P"
dmesg | grep -E "forcing bw" | tail -4

echo "--- RX capture on mon_rx (15 s), TX injecting $N frames"
timeout 15 tcpdump -i mon_rx -nn -e -q 'wlan addr2 02:00:00:00:00:01' > /tmp/exp1_rx.txt 2>/tmp/exp1_rx.err &
sleep 1
python3 "$HERE/inject.py" mon_tx "$N" 5 12
wait || true
echo "received: $(grep -c . /tmp/exp1_rx.txt)/$N frames (force_cbw tx=$CBW rx=$RXCBW)"
head -3 /tmp/exp1_rx.txt
dmesg | tail -15 | grep -i -E "mt79|mcu|timeout|error" || true
