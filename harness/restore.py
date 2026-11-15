"""Put a function's recorded best back into its unit.

Some functions have a `best_source` in the app but no definition left in the unit file - an app rewrite during a
neighbouring match, a split, or an agent edit dropped it. `loop` cannot see them (it needs a definition to permute),
and the app keeps a score for C that is no longer anywhere in the tree.

`restore` appends the recorded body at file scope, measures the whole unit before and after (a definition is
translation-unit state: it can move code in its neighbours), and, with `--submit`, lets the app judge it again so the
tree and the app agree on the body.

    outmatch.sh restore                 # report which functions need it and what each would score
    outmatch.sh restore --submit        # write them back and hand them to /api/try
"""

from __future__ import annotations

import time

from . import common, csource, search


def missing() -> list[dict]:
    """Open functions the app has a best for, but whose unit file no longer defines."""
    out = []
    for fn in common.open_functions():
        text = common.source_path(fn["unit"]).read_text(encoding="utf-8")
        if common.definition_span(text, fn["name"])[1] < 0:
            out.append(fn)
    return out


def plan(fn: dict) -> dict:
    """The restored unit text, plus every symbol's score before and after adding the definition back."""
    unit, symbol = fn["unit"], fn["name"]
    text = common.source_path(unit).read_text(encoding="utf-8")
    helpers, definition = csource.split_extra(fn.get("source") or "")
    restored = search.source_with(text, symbol, definition, helpers)
    target = common.target_obj(unit)
    root = common.make_scratch(unit, 700 + fn["id"] % 200, fresh=True)
    out_rel = f"build/{common.VERSION}/src/auto/{unit}.obj"
    before = common.scores(target, common.scratch_compile(root, unit, text, out_rel))
    after = common.scores(target, common.scratch_compile(root, unit, restored, out_rel))
    lost = {name: (round(was, 3), round(after[name], 3)) for name, was in before.items()
            if was is not None and after.get(name) is not None and after[name] < was - 1e-6}
    return {"symbol": symbol, "unit": unit, "definition": definition, "helpers": helpers,
            "pct": after.get(symbol), "before_pct": before.get(symbol), "lost": lost,
            "text": restored}


def run(fn: dict, submit_now: bool = False, force: bool = False, tolerance: float = 0.1) -> dict:
    """Measure a function that has a recorded best but no definition; optionally let the app re-measure it.

    Two different faults look the same from the app's side: the body was dropped, or it is there under a name the
    symbol does not spell (`Class::Class`). `pct` is what the body in the tree (or the restored one) really scores,
    `want` what the app recorded; a large gap means the record is stale, not that the C is lost.
    """
    started = time.monotonic()
    try:
        p = plan(fn)
    except common.OutmatchError as e:
        return {"symbol": fn["name"], "unit": fn["unit"], "error": str(e)[:300]}
    want = search.db_best_pct(fn)
    row = {"symbol": fn["name"], "unit": fn["unit"], "pct": p["pct"], "want": round(want, 3),
           "in_file": p["before_pct"] is not None, "neighbours_lost": p["lost"], "submitted": {},
           "seconds": round(time.monotonic() - started, 1)}
    if p["lost"] and not force:
        row["skipped"] = "adding the definition would change neighbours: " + ", ".join(sorted(p["lost"])[:5])
        return row
    if p["pct"] is None:
        row["skipped"] = "the recorded body no longer compiles against this unit"
    elif submit_now:
        if p["pct"] < want - tolerance and not force:
            row["skipped"] = f"the body scores {p['pct']:.2f}, the app recorded {want:.2f}: stale record, not resubmitted"
        else:
            body = p["definition"]
            if p["before_pct"] is not None:  # it is in the tree: re-measure that, not the recorded text
                _, start, end = common.definition_span(
                    common.source_path(fn["unit"]).read_text(encoding="utf-8"), fn["name"])
                body = common.source_path(fn["unit"]).read_text(encoding="utf-8")[start:end]
            row["submitted"] = search.submit(fn["name"], body, p["helpers"], fn=None, force=True)
    common.log("restore", symbol=fn["name"], pct=p["pct"], want=round(want, 3), lost=len(p["lost"]),
               submitted=bool(row["submitted"]))
    return row
