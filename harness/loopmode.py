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
PHASES = ("levers", "pragma", "permuter")


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


def pending(fn: dict, phases: tuple[str, ...], ledger: Ledger) -> bool:
    """Whether any requested phase is still worth running on the unit as it is now."""
    try:
        src = sha(_unit_text(fn))
    except OSError:
        return False
    return any(not ledger.done(fn["name"], p, src) for p in phases)


def candidates(max_score: float | None = 0.06, unit: str = "", limit: int = 0, any_permuted: bool = False,
               ledger: Ledger | None = None, phases: tuple[str, ...] = PHASES, again: bool = False) -> list[dict]:
    """Open functions with a draft, never visited (or whose source changed) first, then worst relative score."""
    ledger = ledger or Ledger()
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
        return (s["visits"], fn["score"] / max(fn["n_insns"], 1), fn["n_insns"])
    out.sort(key=key)
    return out[:limit] if limit else out


def _unit_text(fn: dict) -> str:
    return common.source_path(fn["unit"]).read_text(encoding="utf-8")


def visit(fn: dict, ledger: Ledger, phases: tuple[str, ...] = PHASES, seconds: float = 900.0,
          jobs: int = common.JOBS, count: int = 64, seed: int = 0, submit_match: bool = True,
          again: bool = False, say=print) -> dict:
    """Run the requested phases on one function, best-first, and record each in the ledger."""
    symbol, unit = fn["name"], fn["unit"]
    text = _unit_text(fn)
    src_sha = sha(text)
    started = time.monotonic()
    result = {"symbol": symbol, "unit": unit, "phases": [], "matched": False, "best_pct": -1.0}

    for name in phases:
        if not again and ledger.done(symbol, name, src_sha):
            result["phases"].append({"phase": name, "skipped": "already tried on this source"})
            continue
        left = seconds - (time.monotonic() - started)
        if left <= 0:
            result["phases"].append({"phase": name, "skipped": "budget spent"})
            continue
        try:
            if name == "levers":
                from . import levers
                out = levers.apply(symbol, jobs=jobs, submit_match=submit_match)
            elif name == "pragma":
                from . import pragma
                out = pragma.sweep(fn, jobs=jobs, submit_match=submit_match)
            elif name == "permuter":
                got = search.search(fn, seconds=max(5.0, left), jobs=jobs, count=count, seed=seed,
                                    submit_match=submit_match)
                out = {"best_pct": got.best_pct, "matched": got.matched, "submitted": got.submitted,
                       "note": got.note}
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
        say(json.dumps({"symbol": symbol, "unit": unit, "phase": name, "pct": round(pct, 3), "matched": matched}))
        if matched:
            break
    result["seconds"] = round(time.monotonic() - started, 1)
    ledger.record(symbol=symbol, unit=unit, phase="visit", src_sha=src_sha, pct=result["best_pct"],
                  matched=result["matched"], seconds=result["seconds"], base_score=fn["score"],
                  n_insns=fn["n_insns"])
    return result


def loop(max_score: float | None, unit: str, limit: int, seconds: float, jobs: int, count: int, seed: int,
         submit_match: bool, again: bool, forever: bool, idle: float, phases: tuple[str, ...],
         any_permuted: bool = False, say=print) -> dict:
    ledger = Ledger()
    total = {"visited": 0, "matched": 0, "phases": 0}
    try:
        while True:
            todo = candidates(max_score=max_score, unit=unit, limit=limit, any_permuted=any_permuted, ledger=ledger)
            if not todo:
                if not forever:
                    break
                say(json.dumps({"idle": round(idle, 1), "note": "no candidate left; sleeping"}))
                time.sleep(idle)
                continue
            for fn in todo:
                res = visit(fn, ledger, phases=phases, seconds=seconds, jobs=jobs, count=count, seed=seed,
                            submit_match=submit_match, again=again, say=say)
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
