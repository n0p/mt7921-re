// Mark the mmio_* blocks that Annotate.java mapped volatile (or not), so the decompiler keeps every register access.
// Script args: [true|false]   (default true)
// @category MT7961

import ghidra.app.script.GhidraScript;
import ghidra.program.model.mem.MemoryBlock;

public class MmioVolatile extends GhidraScript {

	@Override
	protected void run() throws Exception {
		boolean v = getScriptArgs().length == 0 || Boolean.parseBoolean(getScriptArgs()[0]);
		int n = 0;
		for (MemoryBlock b : currentProgram.getMemory().getBlocks()) {
			if (b.getName().startsWith("mmio_")) {
				b.setVolatile(v);
				n++;
			}
		}
		println("mmio blocks volatile=" + v + ": " + n);
	}
}
