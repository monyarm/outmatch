"""Command line for outmatch.  Every subcommand prints one JSON line per function (and a summary at the end)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if sys.argv[1:2] == ["autopilot"]:  # before common: `autopilot --all` runs outside any project
    from .autopilot import main as _autopilot
    sys.exit(_autopilot(sys.argv[2:]))

from . import common, draft, csource, search, restore


def _say(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _function(name: str) -> dict:
    row = common.query_one("SELECT id,name,unit,n_insns,size,best_score,best_source,permuted,meta "
                           "FROM functions WHERE project_id=? AND name=?", common.project_id(), name)
    if not row:
        raise SystemExit(f"no function {name!r} in the project")
    fid, name, unit, n_insns, size, score, src, permuted, meta = row
    meta = json.loads(meta) if meta else {}
    return {"id": fid, "name": name, "unit": unit or meta.get("source", ""),
            "n_insns": n_insns, "size": size, "score": score, "source": src, "permuted": permuted, "meta": meta}


def cmd_search(args) -> int:
    fn = _function(args.name)
    out = search.search(fn, seconds=args.seconds, jobs=args.jobs, count=args.count,
                        seed=args.seed, submit_match=not args.no_submit)
    _say({"symbol": out.symbol, "best_pct": round(out.best_pct, 3), "matched": out.matched,
          "tries": out.tries, "seconds": round(out.seconds, 1), "submitted": out.submitted, "note": out.note})
    return 0 if out.matched or out.best_pct >= 0 else 1


def cmd_run(args) -> int:
    fns = common.open_functions(max_score=args.max_ratio, only_permuted=not args.any, limit=args.limit)
    claimed = {r[0] for r in common.query("SELECT f.name FROM claims c JOIN functions f ON f.id=c.function_id")}
    fns = [f for f in fns if f["name"] not in claimed]
    done = matched = 0
    for fn in fns:
        out = search.search(fn, seconds=args.seconds, jobs=args.jobs, count=args.count,
                            submit_match=not args.no_submit)
        _say({"symbol": out.symbol, "best_pct": round(out.best_pct, 3), "matched": out.matched,
              "tries": out.tries, "seconds": round(out.seconds, 1), "note": out.note})
        done += 1
        matched += int(out.matched)
        if out.matched and args.stop_on_match:
            break
    _say({"done": done, "matched": matched})
    return 0


def cmd_submit(args) -> int:
    text = open(args.file, encoding="utf-8").read()
    res = search.submit(args.name, text)
    _say({"symbol": args.name, **res})
    return 0 if res.get("matched") else 1


def cmd_status(args) -> int:
    info = common.api("/api/state")
    rows = common.query("SELECT status,count(*) FROM functions WHERE project_id=? GROUP BY status", common.project_id())
    if args.full or not isinstance(info, dict):
        _say({"db": dict(rows), "app": info if args.full else str(info)[:300]})
        return 0
    picked = {k: info[k] for k in ("project", "slug", "name", "running", "autopilot", "stats", "jobs",
                                   "compiler", "progress") if k in info}
    agents = info.get("agents")
    if isinstance(agents, list):
        picked["agents_installed"] = [a.get("name") for a in agents if a.get("installed")]
    _say({"db": dict(rows), "app": picked})
    return 0


def cmd_release(args) -> int:
    common.release(args.name)
    _say({"released": args.name})
    return 0


def cmd_flags(args) -> int:
    from . import flags as flags_mod
    if args.apply:
        unit = args.unit or common.unit_of(args.apply)
        _say(flags_mod.apply_best(unit, args.apply, jobs=args.jobs))
        return 0
    for out in flags_mod.sweep(unit=args.unit, all_units=args.all, seconds=args.seconds, jobs=args.jobs):
        _say(out)
    return 0


def cmd_pragma(args) -> int:
    from . import pragma as pragma_mod
    fn = _function(args.name)
    _say(pragma_mod.sweep(fn, jobs=args.jobs, submit_match=not args.no_submit))
    return 0


def cmd_levers(args) -> int:
    from . import levers
    _say(levers.apply(args.name, only=args.only, jobs=args.jobs, submit_match=not args.no_submit))
    return 0


def cmd_msvc(args) -> int:
    """Try the MSVC6 trick catalogue (the skill's symptom->lever table) on one function."""
    from . import tricks
    if args.list:
        for name, _ in tricks.GROUPS:
            print(name)
        return 0
    fn = _function(args.name)
    definition = Path(args.file).read_text(encoding="utf-8") if args.file else None
    if args.depth > 1:
        out = tricks.ascend(fn, only=args.only, cap=args.cap, jobs=args.jobs, depth=args.depth, beam=args.beam,
                            submit_match=not args.no_submit, apply_best=args.apply, definition=definition,
                            focus=args.focus != "off")
        for step in out.get("steps") or []:
            print(f"{step['pct']:8.3f}  {step['label']}")
        out.pop("definition", None)
        _say(out)
        return 0 if out.get("matched") else 1
    out = tricks.run(fn, only=args.only, cap=args.cap, jobs=args.jobs, submit_match=not args.no_submit,
                     apply_best=args.apply, definition=definition, focus=args.focus != "off")
    for row in sorted(out.get("rows") or [], key=lambda r: r["pct"], reverse=True):
        print(f"{row['pct']:8.3f}  {row['label']}" + (f"  ({row['error']})" if row.get("error") else ""))
    _say({k: v for k, v in out.items() if k != "rows"})
    return 0 if out.get("matched") else 1


def cmd_loop(args) -> int:
    """Work the queue without naming functions: levers, pragmas and the permuter, in that order."""
    from . import loopmode
    phases = tuple(p.strip() for p in args.phases.split(",") if p.strip())
    if phases == ("all",):
        phases = loopmode.PHASES
    bad = [p for p in phases if p not in loopmode.PHASES]
    if bad:
        raise SystemExit(f"unknown phase(s): {', '.join(bad)} (choose from {', '.join(loopmode.PHASES)})")
    ledger = loopmode.Ledger()
    todo = loopmode.candidates(max_score=args.max_ratio, unit=args.unit, limit=args.limit or 0,
                               any_permuted=args.any, ledger=ledger, phases=phases, again=args.again,
                               min_pct=args.min_pct, order=args.order)
    if args.dry_run:
        for fn in todo:
            s = ledger.summary(fn["name"])
            _say({"symbol": fn["name"], "unit": fn["unit"], "score": fn["score"], "insns": fn["n_insns"],
                  "pct": round(loopmode.pct_of(fn), 3), "visits": s["visits"],
                  "best_pct": round(s["best_pct"], 3),
                  "phases": {k: v["n"] for k, v in s["phases"].items()}})
        _say({"candidates": len(todo)})
        return 0
    out = loopmode.loop(max_score=args.max_ratio, unit=args.unit, limit=args.limit or 0, seconds=args.seconds,
                        jobs=args.jobs, count=args.count, seed=args.seed, submit_match=not args.no_submit,
                        again=args.again, forever=args.forever, idle=args.idle, phases=phases,
                        any_permuted=args.any, min_pct=args.min_pct, order=args.order,
                        apply_best=args.apply_improvements, depth=args.depth, beam=args.beam, cap=args.cap,
                        focus=args.focus != "off")
    _say({**out, "queued": len(todo)})
    return 0



def cmd_compiler(args) -> int:
    from . import compilers
    if args.symbol:
        fn = next((f for f in common.open_functions() if f["name"] == args.symbol), None) \
            or common.function_by_name(args.symbol)
        if fn is None:
            print(json.dumps({"error": f"{args.symbol}: no open function with a draft"}))
            return 1
        todo = [fn]
    else:
        todo = [f for f in common.open_functions(limit=0) if not args.unit or f["unit"] == args.unit]
        if args.limit:
            todo = todo[:args.limit]
    for fn in todo:
        out = compilers.sweep(fn)
        compilers.record(out)
        if args.apply and out["gain"] > 0:
            out["applied"] = compilers.apply(out)
        print(json.dumps(out))
    return 0


def cmd_draft(args) -> int:
    todo = common.draftless(min_size=args.min_size, limit=args.limit or (0 if args.symbol else 0))
    if args.symbol:
        todo = [fn for fn in todo if fn["name"] == args.symbol] or [
            fn for fn in common.draftless(min_size=0) if fn["name"] == args.symbol]
    out = Path(args.out)
    if args.write:
        out.mkdir(parents=True, exist_ok=True)
    for fn in todo:
        text = draft.packet(fn)
        path = out / (fn["name"].replace("@", "_").replace("?", "_") + ".md")
        if args.write:
            path.write_text(text, encoding="utf-8")
        print(json.dumps({"symbol": fn["name"], "unit": fn["unit"], "size": fn["size"],
                          "attempts": fn.get("attempts"), "file": str(path) if args.write else None}))
        if args.print and not args.write:
            print(text)
    return 0

def cmd_restore(args) -> int:
    from . import restore as restore_mod
    todo = restore_mod.missing()
    if args.symbol:
        todo = [fn for fn in common.open_functions() if fn["name"] == args.symbol]
    if args.limit:
        todo = todo[:args.limit]
    bad = 0
    for fn in todo:
        row = restore_mod.run(fn, submit_now=args.submit, force=args.force)
        print(json.dumps(row))
        if row.get("error") or row.get("skipped"):
            bad += 1
    print(json.dumps({"missing": len(todo), "skipped_or_failed": bad}))
    return 0

def cmd_twins(args) -> int:
    from . import twins
    twins.run([Path(p) for p in args.source], jobs=args.jobs, submit=not args.no_submit, min_size=args.min_size,
              seed=not args.no_seed, polish=args.polish, near=args.near)
    return 0

def cmd_stale(args) -> int:
    """Re-measure the functions whose stored best the tree cannot reproduce, and record the truth."""
    from . import loopmode
    measured = common.stale()
    symbols = list(args.symbol) or list(measured)
    out = {}
    for sym in symbols:
        fn = _function(sym)
        body = fn.get("source") or ""
        try:
            helpers, definition = search.csource.split_extra(body)
            best_pct = search.verify(fn, definition, helpers) if body else None
        except (common.OutmatchError, KeyError) as e:
            best_pct = None
            definition = ""
        text = common.source_path(fn["unit"]).read_text(encoding="utf-8")
        own, start, end = common.definition_span(text, sym)
        file_pct = None
        if start >= 0:
            try:
                file_pct = search.verify(fn, text[start:end])
            except common.OutmatchError:
                pass
        stored = round(loopmode.pct_of({**fn, "score": fn.get("score")}) if sym not in measured else
                       measured[sym]["stored"], 3)
        row = {"stored": stored, "best_source_pct": None if best_pct is None else round(best_pct, 3),
               "measured": None if file_pct is None else round(file_pct, 3), "unit": fn["unit"]}
        out[sym] = row
        print(json.dumps({"symbol": sym, **row}))
    if args.write:
        merged = {**measured, **out}
        (common.STATE / "stale.json").write_text(json.dumps(merged, indent=1, sort_keys=True), encoding="utf-8")
        print(json.dumps({"wrote": len(merged)}))
    return 0


def cmd_report(args) -> int:
    from . import loopmode
    ledger = loopmode.Ledger()
    if args.symbol:
        _say({"symbol": args.symbol, **ledger.summary(args.symbol),
              "history": [r for r in ledger.rows if r.get("symbol") == args.symbol][-args.limit:]})
        return 0
    report = ledger.report()
    stale = common.stale()
    if stale:
        report["stale"] = {s: {"stored": r.get("stored"), "measured": r.get("measured")} for s, r in stale.items()}
    _say(report)
    return 0


def cmd_try(args) -> int:
    """Score one hand-written candidate locally, and (optionally) let the app judge it."""
    from . import csource as cs
    fn = _function(args.name)
    unit = fn["unit"] or common.unit_of(args.name)
    text = common.source_path(unit).read_text(encoding="utf-8")
    candidate = open(args.file, encoding="utf-8").read()
    helpers, definition = cs.split_extra(candidate)
    if not definition.strip():
        definition, helpers = candidate, ""
    pool = search.Pool(unit, jobs=1)
    try:
        verdict = pool.score(search.source_with(text, args.name, definition, helpers), args.name)
    finally:
        pool.close()
    out = {"symbol": args.name, "unit": unit, "pct": round(verdict.pct, 3),
           "compile_error": verdict.compile_error[-800:]}
    if args.submit:
        out["submitted"] = search.submit(args.name, definition, helpers)
    _say(out)
    return 0 if verdict.pct >= 100.0 else 1


REF_HINT = {
    "constructor": "an out-of-line constructor stands where the original calls func_X: name the class C_X "
                   "(typedef the old name to it) and declare its constructor without a body",
    "import": "the import is decorated (__imp__F@N) where the delinked original's IAT name is not; usually harmless (a "
              "matched function stays matched). A `T (__stdcall* _imp__F)(...)` pointer gets the name but can lose the "
              "register reuse dllimport allows (mov esi,[imp]; call esi), so check the score before keeping it",
    "data_or_code": "same address, but one side names it func_ and the other D_: use the original's spelling",
    "unnamed": "a symbol with no address in its name and no `names` entry: rename it or add the name",
    "other_callee": "the draft calls a different address (a decompilation error, not a naming one)",
}


def ref_kind(mine: str, orig: str) -> str:
    """Classify one `refers to MINE where the original refers to ORIG` pair from the app's diff."""
    import re
    addr = lambda s: (m.group(2).upper() if (m := re.search(r"(func|D)_([0-9A-Fa-f]{8})", s)) else None)
    if mine.startswith("??0") or mine.startswith("??1"):
        return "constructor"
    if mine.startswith("__imp_") and orig.startswith("__imp_"):
        return "import" if mine.split("@")[0] == orig.split("@")[0] else "other_callee"
    if addr(mine) is None:
        return "unnamed"
    if addr(mine) == addr(orig):
        return "data_or_code"
    return "other_callee"


def cmd_refs(args) -> int:
    """Ask the app's own diff (/api/diff, what the UI shows) for every function and list wrong references."""
    import re
    import urllib.parse
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor
    status = {"open": ("open",), "matched": ("matched",), "all": ("open", "matched")}[args.status]
    names = list(args.symbol) or [r[0] for r in common.query(
        f"SELECT name FROM functions WHERE project_id=? AND status IN ({','.join('?' * len(status))}) ORDER BY name",
        common.project_id(), *status)]
    kinds = set(args.kind or REF_HINT)

    def one(name):
        try:
            d = common.api(f"/api/diff?slug={common.SLUG}&name={urllib.parse.quote(name)}")
        except Exception as e:  # noqa: BLE001 - one bad function must not stop the scan
            return name, [], str(e)
        pairs = []
        for p in d.get("problems") or []:
            if m := re.fullmatch(r"refers to `(.+?)` where the original refers to `(.+?)`", p):
                pairs.append((m.group(1), m.group(2)))
        return name, pairs, d.get("error")

    counts = Counter()
    with ThreadPoolExecutor(args.jobs) as ex:
        for name, pairs, error in ex.map(one, names):
            found = [{"kind": k, "mine": m, "original": o} for m, o in pairs if (k := ref_kind(m, o)) in kinds]
            if error:
                counts["error"] += 1
                _say({"symbol": name, "error": error[:300]})
            elif found:
                counts.update(r["kind"] for r in found)
                _say({"symbol": name, "unit": common.unit_of(name), "refs": found})
    _say({"checked": len(names), "found": dict(counts), "hints": {k: REF_HINT[k] for k in counts if k in REF_HINT}})
    return 1 if counts else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="outmatch", description="match functions outside decompdecomp, submit inside it")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="permuter search on one function")
    s.add_argument("name")
    s.add_argument("--seconds", type=float, default=120)
    s.add_argument("--jobs", type=int, default=common.JOBS)
    s.add_argument("--count", type=int, default=64)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--no-submit", action="store_true")
    s.set_defaults(func=cmd_search)

    r = sub.add_parser("run", help="permuter search over the near-miss queue")
    r.add_argument("--max-ratio", type=float, default=0.06, help="worst relative score to include (0.005 = 0.5%%)")
    r.add_argument("--limit", type=int, default=20)
    r.add_argument("--seconds", type=float, default=30, help="per function")
    r.add_argument("--jobs", type=int, default=common.JOBS)
    r.add_argument("--count", type=int, default=64)
    r.add_argument("--any", action="store_true", help="include functions the app hasn't permuted 4 times")
    r.add_argument("--no-submit", action="store_true")
    r.add_argument("--stop-on-match", action="store_true")
    r.set_defaults(func=cmd_run)

    sb = sub.add_parser("submit", help="submit a saved variant through /api/try")
    sb.add_argument("name")
    sb.add_argument("file")
    sb.set_defaults(func=cmd_submit)

    lp = sub.add_parser("loop", help="work the near-miss queue with levers, pragmas, the permuter and (opt-in) flags")
    lp.add_argument("--max-ratio", type=float, default=0.06, help="worst relative score to include (0.005 = 0.5%%)")
    lp.add_argument("--min-pct", type=float, default=0.0,
                    help="only functions already at or above this objdiff percentage (e.g. 99.5), closest first")
    lp.add_argument("--order", choices=("fresh", "closest"), default="fresh",
                    help="fresh = least-visited first (default); closest = highest percentage first")
    lp.add_argument("--unit", default="", help="only functions of this unit")
    lp.add_argument("--limit", type=int, default=0, help="functions this run (0 = every candidate)")
    lp.add_argument("--seconds", type=float, default=900, help="budget per function")
    lp.add_argument("--phases", default="msvc,levers,pragma,permuter,compiler",
                    help="comma-separated phases ('all' = msvc,levers,pragma,permuter,flags); flags sweeps the unit "
                         "and applies only a clean winning set")
    lp.add_argument("--focus", choices=("auto", "off"), default="auto",
                    help="order the catalogue by the observed objdiff diff kinds (default) or run it as listed")
    lp.add_argument("--cap", type=int, default=160, help="variants per generation")
    lp.add_argument("--beam", type=int, default=1, help="bodies kept per generation (1 = greedy)")
    lp.add_argument("--depth", type=int, default=1,
                    help="msvc phase: catalogue passes per function, each from the previous best body")
    lp.add_argument("--jobs", type=int, default=common.JOBS)
    lp.add_argument("--count", type=int, default=64, help="permuter variants per batch")
    lp.add_argument("--seed", type=int, default=0)
    lp.add_argument("--again", action="store_true", help="rerun phases already tried on this exact source")
    lp.add_argument("--any", action="store_true", help="include functions the app hasn't permuted 4 times")
    lp.add_argument("--forever", action="store_true", help="keep waiting for new candidates instead of stopping")
    lp.add_argument("--idle", type=float, default=300, help="seconds to sleep when --forever finds nothing")
    lp.add_argument("--no-submit", action="store_true")
    lp.add_argument("--apply-improvements", action="store_true",
                    help="also submit a candidate that beats the app's best but is not 100 yet")
    lp.add_argument("--dry-run", action="store_true", help="print the queue and exit")
    lp.set_defaults(func=cmd_loop)

    st = sub.add_parser("stale", help="re-measure functions whose stored best the tree cannot reproduce")
    st.add_argument("--symbol", action="append", default=[], help="symbol to check (repeatable; default: all known)")
    st.add_argument("--write", action="store_true", help="record the measurements in .outmatch/stale.json")
    st.set_defaults(func=cmd_stale)
    cp = sub.add_parser("compiler", help="which MSVC version matches a function (6.0 / 7.0 / 7.1)")
    cp.add_argument("--symbol", default="", help="one function")
    cp.add_argument("--unit", default="", help="every open function of a unit")
    cp.add_argument("--limit", type=int, default=0)
    cp.add_argument("--apply", action="store_true", help="pin the unit when no other match is lost")
    cp.set_defaults(func=cmd_compiler)
    dr = sub.add_parser("draft", help="first-draft packets for functions with no C at all")
    dr.add_argument("--symbol", default="", help="one function instead of the queue")
    dr.add_argument("--limit", type=int, default=0)
    dr.add_argument("--min-size", type=int, default=0)
    dr.add_argument("--out", default=str(Path(common.STATE) / "drafts"))
    dr.add_argument("--write", action="store_true", help="save the packets")
    dr.add_argument("--print", action="store_true", help="print each packet")
    dr.set_defaults(func=cmd_draft)
    rs = sub.add_parser("restore", help="put a recorded best back into its unit when the definition is gone")
    rs.add_argument("--symbol", default="")
    rs.add_argument("--limit", type=int, default=0)
    rs.add_argument("--submit", action="store_true", help="write it back and let the app judge it")
    rs.add_argument("--force", action="store_true", help="submit even if a neighbour's score would change")
    rs.set_defaults(func=cmd_restore)
    rp = sub.add_parser("report", help="what the loop has tried (summary, or one function's history)")
    rp.add_argument("symbol", nargs="?", default="")
    rp.add_argument("--limit", type=int, default=40)
    rp.set_defaults(func=cmd_report)

    tr = sub.add_parser("try", help="score one hand-written definition locally (--submit lets the app judge it)")
    tr.add_argument("name")
    tr.add_argument("file")
    tr.add_argument("--submit", action="store_true", help="send it to the app after the local score")
    tr.set_defaults(func=cmd_try)

    rf = sub.add_parser("refs", help="list references the app's diff says name the wrong symbol (naming problems)")
    rf.add_argument("symbol", nargs="*", help="only these functions (default: every function with --status)")
    rf.add_argument("--status", choices=("open", "matched", "all"), default="open")
    rf.add_argument("--kind", action="append", choices=sorted(REF_HINT),
                    help="only these kinds (repeatable; default all, other_callee included)")
    rf.add_argument("--jobs", type=int, default=4)
    rf.set_defaults(func=cmd_refs)

    st = sub.add_parser("status", help="project + app summary")
    st.add_argument("--full", action="store_true", help="print the app's whole /api/state")
    st.set_defaults(func=cmd_status)

    rl = sub.add_parser("release", help="give a claim back")
    rl.add_argument("name")
    rl.set_defaults(func=cmd_release)

    ap = sub.add_parser("autopilot", help="start (default), --stop or --status the app's autopilot")
    ap.add_argument("--all", action="store_true", help="every project the app knows, not this one")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--status", action="store_true")

    st = sub.add_parser("stubs", help="adjustor thunks, jump stubs and padding among open functions (--write: set aside)")
    st.add_argument("--write", action="store_true")
    st.set_defaults(func=lambda a: __import__("harness.stubs", fromlist=["main"]).main(["--write"] if a.write else []))

    tw = sub.add_parser("twins", help="match open functions from identical matched code in other projects")
    tw.add_argument("--from", dest="source", action="append", required=True,
                    help="another decompdecomp project folder (repeatable; . for the game's own duplicates)")
    tw.add_argument("--jobs", type=int, default=common.JOBS)
    tw.add_argument("--min-size", type=int, default=8)
    tw.add_argument("--no-submit", action="store_true", help="score only")
    tw.add_argument("--no-seed", action="store_true", help="don't make a near twin the function's best attempt")
    tw.add_argument("--near", type=float, default=0, metavar="RATIO",
                    help="also pair functions whose masked bytes are this alike (e.g. 0.85), not only identical")
    tw.add_argument("--polish", type=float, default=0, metavar="SECONDS",
                    help="then run the permuter this long on each twin it seeded")
    tw.set_defaults(func=cmd_twins)

    fl = sub.add_parser("flags", help="sweep compiler flags per unit")
    fl.add_argument("--unit")
    fl.add_argument("--all", action="store_true")
    fl.add_argument("--apply", default="", metavar="SYMBOL", help="apply the best set for this function (split if needed)")
    fl.add_argument("--seconds", type=float, default=0)
    fl.add_argument("--jobs", type=int, default=common.JOBS)
    fl.set_defaults(func=cmd_flags)

    pr = sub.add_parser("pragma", help="sweep #pragma / declaration state around one function")
    pr.add_argument("name")
    pr.add_argument("--jobs", type=int, default=common.JOBS)
    pr.add_argument("--no-submit", action="store_true")
    pr.set_defaults(func=cmd_pragma)

    lv = sub.add_parser("levers", help="apply known MSVC6 source idioms to one function")
    lv.add_argument("name")
    lv.add_argument("--only", default="", help="comma-separated lever ids")
    lv.add_argument("--jobs", type=int, default=common.JOBS)
    lv.add_argument("--no-submit", action="store_true")
    lv.set_defaults(func=cmd_levers)

    mv = sub.add_parser("msvc", help="try the MSVC6 trick catalogue (the skill's levers) on one function")
    mv.add_argument("name", nargs="?", default="")
    mv.add_argument("--only", default="", help="comma-separated groups: " + ",".join(
        ["args", "rename", "decl-order", "condition", "ternary", "return", "init", "zero-init", "volatile", "type",
         "stmt-order"]))
    mv.add_argument("--file", default="", help="base the catalogue on this definition instead of the unit's")
    mv.add_argument("--cap", type=int, default=160, help="maximum variants to score")
    mv.add_argument("--focus", choices=("auto", "off"), default="auto",
                    help="order the catalogue by the observed objdiff diff kinds (default) or run it as listed")
    mv.add_argument("--beam", type=int, default=1, help="bodies kept per generation (1 = greedy)")
    mv.add_argument("--depth", type=int, default=1,
                    help="catalogue passes, each starting from the previous best body (2+ chains tricks)")
    mv.add_argument("--jobs", type=int, default=common.JOBS)
    mv.add_argument("--apply", action="store_true", help="submit the best variant even if it is not 100 yet")
    mv.add_argument("--no-submit", action="store_true")
    mv.add_argument("--list", action="store_true", help="print the trick groups and exit")
    mv.set_defaults(func=cmd_msvc)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
