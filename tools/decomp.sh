#!/bin/sh
# Usage: [PROG=patch_0_00900000.bin] tools/decomp.sh <outdir> <selector>...   (selector: 0xADDR or s:REGEX)
cd "$(dirname "$0")/.." || exit 1
out="$1"; shift
/opt/ghidra/support/analyzeHeadless work/ghidra mt7961 -process "${PROG:-ram_0_00915000.bin}" -noanalysis \
  -scriptPath tools/ghidra -postScript Decomp.java "$PWD/$out" "$@" 2>&1 \
  | grep -E "Decomp.java>|ERROR|Exception" | sed 's/^INFO  Decomp.java> //; s/ (GhidraScript) *$//'
