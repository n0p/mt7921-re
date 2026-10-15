#!/bin/bash
# PHY packet-detect oracle: card B (5180 MHz, path 0) in native cbw C plus writes W (fwdbg; "rst" = RST_CTRL pulse:
# 0x83083008 = 0x007fc0ff, 0x83080014 = 0x0103ffff, then both 0; rf:reg=val = A-die), PHY RX counters enabled
# (0x83088234 |= 0x30000, 0x83082004 [11:9]/[2:0] = 5/5, reset pulse 0x83088230 bit 29), then for each X410 width:
# counter deltas over a burst of 200 frames. Counters (firmware FUN_936cc8 / FUN_9624f2 decode):
#   0x83081010 OFDM-PD | CCK-PD   0x83081014 OFDM-MDRDY | CCK-MDRDY   0x8308101c CCK-SIGerr | CCK-SFDerr
#   0x83081020 OFDM-SIGerr | OFDM-TAGerr   0x83081024 OFDM-FCSerr | CCK-FCSerr      ([31:16] | [15:0])
# Usage: pdcnt.sh <cbw> <W reg=val,...|-> <X410 widths, e.g. "20 10 5">
# Env FREQ=<MHz>: tune card B with the CHANNEL_SWITCH frequency override (2.4 GHz band, e.g. 2350 in the 13 cm
# amateur band, where no Wi-Fi traffic pollutes the counters) and transmit there; default 5180 (channel 36).
cd "$(dirname "$0")/../.."
C=$1 W=$2 X=$3 FREQ=${FREQ:-5180}
if [ "$FREQ" = 5180 ]; then SW="149 1 0 > /dev/null; python3 chsw.py \$P 36 1 $C 1 1"; else SW="11 0 0 > /dev/null; python3 chsw.py \$P 1 0 $C 1 1 $FREQ"; fi
tools/g.sh "cd /root/fwre; P=\$(cat /sys/class/net/wlx00c0cab79628/phy80211/name);
	python3 chsw.py \$P $SW > /dev/null;
	for kv in \$(echo '$W' | tr ',' ' '); do case \$kv in
		-) ;;
		rst) python3 fwdbg.py \$P write 0x83083008 0x007fc0ff > /dev/null; python3 fwdbg.py \$P write 0x83080014 0x0103ffff > /dev/null;
		     python3 fwdbg.py \$P write 0x83080014 0 > /dev/null; python3 fwdbg.py \$P write 0x83083008 0 > /dev/null;;
		rf:*) kv=\${kv#rf:}; python3 fwdbg.py \$P call 0x0083a66c \${kv%%=*} \${kv#*=} 0 > /dev/null;;
		*) python3 fwdbg.py \$P write \${kv%%=*} \${kv#*=} > /dev/null;;
	esac; done;
	v=\$(python3 fwdbg.py \$P read 0x83088234 1 | cut -d' ' -f2); python3 fwdbg.py \$P write 0x83088234 \$(printf '0x%x' \$(( 0x\$v | 0x30000 ))) > /dev/null;
	v=\$(python3 fwdbg.py \$P read 0x83082004 1 | cut -d' ' -f2); python3 fwdbg.py \$P write 0x83082004 \$(printf '0x%x' \$(( (0x\$v & ~0xe07) | 0xa05 ))) > /dev/null;
	v=\$(python3 fwdbg.py \$P read 0x83088230 1 | cut -d' ' -f2); python3 fwdbg.py \$P write 0x83088230 \$(printf '0x%x' \$(( 0x\$v | 0x20000000 ))) > /dev/null;
	python3 fwdbg.py \$P write 0x83088230 0x\$v > /dev/null;
	echo \"B: \$(python3 fwdbg.py \$P read 0x83081000 1) \$(python3 fwdbg.py \$P read 0x83082004 1) RF0x184 \$(python3 fwdbg.py \$P call 0x0083a662 0x184 0 | tail -1)\"" </dev/null
# counters saturate at 0xffff (noise packet detects reach that in seconds): clear them before every window with the
# firmware's STSCNT_EN disable -> enable edge (0x83082004 [11:9] = 0, then 5) and report absolute values
clr() { tools/g.sh "cd /root/fwre; P=\$(cat /sys/class/net/wlx00c0cab79628/phy80211/name); v=\$(python3 fwdbg.py \$P read 0x83082004 1 | cut -d' ' -f2);
	python3 fwdbg.py \$P write 0x83082004 \$(printf '0x%x' \$(( 0x\$v & ~0xe00 ))) > /dev/null; python3 fwdbg.py \$P write 0x83082004 \$(printf '0x%x' \$(( (0x\$v & ~0xe00) | 0xa00 ))) > /dev/null" </dev/null; }
rd() { tools/g.sh "cd /root/fwre; P=\$(cat /sys/class/net/wlx00c0cab79628/phy80211/name); python3 fwdbg.py \$P read 0x83081010 6 | cut -d' ' -f2- | tr '\n' ' '" </dev/null; }
for w in $X; do
	clr; a=$(rd)
	if [ "$w" = idle ]; then sleep 3; else timeout 120 tools/h.sh "cd /tmp/mt7921sdr && python3 txframes.py $FREQ $w 200 5 6 40 > /dev/null 2>&1" </dev/null; fi
	b=$(rd)
	python3 - "$w" "$a" "$b" <<'PY'
import sys
w, a, b = sys.argv[1], [int(x, 16) for x in sys.argv[2].split()], [int(x, 16) for x in sys.argv[3].split()]
names = {0: ("OFDM-PD", "CCK-PD"), 1: ("OFDM-MDRDY", "CCK-MDRDY"), 3: ("CCK-SIGerr", "CCK-SFDerr"),
         4: ("OFDM-SIGerr", "OFDM-TAGerr"), 5: ("OFDM-FCSerr", "CCK-FCSerr")}
out = []
for i, (hi, lo) in names.items():
    dh = ((b[i] >> 16) - (a[i] >> 16)) & 0xffff; dl = ((b[i] & 0xffff) - (a[i] & 0xffff)) & 0xffff
    out.append(f"{hi} {dh} {lo} {dl}")
print(f"  X410 {w:>4}: " + " | ".join(out))
PY
done
