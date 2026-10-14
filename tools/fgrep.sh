#!/bin/sh
# grep work/decomp/all.c and prefix each hit with the containing function: tools/fgrep.sh <ERE>
awk -v re="$1" '/^\/\/@@ /{fn=$2" "$3; next} $0 ~ re {print fn ": " $0}' "$(dirname "$0")/../work/decomp/all.c"
