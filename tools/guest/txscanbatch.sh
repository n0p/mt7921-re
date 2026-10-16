#!/bin/bash
# Run tools/guest/txscan.py over a long configuration list in batches (one capture each, analyzed and deleted before
# the next), so the gated dump never outgrows the guest disk. Logs: <prefix>.<n>.log; a disk guard stops the run below
# 800 MB free.
# Usage: txscanbatch.sh <MHz> <config file, one config per line> <batch size> <log prefix>   (env as for txscan.py)
cd /root/fwre
F=$1 LIST=$2 BS=$3 PFX=$4
split -l $BS -d -a 2 "$LIST" /tmp/txb.
for b in /tmp/txb.*; do
	n=${b##*.}
	python3 txscan.py $F - $(cat $b) > $PFX.$n.log 2>&1 &
	pid=$!
	while kill -0 $pid 2>/dev/null; do
		if [ $(df --output=avail /root | tail -1) -lt 800000 ]; then
			pkill -f "^python3 txscan.py"; pkill -f "^python3 nbrxd.py"; echo DISK-GUARD >> $PFX.$n.log
		fi
		sleep 2
	done
	rm -f /root/fwre/scan.bin
	grep -q DISK-GUARD $PFX.$n.log && break
done
rm -f /tmp/txb.*
echo done > $PFX.done
