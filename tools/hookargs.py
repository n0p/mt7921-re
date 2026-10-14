#!/usr/bin/env python3
"""Translate fw/hooks.def into mtfw_patch.py byte-patch arguments using symbol addresses from `nm`.

Usage: hookargs.py <hooks.def> <nm output file> [more .def files...]
Kinds:
  ptr <download addr> <symbol>   write the symbol's address (u32 LE) at addr
  bin <download addr> <file>     write the file's bytes at addr (assembled patches, see fw/asm/)
"""
import struct
import sys
from pathlib import Path


def main():
    syms = {}
    for line in Path(sys.argv[2]).read_text().splitlines():
        parts = line.split()
        if len(parts) == 3:
            syms[parts[2]] = int(parts[0], 16)
    args = []
    for deffile in [sys.argv[1]] + sys.argv[3:]:
        for ln, line in enumerate(Path(deffile).read_text().splitlines(), 1):
            line = line.split("#", 1)[0].split()
            if not line:
                continue
            kind, addr, arg = line
            if kind == "bin":
                args.append(f"{int(addr, 0):#010x}={Path(arg).read_bytes().hex()}")
                continue
            if kind != "ptr":
                sys.exit(f"{deffile}:{ln}: unknown hook kind {kind}")
            if arg not in syms:
                sys.exit(f"{deffile}:{ln}: undefined symbol {arg}")
            args.append(f"{int(addr, 0):#010x}={struct.pack('<I', syms[arg]).hex()}")
    print(" ".join(args))


if __name__ == "__main__":
    main()
