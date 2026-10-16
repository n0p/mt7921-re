#!/bin/sh
# Apply tools/ghidra/annotations to the Ghidra project and regenerate the commented decompilations:
#   RAM 2026 (ram_0_00915000.bin): ram.tsv + regs.tsv -> work/decomp/all.c
#   boot ROM (rom_800000.bin):     rom.tsv + regs.tsv -> work/decomp/rom_all.c
# Each run: Annotate.java (names, plate/PRE comments, register labels), MmioVolatile.java (register pages volatile,
# so no register write is dropped as dead), AutoComment.java ([auto] source files and log formats), DecompAll.java
# (export with comments and a register glossary), then tools/gci_comment.py (decoded register and bits above every
# GCI accessor call). Re-running is idempotent.
# Edit the gen_*.py sources, not the .tsv files: (cd tools/ghidra/annotations && python3 gen_ram.py > ram.tsv &&
#   python3 gen_ram.py regs > regs.tsv && python3 gen_rom.py > rom.tsv)
# Usage: tools/annotate.sh [ram|rom]...   (default both)
cd "$(dirname "$0")/.." || exit 1
A="$PWD/tools/ghidra/annotations"
run() {	# <program> <tsv>... <out>
	prog=$1; shift
	out=$(eval echo \${$#})
	n=$(($# - 1)); tsvs=""; i=0
	for t in "$@"; do i=$((i + 1)); [ $i -le $n ] && tsvs="$tsvs $A/$t"; done
	/opt/ghidra/support/analyzeHeadless work/ghidra mt7961 -process "$prog" -noanalysis -scriptPath tools/ghidra \
		-postScript Annotate.java $tsvs -postScript MmioVolatile.java true -postScript AutoComment.java -postScript DecompAll.java "$PWD/$out" 2>&1 \
		| grep -E "(Annotate|MmioVolatile|AutoComment|DecompAll).java>|ERROR|Exception" | sed 's/^INFO  //; s/ (GhidraScript) *$//'
	python3 tools/gci_comment.py "$out" data/crmap/crmap.json "$A/regs.tsv"
}
for w in ${@:-ram rom}; do
	case $w in
	ram) run ram_0_00915000.bin ram.tsv regs.tsv work/decomp/all.c ;;
	rom) run rom_800000.bin rom.tsv regs.tsv work/decomp/rom_all.c ;;
	*) echo "unknown: $w"; exit 1 ;;
	esac
done
