#!/bin/sh
# Rebuild the Ghidra project for the MT7961 RAM image from work/regions_rt (runtime layout, tools/mkruntime.py).
# Requires the NDS32_MT7961 extension installed in ~/.config/ghidra/<version>/Extensions.
set -e
cd "$(dirname "$0")/.."
GP=0x02003000
H=/opt/ghidra/support/analyzeHeadless
rm -rf work/ghidra/mt7961.gpr work/ghidra/mt7961.rep
mkdir -p work/ghidra
$H work/ghidra mt7961 -import work/regions_rt/ram_0_00915000.bin -loader BinaryLoader -loader-baseAddr 0x915000 \
  -processor NDS32:LE:32:mt7961 -scriptPath tools/ghidra -preScript SetupMt7961.java "$PWD/work/regions_rt" $GP \
  -analysisTimeoutPerFile 3600 > work/ghidra/import.log 2>&1
$H work/ghidra mt7961 -process ram_0_00915000.bin -noanalysis -scriptPath tools/ghidra \
  -postScript SeedFunctions.java -postScript GpRefs.java $GP > work/ghidra/post.log 2>&1
grep -h -E "SCRIPT ERROR|Exception|functions:|gp refs|Total Time" work/ghidra/import.log work/ghidra/post.log
# Command dispatch tables at their live (runtime) addresses
tools/ghidra_script.sh LabelCmdTable.java 0x02026118 ce_cmd | tail -1
tools/ghidra_script.sh LabelCmdTable.java 0x020190e0 ext_cmd byte 38 | tail -1
# ROM patch (separate boot stage, uses the boot ROM's EX9 table, so stock language)
$H work/ghidra mt7961 -import work/regions_rt/patch_0_00900000.bin -loader BinaryLoader -loader-baseAddr 0x900000 \
  -processor NDS32:LE:32:default -analysisTimeoutPerFile 1800 > work/ghidra/patch_import.log 2>&1
# Full decompilation for grepping (tools/fgrep.sh, tools/fshow.sh)
tools/ghidra_script.sh DecompAll.java "$PWD/work/decomp/all.c"
tools/ghidra_script.sh LabelCmdTable.java 0x02026440 uni_cmd byte 10 | tail -1
