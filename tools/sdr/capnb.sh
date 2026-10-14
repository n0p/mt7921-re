#!/bin/sh
# Card A in native narrow (CHANNEL_SWITCH cbw, path 0) at 5180 MHz injecting 6 Mbps OFDM; X410 raw capture.
# Usage: capnb.sh <cbw> <rate Msps> <seconds> <local out.cf32> [gain] [inject rate_500k]
# Env X="reg=val ...": extra register writes on A (firmware debug command) after the channel switch.
# Env CENTER=<ch>: centre channel for cbw >= 1 (default: control channel 36); FC=<MHz>: capture centre (5180).
cd "$(dirname "$0")/../.."
CBW=$1 RATE=$2 SECS=$3 OUT=$4 GAIN=${5:-40} IR=${6:-12}
tools/g.sh "cd /root/fwre; pkill -f '^python3 inject'; ./rigreset.sh > /dev/null; P=\$(cat /sys/class/net/wlx00c0cab7962a/phy80211/name);
    python3 chsw.py \$P 149 1 0 > /dev/null; python3 chsw.py \$P 36 1 $CBW 1 1 0 ${CENTER:-36}; for kv in ${X:-}; do python3 fwdbg.py \$P write \${kv%%=*} \${kv#*=} > /dev/null; done;
    setsid python3 inject.py mon_a 3000 4 $IR > /dev/null 2>&1 < /dev/null &" </dev/null
sleep 2
tools/h.sh "cd /tmp/mt7921sdr && python3 -I capture.py ${FC:-5180} $RATE $SECS /tmp/mt7921sdr/capnb.cf32 $GAIN" </dev/null
tools/h.sh "cat /tmp/mt7921sdr/capnb.cf32; rm -f /tmp/mt7921sdr/capnb.cf32" </dev/null > "$OUT"
tools/g.sh "pkill -f '^python3 inject'" </dev/null
ls -l "$OUT"
