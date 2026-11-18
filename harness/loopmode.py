"""Loop mode: work the near-miss queue without naming a function, and remember what has been tried.

The ledger is an append-only JSONL file (`.outmatch/ledger.jsonl`); visit counts, per-phase attempts and the source
hash at the time of the visit are derived from it.  A function whose unit source changed since its last visit is
retried (the baseline is new); the rest are skipped unless `--again` is given.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from . import common, search

LEDGER = common.STATE / "ledger.jsonl"
PHASES = ("msvc", "levers", "pragma", "permuter", "compiler", "flags")
MSVC_ONLY = {"msvc", "levers", "pragma", "compiler", "flags"}  # Visual C++ idioms, #pragmas, cl versions and flags


def _now() -> float:
    return round(time.time(), 3)


def sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


class Ledger:
    """Append-only record of every phase run, with the summaries the loop needs."""

    def __init__(self, path: Path = LEDGER):
        self.path = path
        self.rows: list[dict] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    self.rows.append(json.loads(line))
                except ValueError:
                    continue  # a half-written last line from a killed run
        self._sum: dict[str, dict] = {}
        for r in self.rows:
            self._add(r)

    def _add(self, r: dict) -> None:
        s = self._sum.setdefault(r.get("symbol", ""), {"visits": 0, "best_pct": -1.0, "matched": False,
                                                       "phases": {}, "last": 0.0})
        if r.get("phase") == "visit":
            s["visits"] += 1
        else:
            p = s["phases"].setdefault(r["phase"], {"n": 0, "best_pct": -1.0, "src_sha": "", "last": 0.0})
            p["n"] += 1
            p["last"] = r.get("t") or 0.0
            p["src_sha"] = r.get("src_sha") or p["src_sha"]
            if r.get("pct", -1) > p["best_pct"]:
                p["best_pct"] = r["pct"]
        if r.get("pct", -1) > s["best_pct"]:
            s["best_pct"] = r["pct"]
        s["matched"] = bool(s["matched"] or r.get("matched"))
        s["last"] = max(s["last"], r.get("t") or 0.0)

    def record(self, **row) -> None:
        row.setdefault("t", _now())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.rows.append(row)
        self._add(row)

    def summary(self, symbol: str) -> dict:
        return self._sum.get(symbol) or {"visits": 0, "best_pct": -1.0, "matched": False, "phases": {}, "last": 0.0}

    def done(self, symbol: str, phase: str, src_sha: str) -> bool:
        """Whether this phase already ran on this exact source."""
        p = self.summary(symbol)["phases"].get(phase)
        return bool(p and p["src_sha"] == src_sha)

    def report(self) -> dict:
        phases: dict[str, dict] = {}
        matched = improvements = visits = 0
        for s in self._sum.values():
            visits += s["visits"]
            matched += int(s["matched"])
            for name, p in s["phases"].items():
                d = phases.setdefault(name, {"runs": 0, "functions": 0, "matches": 0, "best": 0.0})
                d["runs"] += p["n"]
                d["functions"] += 1
                d["best"] = max(d["best"], p["best_pct"])
                d["matches"] += int(p["best_pct"] >= 100.0)
            improvements += int(any(p["best_pct"] >= 100.0 for p in s["phases"].values()))
        return {"ledger": str(self.path), "functions": len(self._sum), "visits": visits, "matched": matched,
                "phases": phases, "rows": len(self.rows)}


def runnable(phases: tuple[str, ...]) -> tuple[str, ...]:
    """The phases this project's compiler can use (the MSVC ones only for Visual C++)."""
    return phases if common.is_msvc() else tuple(p for p in phases if p not in MSVC_ONLY)


def pending(fn: dict, phases: tuple[str, ...], ledger: Ledger) -> bool:
    """Whether any requested phase is still worth running on the unit as it is now."""
    try:
        src = sha(_unit_text(fn))
    except OSError:
        return False
    return any(not ledger.done(fn["name"], p, src) for p in runnable(phases))


def pct_of(fn: dict) -> float:
    """The draft's approximate objdiff percentage: the app's score is hundredths of a percent times the insns.

    A function in `.outmatch/stale.json` uses the percentage its own body actually scores: the app's stored best can
    be unreachable from the current tree (it was recorded against a different one), and queueing on it wastes passes.
    """
    row = common.stale().get(fn["name"])
    if row and row.get("measured") is not None:
        return float(row["measured"])
    return 100.0 - 100.0 * (fn.get("score") or 0) / (100.0 * max(fn.get("n_insns") or 1, 1))


def candidates(max_score: float | None = 0.06, unit: str = "", limit: int = 0, any_permuted: bool = False,
               ledger: Ledger | None = None, phases: tuple[str, ...] = PHASES, again: bool = False,
               min_pct: float = 0.0, order: str = "fresh") -> list[dict]:
    """Open functions with a draft, never visited (or whose source changed) first, then worst relative score."""
    ledger = ledger or Ledger()
    if min_pct:
        # a minimum percentage is the same bound as a maximum relative distance
        bound = (100.0 - min_pct) / 100.0
        max_score = bound if max_score is None else min(max_score, bound)
    claimed = {r[0] for r in common.query("SELECT f.name FROM claims c JOIN functions f ON f.id=c.function_id")}
    fns = common.open_functions(max_score=max_score, only_permuted=not any_permuted)
    out = []
    for fn in fns:
        if fn["name"] in claimed or (unit and fn["unit"] != unit):
            continue
        if not again and not pending(fn, phases, ledger):
            continue
        out.append(fn)
    def key(fn: dict):
        s = ledger.summary(fn["name"])
        ratio = fn["score"] / max(fn["n_insns"], 1)
        return (ratio, s["visits"], fn["n_insns"]) if order == "closest" else (s["visits"], ratio, fn["n_insns"])
    out.sort(key=key)
    return out[:limit] if limit else out


def _unit_text(fn: dict) -> str:
    return common.source_path(fn["unit"]).read_text(encoding="utf-8")


def visit(fn: dict, ledger: Ledger, phases: tuple[str, ...] = PHASES, seconds: float = 900.0,
          jobs: int = common.JOBS, count: int = 64, seed: int = 0, submit_match: bool = True,
          again: bool = False, apply_best: bool = False, depth: int = 1, beam: int = 1, cap: int = 160,
          focus: bool = True,
          say=print) -> dict:
    """Run the requested phases on one function, best-first, and record each in the ledger."""
    symbol, unit = fn["name"], fn["unit"]
    text = _unit_text(fn)
    src_sha = sha(text)
    started = time.monotonic()
    result = {"symbol": symbol, "unit": unit, "phases": [], "matched": False, "best_pct": -1.0}

    msvc = common.is_msvc()
    for name in phases:
        if name in MSVC_ONLY and not msvc:
            result["phases"].append({"phase": name, "skipped": "not an MSVC project"})
            continue
        if not again and ledger.done(symbol, name, src_sha):
            result["phases"].append({"phase": name, "skipped": "already tried on this source"})
            continue
        left = seconds - (time.monotonic() - started)
        if left <= 0:
            result["phases"].append({"phase": name, "skipped": "budget spent"})
            say(json.dumps({"symbol": symbol, "phase": name, "skipped": "budget spent"}))
            continue
        say(json.dumps({"symbol": symbol, "phase": name, "start": True, "budget": round(left, 1)}))
        try:
            if name == "msvc":
                from . import tricks
                if depth > 1:
                    chain = tricks.ascend(fn, jobs=jobs, depth=depth, beam=beam, cap=cap,
                                          submit_match=submit_match, apply_best=apply_best, focus=focus)
                    out = {"symbol": symbol, "variants": chain.get("variants"), "best_pct": chain.get("best_pct"),
                           "best_label": chain.get("best_label"), "matched": chain.get("matched"),
                           "submitted": chain.get("submitted") or {}, "depth": chain.get("depth"),
                           "rows": chain.get("steps") or []}
                else:
                    out = tricks.run(fn, jobs=jobs, submit_match=submit_match, apply_best=apply_best,
                                     focus=focus)
            elif name == "levers":
                from . import levers
                out = levers.apply(symbol, jobs=jobs, submit_match=submit_match, apply_best=apply_best)
            elif name == "pragma":
                from . import pragma
                out = pragma.sweep(fn, jobs=jobs, submit_match=submit_match, apply_best=apply_best)
            elif name == "permuter":
                got = search.search(fn, seconds=max(5.0, left), jobs=jobs, count=count, seed=seed,
                                    submit_match=submit_match, say=say, apply_best=apply_best)
                out = {"best_pct": got.best_pct, "matched": got.matched, "submitted": got.submitted,
                       "note": got.note}
            elif name == "compiler":
                from . import compilers
                out = compilers.sweep(fn)
                compilers.record(out)
                if out["best"] != out["configured"] and out["gain"] > 0:
                    out["note"] = (f"{out['best_pct']}% with {out['best']}, "
                                   f"{out['pct'].get(out['configured'])}% with {out['configured']}")
            elif name == "flags":
                from . import csource, flags
                out = flags.try_for(symbol, jobs=jobs)
                if out.get("applied"):
                    if submit_match:
                        now = _unit_text(fn)
                        _, start, end = common.definition_span(now, symbol)
                        if start >= 0:
                            helpers, definition = csource.split_extra(now[start:end])
                            answer = search.submit(symbol, definition, helpers, fn=fn)
                            out["submitted"] = answer
                            out["matched"] = bool(answer.get("matched"))
                else:
                    out["note"] = f"{out.get('why', 'not applied')} (best {out.get('pct')}%)"
                    out["pct"] = -1.0
            else:
                result["phases"].append({"phase": name, "skipped": "unknown phase"})
                continue
        except common.OutmatchError as e:
            result["phases"].append({"phase": name, "error": str(e)[:400]})
            ledger.record(symbol=symbol, unit=unit, phase=name, src_sha=src_sha, pct=-1, error=str(e)[:400])
            say(json.dumps({"symbol": symbol, "phase": name, "error": str(e)[:400]}))
            continue
        raw = out.get("best_pct")
        pct = float(raw) if raw is not None else -1.0
        matched = bool(out.get("matched"))
        row = {"symbol": symbol, "unit": unit, "phase": name, "src_sha": src_sha, "pct": pct,
               "matched": matched, "submitted": bool(out.get("submitted")),
               "note": str(out.get("note") or "")[:200]}
        ledger.record(**row)
        result["phases"].append({"phase": name, "pct": round(pct, 3), "matched": matched,
                                 "submitted": bool(out.get("submitted"))})
        result["best_pct"] = max(result["best_pct"], pct)
        result["matched"] = result["matched"] or matched
        say(json.dumps({**row, "pct": round(pct, 3), "tries": out.get("tries"),
                        "variants": out.get("variants"), "applied": bool(out.get("won")),
                        "seconds": round(time.monotonic() - started, 1)}))
        if matched:
            break
    result["seconds"] = round(time.monotonic() - started, 1)
    ledger.record(symbol=symbol, unit=unit, phase="visit", src_sha=src_sha, pct=result["best_pct"],
                  matched=result["matched"], seconds=result["seconds"], base_score=fn["score"],
                  n_insns=fn["n_insns"])
    return result


def twin_sources() -> list[Path]:
    """Every project the app knows on this project's platform (this one too: its own duplicates)."""
    mine = json.loads(common.query_one("SELECT config FROM projects WHERE slug=?", common.SLUG)[0] or "{}")
    out = []
    for root, cfg in common.query("SELECT root, config FROM projects"):
        cfg = json.loads(cfg or "{}")
        if root and cfg.get("kind") == "fork" and cfg.get("platform") == mine.get("platform") and Path(root).is_dir():
            out.append(Path(root))
    return out


def twins_pass(submit: bool, jobs: int, near: float, say) -> int:
    """The twins phase: matched code from every project (new matches since the last pass) on this one's open
    functions; pairs already tried with the same code are skipped (twins' memo). Returns matches."""
    from . import twins
    cfg = json.loads(common.query_one("SELECT config FROM projects WHERE slug=?", common.SLUG)[0] or "{}")
    if cfg.get("platform") != "pc":  # twins reads PC (COFF) objects
        say(json.dumps({"phase": "twins", "skipped": f"not a PC project ({cfg.get('platform')})"}))
        return 0
    try:
        out = twins.run(twin_sources(), jobs=jobs, submit=submit, near=near,
                        say=lambda line: say(json.dumps({"phase": "twins", **json.loads(line)})))
    except common.OutmatchError as e:
        say(json.dumps({"phase": "twins", "error": str(e)[:300]}))
        return 0
    return int(out.get("matched", 0))


def loop(max_score: float | None, unit: str, limit: int, seconds: float, jobs: int, count: int, seed: int,
         submit_match: bool, again: bool, forever: bool, idle: float, phases: tuple[str, ...],
         any_permuted: bool = False, min_pct: float = 0.0, order: str = "fresh", apply_best: bool = False,
         depth: int = 1, beam: int = 1, cap: int = 160, focus: bool = True, say=print, twins: bool = False,
         near: float = 0.85) -> dict:
    ledger = Ledger()
    total = {"visited": 0, "matched": 0, "phases": 0}
    try:
        while True:
            if twins:  # first: free matches from code matched anywhere since the last pass
                total["matched"] += twins_pass(submit_match, jobs, near, say)
            todo = candidates(max_score=max_score, unit=unit, limit=limit, any_permuted=any_permuted,
                              ledger=ledger, min_pct=min_pct, order=order) if phases else []
            if not todo:
                if not forever:
                    break
                say(json.dumps({"idle": round(idle, 1), "note": "no candidate left; sleeping"}))
                time.sleep(idle)
                continue
            for fn in todo:
                say(json.dumps({"symbol": fn["name"], "unit": fn["unit"], "score": fn["score"],
                                "insns": fn["n_insns"], "pct": round(pct_of(fn), 3),
                                "visits": ledger.summary(fn["name"]).get("visits", 0)}))
                res = visit(fn, ledger, phases=phases, seconds=seconds, jobs=jobs, count=count, seed=seed,
                            submit_match=submit_match, again=again, apply_best=apply_best, depth=depth,
                            beam=beam, cap=cap, focus=focus, say=say)
                total["visited"] += 1
                total["matched"] += int(res["matched"])
                total["phases"] += len([p for p in res["phases"] if "skipped" not in p and "error" not in p])
                say(json.dumps({"done": fn["name"], "best_pct": round(res["best_pct"], 3),
                                "matched": res["matched"], "seconds": res["seconds"]}))
            if not forever:
                break
    except KeyboardInterrupt:
        say(json.dumps({"interrupted": True, **total}))
    return total
