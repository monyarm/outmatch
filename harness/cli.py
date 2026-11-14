"""Command line for outmatch.  Every subcommand prints one JSON line per function (and a summary at the end)."""

from __future__ import annotations

import argparse
import json
import sys

from . import common, csource, search


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
                        apply_best=args.apply_improvements)
    _say({**out, "queued": len(todo)})
    return 0


def cmd_report(args) -> int:
    from . import loopmode
    ledger = loopmode.Ledger()
    if args.symbol:
        _say({"symbol": args.symbol, **ledger.summary(args.symbol),
              "history": [r for r in ledger.rows if r.get("symbol") == args.symbol][-args.limit:]})
        return 0
    _say(ledger.report())
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
    lp.add_argument("--phases", default="levers,pragma,permuter",
                    help="comma-separated phases ('all' = levers,pragma,permuter,flags); flags sweeps the unit and "
                         "applies only a clean winning set")
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

    rp = sub.add_parser("report", help="what the loop has tried (summary, or one function's history)")
    rp.add_argument("symbol", nargs="?", default="")
    rp.add_argument("--limit", type=int, default=40)
    rp.set_defaults(func=cmd_report)

    tr = sub.add_parser("try", help="score one hand-written definition locally (--submit lets the app judge it)")
    tr.add_argument("name")
    tr.add_argument("file")
    tr.add_argument("--submit", action="store_true", help="send it to the app after the local score")
    tr.set_defaults(func=cmd_try)

    st = sub.add_parser("status", help="project + app summary")
    st.add_argument("--full", action="store_true", help="print the app's whole /api/state")
    st.set_defaults(func=cmd_status)

    rl = sub.add_parser("release", help="give a claim back")
    rl.add_argument("name")
    rl.set_defaults(func=cmd_release)

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

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
