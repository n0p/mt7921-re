// Seed functions at NDS32 prologues (push25 / smw.adm ... lp) in executable RAM blocks, then report counts.
// Prologue candidates are only taken right after a return/branch or at a 4-byte padded boundary,
// to avoid starting functions in the middle of data.
// @category MT7961

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.mem.MemoryBlock;

public class SeedFunctions extends GhidraScript {

	@Override
	protected void run() throws Exception {
		int before = currentProgram.getFunctionManager().getFunctionCount();
		int created = 0;
		for (MemoryBlock b : currentProgram.getMemory().getBlocks()) {
			if (!b.isInitialized() || !b.isExecute() || !b.getName().startsWith("ram_")) {
				continue;
			}
			// Region 1 (0x02015c00) is mostly data/strings; leave it to reference-driven analysis.
			if (b.getStart().getOffset() == 0x02015c00L) {
				continue;
			}
			for (Address a = b.getStart(); a.compareTo(b.getEnd().subtract(4)) < 0; a = a.add(2)) {
				monitor.checkCancelled();
				int h = (getByte(a) & 0xff) << 8 | (getByte(a.add(1)) & 0xff);
				// push25 Re, imm: 16-bit 0b1111110 0 Re(2) imm5 (e.g. "push25 s0, 8" = 0xfc01); pop25 has bit 7 set
				boolean push25 = (h & 0xff80) == 0xfc00;
				// smw.* (Opc=0b011101) with lp in Enable4 (bit 7 of the big-endian word); mnemonic checked below
				boolean smwAdm = (h & 0xfe00) == 0x3a00 && (getByte(a.add(3)) & 0x80) != 0;
				if (!push25 && !smwAdm) {
					continue;
				}
				if (getFunctionContaining(a) != null || getInstructionContaining(a) != null) {
					continue;
				}
				if (!afterTerminator(a)) {
					continue;
				}
				if (!disassemble(a)) {
					continue;
				}
				Instruction ins = getInstructionAt(a);
				if (smwAdm && (ins == null || !ins.getMnemonicString().equalsIgnoreCase("smw.adm"))) {
					clearListing(a);
					continue;
				}
				if (createFunction(a, null) != null) {
					created++;
				}
			}
		}
		analyzeChanges(currentProgram);
		println(String.format("functions: %d before, %d seeded, %d now", before, created,
			currentProgram.getFunctionManager().getFunctionCount()));
	}

	private boolean afterTerminator(Address a) throws Exception {
		Instruction prev = getInstructionBefore(a);
		if (prev != null && prev.getMaxAddress().next().equals(a)) {
			return !prev.getFlowType().hasFallthrough();
		}
		// Nothing decoded right before: accept if preceded by padding or at an aligned start.
		return a.getOffset() % 4 == 0 || (getByte(a.subtract(1)) == 0 && getByte(a.subtract(2)) == 0);
	}
}
