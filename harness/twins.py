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
import subprocess
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
    code: bytes = b""  # masked bytes (near twins compare these)


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
            out[owner] = Print(key, refs, unit, len(raw), bytes(code))
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


def near_renames(a: Print, b: Print, a_name: str, b_name: str) -> dict[str, str]:
    """renames() for code that is close, not identical: the relocations aligned in order (difflib), each aligned
    pair of address names renamed; a pair that disagrees with an earlier one is left out, not fatal."""
    import difflib
    kind = lambda r: "$L" if r.startswith("$L") else (ADDR_NAME.search(r).group(0).split("_")[0]
                                                      if ADDR_NAME.search(r) else r)
    out: dict[str, str] = {}
    pairs = [(a_name, b_name)]
    sm = difflib.SequenceMatcher(None, [kind(r) for r in a.refs], [kind(r) for r in b.refs], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("equal", "replace") and i2 - i1 == j2 - j1:
            pairs += zip(a.refs[i1:i2], b.refs[j1:j2])
    for x, y in pairs:
        if x == y or x.startswith("$L") or y.startswith("$L"):
            continue
        xs, ys = ADDR_NAME.findall(x), ADDR_NAME.findall(y)
        if xs and len(xs) == len(ys):
            for p, q in zip(xs, ys):
                if p.split("_")[0] == q.split("_")[0]:
                    out.setdefault(p, q)
        elif IDENT.match(x) and IDENT.match(y):
            out.setdefault(x, y)
    return {k: v for k, v in out.items() if k != v}


def near_candidates(mine: dict[str, Print], wanted: set[str], theirs: dict[str, Print], matched: list[str],
                    ratio: float) -> dict[str, tuple[float, str]]:
    """For each wanted function here, the matched one there whose masked bytes are most alike (>= ratio)."""
    import difflib
    pool = sorted(((theirs[a].size, a) for a in matched if a in theirs and theirs[a].code), key=lambda x: x[0])
    sizes = [x[0] for x in pool]
    import bisect
    best: dict[str, tuple[float, str]] = {}
    for b in wanted:
        pb = mine[b]
        lo, hi = bisect.bisect_left(sizes, pb.size * ratio), bisect.bisect_right(sizes, pb.size / ratio)
        sm = difflib.SequenceMatcher(None, autojunk=False)
        sm.set_seq2(pb.code)
        top = (0.0, "")
        for _, a in pool[lo:hi]:
            sm.set_seq1(theirs[a].code)
            if sm.real_quick_ratio() < ratio or sm.quick_ratio() < ratio:
                continue
            r = sm.ratio()
            if r > top[0]:
                top = (r, a)
        if top[0] >= ratio:
            best[b] = top
    return best


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
        elif name in my_types and "{" in renamed and flat(renamed) != my_types[name] and "func_" not in name:
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


def find_pairs(sources: list[Path], min_size: int = 8, near: float = 0) -> tuple[list[Pair], list[dict]]:
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
        world = {} if root.resolve() == common.ROOT.resolve() else address_map(theirs, mine)  # itself: own names
        texts: dict[str, str] = {}
        decls = project_decls(root)
        def make(a, b, own, near=None):
            pa = theirs[a]
            if pa.unit not in texts:
                path = root / "src" / "auto" / f"{pa.unit}.cpp"
                texts[pa.unit] = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
            text = texts[pa.unit]
            hint = names_there.get(m.group(0)) if (m := ADDR_NAME.search(c_name(a))) else None
            _, s, e = csource.find(text, [hint] if hint else [], a)
            if s < 0:
                skipped.append({"symbol": b, "from": a, "skipped": "no definition in its source"})
                return
            if (hint and "::" in hint and not ADDR_NAME.search(hint) and root.resolve() == common.ROOT.resolve()
                    and (mb := ADDR_NAME.search(c_name(b)))):
                # a ctor/dtor twin in the same game is another class with the same layout, not that class again
                own = {**own, hint.split("::")[0].lstrip("~"): "C_" + mb.group(0).split("_")[-1]}
            names = widen({**world, **widen(own, text)}, text)  # its own say wins; the map keeps shared structs alike
            for k, v in list(names.items()):  # the source's C calls an address by its real name: rename that too
                real = names_there.get(k.lstrip("_"))
                if real and IDENT.match(real) and real not in names:
                    mine_real = names_here.get(v.lstrip("_"))
                    names[real] = mine_real if mine_real and IDENT.match(mine_real) else v.lstrip("_")
            code = apply(text[s:e], names)
            key_b = m.group(0).lstrip("_") if (m := ADDR_NAME.search(c_name(b))) else None
            hint_b, entry = names_here.get(key_b) if key_b else None, None
            if hint_b is None and hint and key_b:  # theirs is `Class::~Class` by its names entry: so is ours
                hint_b, entry = apply(hint, names), (key_b, apply(hint, names))
            if csource.find(code, [hint_b] if hint_b else [], b)[1] < 0:
                skipped.append({"symbol": b, "from": a, "skipped": "renamed definition isn't under its symbol"})
                return
            mine_text = common.source_path(mine[b].unit).read_text(encoding="utf-8")
            helpers, private = borrow(text, (s, e), code, mine_text, names, decls)
            pairs.append(Pair(b, mine[b].unit, a, root.name + (f"~{near:.2f}" if near else ""), apply(code, private),
                              helpers, entry, {**names, **private}, root, pa.unit))
            taken.add(b)

        for a in matched:
            pa = theirs.get(a)
            for b in (wanted.get(pa.key, []) if pa else []):
                if b in taken or b == a:
                    continue
                names = renames(pa, mine[b], c_name(a), c_name(b))
                if names is None:
                    skipped.append({"symbol": b, "from": a, "skipped": "relocations don't pair up name for name"})
                    continue
                make(a, b, names)
        if near:  # close, not identical: the most alike matched function's C as the start
            rest = {b for b in open_ if b in mine and b not in taken and mine[b].size >= max(min_size, 16)}
            for b, (r, a) in sorted(near_candidates(mine, rest, theirs, matched, near).items()):
                if b != a and b not in taken:
                    make(a, b, near_renames(theirs[a], mine[b], c_name(a), c_name(b)), r)
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
    def attempt(q):
        """(q as it compiled, pct, neighbours it costs) or None when it doesn't compile."""
        try:
            q, got = repair(q, base, run)
        except common.OutmatchError:
            return None
        return q, got.get(q.symbol) or 0.0, before - ok(got)

    def under(q, **build):
        """(pct, neighbours lost) for q and the unit built another way (compiler, flags)."""
        got = common.scores(target, common.scratch_compile(root, unit, _unit_text(base, [q]), out_rel, **build))
        was = ok(common.scores(target, common.scratch_compile(root, unit, base, out_rel, **build)))
        return got.get(q.symbol) or 0.0, (before & was) - ok(got) | (before - was)

    for i, p in enumerate(items):  # each twin short of 100 on its own
        if results.get(p.symbol, 0) >= 99.999:
            continue
        best = attempt(p)
        # 1. every name it uses declared the way its own game declares it (callees' conventions, globals' types)
        if best is None or best[1] < 99.999 or best[2]:
            alt = attempt(adopt(p, base))
            if alt and (best is None or (alt[1], -len(alt[2])) > (best[1], -len(best[2]))):
                best = alt
        if best is None:
            results[p.symbol] = -1.0
            continue
        p, pct, lost = best
        items[i] = p
        if lost:
            if pct >= 99.999:  # its declarations cost a neighbour here: alone in a unit they cost nothing
                items[i] = replace(p, split=(mine[0], [], "add"))
            results[p.symbol] = -2.0
            continue
        # 2. its own game's build of that file, then the flag sweep
        builds = []
        if p.source and (theirs := their_build(p)) != mine:
            extra = [f for f in theirs[1] if f not in mine[1]]
            builds.append(((theirs[0], extra, "add"), {"compiler": theirs[0], "extra": extra}))
        if pct >= SWEEP_FROM:
            from . import flags as fl
            for _, full in fl.candidate_sets(unit):
                delta = fl.delta_flags(unit, full)
                if delta != mine[1]:
                    builds.append(((mine[0], delta, "set"), {"flags": full}))
        for build, kw in builds:
            if pct >= 99.999:
                break
            try:
                alt_pct, alt_lost = under(p, **kw)
            except common.OutmatchError:
                continue
            if alt_pct >= 99.999:
                items[i] = p = replace(p, **({"split": build} if alt_lost else {"pin": build}))
                pct = 100.0 if not alt_lost else pct
                if alt_lost:
                    break
        results[p.symbol] = pct
    return results


SWEEP_FROM = 50.0  # the flag sweep (~24 builds) only for twins this close already


def adopt(p: Pair, base: str) -> Pair:
    """p with every name its code uses that this file declares (a prototype, an extern) declared as the other
    project does instead: the twin's code is identical, so its game's declarations are the right ones."""
    if not p.source:
        return p
    used = set(re.findall(r"\b\w+\b", p.code))
    back = {v: k for k, v in p.names.items()}
    drop, add = set(p.drop), []
    for s0, e0 in chunks(base):
        chunk = base[s0:e0]
        if "{" in chunk or chunk.startswith("#"):
            continue
        name = _declares(chunk)
        if not name or name not in used or name in drop:
            continue
        theirs = source_decl(p.source, back.get(name, name))
        if theirs and re.sub(r"\s+", " ", apply(theirs, p.names)) != re.sub(r"\s+", " ", chunk):
            drop.add(name)
            add.append(apply(theirs, p.names))
    if not add:
        return p
    return replace(p, drop=tuple(sorted(drop)), helpers="\n\n".join([*dict.fromkeys(add), p.helpers]))


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
    comp, flags, *mode = p.pin
    key = f"src/auto/{p.unit}.cpp"
    with _PIN_LOCK:
        path = common.FILE_COMPILERS
        table = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        want = None if comp == common.default_compiler() else comp
        if table.get(key) != want:
            table.pop(key, None) if want is None else table.__setitem__(key, want)
            path.write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8")
        ff = common.file_flags()
        want_flags = list(flags) if mode == ["set"] else sorted(set(ff.get(key, [])) | set(flags))
        if want_flags != ff.get(key, []):
            ff[key] = want_flags  # "set": a whole flag set from a sweep; else flags added to the file's own
            if not want_flags:
                ff.pop(key)
            common.save_file_flags(ff)
        common.log("twin-pin", unit=p.unit, compiler=comp, flags=flags)


def _wait_check() -> None:
    job = common.check_project().get("job")
    while job and common.api(f"/api/job?id={job}").get("state") == "running":
        time.sleep(3)


def _build_args(unit: str, pin) -> dict:
    """scratch_compile's keywords for a pin: (compiler, extra flags) or (compiler, flags relative to the
    global set, "set")."""
    if not pin:
        return {}
    from . import flags as fl
    comp, flags, *mode = pin
    if mode == ["set"]:
        return {"compiler": comp, "flags": [*fl.global_flags(unit), *flags]}
    return {"compiler": comp, "extra": list(flags)}


def _save_json(path: Path, data: dict, like: str) -> None:
    """Write `data` the way the file was written (indent, trailing newline), so diffs stay small."""
    indent = 1 if like.startswith("{\n \"") else 2
    path.write_text(json.dumps(data, indent=indent) + ("\n" if like.endswith("\n") else ""), encoding="utf-8")


def delinker() -> Path:
    """tools/delink.py: the project's, else the one decompdecomp makes PC projects with (it delinks those itself)."""
    own = common.ROOT / "tools" / "delink.py"
    if own.is_file():
        return own
    out = subprocess.run([common.app_python(), "-c", "import decompnet, pathlib; print(pathlib.Path(decompnet.__file__)"
                          ".parent / 'consoles/pc/project_template/tools/delink.py')"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return Path(out)


def split_and_try(p: Pair) -> dict:
    """Put p in its unit, move it into a unit of its own (its address's) built as p.split says, have the app
    re-read the project (it delinks the new unit), and try it. Short of a match, every file goes back."""
    src = common.source_path(p.unit)
    paths = [common.FUNCTIONS_JSON, common.FILE_FLAGS, common.FILE_COMPILERS, src]
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

    text = _unit_text(snap[src], [p])
    info = json.loads(snap[common.FUNCTIONS_JSON])
    entries = info["units"].get(p.unit) or []
    entry = next((e for e in entries if e.get("name") == p.symbol), None)
    if entry is None:
        return {"matched": False, "why": f"{p.symbol} isn't listed under {p.unit}"}
    new_unit = f"text_{int(entry['addr']):08X}"
    if new_unit in info["units"] or new_unit == p.unit:
        return {"matched": False, "why": f"{new_unit} already exists"}
    _, start, end = common.definition_span(text, p.symbol)
    starts = [st for e in entries if (st := common.definition_span(text, e["name"])[1]) >= 0]
    first = min(starts, default=start)
    new_path = common.source_path(new_unit)
    # the unit's declarations (the twin's among them) go with it: its prologue is everything before the first body
    new_path.write_text(text[:first].rstrip() + "\n\n" + text[start:end].rstrip() + "\n", encoding="utf-8")
    src.write_text(text[:start] + text[end:], encoding="utf-8")
    info["units"][p.unit] = [e for e in entries if e is not entry]
    info["units"][new_unit] = [entry]
    _save_json(common.FUNCTIONS_JSON, info, snap[common.FUNCTIONS_JSON])
    common._functions = None
    key_old, key_new = f"src/auto/{p.unit}.cpp", f"src/auto/{new_unit}.cpp"
    ff = common.file_flags()
    if key_old in ff:  # the split recipe: the new unit starts with the old one's flags
        ff[key_new] = list(ff[key_old])
        common.save_file_flags(ff)
    fc = common.file_compilers()
    if key_old in fc:
        fc[key_new] = fc[key_old]
        _save_json(common.FILE_COMPILERS, fc, snap.get(common.FILE_COMPILERS, "{}\n"))
    apply_pin(replace(p, unit=new_unit, pin=p.split))
    try:  # the new unit's target object: the project's delinker, as the app's own template has it
        subprocess.run([common.app_python(), "-I", str(delinker()), str(common.FUNCTIONS_JSON), str(common.TARGETS)],
                       cwd=common.ROOT, check=True, capture_output=True, text=True, timeout=600)
    except (subprocess.SubprocessError, OSError) as e:
        return undo(f"delink failed: {getattr(e, 'stderr', '') or e}")
    _wait_check()
    if not common.target_obj(new_unit).is_file():
        return undo("no target object for the new unit")
    try:
        out = common.try_function(p.symbol, p.code)
    except common.OutmatchError as e:
        out = {"matched": False, "text": str(e)}
    if not out.get("matched"):
        common.release(p.symbol)
        return undo("app: " + str(out.get("text", out)))
    common.log("twin-split", symbol=p.symbol, unit=new_unit, build=p.split)
    return {"matched": True, "unit": new_unit}


def seed_one(p: Pair, pct: float) -> bool:
    """A twin short of 100 but better than the app's best: make it the best (agents and the permuter start
    there). The app keeps an improving attempt in the file; anything else leaves the file as it was."""
    from . import search as srch
    fn = common.function_by_name(p.symbol)
    if not fn or pct <= srch.db_best_pct(fn) + 1e-6 or p.pin or p.split:
        return False
    path = common.source_path(p.unit)
    before = path.read_text(encoding="utf-8")
    ours = _unit_text(before, [p])
    path.write_text(ours, encoding="utf-8")
    try:
        common.try_function(p.symbol, p.code)
    except common.OutmatchError:
        pass
    common.release(p.symbol)
    after = common.function_by_name(p.symbol)
    if after and srch.db_best_pct(after) > srch.db_best_pct(fn) + 1e-6:
        return True  # the app took it as the best: its declarations stay with it
    if path.read_text(encoding="utf-8") == ours:  # the app undid only its own edit: ours goes too
        path.write_text(before, encoding="utf-8")
    return False


def submit_one(p: Pair, slot) -> dict:
    """/api/try one twin with its declarations written into the unit first (where _unit_text puts them). The app
    undoes only its definition on a miss: the declarations come back out here, so a miss leaves the file as it was."""
    path = common.source_path(p.unit)
    before = path.read_text(encoding="utf-8")
    ours = _unit_text(before, [p])
    pin = _build_args(p.unit, p.pin)
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
        matched_now = [n for (n,) in common.query("SELECT name FROM functions WHERE project_id=? AND unit=? AND "
                                                   "status='matched'", common.project_id(), p.unit)]
        if lost := [n for n in matched_now if not (got.get(n) or 0) >= 99.999]:  # (an earlier twin's pin, say)
            return {"matched": False, "text": "would cost matches in the unit as it is now: " + ", ".join(lost[:5])}
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
        seed: bool = True, polish: float = 0, near: float = 0, say=lambda line: print(line, flush=True)) -> dict:
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
    pairs, skipped = find_pairs(sources, min_size, near)
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
    tally = {"matched": 0, "exact": 0, "done": 0, "seeded": []}

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
            elif pct > 0 and submit and seed and not p.split:
                row["seeded"] = seed_one(p, pct)
                if row["seeded"]:
                    with lock:
                        tally["seeded"].append(p.symbol)
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
    if polish and submit:  # 3. the permuter on what the twins got close (each from the app's best: the twin's now)
        from . import search as srch
        for sym in tally["seeded"]:
            fn = common.function_by_name(sym)
            if not fn or common.query_one("SELECT status FROM functions WHERE id=?", fn["id"])[0] != "open":
                continue
            out = srch.search(fn, seconds=polish, jobs=jobs, submit_match=True)
            tally["matched"] += bool(out.matched)
            say(json.dumps({"symbol": sym, "polish_pct": round(out.best_pct, 3), "matched": out.matched}))
    if submit and (tally["exact"] or tally["seeded"]):  # a rejected try leaves the app's last object of that unit built from it: re-read
        _wait_check()
    matched, exact = tally["matched"], tally["exact"]
    summary = {"pairs": len(pairs), "exact_here": exact, "matched": matched, "seeded": len(tally["seeded"]),
               "skipped": len(skipped)}
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
