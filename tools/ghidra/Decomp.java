// Decompile functions selected by address or by referenced string regex.
// Script args: <outdir> <selector>...   selector = 0xADDR | s:REGEX
// Writes <outdir>/<funcaddr>.c and prints which selector hit which function.
// @category MT7961

import java.io.File;
import java.io.PrintWriter;
import java.util.*;
import java.util.regex.Pattern;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.Function;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Reference;

public class Decomp extends GhidraScript {

	@Override
	protected void run() throws Exception {
		String[] args = getScriptArgs();
		File outdir = new File(args[0]);
		outdir.mkdirs();
		Map<Function, List<String>> targets = new LinkedHashMap<>();
		for (int i = 1; i < args.length; i++) {
			String sel = args[i];
			if (sel.startsWith("s:")) {
				for (Map.Entry<Address, String> e : findStrings(Pattern.compile(sel.substring(2))).entrySet()) {
					Reference[] refs = getReferencesTo(e.getKey());
					if (refs.length == 0) {
						println("NOREF " + e.getKey() + " \"" + e.getValue() + "\"");
					}
					for (Reference r : refs) {
						Function f = getFunctionContaining(r.getFromAddress());
						String why = "\"" + e.getValue() + "\" @" + r.getFromAddress();
						if (f == null) {
							println("NOFUNC " + why);
							continue;
						}
						targets.computeIfAbsent(f, k -> new ArrayList<>()).add(why);
					}
				}
			}
			else {
				Address a = toAddr(Long.decode(sel));
				Function f = getFunctionContaining(a);
				if (f == null) {
					disassemble(a);
					f = createFunction(a, null);
				}
				if (f != null) {
					targets.computeIfAbsent(f, k -> new ArrayList<>()).add(sel);
				}
				else {
					println("NOFUNC " + sel);
				}
			}
		}
		DecompInterface di = new DecompInterface();
		di.openProgram(currentProgram);
		for (Map.Entry<Function, List<String>> e : targets.entrySet()) {
			Function f = e.getKey();
			DecompileResults r = di.decompileFunction(f, 120, monitor);
			String name = f.getEntryPoint().toString().replace("ram:", "");
			try (PrintWriter pw = new PrintWriter(new File(outdir, name + ".c"))) {
				for (String w : e.getValue()) {
					pw.println("// hit: " + w);
				}
				pw.println(r.decompileCompleted() ? r.getDecompiledFunction().getC() : "// FAILED " + r.getErrorMessage());
			}
			println("FUNC " + name + " " + f.getName() + " <- " + String.join(" | ", e.getValue()));
		}
		di.dispose();
	}

	/** Find NUL-terminated ASCII strings matching the regex in initialized memory. */
	private Map<Address, String> findStrings(Pattern p) throws Exception {
		Map<Address, String> res = new LinkedHashMap<>();
		for (MemoryBlock b : currentProgram.getMemory().getBlocks()) {
			if (!b.isInitialized()) {
				continue;
			}
			byte[] buf = new byte[(int) b.getSize()];
			b.getBytes(b.getStart(), buf);
			int start = -1;
			for (int i = 0; i < buf.length; i++) {
				int c = buf[i] & 0xff;
				boolean pr = (c >= 0x20 && c < 0x7f) || c == '\t' || c == '\n';
				if (pr && start < 0) {
					start = i;
				}
				else if (!pr) {
					if (c == 0 && start >= 0 && i - start >= 4) {
						String s = new String(buf, start, i - start, "ISO-8859-1");
						if (p.matcher(s).find()) {
							Address a = b.getStart().add(start);
							Data d = getDataAt(a);
							if (d == null) {
								try {
									createAsciiString(a);
								}
								catch (Exception ignored) {
									// overlapping data; keep going, refs may still exist
								}
							}
							res.put(a, s);
						}
					}
					start = -1;
				}
			}
		}
		return res;
	}
}
