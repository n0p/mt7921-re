// Map remaining MT7961 firmware regions, set $gp and entry points before auto-analysis.
// Script args: <regions dir containing regions.json> <gp hex>
// @category MT7961

import java.io.File;
import java.io.FileInputStream;
import java.math.BigInteger;
import java.nio.file.Files;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.lang.Register;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.SourceType;

public class SetupMt7961 extends GhidraScript {

	@Override
	protected void run() throws Exception {
		String[] args = getScriptArgs();
		File dir = new File(args[0]);
		long gp = Long.decode(args[1]);
		Memory mem = currentProgram.getMemory();

		// The debug-log routine (EX9 #2, 0x9217ba) gets misdetected as non-returning, which truncates
		// every function after its first log call. Turn that analyzer off for this image.
		setAnalysisOption(currentProgram, "Non-Returning Functions - Discovered", "false");

		// Only RAM regions: the ROM patch (0x900000) overlaps region 0 and is a separate boot stage.
		// regions.json entries look like: "file": "ram_1_02015c00.bin", ... "addr": "0x02015c00"
		String json = Files.readString(new File(dir, "regions.json").toPath());
		Matcher m = Pattern.compile("\"file\": \"(ram_\\d+_([0-9a-f]{8})\\.bin)\"").matcher(json);
		while (m.find()) {
			Address a = toAddr(Long.parseLong(m.group(2), 16));
			if (mem.getBlock(a) != null) {
				continue; // the region used for the initial import
			}
			File f = new File(dir, m.group(1));
			try (FileInputStream in = new FileInputStream(f)) {
				MemoryBlock b = mem.createInitializedBlock(m.group(1).replace(".bin", ""), a, in, f.length(),
					monitor, false);
				b.setRead(true);
				b.setWrite(true);
				b.setExecute(true);
			}
			println("mapped " + f.getName() + " @ " + a);
		}

		// Not shipped in the image: boot ROM (gp-relative ROM API tables live here) and RAM data/bss below region 1.
		uninit("boot_rom", 0x00800000L, 0x00100000L);
		uninit("dlm_bss", 0x02000000L, 0x00015c00L);

		Register gpReg = currentProgram.getRegister("gp");
		for (MemoryBlock b : mem.getBlocks()) {
			if (b.isExecute()) {
				currentProgram.getProgramContext()
						.setValue(gpReg, b.getStart(), b.getEnd(), BigInteger.valueOf(gp));
			}
		}
		createLabel(toAddr(gp), "_gp", true, SourceType.USER_DEFINED);
		entry(0x00915000L, "ram_entry");
		entry(0x00915b34L, "ram_reset_main");
		entry(0x0091500cL, "ram_init_data_bss");
		entry(0x00915074L, "ram_set_itb");
	}

	private void uninit(String name, long start, long len) throws Exception {
		Address a = toAddr(start);
		if (currentProgram.getMemory().getBlock(a) == null) {
			MemoryBlock b = currentProgram.getMemory().createUninitializedBlock(name, a, len, false);
			b.setRead(true);
			b.setWrite(true);
			b.setExecute(true);
		}
	}

	private void entry(long addr, String name) throws Exception {
		Address a = toAddr(addr);
		createLabel(a, name, true, SourceType.USER_DEFINED);
		addEntryPoint(a);
		disassemble(a);
		createFunction(a, name);
	}
}
