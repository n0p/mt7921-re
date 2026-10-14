// Label an MCU command dispatch table of {u32 cid; u32 handler} pairs and create the handler functions.
// Script args: <table addr hex> <prefix> [byte <count>]
//   default: u32 cid, table ends at the first entry whose handler is not code
//   byte:    u8 cid + padding, exactly <count> entries, NULL handlers skipped
// @category MT7961

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.data.DWordDataType;
import ghidra.program.model.data.PointerDataType;
import ghidra.program.model.listing.Function;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.SourceType;

public class LabelCmdTable extends GhidraScript {

	@Override
	protected void run() throws Exception {
		String[] args = getScriptArgs();
		Address t = toAddr(Long.decode(args[0]));
		String prefix = args[1];
		createLabel(t, prefix + "_table", true, SourceType.USER_DEFINED);
		boolean byteCid = args.length > 3 && args[2].equals("byte");
		int count = byteCid ? Integer.decode(args[3]) : Integer.MAX_VALUE;
		int n = 0;
		for (Address e = t; n < count; e = e.add(8), n++) {
			long cid = byteCid ? getByte(e) & 0xffL : getInt(e) & 0xffffffffL;
			Address h = toAddr(getInt(e.add(4)) & 0xffffffffL);
			if (byteCid && h.getOffset() == 0) {
				continue;
			}
			MemoryBlock b = getMemoryBlock(h);
			if (cid > 0xffff || b == null || !b.isInitialized() || !b.isExecute() || b.getName().equals("ram_1_02015c00")) {
				break;
			}
			clearListing(e, e.add(7));
			createData(e, DWordDataType.dataType);
			createData(e.add(4), PointerDataType.dataType);
			String name = String.format("%s_%02x", prefix, cid);
			Function f = getFunctionAt(h);
			if (f == null) {
				disassemble(h);
				f = createFunction(h, name);
			}
			if (f != null && f.getName().startsWith("FUN_")) {
				f.setName(name, SourceType.USER_DEFINED);
			}
			else if (f != null && !f.getName().contains(name)) {
				f.setName(f.getName() + "__" + name, SourceType.USER_DEFINED);
			}
			println(String.format("%s cid=%#04x -> %s %s", e, cid, h, f == null ? "(no func)" : f.getName()));
		}
		analyzeChanges(currentProgram);
		println(prefix + ": " + n + " entries");
	}
}
