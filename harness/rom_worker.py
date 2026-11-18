"""Compile and judge C for a ROM project's function, the way decompdecomp's judge does it.

Runs under the app's own Python (it imports decompnet's compiler, console and scoring code; the harness itself stays
stdlib) and reads the app's database read-only. JSON lines on stdin/stdout:

  {"op": "setup", "slug": "yugiohgxda-2"}                    -> {"ok": true, "family": "agbcc", "toolchain": ..., "flags": [...]}
  {"op": "score", "name": "sub_0800A1B4", "sources": [...],
   "compiler": {"toolchain": ..., "flags": [...]} | null}      -> {"results": [{"pct", "score", "exact", "error"}...]}
  {"op": "toolchains"}                                         -> {"toolchains": [ids of the same family, installed]}
  {"op": "close"}                                              -> {"done": true}

A candidate is linked at the function's address and compared with the ROM (console.link_function, as the app's
judge for ROM projects); `pct` comes from the app's diff against the target object. The app's /api/try stays the
verdict: this only ranks variants.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

DB = Path(os.environ.get("DECOMPNET_HOME", Path.home() / "Decomp" / ".decomp")) / "decomp.db"
S: dict = {}


def _db():
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def setup(slug: str) -> dict:
    from decompnet import consoles, project
    from decompnet.rom import split
    from decompnet.rom.symbols import Symbol, SymbolTable

    db = _db()
    proj = dict(db.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone())
    cfg = json.loads(proj["config"])
    rows = db.execute("SELECT s.name, s.address, s.size, s.kind, f.id IS NOT NULL AS fn FROM symbols s LEFT JOIN "
                      "functions f ON f.project_id=s.project_id AND f.name=s.name WHERE s.project_id=?",
                      (proj["id"],)).fetchall()
    syms = SymbolTable([Symbol(r["name"], r["address"], r["size"], r["kind"]) for r in rows])
    for r in rows:  # (as the app's symbols(): a function's own name the address's other name hid still resolves)
        if r["fn"] and r["name"] not in syms.by_name:
            syms.alias(Symbol(r["name"], r["address"], r["size"], r["kind"]))
    rom = (Path(proj["root"]) / cfg["rom"]["file"]).read_bytes()
    segs = [split.Segment(**s) for s in cfg["segments"]]
    S.update(db=db, proj=proj, cfg=cfg, syms=syms, rom=rom, segs=segs, console=consoles.of(cfg),
             compiler_for=project.compiler_for, compiler_config=project.compiler_config)
    tc = cfg["compiler"].get("toolchain", "") if isinstance(cfg.get("compiler"), dict) else ""
    return {"ok": True, "family": family(tc), "toolchain": tc, "flags": cfg["compiler"].get("flags", []),
            "platform": cfg.get("platform")}


def family(toolchain: str) -> str:
    """agbcc, old_agbcc, agbcc_arm and own copies of them are one family; gcc-likes another; msvc; mwcc; ido."""
    t = toolchain.lower()
    for name in ("agbcc", "msvc", "mwcc", "ido", "gcc"):
        if name in t:
            return name
    from decompnet import kits  # an own copy says what it is like
    own = kits.own_compiler(toolchain)
    return family(own.like) if own is not None and own.like else t.split("-")[0]


def toolchains() -> list[str]:
    """Installed compilers of the project's compiler's family: the app's pinned ones and the user's own copies."""
    from decompnet import kits, toolchains as tcs

    mine = S["cfg"]["compiler"].get("toolchain", "")
    want = family(mine)
    out = {mine}
    for tid in tcs.registry():
        if family(tid) == want:
            try:
                if tcs.installed(tid) is not None:
                    out.add(tid)
            except Exception:  # noqa: BLE001  (a pack this platform can't run)
                pass
    for own in kits.compilers(k.id for k in kits.KITS):
        if family(own.version) == want or (own.like and family(own.like) == want):
            out.add(own.version)
    return sorted(out)


def _reader(fn):
    rom, segs = S["rom"], S["segs"]

    def read(addr: int, n: int):
        cands = [s for s in segs if s.contains(addr, n)]
        if not cands:
            return None
        at = cands[0].rom_start + addr - cands[0].vram
        return rom[at:at + n]
    return read


def score(name: str, sources: list[str], compiler: dict | None) -> list[dict]:
    from decompnet import scoring
    from decompnet.compiler import compile_source

    db, proj, cfg, console, syms = S["db"], S["proj"], S["cfg"], S["console"], S["syms"]
    fn = dict(db.execute("SELECT * FROM functions WHERE project_id=? AND name=?", (proj["id"], name)).fetchone())
    unit = fn.get("unit") or ""
    ctx = db.execute("SELECT header FROM context_versions WHERE project_id=? AND unit IN (?, '') "
                     "ORDER BY unit=? DESC, version DESC LIMIT 1", (proj["id"], unit, unit)).fetchone()
    header = ctx["header"] if ctx else ""
    comp = {**S["compiler_for"](cfg, fn["vram"]), **(compiler or {})}
    ccfg = S["compiler_config"](Path(proj["root"]), {**cfg, "compiler": comp})
    link_target = console.target_from_row(fn, cfg)
    diff_target = console.target_object(fn, cfg, syms)
    read = _reader(fn)
    out = []
    for src in sources:
        try:
            res = compile_source(ccfg, header, src)
            if not res.ok:
                out.append({"pct": -1.0, "error": (res.log or "compile failed")[-300:]})
                continue
            linked = console.link_function(res.obj, fn["name"], link_target, syms, read)
            if linked.candidate is None:
                out.append({"pct": 0.0, "error": "; ".join(linked.problems)[:300] or "not linked"})
                continue
            d = scoring.diff(diff_target, linked.candidate)
            exact = bool(linked.exact)
            pct = 100.0 if exact else max(0.0, 100.0 * (1 - d.score / max(1, d.max_score)))
            out.append({"pct": round(pct, 3), "score": d.score, "exact": exact})
        except Exception as e:  # noqa: BLE001  (one variant's trouble is that variant's)
            out.append({"pct": -1.0, "error": f"{type(e).__name__}: {e}"[:300]})
    return out


def main() -> int:
    for line in sys.stdin:
        req = json.loads(line)
        op = req.get("op")
        try:
            if op == "setup":
                res = setup(req["slug"])
            elif op == "score":
                res = {"results": score(req["name"], req["sources"], req.get("compiler"))}
            elif op == "toolchains":
                res = {"toolchains": toolchains()}
            elif op == "close":
                print(json.dumps({"done": True}), flush=True)
                return 0
            else:
                res = {"error": f"unknown op {op!r}"}
        except Exception as e:  # noqa: BLE001
            res = {"error": f"{type(e).__name__}: {e}"[:500]}
        print(json.dumps(res), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
