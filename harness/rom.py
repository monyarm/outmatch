"""ROM projects (GBA, N64, PS1/PS2, ...): outmatch's phases for a project decompdecomp judges against a ROM.

A ROM project has no source tree to build: the app compiles one function's C against the project's context header
with its compiler (per address range), links it at the function's address and compares it with the ROM. The local
judge is rom_worker (the app's own code, in the app's Python, with the app's PATH); a 100 goes to /api/try.

Phases, by compiler family:
  levers    the family's mechanical rewrites (gcc/agbcc: rom_levers.py)
  flags     flag sets of the family (agbcc: -O levels, -mthumb-interwork, -fno-common, ...)
  compiler  every installed compiler of the family (agbcc, old_agbcc, the user's own copies); a winner is pinned to
            the function's address range through /api/compiler (the app refuses a range that breaks a match)
  permuter  decomp-permuter's generator (permuter_worker), judged here
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
from pathlib import Path

from . import common

FLAG_SETS = {  # the project's flags are the base; each set replaces its -O level and adds/drops the rest
    "agbcc": [["-O2"], ["-O1"], ["-O0"], ["-O3"], ["-O2", "-fno-common"], ["-O1", "-fno-common"],
              ["-O2", "-fprologue-bugfix"], ["-O2", "-fhex-asm"]],
    "gcc": [["-O2"], ["-O1"], ["-O0"], ["-O3"], ["-Os"], ["-O2", "-fno-strict-aliasing"], ["-O2", "-fomit-frame-pointer"],
            ["-O2", "-fno-omit-frame-pointer"], ["-O1", "-fno-delayed-branch"]],
    "ido": [["-O2"], ["-O1"], ["-O0"], ["-O3"], ["-O2", "-g3"]],
    "mwcc": [["-O4,p"], ["-O4,s"], ["-O3"], ["-O2"], ["-O0"]],
}


def _fhs_prefix(pid: int) -> list[str]:
    """The bwrap options of the FHS sandbox the server runs in (decompdecomp-nix starts it in one, where the
    toolchains' generic Linux binaries work), to run the judge in the same one; [] when it runs outside any."""
    while pid > 1:
        try:
            argv = [a.decode() for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return []
        if argv and argv[0].endswith("/bin/bwrap"):
            opts = argv[:-1]  # the last argument is the program it starts (the app's container init)
            if "--chdir" in opts:
                opts[opts.index("--chdir") + 1] = str(common.ROOT)
            return [*opts, "--"]
        pid = int(stat.rsplit(")", 1)[1].split()[1])  # the parent
    return []


def _server() -> tuple[str, dict, list[str]] | None:
    """(python, environment, FHS sandbox prefix) of the running decompdecomp server: the process started from a decompdecomp-nix venv
    (the newest version when several are)."""
    found = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            first = argv[0].decode(errors="replace")
            if "decompdecomp-nix" in first and first.endswith("/venv/bin/python") and b"harness" not in b" ".join(argv):
                env = dict(item.decode(errors="replace").split("=", 1)
                           for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0") if b"=" in item)
                found.append((first, env, int(pid)))
        except OSError:
            continue
    best = max(found, key=lambda x: x[0]) if found else None
    if not best:
        return None
    # the server's own namespaces (its FHS sandbox, as its init set it up: the loader finds /usr/lib there); a new
    # sandbox with the same options lacks what the init does (libzstd unfound), so this is first
    enter = (["nsenter", "-t", str(best[2]), "-U", "-m", "--preserve-credentials", f"--wd={common.ROOT}", "--"]
             if shutil.which("nsenter") else _fhs_prefix(best[2]))
    return best[0], best[1], enter


def app_env() -> dict:
    """The environment the app compiles in: its PATH decides the sandbox (no bwrap there: none) and its NIX_LD*
    lets the toolchains' generic Linux binaries run on NixOS. This process's own when the app isn't found."""
    found = _server()
    return dict(found[1]) if found else dict(os.environ)


class Judge:
    """One rom_worker process for this project."""

    def __init__(self):
        env = app_env()
        here = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = here + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        found = _server()
        prefix = found[2] if found else []
        self.proc = subprocess.Popen([*prefix, app_python(), "-m", "harness.rom_worker"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, env=env, cwd=common.ROOT)
        self.info = self._ask({"op": "setup", "slug": common.SLUG})
        if "error" in self.info:
            raise common.OutmatchError("rom judge: " + self.info["error"])

    def _ask(self, req: dict) -> dict:
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise common.OutmatchError("rom judge stopped")
        return json.loads(line)

    def score(self, name: str, sources: list[str], compiler: dict | None = None) -> list[dict]:
        res = self._ask({"op": "score", "name": name, "sources": sources, "compiler": compiler})
        if "error" in res:
            raise common.OutmatchError("rom judge: " + res["error"])
        return res["results"]

    def toolchains(self) -> list[str]:
        return self._ask({"op": "toolchains"}).get("toolchains", [])

    def close(self) -> None:
        try:
            self._ask({"op": "close"})
        except (common.OutmatchError, OSError, ValueError):
            pass
        self.proc.wait(timeout=30)


def app_python() -> str:
    """The app's interpreter (it imports decompnet): the running server's, else the newest decompdecomp-nix venv."""
    if found := _server():
        return found[0]
    venvs = sorted(Path.home().glob(".cache/decompdecomp-nix/*/venv/bin/python"))
    if venvs:
        return str(venvs[-1])
    raise common.OutmatchError("can't find decompdecomp's Python (is the app running?)")


def function(name: str) -> dict:
    row = common.query_one("SELECT name, vram, size, n_insns, best_score, best_source, status FROM functions "
                           "WHERE project_id=? AND name=?", common.project_id(), name)
    if not row:
        raise common.OutmatchError(f"no function {name}")
    return dict(zip(("name", "vram", "size", "n_insns", "score", "source", "status"), row))


def best_pct(fn: dict) -> float:
    if fn.get("score") is None:
        return -1.0
    return 100.0 - float(fn["score"]) / max(1, int(fn.get("n_insns") or 1))


def submit(name: str, source: str) -> bool:
    res = common.try_function(name, source)
    if not res.get("matched"):
        try:
            common.api("/api/release", {"slug": common.SLUG, "name": name})
        except common.OutmatchError:
            pass
    common.log("submitted", symbol=name, matched=bool(res.get("matched")), score=res.get("score"))
    return bool(res.get("matched"))


def climb(judge: Judge, fn: dict, start: str, make, rounds: int, compiler: dict | None = None, say=None) -> tuple[float, str]:
    """Hill-climb: each round scores make(best) variants, keeps the best; stops at 100 or when nothing improves."""
    best_src = start
    best = judge.score(fn["name"], [start], compiler)[0]["pct"]
    for _ in range(rounds):
        variants = [v for v in dict.fromkeys(make(best_src)) if v != best_src]
        if not variants:
            break
        scored = judge.score(fn["name"], variants, compiler)
        top = max(range(len(variants)), key=lambda i: scored[i]["pct"])
        if scored[top]["pct"] <= best:
            break
        best, best_src = scored[top]["pct"], variants[top]
        if say:
            say(json.dumps({"symbol": fn["name"], "climb": round(best, 3)}))
        if best >= 99.999:
            break
    return best, best_src


def phase(name: str, fn: dict, judge: Judge, seconds: float, submit_match: bool, say) -> dict:
    """One phase on one ROM function: {"pct", "matched", "note"}."""
    from . import rom_levers

    src = fn["source"] or ""
    if not src:
        return {"pct": -1.0, "matched": False, "note": "no C to start from"}
    fam = judge.info.get("family", "")
    base_flags = list(judge.info.get("flags") or [])
    tc = judge.info.get("toolchain", "")
    out = {"pct": -1.0, "matched": False, "note": ""}
    if name == "levers":
        pct, best = climb(judge, fn, src, lambda s: rom_levers.variants(s, fam), rounds=6, say=say)
        out.update(pct=pct, source=best)
    elif name == "permuter":
        pct, best = permute(judge, fn, src, seconds, say)
        out.update(pct=pct, source=best)
    elif name in ("flags", "compiler"):
        builds = []
        if name == "flags":
            for extra in FLAG_SETS.get(fam, []):
                flags = [f for f in base_flags if not f.startswith("-O")] + extra
                builds.append({"toolchain": tc, "flags": flags})
        else:
            builds = [{"toolchain": t, "flags": base_flags} for t in judge.toolchains() if t != tc]
        results = [(b, judge.score(fn["name"], [src], b)[0]) for b in builds]
        b, r = max(results, key=lambda x: x[1]["pct"], default=(None, {"pct": -1.0}))
        out.update(pct=r["pct"], build=b)
        if b and r["pct"] >= 99.999 and submit_match:
            out["matched"] = pin_and_submit(fn, b, src)
            out["note"] = "pinned " + json.dumps(b)
        return out
    else:
        return {"pct": -1.0, "matched": False, "note": f"{name}: not a phase for {fam or 'this compiler'}"}
    if out["pct"] >= 99.999 and submit_match:
        out["matched"] = submit(fn["name"], out["source"])
    elif submit_match and out.get("source") and out["source"] != src and out["pct"] > best_pct(fn) + 1e-6:
        submit(fn["name"], out["source"])  # better, not exact: one try records it as the function's best attempt
        out["note"] = "improved: recorded as the best attempt"
    return out


def pin_and_submit(fn: dict, build: dict, source: str) -> bool:
    """Pin the function's address range to `build` (the app checks the matches in it), then judge."""
    rng = f"0x{fn['vram']:X}-0x{fn['vram'] + max(1, fn['size'] or 1):X}"
    try:
        common.api("/api/compiler", {"slug": common.SLUG, "ranges": rng, "toolchain": build["toolchain"],
                                     "flags": build["flags"]})
    except common.OutmatchError as e:
        common.log("rom-pin", symbol=fn["name"], refused=str(e)[:200])
        return False
    ok = submit(fn["name"], source)
    if not ok:  # the range goes back to the project's compiler
        try:
            common.api("/api/compiler", {"slug": common.SLUG, "ranges": rng, "remove": True})
        except common.OutmatchError:
            pass
    common.log("rom-pin", symbol=fn["name"], build=build, matched=ok)
    return ok


def permute(judge: Judge, fn: dict, src: str, seconds: float, say) -> tuple[float, str]:
    """decomp-permuter's generator over the context header, judged by the ROM judge, greedy."""
    import time
    from .search import Generator

    header = common.query_one("SELECT header FROM context_versions WHERE project_id=? ORDER BY version DESC LIMIT 1",
                              common.project_id())
    head = header[0] if header else ""
    gen = Generator()
    best = judge.score(fn["name"], [src], None)[0]["pct"]
    best_src, end, seed = src, time.monotonic() + seconds, random.randrange(1 << 30)
    try:
        while time.monotonic() < end and best < 99.999:
            batch = gen.variants(head, best_src, fn["name"], seed, count=32)
            seed += 1
            if not batch:
                break
            scored = judge.score(fn["name"], batch, None)
            top = max(range(len(batch)), key=lambda i: scored[i]["pct"])
            if scored[top]["pct"] > best:
                best, best_src = scored[top]["pct"], batch[top]
                if say:
                    say(json.dumps({"symbol": fn["name"], "permuter": round(best, 3)}))
    finally:
        gen.close()
    return best, best_src
