// Decompile every function into one file, each prefixed with a "//@@ <addr> <name>" marker line for grepping.
// The file opens with a register glossary: every MMIO label that Annotate.java placed (user-defined labels in the
// mmio_* blocks) with its comment. Function plate comments and PRE comments are included in the C.
// Script args: <output file>
// @category MT7961

import java.io.PrintWriter;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileOptions;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.CommentType;
import ghidra.program.model.listing.Function;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.SourceType;
import ghidra.program.model.symbol.Symbol;

public class DecompAll extends GhidraScript {

	@Override
	protected void run() throws Exception {
		DecompileOptions opt = new DecompileOptions();
		opt.setHeadCommentIncluded(true);
		opt.setPRECommentIncluded(true);
		DecompInterface di = new DecompInterface();
		di.setOptions(opt);
		di.openProgram(currentProgram);
		int ok = 0, fail = 0;
		try (PrintWriter pw = new PrintWriter(getScriptArgs()[0])) {
			pw.println("/* " + currentProgram.getName() + ": Ghidra decompilation, annotated from tools/ghidra/annotations");
			pw.println(" * (Annotate.java: names and comments; AutoComment.java: [auto] source files and log formats).");
			pw.println(" * GCI accessor calls carry a decoded comment (tools/gci_comment.py; '+band' = the band offset is added).");
			pw.println(" * 'Bad instruction / halt_baddata()': the NDS32 SLEIGH cannot follow ifcall9/ifret16 inline calls, so the C");
			pw.println(" * stops there; the disassembly (work/dis/*.txt) and some plate comments give the rest.");
			pw.println(" * Look a function up by address: tools/fshow.sh 0xADDR. Register glossary:");
			for (MemoryBlock b : currentProgram.getMemory().getBlocks()) {
				if (!b.getName().startsWith("mmio_")) {
					continue;
				}
				for (Symbol s : currentProgram.getSymbolTable().getSymbols(new ghidra.program.model.address.AddressSet(
					b.getStart(), b.getEnd()), ghidra.program.model.symbol.SymbolType.LABEL, true)) {
					if (s.getSource() != SourceType.USER_DEFINED) {
						continue;
					}
					String c = currentProgram.getListing().getComment(CommentType.REPEATABLE, s.getAddress());
					pw.printf(" *   %-26s 0x%08x  %s%n", s.getName(), s.getAddress().getOffset(),
						c == null ? "" : c.replace("\n", " ").replace("*/", "* /"));
				}
			}
			pw.println(" */");
			pw.println();
			for (Function f : currentProgram.getFunctionManager().getFunctions(true)) {
				monitor.checkCancelled();
				DecompileResults r = di.decompileFunction(f, 60, monitor);
				pw.println("//@@ " + f.getEntryPoint() + " " + f.getName());
				if (r.decompileCompleted()) {
					pw.println(r.getDecompiledFunction().getC());
					ok++;
				}
				else {
					pw.println("// FAILED: " + r.getErrorMessage());
					fail++;
				}
			}
		}
		di.dispose();
		println("decompiled " + ok + ", failed " + fail);
	}
}
