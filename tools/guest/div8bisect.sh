#!/bin/bash
# Which receive-side setup step turns the ÷8 transmitter into an unmodulated carrier? Card B transmits 30 frames at
# ÷8 (code 7 set right after the channel switch, MAC TX on, one TX attempt each) after the steps named in $1
# (comma list: icap = icap.py capture setup, gain = all manual-gain forces, or one of them: rf768 (RF 0x768 analog
# clamp, both paths), dig (0x83000108), f150 (0x83000150), fc4 (0x830000c4); none); card A counts them through its
# continuous 10 Msps stream at 2.5 MHz.  Usage: div8bisect.sh <steps> [MHz 2350]
cd /root/fwre
STEPS=$1 F=${2:-2350}
./rigreset.sh > /dev/null
echo 1 > /sys/module/mt7921dbg/parameters/tx_count
PB=$(cat /sys/class/net/wlx00c0cab79628/phy80211/name)
w() { python3 fwdbg.py $PB write $1 $2 > /dev/null; }
rmw() { local v; v=$(python3 fwdbg.py $PB read $1 1 | cut -d' ' -f2); w $1 $(printf "0x%x" $(( (0x$v & ~$2) | ($3 & $2) ))); }
python3 chsw.py $PB 11 0 0 > /dev/null; python3 chsw.py $PB 1 0 4 1 1 $F > /dev/null
rmw 0x83081000 0x70000000 0x70000000
case ,$STEPS, in *,icap,*) ICAP_PACK=2 ICAP_SETUP_ONLY=1 python3 icap.py $PB /dev/null 4 3 > /dev/null ;; esac
case ,$STEPS, in *,gain,*|*,rf768,*) for p in 0 1; do python3 fwdbg.py $PB call 0x0083a66c 0x768 0x80000000 $p > /dev/null; done ;; esac
case ,$STEPS, in *,gain,*|*,dig,*) rmw 0x83000108 0x3f3f0000 0 ;; esac
case ,$STEPS, in *,gain,*|*,f150,*) rmw 0x83000150 0x8070f000 0x80000000 ;; esac
case ,$STEPS, in *,gain,*|*,fc4,*) rmw 0x830000c4 0xf7f7f707 7 ;; esac
rmw 0x820e3000 0x100 0
timeout 40 python3 nbrxd.py A $F --pack 2 --gain 0,0,0 --rx-only --quiet --width 2.5 --seconds 9 --pcap /tmp/bis.pcap \
	> /tmp/bis.log 2>&1 &
sleep 4
python3 inject8.py mon_b 30 60 12
wait
echo "steps [$STEPS]: $(grep '^# total' /tmp/bis.log); frames with good inner CRC: $(grep -ao 'MT7921-NBW-[0-9]*' /tmp/bis.pcap | sort -u | wc -l)"
