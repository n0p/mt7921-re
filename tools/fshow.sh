#!/bin/sh
# Print decompiled functions by address: tools/fshow.sh 0x00942544 [...]
# Boot-ROM addresses (0x800000..0x8fffff) come from work/decomp/rom_all.c, everything else from all.c.
d="$(dirname "$0")/../work/decomp"
for a in "$@"; do
  f=$d/all.c
  [ $((a)) -ge $((0x800000)) ] && [ $((a)) -lt $((0x900000)) ] && f=$d/rom_all.c
  a=$(printf '%08x' "$a")
  awk -v a="ram:$a" '/^\/\/@@ /{p = ($2 == a)} p' "$f"
done
