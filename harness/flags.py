"""Compiler-flag sweep, per unit, scored for every function at once.

MSVC6's flags belong to the file, and a few -- the optimisation level, frame-pointer omission, inlining -- change the
machine code of every function in it.  The app's own `search_flags` tries four variants and keeps one only when the
whole file still matches; this sweeps far wider and scores *every* function, so a set that fixes one function but
breaks another becomes a split instead of being thrown away.  `--apply` does that automatically: a clean set goes into
`file_flags.json`, a set that regresses unit mates moves the function into a unit of its own with those flags.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import common

LEVELS = ("/O1", "/O2", "/Ox", "/Os", "/Ot")
EXTRAS = ("/Oy-", "/Ob1", "/Ob2", "/Og", "/Oi", "/Oa", "/Ow", "/Op", "/Gf", "/Gy", "/GX-", "/G6")


# ------------------------------------------------------------------ flag vectors

def command_flags(unit: str) -> list[str]:
    """The compiler flags ninja passes for the unit, exactly (global CL_FLAGS then the file's own)."""
    argv = common.build_command(unit)
    return list(argv[argv.index("--") + 1:]) if "--" in argv else []


def file_flags(unit: str) -> list[str]:
    return list(common.file_flags().get(f"src/auto/{unit}.cpp", []))


def global_flags(unit: str) -> list[str]:
    """The flags that are not the file's own (command_flags minus its trailing file-specific ones)."""
    own, extra = command_flags(unit), file_flags(unit)
    if extra and own[-len(extra):] == extra:
        return own[:-len(extra)]
    return [f for f in own if f not in extra]


def base_global(unit: str) -> list[str]:
    """The global flags without an optimisation-level switch (the level is what the sweep varies)."""
    return [f for f in global_flags(unit) if f not in LEVELS]


def candidate_sets(unit: str) -> list[tuple[str, list[str]]]:
    base = base_global(unit)
    out: list[tuple[str, list[str]]] = [("as-built", command_flags(unit))]
    seen = {tuple(out[0][1])}
    for level in LEVELS:
        flags = [*base, level]
        if tuple(flags) not in seen:
            seen.add(tuple(flags))
            out.append((level, flags))
    for extra in EXTRAS:
        flags = [*base, extra]
        if tuple(flags) not in seen:
            seen.add(tuple(flags))
            out.append((extra, flags))
    for level in ("/O1", "/O2"):
        for extra in ("/Oy-", "/Ob1", "/Ob2"):
            flags = [*base, level, extra]
            if tuple(flags) not in seen:
                seen.add(tuple(flags))
                out.append((f"{level} {extra}", flags))
    return out


def delta_flags(unit: str, flags: list[str]) -> list[str]:
    """The part of `flags` that belongs in file_flags.json (what the global CL_FLAGS doesn't already provide)."""
    glob = global_flags(unit)
    return [f for f in flags if f not in glob]


# ------------------------------------------------------------------ sweep

@dataclass
class UnitSweep:
    unit: str
    rows: list[dict] = field(default_factory=list)
    wins: dict[str, dict] = field(default_factory=dict)   # function -> {"pct", "flags", "set"}
    regressions: dict[str, list[str]] = field(default_factory=dict)  # set name -> functions knocked off 100


def _unit_functions(unit: str) -> tuple[list[dict], list[str]]:
    """(open functions of the unit, matched function names of the unit)."""
    proj = common.project_id()
    open_rows = common.query(
        "SELECT name,best_score,n_insns FROM functions WHERE project_id=? AND unit=? AND status='open' "
        "AND best_source IS NOT NULL", proj, unit)
    matched = [r[0] for r in common.query(
        "SELECT name FROM functions WHERE project_id=? AND unit=? AND status='matched'", proj, unit)]
    return [{"name": r[0], "score": r[1], "n_insns": r[2]} for r in open_rows], matched


def sweep_unit(unit: str, jobs: int = common.JOBS) -> UnitSweep:
    from .search import Pool

    out = UnitSweep(unit)
    opens, matched = _unit_functions(unit)
    if not opens:
        return out
    text = common.source_path(unit).read_text(encoding="utf-8")
    pool = Pool(unit, jobs)
    target = common.target_obj(unit)
    try:
        sets = candidate_sets(unit)
        results: list[tuple[str, list[str], dict]] = []
        with ThreadPoolExecutor(max_workers=jobs) as pool_exec:
            def run(item: tuple[str, list[str]]) -> tuple[str, list[str], dict]:
                name, flags = item
                root = pool.free.get()
                try:
                    out_rel = f"build/{common.VERSION}/src/auto/{unit}.obj"
                    try:
                        obj = common.scratch_compile(root, unit, text, out_rel, flags)
                    except common.OutmatchError as e:
                        return name, flags, {"__error__": str(e)[-200:]}
                    return name, flags, common.scores(target, obj)
                finally:
                    pool.free.put(root)

            results = list(pool_exec.map(run, sets))
        for name, flags, scores in results:
            broken = [f for f in matched if scores.get(f) != 100.0]
            if broken:
                out.regressions[name] = broken
        for fn in opens:
            best = None
            for name, flags, scores in results:
                pct = scores.get(fn["name"])
                if pct is None:
                    continue
                if best is None or pct > best["pct"]:
                    best = {"pct": pct, "flags": flags, "set": name}
            if best:
                out.wins[fn["name"]] = best
                out.rows.append({"unit": unit, "symbol": fn["name"], "flags": best["flags"],
                                 "set": best["set"], "pct": round(best["pct"], 3),
                                 "was": fn["score"], "regressions": out.regressions.get(best["set"], [])})
    finally:
        pool.close()
    return out


def sweep(unit: str | None = None, all_units: bool = False, seconds: float = 0, jobs: int = common.JOBS):
    if unit:
        units = [unit]
    elif all_units:
        units = [r[0] for r in common.query(
            "SELECT DISTINCT unit FROM functions WHERE project_id=? AND status='open' AND best_source IS NOT NULL "
            "ORDER BY unit", common.project_id())]
    else:
        units = [r[0] for r in common.query(
            "SELECT DISTINCT unit FROM functions WHERE project_id=? AND status='open' AND best_source IS NOT NULL "
            "ORDER BY unit LIMIT 1", common.project_id())]
    started = time.monotonic()
    for u in units:
        if seconds and time.monotonic() - started > seconds:
            break
        result = sweep_unit(u, jobs)
        yield {"unit": u, "wins": result.wins, "regressions": result.regressions,
               "functions": len(result.wins)}


# ------------------------------------------------------------------ applying

def _clear_caches() -> None:
    """The flag/config change makes the cached compile command and scratch trees stale."""
    for path in (common.STATE / "build_commands.json",):
        path.unlink(missing_ok=True)
    shutil.rmtree(common.WORK, ignore_errors=True)


def _recompile(unit: str, flags: list[str], jobs: int = 1) -> dict[str, float | None]:
    from .search import Pool

    text = common.source_path(unit).read_text(encoding="utf-8")
    pool = Pool(unit, jobs)
    try:
        root = pool.free.get()
        try:
            obj = common.scratch_compile(root, unit, text, f"build/{common.VERSION}/src/auto/{unit}.obj", flags)
        finally:
            pool.free.put(root)
        return common.scores(common.target_obj(unit), obj)
    finally:
        pool.close()


def _check(unit: str, flags: list[str], expect: list[str]) -> dict:
    scores = _recompile(unit, flags)
    matched = [r[0] for r in common.query(
        "SELECT name FROM functions WHERE project_id=? AND unit=? AND status='matched'", common.project_id(), unit)]
    broken = [n for n in matched if scores.get(n) != 100.0]
    won = [n for n in expect if scores.get(n) == 100.0]
    return {"unit": unit, "flags": flags, "won": won, "broken": broken}


def _ninja(*args: str) -> None:
    res = subprocess.run(["ninja", *args], cwd=common.ROOT, capture_output=True, text=True)
    if res.returncode:
        raise common.OutmatchError((res.stdout + res.stderr).strip()[-1200:])


def _regenerate() -> None:
    """Let ninja's own configure rule read the edited configs (build.ninja lists them as dependencies)."""
    _ninja("build.ninja")


def apply_flags(unit: str, flags: list[str], expect: list[str]) -> dict:
    """Put a flag set that keeps every matched function at 100 into file_flags.json and let the app rebuild."""
    check = _check(unit, flags, expect)
    out = {**check, "applied": False}
    if check["broken"] or not check["won"]:
        out["why"] = ("would break " + ", ".join(check["broken"])) if check["broken"] else \
            "nothing reaches 100 with these flags"
        return out
    table = common.file_flags()
    table[f"src/auto/{unit}.cpp"] = delta_flags(unit, flags)
    common.save_file_flags(table)
    _clear_caches()
    _regenerate()
    _ninja(f"build/{common.VERSION}/src/auto/{unit}.obj")
    out["applied"] = True
    out["file_flags"] = table[f"src/auto/{unit}.cpp"]
    common.log("flags", unit=unit, flags=out["file_flags"], won=check["won"])
    return out


def split_out(unit: str, symbol: str, flags: list[str], expect: list[str] | None = None) -> dict:
    """Move one function into `src/auto/text_<addr>.cpp` with its own flags, leaving the rest of the unit alone.

    For PC manifest forks the app has no split-file tool (that is a console/module feature), so this is the same
    surgery by hand: the unit's declaration prologue, the definition, a functions.json entry moved to the new unit,
    a file_flags.json entry -- then ninja reconfigures and delinks, and the app re-measures.
    """
    from . import csource

    info = common.functions()
    entries = info["units"].get(unit) or []
    entry = next((e for e in entries if e.get("name") == symbol), None)
    if entry is None:
        return {"symbol": symbol, "unit": unit, "split": None, "why": f"{symbol} is not in functions.json under {unit}"}
    new_unit = f"text_{int(entry['addr']):08X}"
    if new_unit in info["units"]:
        return {"symbol": symbol, "unit": unit, "split": None, "why": f"{new_unit} already exists"}
    text = common.source_path(unit).read_text(encoding="utf-8")
    own, start, end = csource.find(text, [], symbol)
    if start < 0:
        return {"symbol": symbol, "unit": unit, "split": None, "why": "definition not found"}
    starts = [s for _, s, _ in (csource.find(text, [], e["name"]) for e in entries)]
    first = min([s for s in starts if s >= 0], default=start)
    prologue, definition = text[:first], text[start:end]
    new_path = common.source_path(new_unit)
    new_text = prologue.rstrip() + "\n\n" + definition.rstrip() + "\n"
    new_path.write_text(new_text, encoding="utf-8")
    common.source_path(unit).write_text(text[:start] + text[end:], encoding="utf-8")
    info["units"][unit] = [e for e in entries if e.get("name") != symbol]
    info["units"][new_unit] = [entry]
    common.save_functions(info)
    table = common.file_flags()
    table[f"src/auto/{new_unit}.cpp"] = delta_flags(unit, flags)
    common.save_file_flags(table)
    _clear_caches()
    try:
        _regenerate()
        _ninja(f"build/{common.VERSION}/target/{new_unit}.obj")
    except common.OutmatchError as e:
        # undo the config so the project stays buildable
        new_path.unlink(missing_ok=True)
        common.source_path(unit).write_text(text, encoding="utf-8")
        info["units"][unit] = entries
        info["units"].pop(new_unit, None)
        common.save_functions(info)
        table.pop(f"src/auto/{new_unit}.cpp", None)
        common.save_file_flags(table)
        _clear_caches()
        _regenerate()
        return {"symbol": symbol, "unit": unit, "split": None, "why": f"target object failed: {e}"[:600]}
    check = _check(new_unit, flags, expect or [symbol])
    out = {"symbol": symbol, "unit": unit, "split": new_unit, "flags": table[f"src/auto/{new_unit}.cpp"],
           "won": check["won"], "broken": check["broken"], "applied": True}
    common.log("split", symbol=symbol, old=unit, new=new_unit, flags=out["flags"], won=check["won"])
    return out


def apply_best(unit: str, symbol: str, jobs: int = common.JOBS) -> dict:
    """Sweep, then apply the best set for `symbol` -- whole-unit if clean, as a split if it regresses the unit."""
    result = sweep_unit(unit, jobs)
    win = result.wins.get(symbol)
    if not win:
        return {"symbol": symbol, "unit": unit, "applied": False, "why": "no flag set improved it"}
    if win["pct"] < 100:
        return {"symbol": symbol, "unit": unit, "applied": False,
                "why": f"best set {win['set']} reaches {win['pct']:.3f}%, not 100",
                "flags": win["flags"]}
    clean = not result.regressions.get(win["set"])
    out = apply_flags(unit, win["flags"], [symbol]) if clean else split_out(unit, symbol, win["flags"], [symbol])
    return {"symbol": symbol, "unit": unit, "set": win["set"], "flags": win["flags"], **out}
