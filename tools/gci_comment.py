#!/usr/bin/env python3
"""Comment every GCI register-field access in a decompilation export (work/decomp/all.c, rom_all.c).

The firmware writes most MAC/PHY fields through the GCI accessor table (DLM 0x02013800.., labelled gci*_p by
tools/ghidra/annotations) or, in the ROM, the accessor functions themselves. Their selectors
(module << 16 | group << 5 | field) mean nothing on sight. This inserts, above each call, the decoded register and
bit range for every constant selector (work/crmap/crmap.json), with the register name from regs.tsv; selectors whose
field or group crmap lacks fall back to the register address or the module and group. Old GCI comment lines are
dropped first, so re-running refreshes them. Usage: gci_comment.py <export.c> [crmap.json] [regs.tsv]"""
import json
import re
import sys

OPS = {0x00: "GetB", 0x04: "GetW", 0x08: "GetD", 0x0c: "Set", 0x1c: "SetTriples", 0x20: "SetPairs",
       0x28: "TxnSetPairs", 0x84: "GetModuleIfc"}
ROMFN = {"gciGetByte": "GetB", "gciGetWord": "GetW", "gciGetDword": "GetD", "gciSet": "Set",
         "gciSetTriples": "SetTriples", "gciSetPairs": "SetPairs", "gciSetPairsTxn": "TxnSetPairs"}
CALL = re.compile(r"\(\*(gci(\w+?)_p|DAT_ram_020138([0-9a-f]{2}))\)\(|\b(" + "|".join(ROMFN) + r")\(")


def load_crmap(path):
    """selector -> (addr, msb, lsb); (module, group) -> addr; module -> (name, base)"""
    fields, groups, mods = {}, {}, {}
    for m in json.load(open(path))["modules"]:
        for mod, base in m["bases"].items():
            mods[int(mod, 16)] = (m.get("name", ""), int(base, 16))
            for g in m["groups"]:
                groups[(int(mod, 16), g["group"])] = int(base, 16) + g["offset"]
                for f in g["fields"]:
                    sel = int(mod, 16) << 16 | g["group"] << 5 | f["field"]
                    fields[sel] = (int(base, 16) + g["offset"], f["msb"], f["lsb"])
    return fields, groups, mods


def describe(v, crmap, names):
    fields, groups, mods = crmap
    mod, grp, fld = v >> 16, (v >> 5) & 0x7ff, v & 0x1f
    if v in fields:
        addr, msb, lsb = fields[v]
        reg = names.get(addr, "")
        return f"{reg + ' ' if reg else ''}{addr:#010x}[{msb}:{lsb}]"
    if (mod, grp) in groups:
        addr = groups[(mod, grp)]
        reg = names.get(addr, "")
        return f"{reg + ' ' if reg else ''}{addr:#010x} field {fld}"
    if mod in mods:
        return f"{mods[mod][0]} (base {mods[mod][1]:#010x}) group {grp} field {fld}"
    return f"selector {v:#x} (module {mod:#x} group {grp} field {fld})"


def args_of(text, start):
    """Top-level arguments of the call whose '(' is at text[start]."""
    depth, cur, out = 0, "", []
    for ch in text[start:]:
        if ch == "(":
            depth += 1
            if depth == 1:
                continue
        elif ch == ")":
            depth -= 1
            if depth == 0:
                out.append(cur.strip())
                return out
        if ch == "," and depth == 1:
            out.append(cur.strip())
            cur = ""
        elif depth >= 1:
            cur += ch
    return None


def lit(s):
    try:
        return int(s, 0)
    except ValueError:
        return None


def selector(expr):
    """(value, suffix) for a selector argument: a literal, or a literal plus a band offset (band * 0x10000, or a
    variable holding it), e.g. "param_1 * 0x10000 + 0x170240" or "iVar2 + 0x1d00c2"; None if no literal part."""
    v = lit(expr)
    if v is not None:
        return v, ""
    depth, cur, terms = 0, "", []
    for ch in expr:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "+" and depth == 0:
            terms.append(cur.strip())
            cur = ""
        else:
            cur += ch
    terms.append(cur.strip())
    nums = [lit(t) for t in terms]
    total = sum(n for n in nums if n is not None)
    others = [t for t, n in zip(terms, nums) if n is None]
    if not total or not others:
        return None
    band = all(re.search(r"\*\s*0x10000$", t) for t in others)
    return total, " (+band)" if band or total >= 0x10000 else " (+?)"


def main():
    path = sys.argv[1]
    crmap = load_crmap(sys.argv[2] if len(sys.argv) > 2 else "work/crmap/crmap.json")
    names = {}
    for line in open(sys.argv[3] if len(sys.argv) > 3 else "tools/ghidra/annotations/regs.tsv"):
        if line[:2] == "0x":
            a, _, n = line.split("\t")[:3]
            names[int(a, 16)] = n
    lines = [l for l in open(path, errors="replace").read().split("\n")
             if not (l.lstrip().startswith("/* GCI ") and l.rstrip().endswith("*/"))]  # refresh: drop old ones
    out, added = [], 0
    for i, line in enumerate(lines):
        m = CALL.search(line)
        if m:
            if m.group(4):
                op = ROMFN[m.group(4)]
            elif m.group(3):
                op = OPS.get(int(m.group(3), 16), f"op{m.group(3)}")
            else:
                op = m.group(2)
            stmt = " ".join(l.strip() for l in lines[i:i + 6])
            k = stmt.find(m.group(0).strip()) + len(m.group(0).strip()) - 1
            args = args_of(stmt, k) or []
            if op in ("SetPairs", "TxnSetPairs", "SetTriples"):
                step = 3 if op == "SetTriples" else 2
                n = lit(args[0]) if args else None
                body = args[1:1 + n] if n else args[1:]
                items = [(body[j], body[j + 1] if step == 2 and j + 1 < len(body) else None)
                         for j in range(0, len(body), step)]
            else:
                items = [(args[0], args[1] if op == "Set" and len(args) > 1 else None)] if args else []
            parts = []
            for sel, val in items:
                sv = selector(sel)
                if sv is None or sv[0] <= 0 or sv[0] >> 16 > 0x60:  # computed or not a selector
                    continue
                parts.append(describe(sv[0], crmap, names) + sv[1] + (f" = {val}" if val else ""))
            if parts:
                indent = line[:len(line) - len(line.lstrip())]
                out.append(f"{indent}/* GCI {op}: " + "; ".join(parts).replace("*/", "* /") + " */")
                added += 1
        out.append(line)
    open(path, "w").write("\n".join(out))
    print(f"{path}: {added} GCI calls commented")


if __name__ == "__main__":
    main()
