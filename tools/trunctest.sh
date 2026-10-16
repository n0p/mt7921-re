#!/bin/sh
# Laptop-side driver for the narrow-PPDU TX truncation check (notes/10 §5): card A runs nbmesh (TUN, FEC off) at
# 2350 MHz and pings an absent peer with growing sizes (ICMP data 0x5a); the X410 captures on RX1 and
# tools/sdr/trunccheck.py reports the symbols that arrive per PSDU length. Card A transmits at 2350 MHz, this rig's
# test frequency (inside the operator's amateur licence).
# Usage: tools/trunctest.sh <width 5|2.5> ["extra nbmesh options", e.g. "--tickcr0 0xa"] ["ping sizes"]
cd "$(dirname "$0")"
W=${1:-2.5}
EXTRA=${2:-}
SIZES=${3:-"40 80 200 400 700 1000 1300"}
N=$(echo $SIZES | wc -w)
CAP=$(( N * 2 + 3 ))
scp -q sdr/trunccheck.py sdr/rx80211.py sdr/ofdm80211.py 155.54.95.95:/tmp/mt7921sdr/
./g.sh "cd /root/fwre && (nohup timeout 90 ./nbmesh wlx00c0cab7962a --freq 2350 --width $W --seconds $((CAP + 12)) \
  --status-s 60 --dev nba --netns nsa --addr 10.42.0.1/24 --quiet --gain 0,0,0 --fec off $EXTRA \
  > /dev/null 2> /tmp/nbm_trunc.log &); sleep 7; (nohup sh -c 'for s in $SIZES; do ip netns exec nsa ping -i 0.25 \
  -c 6 -w 2 -s \$s -p 5a 10.42.0.2; done' > /dev/null 2>&1 &); echo started"
./h.sh "cd /tmp/mt7921sdr && python3 capture.py 2350 $W $CAP /tmp/mt7921sdr/trunc.cf32 30 RX1 2>&1 | tail -1; \
  python3 trunccheck.py /tmp/mt7921sdr/trunc.cf32 $W; rm -f /tmp/mt7921sdr/trunc.cf32"
sleep 6
./g.sh "grep -E 'stalled \(|total' /tmp/nbm_trunc.log | cut -c1-120"
