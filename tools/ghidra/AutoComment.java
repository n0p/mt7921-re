// Automatic context for every function: append "[auto]" lines to its plate comment naming the source files its
// assert/trace calls cite (MediaTek strings such as "wifi_core/wificore_rlm_ra_wrapper.c") and the first log formats
// it prints. Runs after Annotate.java; old [auto] lines are replaced, manual text is kept.
// Script args: none
// @category MT7961

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.symbol.Reference;

public class AutoComment extends GhidraScript {

	private static final int MAXLOG = 3, MAXLEN = 100;

	@Override
	protected void run() throws Exception {
		int tagged = 0;
		for (Function f : currentProgram.getFunctionManager().getFunctions(true)) {
			monitor.checkCancelled();
			Set<String> src = new LinkedHashSet<>(), logs = new LinkedHashSet<>();
			for (Instruction in : currentProgram.getListing().getInstructions(f.getBody(), true)) {
				for (Reference r : in.getReferencesFrom()) {
					String s = stringAt(r.getToAddress());
					if (s == null) {
						continue;
					}
					if (s.matches("[\\w./-]+\\.(c|h|cpp)")) {
						src.add(s);
					}
					else if (s.length() >= 4 && logs.size() < MAXLOG) {
						logs.add(s.length() > MAXLEN ? s.substring(0, MAXLEN) + "..." : s);
					}
				}
			}
			List<String> lines = new ArrayList<>();
			String old = f.getComment();
			if (old != null) {
				for (String l : old.split("\n")) {
					if (!l.startsWith("[auto]")) {
						lines.add(l);
					}
				}
			}
			if (!src.isEmpty()) {
				lines.add("[auto] source: " + String.join(", ", src));
			}
			for (String l : logs) {
				lines.add("[auto] log: \"" + l.replace("\n", "\\n").replace("*/", "* /") + "\"");
			}
			String c = String.join("\n", lines).strip();
			if (!c.equals(old == null ? "" : old.strip())) {
				f.setComment(c.isEmpty() ? null : c);
			}
			if (!src.isEmpty() || !logs.isEmpty()) {
				tagged++;
			}
		}
		println("auto-commented " + tagged + " functions");
	}

	private String stringAt(Address a) {
		Data d = getDataAt(a);
		if (d == null || !d.hasStringValue()) {
			return null;
		}
		Object v = d.getValue();
		return v == null ? null : v.toString().strip();
	}
}
