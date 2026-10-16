#!/bin/bash
# Field scan for a 10 MHz demodulator on card B. Base: native cbw 4 (front end +-5 MHz) at 2350 MHz, class code 4
# (the chain behind the front end at 20 Msps, notes/03), RXFE_CTRL [7:4] = 4 and 0x83088014 [31:24] = 0x75 (the timing
# byte that revives SIGNAL decoding at class 4). There the demodulator decodes 20 MHz frames off a 20 Msps stream; a
# field that restores its internal decimation would make it decode 802.11p (10 MHz) frames.
# The X410 must be sending 10 MHz frames throughout (tools/sdr/txframes.py 2350 10 ...). pdscan.py SCORE=good: a hit
# is RxOk - FCS errors above the baseline. Usage: rxscan10.sh <T s per candidate> <page> [page...]
cd /root/fwre
T=$1; shift
./rigreset.sh > /dev/null
P=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
python3 chsw.py $P 11 0 0 > /dev/null; python3 chsw.py $P 1 0 4 1 1 2350 > /dev/null
for kv in 0x83081000=0xd4000000 0x8308f064=0x01010040 0x83088014=0x75070808; do
	python3 fwdbg.py $P write ${kv%%=*} ${kv#*=} > /dev/null
done
v=$(python3 fwdbg.py $P read 0x83088234 1 | cut -d' ' -f2); python3 fwdbg.py $P write 0x83088234 $(printf 0x%x $((0x$v | 0x30000))) > /dev/null
v=$(python3 fwdbg.py $P read 0x83082004 1 | cut -d' ' -f2); python3 fwdbg.py $P write 0x83082004 $(printf 0x%x $(((0x$v & ~0xe07) | 0xa05))) > /dev/null
# the real oracle: frames from the X410 (SA 02:00:00:00:00:02) on mon_b with their arrival time; rx10match.py counts the
# good-FCS ones inside each candidate's window
tcpdump -i mon_b -nn -e -tt 'wlan addr2 02:00:00:00:00:02' > /root/fwre/rx10.tcp 2>/dev/null &
TD=$!
SCORE=good python3 pdscan.py $P $T "$@"
kill $TD
python3 rx10match.py /root/fwre/rxscan10.log /root/fwre/rx10.tcp
