// Apply the project's annotations (tools/ghidra/annotations/*.tsv) to the current program, so the decompilation
// (DecompAll.java) carries what the notes document. One entry per line, tab-separated:
//   <addr> <kind> <name> <comment>
//   kind func  : name (or "-") the function at addr, creating it if needed; comment = its plate comment
//   kind reg   : 32-bit MMIO register: an uninitialized volatile 4 KB block is mapped around it if needed, a dword
//                defined at it and the primary label set, so the decompiler prints PHY_BN_MODE = x (a label on
//                undefined bytes prints as _PHY_BN_MODE); comment = repeatable comment, listed in the export's
//                register glossary
//   kind label : primary label for code/data; comment as for reg
//   kind pre / eol : comment at an instruction (shown inline by the decompiler for pre)
// "\n" in a comment is a line break. Lines starting with # and blank lines are skipped.
// Script args: <tsv file>...
// @category MT7961

import java.io.File;
import java.nio.file.Files;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.CommentType;
import ghidra.program.model.listing.Function;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.SourceType;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolTable;

public class Annotate extends GhidraScript {

	@Override
	protected void run() throws Exception {
		int funcs = 0, labels = 0, comments = 0, bad = 0;
		for (String path : getScriptArgs()) {
			int n = 0;
			for (String line : Files.readAllLines(new File(path).toPath())) {
				n++;
				if (line.isBlank() || line.startsWith("#")) {
					continue;
				}
				String[] f = line.split("\t", 4);
				if (f.length < 3) {
					println(path + ":" + n + ": need addr, kind, name");
					bad++;
					continue;
				}
				Address a = toAddr(Long.decode(f[0].trim()));
				String kind = f[1].trim(), name = f[2].trim();
				String comment = f.length > 3 ? f[3].trim().replace("\\n", "\n") : "";
				try {
					switch (kind) {
						case "func" -> {
							Function fn = getFunctionAt(a);
							if (fn == null) {
								fn = createFunction(a, null);
							}
							if (fn == null) {
								println(path + ":" + n + ": no function at " + a);
								bad++;
								continue;
							}
							if (!name.equals("-")) {
								fn.setName(name, SourceType.USER_DEFINED);
							}
							if (!comment.isEmpty()) {
								fn.setComment(comment);
							}
							funcs++;
						}
						case "reg", "label" -> {
							if (kind.equals("reg")) {
								if (currentProgram.getMemory().getBlock(a) == null) {
									mapPage(a);
								}
								if (!(getDataAt(a) != null && getDataAt(a).getLength() == 4)) {
									clearListing(a, a.add(3));
									createDWord(a);
								}
							}
							setLabel(a, name);
							if (!comment.isEmpty()) {
								currentProgram.getListing().setComment(a, CommentType.REPEATABLE, comment);
							}
							labels++;
						}
						case "pre", "eol" -> {
							currentProgram.getListing()
									.setComment(a, kind.equals("pre") ? CommentType.PRE : CommentType.EOL, comment);
							comments++;
						}
						default -> {
							println(path + ":" + n + ": unknown kind " + kind);
							bad++;
						}
					}
				}
				catch (Exception e) {
					println(path + ":" + n + ": " + a + " " + name + ": " + e.getMessage());
					bad++;
				}
			}
		}
		println("annotated: " + funcs + " functions, " + labels + " labels, " + comments + " comments, " + bad +
			" problems");
	}

	private void setLabel(Address a, String name) throws Exception {
		SymbolTable st = currentProgram.getSymbolTable();
		for (Symbol s : st.getSymbols(a)) {
			if (s.getName().equals(name)) {
				s.setPrimary();
				return;
			}
		}
		Symbol s = st.createLabel(a, name, SourceType.USER_DEFINED);
		s.setPrimary();
	}

	// MMIO registers sit outside the image's blocks. An uninitialized 4 KB block per register page gives the data type
	// a home. It must be volatile: as plain memory the decompiler drops "dead" stores, e.g. the second of two
	// back-to-back register writes (contiTxStart's 0x83081044 = 0x10, then 0x30). Volatile accesses still print as
	// assignments; unlabeled addresses in such a page print as RamXXXXXXXX.
	private void mapPage(Address a) throws Exception {
		Address p = toAddr(a.getOffset() & ~0xfffL);
		if (currentProgram.getMemory().getBlock(p) != null) {
			return;
		}
		MemoryBlock b = currentProgram.getMemory()
				.createUninitializedBlock(String.format("mmio_%08x", p.getOffset()), p, 0x1000, false);
		b.setRead(true);
		b.setWrite(true);
		b.setVolatile(true);
	}
}
