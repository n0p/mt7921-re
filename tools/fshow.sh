#!/bin/sh
# Print decompiled functions from work/decomp/all.c by address: tools/fshow.sh 0x00942544 [...]
for a in "$@"; do
  a=$(printf '%08x' "$a")
  awk -v a="ram:$a" '/^\/\/@@ /{p = ($2 == a)} p' "$(dirname "$0")/../work/decomp/all.c"
done
