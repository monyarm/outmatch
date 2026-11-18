"""Find functions in the game that came straight out of a static library, by matching library objects.

A statically linked library (.lib) is an archive of COFF objects.  Visual C++ 6 builds them with
function-level sections (/Gy), so each function is one section with its own bytes and relocations.
The project's own target objects (delinked from the game) have the same shape.  A function the game
did not write is byte-identical to the library's copy except for the four-byte (or two-byte section)
fields a relocation fills in, so masking those bytes and comparing size + bytes + the relocation
offset/type list identifies it.  A signature that occurs once in the library is high confidence; a
signature that occurs several times is only accepted when every copy has the same symbol name
(LIBCMT and LIBCPMT both carry some of the same bits) and the function is not tiny.

    python -m harness.libmatch [--libs [BUILD=]DIR ...] [--only all|A.LIB,B.LIB] [--write] [--verbose]

Several --libs (one per Visual C++ build: 8168=.../msvc-6.00-8168/LIB, ...) are indexed together; a function
every build carries unchanged counts once, and its hit lists the builds that have it.

--write merges the hits into config/<version>/set_aside.json (symbol -> reason) and adds the real
library names to functions.json's "names".  Nothing is measured until the app syncs; decompdecomp
reads set_aside.json on every sync, and the functions it lists leave the progress denominator.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from . import common

sys.path.insert(0, str(common.ROOT / "tools"))  # the project's tools/, for rebuild.py's COFF reader

LIB_DIR_DEFAULT = Path(os.environ.get("OUTMATCH_LIBS", "/tmp/opencode/vs60/vc_studio/main/VC98/Lib"))
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


def library_files(lib_dir: Path, names):
    """(name, path) of each library in `lib_dir` (any case), or every .lib there for names == ["all"]."""
    found = {p.name.upper(): p for p in lib_dir.iterdir()
             if p.suffix.lower() in (".lib", ".obj")} if lib_dir.is_dir() else {}  # (.obj: a library built here)
    return sorted(found.items()) if names == ["all"] else [(n.upper(), found[n.upper()]) for n in names
                                                           if n.upper() in found]


def library_index(lib_dirs, names):
    """signature -> [(build, lib, member, symbol)], over every object of the libraries that exist."""
    index = defaultdict(list)
    files = 0
    for build, lib_dir in lib_dirs:
        for lib, path in library_files(Path(lib_dir), names):
            try:
                members = [(path.name, path.read_bytes())] if path.suffix.lower() == ".obj" else archive(path)
            except ValueError:
                continue  # OMF or an import library's stub, not MS COFF
            files += 1
            for member, data in members:
                if not member.lower().endswith(".obj"):
                    continue
                try:
                    functions = object_functions(data)
                except Exception:
                    continue
                for function in functions:
                    index[signature(function["size"], function["data"], function["relocs"])].append(
                        (build, lib, member_name(member), function["name"]))
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


def scan(lib_dirs=(("SP5", LIB_DIR_DEFAULT),), libs=DEFAULT_LIBS):
    index, files = library_index(lib_dirs, libs)
    lib_order = {lib.upper(): i for i, lib in enumerate(libs)}
    build_order = {build: i for i, (build, _) in enumerate(lib_dirs)}
    targets = list(target_functions())
    shared = defaultdict(list)  # a signature two target functions share is a common body, not a library function
    for unit, entry, function in targets:
        shared[signature(function["size"], function["data"], function["relocs"])].append(entry["name"])
    hits, ambiguous = [], []
    for unit, entry, function in targets:
        sig = signature(function["size"], function["data"], function["relocs"])
        carried = index.get(sig)
        if not carried:
            continue
        # the same object (member) in several builds' or libraries' copies is one candidate, not a duplicate, even
        # where a build renamed its symbol (exception -> std::exception) or LIBC's _toupper is LIBCMT's
        # __toupper_lk; the copy reported is the first library in --only, then the first build in --libs
        first = {}
        for _, lib, member, sym in sorted(carried, key=lambda c: (lib_order.get(c[1], len(lib_order)),
                                                                   build_order.get(c[0], len(build_order)))):
            first.setdefault(member.lower(), (lib, member, sym))
        found = list(first.values())
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
                     "lib": lib, "member": member, "symbol": symbol,
                     "builds": sorted({b for b, *_ in carried}),
                     "confidence": "unique" if unique else "same-symbol"})
    return hits, ambiguous, files, len(index)


def other_block(root: Path) -> list[tuple]:
    """Another project's library block, as (size, masked bytes, project, "", its function): the functions inside
    its set_aside.json ranges (the block its own libmatch proved and set aside)."""
    cfg = next((root / "config").glob("*/functions.json")).parent
    aside = json.loads((cfg / "set_aside.json").read_text()) if (cfg / "set_aside.json").is_file() else {}
    spans = [(int(r["from"], 16), int(r["to"], 16)) for r in aside.get("ranges") or []]
    info = json.loads((cfg / "functions.json").read_text())
    from rebuild import read_coff  # noqa: F401  (object_functions reads the targets)
    out = []
    for unit, entries in info["units"].items():
        path = root / "build" / cfg.name / "target" / f"{unit}.obj"
        inside = [e for e in entries if any(a <= e["addr"] < b for a, b in spans)]
        if not inside or not path.is_file():
            continue
        by_name = {f["name"]: f for f in object_functions(path.read_bytes())}
        for e in inside:
            if f := by_name.get(e["name"]):
                named = (info.get("names") or {}).get(f"func_{e['addr']:08X}") or e["name"]
                out.append((f["size"], signature(f["size"], f["data"], f["relocs"])[1], root.name, cfg.name, named))
    return out


def near_tail(hits, lib_dirs, libs, ratio: float, like: list = ()) -> list[dict]:
    """Open functions inside the library block (from tail_range(hits) to the end of .text) that libmatch can't prove
    byte for byte but whose masked bytes are at least `ratio` like one library function, and more like it than
    like any function of the game's own code before the block: the same library, another build of it."""
    import bisect
    import difflib
    start = tail_range(hits)
    if start is None:
        return []
    still = {n for (n,) in common.query("SELECT name FROM functions WHERE project_id=? AND status='open'",
                                        common.project_id())}
    proven = {h["name"] for h in hits}
    body = lambda f: signature(f["size"], f["data"], f["relocs"])[1]
    pool = []  # (size, masked bytes, lib, member, symbol)
    for build, lib_dir in lib_dirs:
        for lib, path in library_files(Path(lib_dir), libs):
            try:
                members = [(path.name, path.read_bytes())] if path.suffix.lower() == ".obj" else archive(path)
            except ValueError:
                continue
            for member, data in members:
                if not member.lower().endswith(".obj"):
                    continue
                try:
                    for f in object_functions(data):
                        pool.append((f["size"], body(f), lib, member_name(member), f["name"]))
                except Exception:
                    continue
    for root in like:  # another game's library block (set aside there by its range) counts as the same library
        pool += other_block(Path(root))
    pool.sort(key=lambda x: x[0])
    sizes = [x[0] for x in pool]
    game, inside = [], []
    for unit, entry, f in target_functions():
        (inside if entry["addr"] >= start else game).append((entry, body(f)))
    game.sort(key=lambda x: len(x[1]))
    gsizes = [len(x[1]) for x in game]

    def best(code, cands):
        sm, top = difflib.SequenceMatcher(None, autojunk=False), (0.0, None)
        sm.set_seq2(code)
        for c in cands:
            sm.set_seq1(c[1])
            if sm.real_quick_ratio() <= top[0] or sm.quick_ratio() <= top[0]:
                continue
            if (r := sm.ratio()) > top[0]:
                top = (r, c)
        return top

    out = []
    for entry, code in inside:
        if entry["name"] not in still or entry["name"] in proven or len(code) < 8:
            continue
        lo, hi = len(code) * ratio, len(code) / ratio
        r, c = best(code, pool[bisect.bisect_left(sizes, lo):bisect.bisect_right(sizes, hi)])
        g, _ = best(code, [(None, b) for _, b in game[bisect.bisect_left(gsizes, lo):bisect.bisect_right(gsizes, hi)]])
        if r >= ratio and r > g:
            out.append({"unit": "", "addr": entry["addr"], "name": entry["name"], "size": entry["size"], "lib": c[2],
                        "member": c[3], "symbol": c[4], "near": round(r, 3), "game_like": round(g, 3)})
    return out


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


def write(hits, range_from: int | None = None, tail: bool = True) -> dict:
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
        if "$$$" in hit["symbol"]:  # an assembler-local label, not a name
            pass
        elif key not in names or names[key] == old:  # a name the project gave it wins
            if new not in {v for k, v in names.items() if k != key}:  # never map two addresses to one name
                if names.get(key) != new:
                    names[key] = new
                    added_names += 1
        if hit["name"] in aside:
            kept += 1
            continue
        aside[hit["name"]] = (f"MSVC static library — {hit['lib']} ({hit['member']}): {hit['symbol']}"
                              + (f" [builds {', '.join(hit['builds'])}]" if hit.get("builds") else ""))
        added_aside += 1
    start = range_from if range_from is not None else tail_range(hits) if tail else None
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
    parser.add_argument("--libs", action="append", default=[],
                        help="[BUILD=]folder with a build's LIB; repeat for several builds (default: VC98 SP5)")
    parser.add_argument("--only", default=",".join(DEFAULT_LIBS), help="comma-separated library names, or all")
    parser.add_argument("--write", action="store_true", help="update set_aside.json and functions.json")
    parser.add_argument("--range-from", default="", help="hex address the library block at the end starts at")
    parser.add_argument("--no-range", action="store_true", help="only the proven functions, no tail range entry")
    parser.add_argument("--open-only", action="store_true", help="leave functions that already match from C alone")
    parser.add_argument("--app", action="store_true", help="with --write: also set each one aside in the app")
    parser.add_argument("--near", type=float, default=0, metavar="RATIO",
                        help="the library block's open functions at least this like a library function (no proof "
                             "byte for byte): report, and with --write set aside")
    parser.add_argument("--like", action="append", default=[], metavar="PROJECT",
                        help="with --near: another project whose set_aside ranges hold the same library's block")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    libs = [x.strip() for x in args.only.split(",") if x.strip()]
    dirs = [tuple(d.split("=", 1)) if "=" in d else (Path(d).parent.name, d) for d in args.libs] \
        or [("SP5", str(LIB_DIR_DEFAULT))]
    hits, ambiguous, files, signatures = scan(dirs, libs)
    by_lib, by_build = defaultdict(int), defaultdict(int)
    for h in hits:
        by_lib[h["lib"]] += 1
        for b in h["builds"]:
            by_build[b] += 1
    print(json.dumps({"libraries": files, "signatures": signatures, "hits": len(hits),
                      "ambiguous": len(ambiguous), "by_lib": by_lib, "by_build": by_build}, indent=None))
    if args.verbose:
        for hit in sorted(hits, key=lambda h: h["addr"]):
            print(f"0x{hit['addr']:08X} {hit['size']:5d} {hit['name']:<24} <- {hit['lib']}:{hit['member']}:{hit['symbol']}"
                  f" {','.join(hit['builds'])}")
    out = common.STATE / "libmatch.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"hits": hits, "ambiguous": ambiguous}, indent=1))
    if args.near:
        near = near_tail(hits, dirs, libs, args.near, args.like)
        print(json.dumps({"near": len(near), "bytes": sum(h["size"] for h in near)}))
        if args.verbose:
            for h in near:
                print(f"0x{h['addr']:08X} {h['size']:5d} {h['near']:.2f} (game {h['game_like']:.2f}) "
                      f"{h['name']} ~ {h['lib']}:{h['member']}:{h['symbol']}")
        if args.write:
            aside_path = common.CONFIG / "set_aside.json"
            aside = json.loads(aside_path.read_text()) if aside_path.is_file() else {}
            for h in near:
                src = (f"{h['lib']} ({h['member']}): {h['symbol']}" if h["lib"].upper().endswith((".LIB", ".OBJ")) else
                       f"{h['symbol']} in {h['lib']}'s runtime block ({h['member']} set_aside range)")
                aside.setdefault(h["name"], f"MSVC static runtime, another build of it — in the library block after the "
                                            f"game's code, {h['near']:.0%} like {src}, more than like any of the "
                                            f"game's own functions ({h['game_like']:.0%})")
            aside_path.write_text(json.dumps(dict(sorted(aside.items())), indent=1) + "\n")
            if args.app:
                done = 0
                for h in near:
                    try:
                        common.api("/api/setaside", {"slug": common.SLUG, "name": h["name"], "why": aside[h["name"]]})
                        done += 1
                    except common.OutmatchError as e:
                        print(json.dumps({"name": h["name"], "error": str(e)[:200]}))
                print(json.dumps({"set_aside_in_app": done}))
        return 0
    if args.open_only:
        still = {n for (n,) in common.query("SELECT name FROM functions WHERE project_id=? AND status='open'",
                                            common.project_id())}
        hits = [h for h in hits if h["name"] in still]
    if args.write:
        print(json.dumps(write(hits, int(args.range_from, 0) if args.range_from else None, tail=not args.no_range)))
        if args.app:  # the app's own record: status 'asm' with the reason, as its Set aside does
            aside = json.loads((common.CONFIG / "set_aside.json").read_text())
            done = 0
            for h in hits:
                try:
                    common.api("/api/setaside", {"slug": common.SLUG, "name": h["name"], "why": aside[h["name"]]})
                    done += 1
                except common.OutmatchError as e:
                    print(json.dumps({"name": h["name"], "error": str(e)[:200]}))
            print(json.dumps({"set_aside_in_app": done}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
