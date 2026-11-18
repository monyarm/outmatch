"""Open functions no C source can be: compiler-made thunks and stubs, and padding taken for a function.

  adjustor thunk  sub ecx,N (or add ecx,-N); jmp <method>: the this-adjustment MSVC synthesises for a method reached
                  through a secondary base of a multiple-inheritance class
  jump stub       a lone jmp into the middle of another function (an EH continuation or a shared tail): no C function
                  jumps into another one's body
  padding         nothing but int3/nop: alignment fill the analysis took for a function

Each is read from the original program (functions.json's exe) and, with --write, set aside with that reason in
set_aside.json and in the app (/api/setaside), as was done by hand for Case File #1.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys

from . import common

ADJUST = re.compile(rb"(\x83\xe9(.)|\x81\xe9(....)|\x83\xc1(.)|\x81\xc1(....))\xe9(....)", re.S)


def program(info: dict):
    """address -> bytes reader for the project's .exe (its sections by virtual address)."""
    exe = (common.ROOT / info["exe"]).read_bytes()
    at = struct.unpack_from("<I", exe, 0x3C)[0]
    count, opt = struct.unpack_from("<H", exe, at + 6)[0], struct.unpack_from("<H", exe, at + 20)[0]
    base = struct.unpack_from("<I", exe, at + 52)[0]
    secs = []
    for i in range(count):
        _, va, size, off = struct.unpack_from("<IIII", exe, at + 24 + opt + 40 * i + 8)
        secs.append((base + va, size, off))

    def read(addr: int, n: int) -> bytes:
        for va, size, off in secs:
            if va <= addr < va + size:
                return exe[off + addr - va: off + addr - va + n]
        return b""
    return read


def scan() -> list[dict]:
    info = common.functions()
    read = program(info)
    fns = sorted((f for unit in info["units"].values() for f in unit), key=lambda f: f["addr"])
    starts = {f["addr"]: f for f in fns}

    def owner(addr):
        for f in fns:  # (small lists of candidates: a linear look is fine)
            if f["addr"] < addr < f["addr"] + f["size"]:
                return f
        return None

    open_ = {n for (n,) in common.query("SELECT name FROM functions WHERE project_id=? AND status='open'",
                                        common.project_id())}
    out = []
    for f in fns:
        if f["name"] not in open_ or f["size"] > 16:
            continue
        b = read(f["addr"], f["size"])
        if b and all(c in (0xCC, 0x90) for c in b):
            out.append({"name": f["name"], "kind": "padding",
                        "why": f"Alignment padding, not a function — {len(b)} bytes of int3/nop the analysis took "
                               f"for one; there is no code to write"})
            continue
        if m := ADJUST.fullmatch(b):
            if m.group(2) is not None:
                n = m.group(2)[0]
            elif m.group(3) is not None:
                n = struct.unpack("<i", m.group(3))[0]
            elif m.group(4) is not None:
                n = -struct.unpack("<b", m.group(4))[0]
            else:
                n = -struct.unpack("<i", m.group(5))[0]
            target = f["addr"] + len(b) + struct.unpack("<i", m.group(6))[0]
            t = starts.get(target)
            if n > 0 and t:
                out.append({"name": f["name"], "kind": "adjustor",
                            "why": f"MSVC adjustor thunk — sub ecx,0x{n:x}; jmp {t['name']} (this-adjustment to a "
                                   f"base at offset 0x{n:x} of a multiple-inheritance class); compiler-synthesised, "
                                   f"no C body emits it"})
            continue
        if len(b) == 5 and b[0] == 0xE9:
            target = f["addr"] + 5 + struct.unpack("<i", b[1:])[0]
            if target not in starts and (o := owner(target)):
                out.append({"name": f["name"], "kind": "jump-stub",
                            "why": f"Compiler-generated jump stub — jmp 0x{target:X}, into the middle of {o['name']} "
                                   f"(offset {target - o['addr']}); no C function can jump into another function's "
                                   f"body (an EH continuation or shared tail)"})
    return out


def write(found: list[dict]) -> dict:
    path = common.CONFIG / "set_aside.json"
    aside = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    added = 0
    for x in found:
        if x["name"] not in aside:
            aside[x["name"]] = x["why"]
            added += 1
    path.write_text(json.dumps(dict(sorted(aside.items())), indent=1) + "\n", encoding="utf-8")
    done = 0
    for x in found:
        try:
            common.api("/api/setaside", {"slug": common.SLUG, "name": x["name"], "why": x["why"]})
            done += 1
        except common.OutmatchError as e:
            print(json.dumps({"name": x["name"], "error": str(e)[:200]}))
    return {"added_to_set_aside_json": added, "set_aside_in_app": done}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--write", action="store_true", help="set them aside (set_aside.json and the app)")
    args = p.parse_args(argv)
    found = scan()
    for x in found:
        print(json.dumps(x))
    kinds: dict[str, int] = {}
    for x in found:
        kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1
    print(json.dumps({"found": len(found), **kinds}))
    if args.write and found:
        print(json.dumps(write(found)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
