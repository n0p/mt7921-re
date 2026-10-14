#!/bin/sh
# Card A native narrow (direct CHANNEL_SWITCH cbw 4 @5180, path 0) + register overrides, inject OFDM, measure
# with the X410: -20 dB width, frame duration and OFDM cyclic-prefix lag (3.2 us = 20 MHz, 6.4 = 10, 12.8 = 5).
# Usage: txprobe.sh "<reg=val[,reg=val...]>" ...   (rf:<reg>=<val> writes an A-die RF register, path 0)
cd "$(dirname "$0")/../.."
for c in "$@"; do
	w=$(echo "$c" | tr ',' ' ')
	tools/g.sh "cd /root/fwre; pkill -f '^python3 inject'; P=\$(cat /sys/class/net/wlx00c0cab7962a/phy80211/name);
	    python3 chsw.py \$P 149 1 0 >/dev/null; python3 chsw.py \$P 36 1 ${CBW:-4} 1 1 >/dev/null;
	    for kv in $w; do case \$kv in
	        rf:*) kv=\${kv#rf:}; python3 fwdbg.py \$P call 0x0083a66c \${kv%%=*} \${kv#*=} 0 > /dev/null;;
	        *) python3 fwdbg.py \$P write \${kv%%=*} \${kv#*=} > /dev/null;;
	    esac; done;
	    setsid python3 inject.py mon_a 2000 10 12 > /dev/null 2>&1 < /dev/null &" </dev/null
	sleep 2
	b=$(tools/h.sh "cd /tmp/mt7921sdr && python3 burst.py 5180 0.3 31.25 30" </dev/null 2>/dev/null | grep '^{' | python3 -c "
import sys, json
l = sys.stdin.readline()
d = json.loads(l) if l.strip() else {}
print(f\"first: obw20 {d.get('burst_obw20_mhz')} cp {d.get('cp_lag_us')} | all bursts by CP lag: {d.get('bursts_by_cp_lag')}\")")
	p=""
	echo "$c | $p | $b"
done
tools/g.sh "pkill -f '^python3 inject'" </dev/null
