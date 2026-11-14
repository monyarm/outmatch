"""Find functions in the game that came straight out of a static library, by matching library objects.

A statically linked library (.lib) is an archive of COFF objects.  Visual C++ 6 builds them with
function-level sections (/Gy), so each function is one section with its own bytes and relocations.
The project's own target objects (delinked from the game) have the same shape.  A function the game
did not write is byte-identical to the library's copy except for the four-byte (or two-byte section)
fields a relocation fills in, so masking those bytes and comparing size + bytes + the relocation
offset/type list identifies it.  A signature that occurs once in the library is high confidence; a
signature that occurs several times is only accepted when every copy has the same symbol name
(LIBCMT and LIBCPMT both carry some of the same bits) and the function is not tiny.

    python -m harness.libmatch [--libs DIR] [--write] [--verbose]

--write merges the hits into config/casefile1/set_aside.json (symbol -> reason) and adds the real
library names to functions.json's "names".  Nothing is measured until the app syncs; decompdecomp
reads set_aside.json on every sync, and the functions it lists leave the progress denominator.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from . import common

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # tools/, for rebuild.py's COFF reader

LIB_DIR_DEFAULT = Path("/tmp/opencode/vs60/vc_studio/main/VC98/Lib")
# the static C and C++ runtimes, plus the compatibility names library VC6 links by default
DEFAULT_LIBS = ("LIBCMT.LIB", "LIBCPMT.LIB", "OLDNAMES.LIB")
MIN_UNIQUE = 10       # a unique signature shorter than this is still coincidence-prone
MIN_SHARED = 24       # a signature several libraries repeat needs more bytes to be believable
WIDTH = {0x0A: 2}     # a section-relative reference is two bytes; everything else here is four


def archive(path: Path):
    """(member name, bytes) for every real member of a Microsoft COFF archive."""
    b = path.read_bytes()
    if b[:8] != b"!<arch>\n":
        raise ValueError(f"{path} is not a COFF archive")
    off, longnames, out = 8, b"", []
    while off + 60 <= len(b):
        h = b[off:off + 60]
        name = h[:16].decode("latin-1").rstrip()
        try:
            size = int(h[48:58].decode().strip() or 0)
        except ValueError:
            break
        data = b[off + 60:off + 60 + size]
        off += 60 + size + (size & 1)
        if name == "//":
            longnames = data
            continue
        if name in ("/", "/SYM64/") or not data:
            continue
        if m := re.match(r"/(\d+)$", name):
            name = longnames[int(m.group(1)):].split(b"\0")[0].decode("latin-1")
        out.append((name.rstrip("/"), data))
    return out


def object_functions(obj: bytes):
    """Every function an object defines: name, size, bytes, function-relative (offset, type) relocs."""
    from rebuild import read_coff

    sections, symbols = read_coff(obj)
    out = []
    for name, value, section, typ in (s for s in symbols if s):
        if not (typ & 0x20) or not 0 < section <= len(sections):
            continue
        data = sections[section - 1].get("data") or b""
        end = len(data)
        for other in symbols:
            if other and other[2] == section and (other[3] & 0x20) and other[1] > value:
                end = min(end, other[1])
        if end <= value:
            end = len(data)
        relocs = [(o - value, t) for o, _, t in sections[section - 1]["relocs"] if value <= o < end]
        out.append({"name": name, "size": end - value, "data": data[value:end], "relocs": relocs})
    return out


def signature(size: int, data: bytes, relocs):
    masked = bytearray(data)
    for offset, kind in relocs:
        if not kind:
            continue
        width = WIDTH.get(kind, 4)
        for k in range(offset, min(offset + width, len(masked))):
            masked[k] = 0
    return size, bytes(masked), tuple(relocs)


def library_index(lib_dir: Path, names):
    """signature -> [(lib, member, symbol)], over every object of the libraries that exist."""
    index = defaultdict(list)
    files = 0
    for lib in names:
        path = lib_dir / lib
        if not path.is_file():
            continue
        files += 1
        for member, data in archive(path):
            if not member.lower().endswith(".obj"):
                continue
            try:
                functions = object_functions(data)
            except Exception:
                continue
            for function in functions:
                index[signature(function["size"], function["data"], function["relocs"])].append(
                    (lib, member, function["name"]))
    return index, files


def target_functions():
    """(unit, entry, function) for every function of every delinked target object."""
    from rebuild import read_coff

    info = common.functions()
    for unit, entries in info["units"].items():
        path = common.target_obj(unit)
        if not path.is_file():
            continue
        sections, symbols = read_coff(path.read_bytes())
        where = {}
        for name, value, section, typ in (s for s in symbols if s):
            if (typ & 0x20) and section > 0:  # a definition, not the extern reference beside it
                where.setdefault(name, (section, value))
        for entry in entries:
            if entry["name"] not in where:
                continue
            section, value = where[entry["name"]]
            sec = sections[section - 1]
            data = (sec.get("data") or b"")[value:]
            size = int(entry.get("size") or len(data))
            relocs = [(o - value, t) for o, _, t in sec["relocs"] if value <= o < value + size]
            yield unit, entry, {"name": entry["name"], "size": size, "data": data[:size], "relocs": relocs}


def clean(symbol: str) -> str:
    """A library symbol as a C name: _malloc -> malloc, __nh_malloc -> _nh_malloc, _RtlUnwind@16 -> RtlUnwind."""
    if symbol.startswith("?"):
        return symbol
    # one leading underscore is the C decoration; a second one is part of the CRT's own internal name (__nh_malloc)
    name = symbol[1:] if symbol.startswith("_") else symbol
    return re.sub(r"@\d+$", "", name) or symbol


def member_name(member: str) -> str:
    return re.split(r"[\\/]", member)[-1]


def scan(lib_dir: Path = LIB_DIR_DEFAULT, libs=DEFAULT_LIBS):
    index, files = library_index(lib_dir, libs)
    targets = list(target_functions())
    shared = defaultdict(list)  # a signature two target functions share is a common body, not a library function
    for unit, entry, function in targets:
        shared[signature(function["size"], function["data"], function["relocs"])].append(entry["name"])
    hits, ambiguous = [], []
    for unit, entry, function in targets:
        sig = signature(function["size"], function["data"], function["relocs"])
        found = index.get(sig)
        if not found:
            continue
        symbols = {sym for _, _, sym in found}
        unique = len(found) == 1
        if len(shared[sig]) > 1:
            ambiguous.append({"unit": unit, "addr": entry["addr"], "name": entry["name"],
                              "size": function["size"], "why": "shared-body", "candidates": found})
            continue
        if unique and function["size"] < MIN_UNIQUE:
            ambiguous.append({"unit": unit, "addr": entry["addr"], "name": entry["name"],
                              "size": function["size"], "why": "tiny", "candidates": found})
            continue
        if not unique and (len(symbols) > 1 or function["size"] < MIN_SHARED):
            ambiguous.append({"unit": unit, "addr": entry["addr"], "name": entry["name"],
                              "size": function["size"], "why": "duplicate", "candidates": found})
            continue
        lib, member, symbol = found[0]
        hits.append({"unit": unit, "addr": entry["addr"], "name": entry["name"], "size": function["size"],
                     "lib": lib, "member": member_name(member), "symbol": symbol,
                     "confidence": "unique" if unique else "same-symbol"})
    return hits, ambiguous, files, len(index)


def tail_range(hits) -> int | None:
    """Where the library's block at the end of .text starts: the address after the largest gap between hits.

    The game's own code has no library functions in it, so its hits form one dense block at the end; the widest
    gap between consecutive hits is the boundary between that block and a stray hit (none today). Requires the
    block to hold at least 80% of the hits, so a scattered result never turns into a set-aside range."""
    addrs = sorted({h["addr"] for h in hits})
    if len(addrs) < 20:
        return None
    # index 0 is a candidate too: with no stray hits the block starts at the first one, and the widest gap is the
    # one before it. (Starting at 1 silently moved the boundary to the second hit.)
    best = max(range(len(addrs)), key=lambda i: addrs[i] - (addrs[i - 1] if i else 0))
    return addrs[best] if len(addrs) - best >= 0.8 * len(addrs) else None


def write(hits, range_from: int | None = None) -> dict:
    """Merge hits into set_aside.json and functions.json's names; returns what changed.

    Two kinds of entry: each proven function by symbol with the library object it came from, and one `ranges`
    entry for the whole library block at the end of .text (`tail_range`), so the rest of that block -- functions
    of the same library this build of it doesn't reproduce byte for byte -- is set aside with a reason too."""
    aside_path = common.CONFIG / "set_aside.json"
    aside = json.loads(aside_path.read_text()) if aside_path.is_file() else {}
    info = common.functions()
    names = info.setdefault("names", {})
    added_aside, added_names, kept = 0, 0, 0
    for hit in hits:
        # names first: a set-aside entry written by an earlier run must not stop a stale name being repaired
        key = f"func_{hit['addr']:08X}"
        new = clean(hit["symbol"])
        # what an older version of this tool wrote (it stripped every leading underscore)
        old = re.sub(r"@\d+$", "", hit["symbol"].lstrip("_")) or hit["symbol"]
        if key not in names or names[key] == old:  # a name the project gave it wins
            if new not in {v for k, v in names.items() if k != key}:  # never map two addresses to one name
                if names.get(key) != new:
                    names[key] = new
                    added_names += 1
        if hit["name"] in aside:
            kept += 1
            continue
        aside[hit["name"]] = (f"MSVC 6.0 static runtime — {hit['lib']} ({hit['member']}): "
                              f"{hit['symbol']}")
        added_aside += 1
    start = range_from if range_from is not None else tail_range(hits)
    end = max((fn["addr"] + fn["size"] for unit in info["units"].values() for fn in unit), default=0)
    if start is not None:
        ranges = [r for r in (aside.get("ranges") or []) if not str(r.get("why", "")).startswith("MSVC 6.0")]
        ranges.append({"from": f"0x{start:08X}", "to": f"0x{end:08X}",
                       "why": "MSVC 6.0 static runtime — the image's CRT tail from _onexit to the end of .text "
                              "(heap/SBH, exit and error reporting, stdio, locale/NLS, C++ EH and startup); the "
                              "functions listed by symbol here are byte-identical to LIBCMT.LIB objects, the rest "
                              "is the same library as this build of it"})
        aside["ranges"] = ranges
    aside_path.write_text(json.dumps(dict(sorted(aside.items())), indent=1) + "\n")
    info["names"] = dict(sorted(names.items()))
    common.save_functions(info)
    return {"set_aside": len([k for k, v in aside.items() if isinstance(v, str)]),
            "added_aside": added_aside, "already": kept, "range": None if start is None else
            f"0x{start:08X}-0x{end:08X}", "added_names": added_names, "names": len(names)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--libs", default=str(LIB_DIR_DEFAULT), help="folder with VC98/LIB")
    parser.add_argument("--only", default=",".join(DEFAULT_LIBS), help="comma-separated library names")
    parser.add_argument("--write", action="store_true", help="update set_aside.json and functions.json")
    parser.add_argument("--range-from", default="", help="hex address the library block at the end starts at")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    libs = [x.strip() for x in args.only.split(",") if x.strip()]
    hits, ambiguous, files, signatures = scan(Path(args.libs), libs)
    print(json.dumps({"libraries": files, "signatures": signatures, "hits": len(hits),
                      "ambiguous": len(ambiguous),
                      "by_lib": {lib: sum(1 for h in hits if h["lib"] == lib) for lib in libs}}, indent=None))
    if args.verbose:
        for hit in sorted(hits, key=lambda h: h["addr"]):
            print(f"0x{hit['addr']:08X} {hit['size']:5d} {hit['name']:<24} <- {hit['lib']}:{hit['member']}:{hit['symbol']}")
    out = common.STATE / "libmatch.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"hits": hits, "ambiguous": ambiguous}, indent=1))
    if args.write:
        print(json.dumps(write(hits, int(args.range_from, 0) if args.range_from else None)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
