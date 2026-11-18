"""Twins across games: matched C from another decompdecomp project tried on this one's identical functions.

Two functions are twins when their target bytes are the same once relocated fields are masked and the relocations
sit at the same places with the same kinds.  The other project's matched definition is renamed as the relocations
say (func_/D_/C_ address names inside each side's symbol), the declarations it needs come along from its file,
and the result is compiled with *this* unit's own compiler and flags: no same-compiler rule, the compile decides.

Fast where the app's autopilot is slow: fingerprints are read straight from both projects' delinked targets, every
twin of a unit goes in at once (one compile per unit, units in parallel), and only a 100 goes to /api/try.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import common, csource

ADDR_NAME = re.compile(r"\b(_*[A-Za-z]+_[0-9A-Fa-f]{8})\b")  # also _func_X: a C name spelled with its underscore  # func_0041B470, D_00496524, C_00422E2A, Class_0045E3AC
IDENT = re.compile(r"^[A-Za-z_]\w*$")
DECORATED = re.compile(r"^[_@]([A-Za-z_$?][\w$?]*?)(?:@\d+)?$")
TYPE_HEAD = re.compile(r"^\s*(?:typedef\s+)?(?:struct|class|union|enum)\s+(\w+)")


@dataclass
class Print:
    key: str
    refs: list[str]
    unit: str
    size: int


def c_name(symbol: str) -> str:
    m = DECORATED.match(symbol)
    return m.group(1) if m and not symbol.startswith("__imp_") else symbol


def _coff(obj: bytes):
    """(sections [(owner, bytes, [(offset, kind, symbol name, symbol section, value)])]) of a delinked object."""
    _, count, _, table, nsyms, opt, _ = struct.unpack_from("<HHIIIHH", obj, 0)
    strings = table + 18 * nsyms
    syms, k = [], 0
    while k < nsyms:
        raw, value, section, _, _, aux = struct.unpack_from("<8sIhHBB", obj, table + 18 * k)
        if raw[:4] == b"\0\0\0\0":
            at = strings + struct.unpack_from("<I", raw, 4)[0]
            raw = obj[at:obj.index(b"\0", at)]
        syms.append((raw.rstrip(b"\0").decode("latin-1"), value, section))
        syms.extend([("", 0, 0)] * aux)
        k += 1 + aux
    out = []
    for i in range(count):
        _, _, _, size, data, relocs, _, nrel, _, _ = struct.unpack_from("<8sIIIIIIHHI", obj, 20 + opt + 40 * i)
        owner = next((n for n, _, s in syms if s == i + 1 and n and not n.startswith((".", "$"))), "")
        rel = []
        for r in range(nrel):
            off, index, kind = struct.unpack_from("<IIH", obj, relocs + 10 * r)
            rel.append((off, kind, *syms[index]))
        out.append((owner, obj[data:data + size] if data else b"", rel))
    return out


def prints(root: Path, version: str) -> dict[str, Print]:
    """symbol -> Print for every function of a project's delinked targets."""
    info = json.loads((root / "config" / version / "functions.json").read_text(encoding="utf-8"))
    out = {}
    for unit in info["units"]:
        path = root / "build" / version / "target" / f"{unit}.obj"
        if not path.is_file():
            continue
        for owner, raw, relocs in _coff(path.read_bytes()):
            if not owner:
                continue
            code = bytearray(raw)
            for off, *_ in relocs:
                code[off:off + 4] = b"\0" * len(code[off:off + 4])
            layout = ",".join(f"{off}:{kind}" for off, kind, *_ in relocs)
            key = hashlib.sha1(bytes(code) + layout.encode()).hexdigest()
            # a label or a section-relative literal: by its place, the same in both twins
            refs = [f"$L+{value:X}" if name.startswith((".", "$")) or not name else c_name(name)
                    for _, _, name, _, value in relocs]
            out[owner] = Print(key, refs, unit, len(raw))
    return out


def renames(a: Print, b: Print, a_name: str, b_name: str) -> dict[str, str] | None:
    """Identifiers to swap in a's C so it is b's: the address names inside each pair of relocation targets (and the
    two functions' own names). None when the two sides can't be paired name for name."""
    out: dict[str, str] = {}
    for x, y in [(a_name, b_name), *zip(a.refs, b.refs)]:
        if x == y:
            continue
        if x.startswith("$L") or y.startswith("$L"):
            return None  # one label where the other has a symbol: different code after all
        xs, ys = ADDR_NAME.findall(x), ADDR_NAME.findall(y)
        if xs or ys:
            if len(xs) != len(ys) or [s.split("_")[0] for s in xs] != [s.split("_")[0] for s in ys]:
                return None
            pairs = zip(xs, ys)
        elif IDENT.match(x) and IDENT.match(y):
            pairs = [(x, y)]
        else:
            return None
        for p, q in pairs:
            if out.setdefault(p, q) != q:
                return None
    return {k: v for k, v in out.items() if k != v}


def widen(names: dict[str, str], text: str) -> dict[str, str]:
    """Also rename the other address names in `text` that share a renamed address (C_004315D0 goes with
    func_004315D0, Class_0045E3AC with D_0045E3AC): types and classes are named after their method or vtable."""
    hexes = {p.split("_")[-1]: q.split("_")[-1] for p, q in names.items() if ADDR_NAME.fullmatch(p) and ADDR_NAME.fullmatch(q)}
    out = dict(names)
    for tok in set(ADDR_NAME.findall(text)):
        prefix, hx = tok.rsplit("_", 1)
        if tok not in out and hx in hexes:
            out[tok] = f"{prefix}_{hexes[hx]}"
    return out


def address_map(theirs: dict[str, Print], mine: dict[str, Print]) -> dict[str, str]:
    """Their address names -> ours, from every function whose fingerprint is unique on both sides (any status) and
    what its relocations name, widened to the other prefixes (C_, Class_) of each address. One map for every twin,
    so a struct two twins share is written the same both times."""
    def unique(ps):
        seen: dict[str, list[str]] = {}
        for sym, p in ps.items():
            seen.setdefault(p.key, []).append(sym)
        return {k: v[0] for k, v in seen.items() if len(v) == 1}
    ours = unique(mine)
    out: dict[str, str] = {}
    for key, a in unique(theirs).items():
        if (b := ours.get(key)) and (r := renames(theirs[a], mine[b], c_name(a), c_name(b))):
            for k, v in r.items():
                if ADDR_NAME.fullmatch(k):
                    out.setdefault(k, v)
    hexes = {k.split("_")[-1]: v.split("_")[-1] for k, v in out.items()}
    for prefix in ("C", "Class", "S", "T"):  # types named after an address: rename with it
        for hx, hy in hexes.items():
            out.setdefault(f"{prefix}_{hx}", f"{prefix}_{hy}")
    return out


def apply(text: str, names: dict[str, str]) -> str:
    if not names:
        return text
    return re.sub(r"\b\w+\b", lambda m: names.get(m.group(0), m.group(0)), text)  # all at once: a->b, b->c never a->c


def chunks(text: str) -> list[tuple[int, int]]:
    """(start, end) of each file-scope statement: a declaration up to its ';', a body up to its '}', a # line."""
    masked = csource._blank(text)
    out, start, depth, i = [], 0, 0, 0
    while i < len(masked):
        c = masked[i]
        if depth == 0 and c == "#" and not masked[start:i].strip():
            end = i
            while True:  # a #define goes on over lines that end in a backslash
                end = masked.find("\n", end)
                if end < 0 or not masked[:end].rstrip(" \t\r").endswith("\\"):
                    break
                end += 1
            end = len(masked) if end < 0 else end
            out.append((i, end))
            i = start = end
            continue
        if c in "{(":
            depth += 1
        elif c in "})":
            depth -= 1
            if depth == 0 and c == "}":
                rest = masked[i + 1:].lstrip()
                head = masked[start:i].split("{", 1)[0]
                if not rest.startswith(";") and not TYPE_HEAD.match(head) and "=" not in head:
                    out.append((start, i + 1))
                    start = i + 1
        elif c == ";" and depth == 0:
            out.append((start, i + 1))
            start = i + 1
        i += 1
    return [(s + len(text[s:e]) - len(text[s:e].lstrip()), e) for s, e in out if text[s:e].strip()]


def _declares(chunk: str) -> str | None:
    """The name a file-scope declaration introduces (a type, a prototype, a variable, a macro)."""
    if m := re.match(r"#define\s+(\w+)", chunk):
        return m.group(1)
    if m := TYPE_HEAD.match(chunk):
        if "{" in chunk or chunk.rstrip().endswith(";"):
            return m.group(1)
    if chunk.lstrip().startswith("typedef"):
        m = re.search(r"(\w+)\s*(?:\[[^\]]*\]\s*)*;\s*$", chunk)
        return m.group(1) if m else None
    head = re.sub(r"__declspec\s*\([^)]*\)", "", csource._blank(chunk).split("{", 1)[0].split("=", 1)[0])
    if m := re.search(r"\(\s*(?:__\w+\s*)?\*\s*(?:const\s+)?(\w+)\s*\)\s*\(", head):  # a function pointer: (__stdcall* name)(...)
        return m.group(1)
    m = re.search(r"(\w+)\s*(?:\(|\[|;|$)", head.strip())
    return m.group(1) if m else None


def project_decls(root: Path) -> list[str]:
    """Every file-scope declaration of a project (headers, then sources), each name's first one: the fallback for
    a name the twin's own file uses without declaring it there."""
    seen, out = set(), []
    for path in [*sorted((root / "include").glob("*.h")), *sorted((root / "src" / "auto").glob("*.cpp"))]:
        text = path.read_text(encoding="utf-8", errors="replace")
        for s, e in chunks(text):
            body = text[s:e]
            if body.startswith("#") and not body.startswith("#define") or \
                    not body.startswith("#") and body.rstrip().endswith("}") and not TYPE_HEAD.match(body):
                continue
            if (name := _declares(body)) and name not in seen:
                seen.add(name)
                out.append(body)
    return out


def borrow(other_text: str, span: tuple[int, int], code: str, mine: str, names: dict[str, str],
           extra: list[str] = ()) -> tuple[str, dict[str, str]]:
    """The declarations from the other file (else `extra`, the rest of its project) that `code` (already renamed)
    uses and this file doesn't have, and the private names given to structs this file defines otherwise."""
    have = set(re.findall(r"\b\w+\b", csource._blank(mine)))
    flat = lambda t: re.sub(r"\s+", " ", csource._blank(t)).strip()
    my_types = {m.group(1): flat(mine[s:e]) for s, e in chunks(mine)
                if (m := TYPE_HEAD.match(mine[s:e])) and "{" in mine[s:e]}
    pool, private = [], {}
    for s, e in chunks(other_text):
        if s >= span[0] and e <= span[1]:
            continue
        body = other_text[s:e]
        if body.startswith("#") and not body.startswith("#define") or \
                    not body.startswith("#") and body.rstrip().endswith("}") and not TYPE_HEAD.match(body):
            continue  # a pragma/include or another function: never borrowed
        renamed = apply(body, names)
        name = _declares(renamed)
        if name and name not in have:
            pool.append((name, renamed))
        elif name in my_types and "{" in renamed and flat(renamed) != my_types[name]:
            # this file has its own, other struct of that name: the twin keeps its own under a private name (the
            # symbol doesn't carry the class: the build names a method by its address)
            private[name] = name + "_tw"
            pool.append((name, renamed))
    known = {n for n, _ in pool}
    for body in extra:
        renamed = apply(body, names)
        name = _declares(renamed)
        if name and name not in have and name not in known:
            known.add(name)
            pool.append((name, renamed))
    used, want = set(re.findall(r"\b\w+\b", code)), []
    for _ in range(4):  # a borrowed struct names other types: follow them a few levels
        add = [(n, t) for n, t in pool if n in used and (n, t) not in want]
        if not add:
            break
        want += add
        for _, t in add:
            used |= set(re.findall(r"\b\w+\b", t))
    order = {t: i for i, (_, t) in enumerate(pool)}
    includes = [other_text[s:e] for s, e in chunks(other_text) if other_text[s:e].startswith("#include")
                and other_text[s:e] not in mine]
    private = {k: v for k, v in private.items() if any(n == k for n, _ in want)}
    return apply("\n\n".join(includes + [t for _, t in sorted(want, key=lambda x: order[x[1]])]), private), private


@dataclass
class Pair:
    symbol: str      # this project's function
    unit: str
    other: str       # its matched twin's symbol
    origin: str      # which project
    code: str        # the renamed definition
    helpers: str     # declarations it brings
    name_entry: tuple[str, str] | None = None  # functions.json `names` entry it needs here (a ctor/dtor's C name)
    names: dict = field(default_factory=dict)  # their names -> ours, for declarations fetched later
    source: Path | None = None                # the other project's folder
    other_unit: str = ""
    drop: tuple = ()                           # this file's declarations of these names give way to the twin's
    pin: tuple | None = None                   # (compiler, extra flags) the unit needs for it to match
    split: tuple | None = None                 # (compiler, extra flags) it matches with in a unit of its own


def _other_project(root: Path) -> tuple[int, str]:
    with sqlite3.connect(f"file:{common.DB}?mode=ro", uri=True) as db:
        for pid, proot, cfg in db.execute("SELECT id, root, config FROM projects"):
            if proot and Path(proot).resolve() == root.resolve():
                return pid, json.loads(cfg or "{}").get("version") or common._version(root)
    raise common.OutmatchError(f"{root} is not a decompdecomp project")


def find_pairs(sources: list[Path], min_size: int = 8) -> tuple[list[Pair], list[dict]]:
    """Every open function here with a matched twin in one of `sources`, its C made ready (or why not)."""
    pid = common.project_id()
    mine = prints(common.ROOT, common.VERSION)
    open_ = {n: u for n, u in common.query("SELECT name, unit FROM functions WHERE project_id=? AND status='open'", pid)}
    wanted: dict[str, list[str]] = {}
    for sym, p in mine.items():
        if sym in open_ and p.size >= min_size:
            wanted.setdefault(p.key, []).append(sym)
    names_here = common.functions().get("names") or {}
    pairs, skipped, taken = [], [], set()
    for root in sources:
        opid, over = _other_project(root)
        theirs = prints(root, over)
        info = json.loads((root / "config" / over / "functions.json").read_text(encoding="utf-8"))
        names_there = info.get("names") or {}
        matched = [n for (n,) in common.query("SELECT name FROM functions WHERE project_id=? AND status='matched'", opid)]
        world = address_map(theirs, mine)
        texts: dict[str, str] = {}
        decls = project_decls(root)
        for a in matched:
            pa = theirs.get(a)
            for b in (wanted.get(pa.key, []) if pa else []):
                if b in taken:
                    continue
                names = renames(pa, mine[b], c_name(a), c_name(b))
                if names is None:
                    skipped.append({"symbol": b, "from": a, "skipped": "relocations don't pair up name for name"})
                    continue
                own = names
                if pa.unit not in texts:
                    path = root / "src" / "auto" / f"{pa.unit}.cpp"
                    texts[pa.unit] = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
                text = texts[pa.unit]
                hint = names_there.get(m.group(0)) if (m := ADDR_NAME.search(c_name(a))) else None
                _, s, e = csource.find(text, [hint] if hint else [], a)
                if s < 0:
                    skipped.append({"symbol": b, "from": a, "skipped": "no definition in its source"})
                    continue
                names = widen({**world, **widen(own, text)}, text)  # its own say wins; the map keeps shared structs alike
                code = apply(text[s:e], names)
                key_b = m.group(0).lstrip("_") if (m := ADDR_NAME.search(c_name(b))) else None
                hint_b, entry = names_here.get(key_b) if key_b else None, None
                if hint_b is None and hint and key_b:  # theirs is `Class::~Class` by its names entry: so is ours
                    hint_b, entry = apply(hint, names), (key_b, apply(hint, names))
                if csource.find(code, [hint_b] if hint_b else [], b)[1] < 0:
                    skipped.append({"symbol": b, "from": a, "skipped": "renamed definition isn't under its symbol"})
                    continue
                mine_text = common.source_path(mine[b].unit).read_text(encoding="utf-8")
                helpers, private = borrow(text, (s, e), code, mine_text, names, decls)
                pairs.append(Pair(b, mine[b].unit, a, root.name, apply(code, private), helpers, entry,
                                  {**names, **private}, root, pa.unit))
                taken.add(b)
    return pairs, skipped


def _unit_text(base: str, items: list[Pair]) -> str:
    drop = {n for p in items for n in p.drop}
    if drop:  # this file's own prototypes/externs of those names make way for the twin's
        base = _without(base, drop)
    text, helpers = base, []
    for p in items:
        _, s, e = common.definition_span(text, p.symbol)
        text = text[:s] + p.code.rstrip() + "\n" + text[e:] if s >= 0 else text.rstrip() + "\n\n" + p.code.rstrip() + "\n"
        helpers += [h for h in p.helpers.split("\n\n") if h and h not in helpers]
    declared = {_declares(base[s:e]) for s, e in chunks(base)} | {base[s:e] for s, e in chunks(base) if base[s:e].startswith("#")}
    helpers = [h for h in helpers if h not in declared and _declares(h) not in declared]  # never a second struct X
    if helpers:  # before the first function body: declarations must precede every use
        cut = next((s for s, e in chunks(text) if text[s:e].rstrip().endswith("}") and not TYPE_HEAD.match(text[s:e])),
                   len(text))
        text = text[:cut] + "\n".join(helpers) + "\n\n" + text[cut:]
    return text


def _without(text: str, names: set) -> str:
    """`text` less its file-scope declarations (never definitions) of `names`."""
    cut = [(s, e) for s, e in chunks(text) if "{" not in text[s:e] and _declares(text[s:e]) in names]
    for s, e in reversed(cut):
        text = text[:s] + text[e:]
    return text


_DECLS: dict[Path, dict[str, str]] = {}
ERR = re.compile(r"error (C\d+): '([^']+)'")


def source_decl(root: Path, name: str) -> str | None:
    """Where the other project declares `name`: at file scope (headers, sources), else an `extern` inside a body."""
    if root not in _DECLS:
        table: dict[str, str] = {}
        for body in project_decls(root):
            table.setdefault(_declares(body), body)
        for path in sorted((root / "src" / "auto").glob("*.cpp")):
            for m in re.finditer(r"^[ \t]*(extern\b[^;{}()]*?\b(\w+)\s*(?:\[[^\]]*\])?\s*;)", path.read_text(
                    encoding="utf-8", errors="replace"), re.M):
                table.setdefault(m.group(2), m.group(1))
        _DECLS[root] = table
    return _DECLS[root].get(name)


def repair(p: Pair, base: str, run) -> tuple[Pair, dict]:
    """Compile p in the unit, fixing what the compiler names: an undeclared name gets the other project's
    declaration, a name this file declares otherwise gets the twin's instead. (p as it compiled, its scores)"""
    back = {v: k for k, v in p.names.items()}
    for _ in range(6):
        try:
            return p, run(_unit_text(base, [p]))
        except common.OutmatchError as e:
            errs = ERR.findall(str(e))
        add, drop = [], set(p.drop)
        for code, name in errs:
            if code == "C2065" and (d := source_decl(p.source, back.get(name, name))):
                add.append(apply(d, p.names))
            elif code in ("C2556", "C2371", "C2040", "C2373", "C2372") and name not in drop:
                drop.add(name)
                if d := source_decl(p.source, back.get(name, name)):
                    add.append(apply(d, p.names))
        add = [a for a in dict.fromkeys(add) if a not in p.helpers]
        if not add and drop == set(p.drop):
            raise common.OutmatchError("unrepaired: " + "; ".join(f"{c} {n}" for c, n in errs[:3]))
        p = replace(p, helpers="\n\n".join([*add, p.helpers]) if add else p.helpers, drop=tuple(sorted(drop)))
    raise common.OutmatchError("unrepaired after 6 rounds")


def their_build(p: Pair) -> tuple[str, list[str]]:
    """The compiler and per-file flags the other project builds the twin's file with."""
    cfg = next((p.source / "config").glob("*/functions.json")).parent
    key = f"src/auto/{p.other_unit}.cpp"
    comp = json.loads((cfg / "file_compilers.json").read_text()).get(key) if (cfg / "file_compilers.json").is_file() else None
    if not comp:
        m = re.search(r'^MSVC_VERSION = "([^"]+)"', (p.source / "tools" / "configure.py").read_text(), re.M)
        comp = m.group(1) if m else common.default_compiler()
    flags = json.loads((cfg / "file_flags.json").read_text()).get(key, []) if (cfg / "file_flags.json").is_file() else []
    return comp, list(flags)


def score_unit(unit: str, items: list[Pair], slot: int) -> dict[str, float]:
    """symbol -> pct with `items` placed in the unit (all at once; one at a time if that breaks the unit)."""
    base = common.source_path(unit).read_text(encoding="utf-8")
    root = common.make_scratch(unit, common.slot(slot), fresh=False)
    out_rel = f"build/{common.VERSION}/src/auto/{unit}.obj"
    target = common.target_obj(unit)

    def run(text):
        return common.scores(target, common.scratch_compile(root, unit, text, out_rel))

    try:
        before = {k for k, v in run(base).items() if v is not None and v >= 99.999}
    except common.OutmatchError:
        before = set()
    ok = lambda sc: {k for k, v in sc.items() if v is not None and v >= 99.999}
    results: dict[str, float] = {}
    try:
        after = run(_unit_text(base, items))
        if not (before - ok(after)):
            results = {p.symbol: after.get(p.symbol) or 0.0 for p in items}
    except common.OutmatchError:
        pass
    mine = (common.file_compilers().get(f"src/auto/{unit}.cpp") or common.default_compiler(),
            common.file_flags().get(f"src/auto/{unit}.cpp", []))
    for i, p in enumerate(items):  # each twin short of 100 on its own: repaired, then under the other's build
        if results.get(p.symbol, 0) >= 99.999:
            continue
        try:
            p, got = repair(p, base, run)
        except common.OutmatchError:
            results[p.symbol] = -1.0
            continue
        items[i] = p
        pct = got.get(p.symbol) or 0.0
        if before - ok(got):
            if pct >= 99.999:  # its declarations cost a neighbour here: alone in a unit they cost nothing
                items[i] = replace(p, split=(mine[0], []))
            results[p.symbol] = -2.0
            continue
        if pct < 99.999 and p.source and (theirs := their_build(p)) != mine:
            comp, flags = theirs
            try:
                alt = common.scores(target, common.scratch_compile(
                    root, unit, _unit_text(base, [p]), out_rel, compiler=comp,
                    extra=[f for f in flags if f not in mine[1]]))
                alt_before = ok(common.scores(target, common.scratch_compile(
                    root, unit, base, out_rel, compiler=comp, extra=[f for f in flags if f not in mine[1]])))
                extra = [f for f in flags if f not in mine[1]]
                if (alt.get(p.symbol) or 0) >= 99.999 and not (before - ok(alt)) and before <= alt_before | ok(alt):
                    items[i] = p = replace(p, pin=(comp, extra))
                    pct = 100.0
                elif (alt.get(p.symbol) or 0) >= 99.999:  # the other build costs a neighbour: a unit of its own
                    items[i] = p = replace(p, split=(comp, extra))
            except common.OutmatchError:
                pass
        results[p.symbol] = pct
    return results


def _add_names(path: Path, entries: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8")
    info = json.loads(text)
    info.setdefault("names", {}).update(entries)
    path.write_text(json.dumps(info, indent=1) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")


import threading as _threading
_PIN_LOCK = _threading.Lock()


def apply_pin(p: Pair) -> None:
    """Build p's unit with the compiler (and flags) its twin's file has: file_compilers.json / file_flags.json,
    which the app's build reads (scoring already showed every match in the unit keeps)."""
    comp, flags = p.pin
    key = f"src/auto/{p.unit}.cpp"
    with _PIN_LOCK:
        path = common.FILE_COMPILERS
        table = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        want = None if comp == common.default_compiler() else comp
        if table.get(key) != want:
            table.pop(key, None) if want is None else table.__setitem__(key, want)
            path.write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8")
        if flags:
            ff = common.file_flags()
            ff[key] = sorted(set(ff.get(key, [])) | set(flags))
            common.save_file_flags(ff)
        common.log("twin-pin", unit=p.unit, compiler=comp, flags=flags)


def _wait_check() -> None:
    job = common.check_project().get("job")
    while job and common.api(f"/api/job?id={job}").get("state") == "running":
        time.sleep(3)


def split_and_try(p: Pair) -> dict:
    """Put p in its unit, move it into a unit of its own (flags.split_out) built as p.split says, and try it.
    Anything short of a match puts every file this touched back as it was."""
    from . import flags as fl
    paths = [common.FUNCTIONS_JSON, common.FILE_FLAGS, common.FILE_COMPILERS, common.source_path(p.unit)]
    snap = {path: path.read_text(encoding="utf-8") for path in paths if path.is_file()}
    new_path = None

    def undo(why: str) -> dict:
        for path in paths:
            if path in snap:
                path.write_text(snap[path], encoding="utf-8")
            elif path.is_file():
                path.unlink()
        if new_path is not None:
            new_path.unlink(missing_ok=True)  # the unit this split made, nothing else
        common._functions = None
        _wait_check()
        return {"matched": False, "why": why[:300]}

    comp, extra = p.split
    common.source_path(p.unit).write_text(_unit_text(snap[common.source_path(p.unit)], [p]), encoding="utf-8")
    try:
        res = fl.split_out(p.unit, p.symbol, [*fl.command_flags(p.unit), *extra], [p.symbol])
    except common.OutmatchError as e:
        return undo(f"split failed: {e}")
    if not res.get("split"):
        return undo(res.get("why") or "split failed")
    new_path = common.source_path(res["split"])
    apply_pin(replace(p, unit=res["split"], pin=(comp, [])))
    _wait_check()
    try:
        out = common.try_function(p.symbol, p.code)
    except common.OutmatchError as e:
        out = {"matched": False, "text": str(e)}
    if not out.get("matched"):
        common.release(p.symbol)
        return undo("app: " + str(out.get("text", out)))
    common.log("twin-split", symbol=p.symbol, unit=res["split"], compiler=comp, flags=extra)
    return {"matched": True, "unit": res["split"]}


def submit_one(p: Pair, slot) -> dict:
    """/api/try one twin with its declarations written into the unit first (where _unit_text puts them). The app
    undoes only its definition on a miss: the declarations come back out here, so a miss leaves the file as it was."""
    path = common.source_path(p.unit)
    before = path.read_text(encoding="utf-8")
    ours = _unit_text(before, [p])
    pin = {"compiler": p.pin[0], "extra": p.pin[1]} if p.pin else {}
    try:  # earlier matches in this unit change it (their declarations): score again as it is now
        n = slot.get()
        try:
            got = common.scores(common.target_obj(p.unit), common.scratch_compile(
                common.make_scratch(p.unit, common.slot(n)), p.unit, ours, f"build/{common.VERSION}/src/auto/{p.unit}.obj",
                **pin))
        finally:
            slot.put(n)
        if not (got.get(p.symbol) or 0) >= 99.999:
            return {"matched": False, "text": f"no longer exact in the unit as it is now ({got.get(p.symbol)})"}
    except common.OutmatchError as e:
        return {"matched": False, "text": "no longer compiles in the unit as it is now: " + str(e)[-200:]}
    if p.pin:
        apply_pin(p)
    path.write_text(ours, encoding="utf-8")
    try:
        res = common.try_function(p.symbol, p.code)
    except common.OutmatchError as e:
        res = {"matched": False, "text": str(e)}
    if not res.get("matched"):
        common.release(p.symbol)
        if path.read_text(encoding="utf-8") == ours:  # untouched since (an agent's edit stays)
            path.write_text(before, encoding="utf-8")
    common.log("submitted", symbol=p.symbol, matched=bool(res.get("matched")), score=res.get("score"))
    return res


def run(sources: list[Path], jobs: int = common.JOBS, submit: bool = True, min_size: int = 8,
        say=lambda line: print(line, flush=True)) -> dict:
    """Score every twin unit by unit (in parallel) and submit each unit's 100s as soon as it is scored."""
    import fcntl
    import queue
    from concurrent.futures import as_completed

    common.STATE.mkdir(parents=True, exist_ok=True)
    lock = open(common.STATE / "twins.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise common.OutmatchError("another `twins` run is working on this project (they'd share scratch trees)")
    pairs, skipped = find_pairs(sources, min_size)
    for s in skipped:
        say(json.dumps(s))
    by_unit: dict[str, list[Pair]] = {}
    for p in pairs:
        by_unit.setdefault(p.unit, []).append(p)
    say(json.dumps({"pairs": len(pairs), "units": len(by_unit), "skipped": len(skipped)}))
    entries = dict(p.name_entry for p in pairs if p.name_entry)
    slots: queue.Queue[int] = queue.Queue()
    for k in range(max(1, jobs)):
        slots.put(300 + k)  # one scratch tree per worker, never two units in one at once
        root = common.make_scratch("", common.slot(300 + k), fresh=True)  # today's config (names, flags)
        if entries:
            _add_names(root / "config" / common.VERSION / "functions.json", entries)
    if entries and submit:  # the app finds a ctor/dtor's definition by its names entry: give it ours, re-read config
        _add_names(common.FUNCTIONS_JSON, entries)
        job = common.check_project().get("job")
        while job and (common.api(f"/api/job?id={job}").get("state") == "running"):
            time.sleep(3)
        say(json.dumps({"names_added": entries}))

    def scored_unit(unit):
        n = slots.get()
        try:
            return score_unit(unit, by_unit[unit], n)
        finally:
            slots.put(n)

    import threading
    pid = common.project_id()
    lock = threading.Lock()
    tally = {"matched": 0, "exact": 0, "done": 0}

    def unit_work(unit):
        """Score one unit, then submit its 100s: units in parallel, a unit's tries one after another (one file)."""
        scores = scored_unit(unit)
        with lock:
            tally["done"] += 1
            nth = f"{tally['done']}/{len(by_unit)}"
        for p in by_unit[unit]:
            pct = scores.get(p.symbol, -1.0)
            row = {"unit": nth, "symbol": p.symbol, "from": f"{p.origin}:{p.other}", "pct": round(pct, 3)}
            if pct == -1.0:
                row["error"] = "doesn't compile here"
            elif pct == -2.0:
                row["error"] = "breaks a matched neighbour"
            elif pct >= 99.999:
                with lock:
                    tally["exact"] += 1
                status = common.query_one("SELECT status FROM functions WHERE project_id=? AND name=?", pid, p.symbol)
                if status and status[0] != "open":
                    row["skipped"] = f"already {status[0]}"  # matched meanwhile (autopilot, an agent)
                elif submit:
                    if p.pin:
                        row["pin"] = p.pin
                    res = submit_one(p, slots)
                    row["matched"] = bool(res.get("matched"))
                    if not row["matched"]:
                        row["app"] = str(res.get("text", res))[:200].replace("\n", " ")
                    with lock:
                        tally["matched"] += row["matched"]
            with lock:
                say(json.dumps(row))

    with ThreadPoolExecutor(max(1, jobs)) as ex:
        for fut in as_completed([ex.submit(unit_work, u) for u in by_unit]):
            fut.result()
    splits = [p for u in by_unit for p in by_unit[u] if p.split]
    if submit:
        for p in splits:  # one at a time: a split reconfigures the project
            status = common.query_one("SELECT status FROM functions WHERE project_id=? AND name=?", pid, p.symbol)
            if status and status[0] != "open":
                continue
            row = {"symbol": p.symbol, "from": f"{p.origin}:{p.other}", "split": p.split, **split_and_try(p)}
            tally["matched"] += bool(row.get("matched"))
            say(json.dumps(row))
    if submit and tally["exact"]:  # a rejected try leaves the app's last object of that unit built from it: re-read
        _wait_check()
    matched, exact = tally["matched"], tally["exact"]
    summary = {"pairs": len(pairs), "exact_here": exact, "matched": matched, "skipped": len(skipped)}
    say(json.dumps(summary))
    return summary


if __name__ == "__main__":  # self-check of the pieces that don't need a project
    a = Print("k", ["func_00401000", "D_00402000", "$L+10", "?f@C_00403000@@QAEXXZ"], "u", 40)
    b = Print("k", ["func_00501000", "D_00502000", "$L+10", "?f@C_00503000@@QAEXXZ"], "u", 40)
    r = renames(a, b, "func_00400000", "func_00500000")
    assert r == {"func_00400000": "func_00500000", "func_00401000": "func_00501000", "D_00402000": "D_00502000",
                 "C_00403000": "C_00503000"}, r
    assert apply("C_00403000::func_00400000(D_00402000)", r) == "C_00503000::func_00500000(D_00502000)"
    assert widen({"func_00400000": "func_00500000"}, "C_00400000::func_00400000")["C_00400000"] == "C_00500000"
    t = "#include <x.h>\nstruct S { int a; };\nint g;\nvoid f(void) { if (1) { g = 1; } }\nstruct T;\n"
    assert [t[s:e].split()[0] for s, e in chunks(t)] == ["#include", "struct", "int", "void", "struct"], chunks(t)
    assert _declares('extern "C" void (__stdcall* _imp__Enter)(void* cs);') == "_imp__Enter"
    assert _declares('extern "C" __declspec(dllimport) void __stdcall Enter(void* p);') == "Enter"
    assert widen({"func_0044EFDA": "func_0044FD0A"}, "void _func_0044EFDA(int)")["_func_0044EFDA"] == "_func_0044FD0A"
    assert _declares('extern "C" int (__stdcall* const _imp__P)(void* m);') == "_imp__P"
    m = "#define D(s) if (s) { \\\n  f(); \\\n}\nint g;\n"
    assert [m[a:b].split()[0] for a, b in chunks(m)] == ["#define", "int"], chunks(m)
    assert _declares("#define DEQ(s) x") == "DEQ"
    print("ok")
