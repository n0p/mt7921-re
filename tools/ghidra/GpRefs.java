// Create memory references for every gp-relative access (addi.gp, lwi.gp, ..., and EX9 entries using gp).
// Works on p-code: any INT_ADD of the gp register and a constant yields target = GP + const.
// Script args: <gp hex>
// @category MT7961

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.lang.Register;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.pcode.PcodeOp;
import ghidra.program.model.pcode.Varnode;
import ghidra.program.model.symbol.RefType;
import ghidra.program.model.symbol.ReferenceManager;
import ghidra.program.model.symbol.SourceType;

public class GpRefs extends GhidraScript {

	@Override
	protected void run() throws Exception {
		long gp = Long.decode(getScriptArgs()[0]);
		Register gpReg = currentProgram.getRegister("gp");
		ReferenceManager rm = currentProgram.getReferenceManager();
		int n = 0;
		InstructionIterator it = currentProgram.getListing().getInstructions(true);
		while (it.hasNext()) {
			monitor.checkCancelled();
			Instruction ins = it.next();
			PcodeOp[] ops = ins.getPcode();
			for (int i = 0; i < ops.length; i++) {
				PcodeOp op = ops[i];
				if (op.getOpcode() != PcodeOp.INT_ADD) {
					continue;
				}
				Varnode a = op.getInput(0), b = op.getInput(1);
				Varnode c = isGp(a, gpReg) && b.isConstant() ? b : isGp(b, gpReg) && a.isConstant() ? a : null;
				if (c == null) {
					continue;
				}
				Address target = toAddr((gp + c.getOffset()) & 0xffffffffL);
				if (currentProgram.getMemory().getBlock(target) == null) {
					continue;
				}
				RefType type = RefType.DATA;
				for (int j = i + 1; j < ops.length; j++) {
					if (ops[j].getOpcode() == PcodeOp.LOAD && ops[j].getInput(1).equals(op.getOutput())) {
						type = RefType.READ;
					}
					else if (ops[j].getOpcode() == PcodeOp.STORE && ops[j].getInput(1).equals(op.getOutput())) {
						type = RefType.WRITE;
					}
				}
				int opIdx = Math.max(0, ins.getNumOperands() - 1);
				rm.addMemoryReference(ins.getAddress(), target, type, SourceType.ANALYSIS, opIdx);
				n++;
			}
		}
		println("gp refs created: " + n);
	}

	private static boolean isGp(Varnode v, Register gp) {
		return v.isRegister() && v.getAddress().equals(gp.getAddress()) && v.getSize() == gp.getMinimumByteSize();
	}
}
