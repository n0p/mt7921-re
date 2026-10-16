#!/bin/sh
# grep a decompilation and prefix each hit with the containing function: [F=rom_all.c] tools/fgrep.sh <ERE>
awk -v re="$1" '/^\/\/@@ /{fn=$2" "$3; next} $0 ~ re {print fn ": " $0}' "$(dirname "$0")/../work/decomp/${F:-all.c}"
