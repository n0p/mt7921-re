#!/bin/sh
# Run one Ghidra post-script on the mt7961 project: [PROG=<program>] tools/ghidra_script.sh <Script.java> [args...]
cd "$(dirname "$0")/.." || exit 1
s="$1"; shift
/opt/ghidra/support/analyzeHeadless work/ghidra mt7961 -process "${PROG:-ram_0_00915000.bin}" -noanalysis \
  -scriptPath tools/ghidra -postScript "$s" "$@" 2>&1 \
  | grep -E "${s%.java}.java>|ERROR|Exception" | sed "s/^INFO  ${s%.java}.java> //; s/ (GhidraScript) *$//"
