#!/bin/sh
# Wedge statistics: alternate dualtrx.sh runs (both cards as half-duplex nodes) started right after the rig reset and
# after a settle wait; recover the rig (rigrecover.sh) after any run with vendor-request timeouts.
# Usage: wedgestats.sh <pairs> [secs per run 60] [settle wait 30]   output: one line per run
D=$(dirname "$0")
N=$1 SECS=${2:-60} W=${3:-30}
i=0
while [ $i -lt "$N" ]; do
	for wait in 0 "$W"; do
		out=$($D/g.sh "cd /root/fwre && timeout $((SECS + 200)) ./dualtrx.sh $wait $SECS" 2>&1)
		t=$(echo "$out" | sed -n 's/^new vendor timeouts: \([0-9-]*\).*/\1/p')
		echo "run $i wait ${wait}s: $(echo "$out" | grep -E '^[AB]:' | tr '\n' ' ') timeouts=${t:-?}"
		if [ "${t:-1}" != 0 ]; then
			echo "   first: $(echo "$out" | sed -n 's/.*first: //p')"
			$D/rigrecover.sh > /dev/null 2>&1
		fi
	done
	i=$((i + 1))
done
