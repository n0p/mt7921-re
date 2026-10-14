// Decompile every function into one file, each prefixed with a "//@@ <addr> <name>" marker line for grepping.
// Script args: <output file>
// @category MT7961

import java.io.PrintWriter;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;

public class DecompAll extends GhidraScript {

	@Override
	protected void run() throws Exception {
		DecompInterface di = new DecompInterface();
		di.openProgram(currentProgram);
		int ok = 0, fail = 0;
		try (PrintWriter pw = new PrintWriter(getScriptArgs()[0])) {
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
