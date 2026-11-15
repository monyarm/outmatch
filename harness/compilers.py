"""Which MSVC version built this function: 6.0, 7.0 or 7.1.

The exe came from build 8943, a beta-era 13.00 compiler that was never published, so the closest versions
available sit either side of it (12.00.8168 = 6.0, and the 13.00 line = 7.0/7.1).  A function can therefore be
100% under one version and 90% under another for reasons that have nothing to do with its C.

`loop` measures this as the `compiler` phase: one compile of the unit under each installed version, scored for
the function.  Acting on the result is a unit-level change, so it is a separate step:

    outmatch.sh compiler --symbol SYM          # measure one function under every version
    outmatch.sh compiler --unit text_0041BFF4  # every open function of a unit
    outmatch.sh compiler --apply SYM           # pin the unit to that version if nothing else regresses

A unit that gains here but loses neighbours there cannot be pinned whole - that needs the function split into
its own unit (`split_file` + `set_file_compiler` in the app); `--apply` says so instead of guessing.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import common

VERSIONS = ("6.0", "7.0", "7.1")
STATE = common.STATE / "compiler.json"


def installed() -> list[str]:
    return [v for v in VERSIONS if (common.TOOLCHAINS / v / "Bin" / "CL.EXE").is_file()]


def configured(unit: str) -> str:
    """The version the project builds this unit with (the app's file_compilers.json, default 6.0)."""
    return common.file_compilers().get(f"src/auto/{unit}.cpp", "6.0")


def sweep(fn: dict, versions: list[str] | None = None, text: str | None = None) -> dict:
    """Score the function as compiled by each version, in one freshly copied tree."""
    unit, symbol = fn["unit"], fn["name"]
    versions = versions or installed()
    source = text if text is not None else common.source_path(unit).read_text(encoding="utf-8")
    root = common.make_scratch(unit, common.slot(960), fresh=True)
    out: dict = {"symbol": symbol, "unit": unit, "configured": configured(unit), "pct": {}}
    for version in versions:
        try:
            obj = common.scratch_compile(root, unit, source, f"build/{common.VERSION}/src/auto/{unit}.obj",
                                         compiler=version)
            out["pct"][version] = round(common.score(common.target_obj(unit), obj, symbol), 3)
        except Exception as e:  # a version that cannot build this unit at all
            out["pct"][version] = None
            out.setdefault("errors", {})[version] = str(e)[:200]
    best = max((v for v in out["pct"] if out["pct"][v] is not None), key=lambda v: out["pct"][v])
    out["best"], out["best_pct"] = best, out["pct"][best]
    out["gain"] = round(out["best_pct"] - (out["pct"].get(out["configured"]) or 0.0), 3)
    return out


def load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def record(result: dict) -> None:
    data = load()
    data[result["symbol"]] = {k: result[k] for k in ("unit", "configured", "best", "best_pct", "gain")}
    data[result["symbol"]]["pct"] = result["pct"]
    save(data)


def unit_check(unit: str, version: str) -> dict:
    """Compile the whole unit under `version` and list the matched neighbours that would stop matching."""
    text = common.source_path(unit).read_text(encoding="utf-8")
    root = common.make_scratch(unit, common.slot(961), fresh=True)
    now = common.scores(common.target_obj(unit),
                        common.scratch_compile(root, unit, text, f"build/{common.VERSION}/src/auto/{unit}.obj"))
    other = common.scores(common.target_obj(unit),
                          common.scratch_compile(root, unit, text, f"build/{common.VERSION}/src/auto/{unit}.obj",
                                                 compiler=version))
    lost = [s for s, p in now.items() if (p or 0) >= 99.999 and (other.get(s) or 0) < 99.999]
    gained = [s for s, p in other.items() if (p or 0) >= 99.999 and (now.get(s) or 0) < 99.999]
    return {"unit": unit, "version": version, "lost": lost, "gained": gained}


def apply(result: dict) -> dict:
    """Pin the unit to the winning version when that costs no other match; otherwise report the split."""
    check = unit_check(result["unit"], result["best"])
    if check["lost"]:
        return {"applied": False, "needs_split": True, "version": result["best"],
                "lost": check["lost"], "gained": check["gained"]}
    path = common.FILE_COMPILERS
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    data[f"src/auto/{result['unit']}.cpp"] = result["best"]
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return {"applied": True, "needs_split": False, "version": result["best"], "gained": check["gained"]}
