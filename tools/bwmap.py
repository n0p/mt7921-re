#!/usr/bin/env python3
"""Call-graph walk over the Ghidra decompiles (work/decomp/all.c RAM + rom_all.c ROM) collecting PHY/RF writes.

Usage: bwmap.py <root addr>... [--depth N] [--skip addr,addr] [--list]
Resolves direct FUN_ram_XXXXXXXX calls and indirect calls through the ROM API table (*DAT_ram_00822xxx), whose
slots are read from the ROM dump. Prints, per reachable function, the lines that write 0x830xxxxx registers
(direct, negative-constant bases, &DAT_ram_830.. pointers) and the A-die RF register writes
(FUN_ram_0083a66c / API 0x8229a8: reg, value, path).
"""
import re
import struct
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROM = (ROOT / "work/rom/rom_800000_512k.bin").read_bytes()
ROM_BASE = 0x800000
# generic helpers that only add noise (logging, memset/memcpy, locks, delays)
NOISE = {0x9217ba, 0x921898, 0x915918, 0x921a32, 0x915924, 0x9159b6}

HDR = re.compile(r"^//@@ ram:([0-9a-f]{8}) (\S+)")
CALL = re.compile(r"\bFUN_ram_([0-9a-f]{8})\b")
APICALL = re.compile(r"\(\*DAT_ram_(00822[0-9a-f]{3})\)")
REG830 = re.compile(r"DAT_ram_830[0-9a-f]{5}|-0x7(?:d000000|cf[0-9a-f]{5})\b|0x830[0-9a-f]{5}\b")
RFW = re.compile(r"(?:FUN_ram_0083a66c|\(\*(?:DAT_ram_008229a8|pcVar\d+)\))\s*\(")


def parse(path):
    funcs, cur, name = {}, None, None
    for line in Path(path).read_text(errors="replace").splitlines():
        m = HDR.match(line)
        if m:
            cur, name = int(m.group(1), 16), m.group(2)
            funcs[cur] = [name, []]
            continue
        if cur is not None:
            funcs[cur][1].append(line)
    return funcs


def api_target(slot):
    a = int(slot, 16)
    if ROM_BASE <= a < ROM_BASE + len(ROM) - 4:
        return struct.unpack_from("<I", ROM, a - ROM_BASE)[0]
    return None


def neg_to_addr(tok):
    if tok.startswith("-0x"):
        return (0x100000000 - int(tok[1:], 16)) & 0xffffffff
    if tok.startswith("0x"):
        return int(tok, 16)
    return int(tok.split("_")[-1], 16)


def main():
    args = sys.argv[1:]
    depth = 6
    skip = set(NOISE)
    roots, list_only = [], False
    i = 0
    while i < len(args):
        if args[i] == "--depth":
            depth = int(args[i + 1]); i += 2; continue
        if args[i] == "--skip":
            skip |= {int(x, 16) for x in args[i + 1].split(",")}; i += 2; continue
        if args[i] == "--list":
            list_only = True; i += 1; continue
        roots.append(int(args[i], 16)); i += 1
    funcs = parse(ROOT / "work/decomp/all.c")
    rom = parse(ROOT / "work/decomp/rom_all.c")
    for k, v in rom.items():
        funcs.setdefault(k, v)
    seen, order = {}, []
    q = deque((r, 0, None) for r in roots)
    while q:
        a, d, parent = q.popleft()
        if a in seen or a in skip or a not in funcs:
            continue
        seen[a] = (d, parent)
        order.append(a)
        if d >= depth:
            continue
        body = "\n".join(funcs[a][1])
        for m in CALL.finditer(body):
            q.append((int(m.group(1), 16), d + 1, a))
        for m in APICALL.finditer(body):
            t = api_target(m.group(1))
            if t:
                q.append((t, d + 1, a))
    for a in order:
        name, body = funcs[a]
        hits = []
        for ln in body:
            s = ln.strip()
            if REG830.search(s) or RFW.search(s):
                hits.append(s)
        if list_only:
            print(f"{a:08x} d{seen[a][0]} {name} hits={len(hits)} parent={seen[a][1] and hex(seen[a][1])}")
            continue
        if hits:
            print(f"== {a:08x} {name} (depth {seen[a][0]}, from {seen[a][1] and hex(seen[a][1])})")
            for h in hits:
                print("   ", h[:220])


if __name__ == "__main__":
    main()
