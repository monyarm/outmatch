"""Shared plumbing for outmatch: the project's configs, the live app's API, scratch compiles, objdiff scores.

Standard library only.  Everything here is read-only with respect to the project unless a caller explicitly writes a
file or posts to the app.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

def _project_root() -> Path:
    """The decompdecomp project outmatch works on: $OUTMATCH_PROJECT, else the nearest folder at or above the working
    directory that has config/<version>/functions.json (a decompdecomp PC fork)."""
    if os.environ.get("OUTMATCH_PROJECT"):
        return Path(os.environ["OUTMATCH_PROJECT"]).resolve()
    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        if any((d / "config").glob("*/functions.json")):
            return d
    sys.exit("outmatch: run it inside a decompdecomp project (a folder with config/<version>/functions.json), "
             "or set OUTMATCH_PROJECT")


def _version(root: Path) -> str:
    """The game version the project builds: $OUTMATCH_VERSION, else its only config/<version>/functions.json."""
    if os.environ.get("OUTMATCH_VERSION"):
        return os.environ["OUTMATCH_VERSION"]
    found = sorted(p.parent.name for p in (root / "config").glob("*/functions.json"))
    if len(found) != 1:
        sys.exit(f"outmatch: {root} has config for {found or 'no version'}: set OUTMATCH_VERSION")
    return found[0]


ROOT = _project_root()
APP_HOME = Path(os.environ.get("DECOMPNET_HOME", Path.home() / "Decomp" / ".decomp"))
DB = APP_HOME / "decomp.db"
VERSION = _version(ROOT)
CONFIG = ROOT / "config" / VERSION
FUNCTIONS_JSON = CONFIG / "functions.json"
FILE_FLAGS = CONFIG / "file_flags.json"
FILE_COMPILERS = CONFIG / "file_compilers.json"
TOOLCHAINS = Path.home() / ".local/share/decompnet/toolchains/msvc"
PACKS = (TOOLCHAINS, TOOLCHAINS.parent / "msvc2005")  # decompdecomp's MSVC packs: 6.0-7.1 (+ hand-added), 2005-2010


KITS = TOOLCHAINS.parent / "visualcpp" / ".decompnet-toolchain.json"  # Setup's "Visual C++ (your own copy)" folders


def own_compilers() -> dict[str, Path]:
    """{"own-13.0.9044": folder} for each own copy registered in Setup, named as decompdecomp names them (kits/
    visualcpp.py: c2.dll's file version, first three parts)."""
    try:
        folders = json.loads(KITS.read_text(encoding="utf-8")).get("folders") or []
    except (OSError, ValueError):
        return {}
    out = {}
    for folder in map(Path, folders):
        c2 = next((p for p in (folder / "Bin" / "c2.dll", folder / "Bin" / "C2.DLL") if p.is_file()), None)
        if c2 and (v := file_version(c2)):
            out["own-" + ".".join(v.split(".")[:3])] = folder
    return out


def file_version(path: Path) -> str | None:
    """A PE's VS_FIXEDFILEINFO file version as "13.0.9044.0"."""
    b = path.read_bytes()
    i = b.find(b"\xbd\x04\xef\xfe")  # VS_FIXEDFILEINFO signature
    if i < 0:
        return None
    ms, ls = int.from_bytes(b[i + 8:i + 12], "little"), int.from_bytes(b[i + 12:i + 16], "little")
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def cl_exe(version: str) -> Path | None:
    """A version's CL.EXE in the first pack that has it, as tools/configure.py looks it up, or an own copy's."""
    homes = [own] if (own := own_compilers().get(version)) else [pack / version for pack in PACKS]
    return next((p for home in homes for p in (home / "Bin" / "CL.EXE", home / "Bin" / "cl.exe") if p.is_file()), None)


def default_compiler() -> str:
    """The project's own Visual C++ (tools/configure.py's MSVC_VERSION): every file file_compilers.json doesn't name."""
    m = re.search(r'^MSVC_VERSION = "([^"]+)"', (ROOT / "tools" / "configure.py").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "6.0"
BUILD = ROOT / "build" / VERSION
TARGETS = BUILD / "target"
STATE = ROOT / ".outmatch"
WORK = Path(os.environ.get("OUTMATCH_WORK", f"/tmp/outmatch/{ROOT.name}"))  # scratch trees, one set per project
OBJDIFF_OPTS = ("functionRelocDiffs=none", "combineDataSections=true", "combineTextSections=true",
                "ppc.calculatePoolRelocations=false")
def _slug(root: Path) -> str:
    """decompdecomp's name for the project: $OUTMATCH_SLUG, else the projects row whose folder is this one."""
    if os.environ.get("OUTMATCH_SLUG"):
        return os.environ["OUTMATCH_SLUG"]
    try:
        with sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30) as db:
            row = db.execute("SELECT slug FROM projects WHERE root=?", (str(root),)).fetchone()
    except sqlite3.Error:
        row = None
    return row[0] if row else root.name


SLUG = _slug(ROOT)
JOBS = int(os.environ.get("OUTMATCH_JOBS", max(1, min(8, (os.cpu_count() or 4) // 2))))


class OutmatchError(RuntimeError):
    pass


# ------------------------------------------------------------------ project configs

_functions: dict | None = None


def functions() -> dict:
    global _functions
    if _functions is None:
        _functions = json.loads(FUNCTIONS_JSON.read_text(encoding="utf-8"))
    return _functions


def file_flags() -> dict:
    return json.loads(FILE_FLAGS.read_text(encoding="utf-8")) if FILE_FLAGS.is_file() else {}


def file_compilers() -> dict:
    return json.loads(FILE_COMPILERS.read_text(encoding="utf-8")) if FILE_COMPILERS.is_file() else {}


def save_functions(info: dict) -> None:
    global _functions
    FUNCTIONS_JSON.write_text(json.dumps(info, indent=1) + "\n", encoding="utf-8")
    _functions = info


def save_file_flags(flags: dict) -> None:
    FILE_FLAGS.write_text(json.dumps(flags, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def unit_of(symbol: str) -> str:
    """The unit a function belongs to, by the DB's meta.source (src/auto/X.cpp -> X), else by functions.json."""
    for name, fns in functions()["units"].items():
        if any(f["name"] == symbol for f in fns):
            return name
    row = query_one("SELECT meta FROM functions WHERE project_id=1 AND name=?", symbol)
    if row and row[0]:
        meta = json.loads(row[0])
        src = meta.get("source")
        if src:
            return Path(src).stem
    raise OutmatchError(f"no unit for {symbol}")


def source_path(unit: str) -> Path:
    return ROOT / "src" / "auto" / f"{unit}.cpp"


def target_obj(unit: str) -> Path:
    return TARGETS / f"{unit}.obj"


# ------------------------------------------------------------------ database (read only)

def connect() -> sqlite3.Connection:
    if not DB.is_file():
        raise OutmatchError(f"no database at {DB}")
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)


def query(sql: str, *args) -> list[tuple]:
    with connect() as con:
        return con.execute(sql, args).fetchall()


def query_one(sql: str, *args):
    rows = query(sql, *args)
    return rows[0] if rows else None


def project_id() -> int:
    row = query_one("SELECT id FROM projects WHERE slug=?", SLUG)
    if not row:
        raise OutmatchError(f"project {SLUG} is not in {DB}")
    return int(row[0])


def open_functions(max_score: float | None = None, only_permuted: bool = False, limit: int | None = None) -> list[dict]:
    """Open functions with a draft, worst-first by relative score (the harness's queue of things worth trying)."""
    sql = ("SELECT id,name,unit,n_insns,size,best_score,best_source,permuted,meta FROM functions "
           "WHERE project_id=? AND status='open' AND best_source IS NOT NULL AND best_score>0")
    args: list = [project_id()]
    if only_permuted:
        sql += " AND permuted>=4"
    if max_score is not None:
        sql += " AND best_score<=n_insns*100*?"
        args.append(max_score)
    sql += " ORDER BY best_score*1.0/max(n_insns,1)"
    if limit:
        sql += f" LIMIT {int(limit)}"
    out = []
    for fid, name, unit, n_insns, size, score, src, permuted, meta in query(sql, *args):
        meta = json.loads(meta) if meta else {}
        out.append({"id": fid, "name": name, "unit": unit or Path(meta.get("source", "")).stem,
                    "n_insns": n_insns, "size": size, "score": score, "source": src, "permuted": permuted,
                    "meta": meta})
    return out


# ------------------------------------------------------------------ the live app's API

def server() -> dict:
    p = APP_HOME / "server.json"
    if not p.is_file():
        raise OutmatchError(f"decompdecomp isn't running ({p} missing)")
    return json.loads(p.read_text(encoding="utf-8"))


def api(path: str, body: dict | None = None, timeout: float = 600) -> dict:
    """Call the running app exactly as its own window/cli does (User-Agent carries the launch secret)."""
    info = server()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{info['port']}{path}", data=data,
                                 headers={"User-Agent": f"decompdecomp-window/{info['secret']}",
                                          "X-Decomp": "1", "Content-Type": "application/json",
                                          "Host": f"127.0.0.1:{info['port']}"},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raise OutmatchError(f"{path}: HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:800]}") from e


def try_function(name: str, source: str) -> dict:
    """Judge + record one function through the app (this is the only supported way in: source tree + git stay king)."""
    return api("/api/try", {"slug": SLUG, "name": name, "source": source})


def release(name: str) -> None:
    try:
        api("/api/release", {"slug": SLUG, "name": name})
    except OutmatchError:
        pass


def check_project() -> dict:
    return api("/api/fork/check", {"slug": SLUG})


# ------------------------------------------------------------------ compiles

def app_python() -> str:
    """The interpreter the project's build uses (it can import decompnet; also has stdlib)."""
    argv = build_command(next(iter(functions()["units"])))
    return argv[0]


def build_command(unit: str) -> list[str]:
    """The exact argv ninja uses for a unit, cached per unit (authoritative source of compiler/include paths)."""
    cache = STATE / "build_commands.json"
    try:
        data = json.loads(cache.read_text()) if cache.is_file() else {}
    except ValueError:  # a cache half-written by another process: rebuild it
        data = {}
    stamp = str((ROOT / "build.ninja").stat().st_mtime_ns)
    if data.get("__build.ninja__") != stamp:  # configure ran again (a compiler or flags changed): every argv may differ
        data = {"__build.ninja__": stamp}
    if unit in data:
        return data[unit]
    obj = f"build/{VERSION}/src/auto/{unit}.obj"
    res = subprocess.run(["ninja", "-t", "commands", obj], cwd=ROOT, capture_output=True, text=True)
    lines = [l for l in res.stdout.splitlines() if "tools/compile.py" in l]
    if not lines:
        raise OutmatchError(f"ninja has no compile command for {unit}: {res.stderr.strip()[:300]}")
    argv = shlex.split(lines[-1])
    data[unit] = argv
    STATE.mkdir(exist_ok=True)
    tmp = cache.with_name(f"{cache.name}.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(cache)  # atomic: parallel sweeps read it while others add units
    return argv


@dataclass
class Compile:
    unit: str
    source: str            # the unit's source text (definition already replaced)
    flags: list[str] | None = None   # None: the unit's own flags
    output: str = ""       # relative to the scratch root
    extra_flags: list[str] | None = None


def _split_command(argv: list[str]) -> tuple[list[str], list[str]]:
    """(driver args up to and including --source/--output, compiler flags after --)."""
    if "--" not in argv:
        raise OutmatchError("compile command has no -- separator")
    at = argv.index("--")
    return argv[:at], argv[at + 1:]


def scratch_compile(root: Path, unit: str, source_text: str, out_rel: str,
                    flags: list[str] | None = None, extra: list[str] | None = None,
                    compiler: str | None = None) -> Path:
    """Compile `unit` inside a scratch root (a copy of src/include/config/tools), returning the object path."""
    src = root / "src" / "auto" / f"{unit}.cpp"
    src.write_text(source_text, encoding="utf-8", newline="")
    head, own = _split_command(build_command(unit))
    use = list(own if flags is None else flags)
    if extra:
        use += extra
    # strip --output/--source pairs from head, then append our own (relative paths: the compiler sees the same names)
    cleaned, i = [], 0
    while i < len(head):
        if head[i] in ("--output", "--source"):
            i += 2
            continue
        cleaned.append(head[i])
        i += 1
    if compiler:
        cl = cl_exe(compiler)
        if cl is None:
            raise OutmatchError(f"Visual C++ {compiler} isn't installed in {' or '.join(map(str, PACKS))}")
        for i, tok in enumerate(cleaned[:-1]):
            if tok == "--compiler":
                cleaned[i + 1] = cl
            elif tok == "--include" and (Path(cleaned[i + 1]).parent.parent in PACKS
                                         or Path(cleaned[i + 1]).parent in own_compilers().values()):
                cleaned[i + 1] = cl.parent.parent / "Include"  # its own headers, not the unit's version's
    out = root / out_rel
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = [*cleaned, "--output", out_rel, "--source", f"src/auto/{unit}.cpp", "--", *use]
    env = dict(os.environ)
    res = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=600, env=env)
    if res.returncode != 0 or not out.is_file():
        raise OutmatchError((res.stdout + res.stderr).strip()[-1200:] or f"compile of {unit} failed")
    return out


def sync_tree(root: Path) -> None:
    """Refresh a reused worker tree's headers from the live project (agents edit them, and they change codegen)."""
    for base in ("include", "src"):
        for path in (ROOT / base).rglob("*.h"):
            if not path.is_file():
                continue
            target = root / path.relative_to(ROOT)
            src_mtime = path.stat().st_mtime_ns
            if not target.is_file() or src_mtime > target.stat().st_mtime_ns:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)


def slot(index: int) -> int:
    """A scratch-tree index, offset by OUTMATCH_SLOT so parallel agents never share a worker tree."""
    return int(os.environ.get("OUTMATCH_SLOT", "0")) + int(index)


def make_scratch(unit: str, index: int = 0, fresh: bool = False) -> Path:
    """A reusable scratch tree for one worker: the project's sources/headers/config/tools, nothing else.

    One tree per worker index, reused for every unit (rebuilding the file it compiles each time), so a sweep over
    many units copies once.  Set OUTMATCH_FRESH=1 (or pass fresh=True) to force a new copy.
    """
    root = WORK / f"w{index}"
    stamp = root / ".ready"
    if stamp.is_file() and not fresh and not os.environ.get("OUTMATCH_FRESH"):
        sync_tree(root)
        return root
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    for name in ("src", "include", "config", "tools"):
        shutil.copytree(ROOT / name, root / name, symlinks=True)
    (root / "build" / VERSION / "src" / "auto").mkdir(parents=True)
    stamp.write_text(str(time.time()))
    return root


# ------------------------------------------------------------------ objdiff scoring

def objdiff() -> str:
    found = shutil.which("objdiff-cli")
    if found:
        return found
    for cand in (Path.home() / ".local/share/decompnet/toolchains/objdiff-cli/objdiff-cli",
                 Path.home() / ".local/share/decompnet/toolchains/objdiff-cli"):
        if cand.is_file():
            return str(cand)
    raise OutmatchError("objdiff-cli not found (install it with decompdecomp's Compilers & tools)")


def _diff(target: Path, base: Path, symbol: str | None) -> dict:
    argv = [objdiff(), "--no-color", "diff", "-o", "-", "--format", "json"]
    for opt in OBJDIFF_OPTS:
        argv += ["-c", opt]
    argv += ["-1", str(target), "-2", str(base)]
    if symbol:
        argv.append(symbol)
    res = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if res.returncode:
        raise OutmatchError(res.stderr.strip()[-800:] or f"objdiff exited {res.returncode}")
    return json.loads(res.stdout)


def scores(target: Path, base: Path) -> dict[str, float | None]:
    """Every function of the target unit's score against `base` (None: not built/compiled in base)."""
    data = _diff(target, base, None)
    built = {s.get("name") for s in (data.get("right") or {}).get("symbols", [])
             if s.get("kind") == "SYMBOL_FUNCTION" and s.get("instructions")}
    return {s["name"]: (float(s["match_percent"]) if s.get("match_percent") is not None and s["name"] in built else None)
            for s in (data.get("left") or {}).get("symbols", [])
            if s.get("kind") == "SYMBOL_FUNCTION" and s.get("instructions")}


def score(target: Path, base: Path, symbol: str) -> float:
    """One function's score (0-100); 100 = the game's machine code, exactly."""
    data = _diff(target, base, symbol)
    right = next((s for s in (data.get("right") or {}).get("symbols", []) if s.get("name") == symbol), None)
    if right is None or not right.get("instructions"):
        return -1.0
    pct = right.get("match_percent")
    return float(pct) if pct is not None else 0.0


def is_match(target: Path, base: Path, symbol: str) -> bool:
    """A match in the app's own sense (score 100; the app adds literal/mapping checks itself before committing)."""
    return score(target, base, symbol) >= 100.0


# ------------------------------------------------------------------ state/logs

def log(kind: str, **fields) -> None:
    STATE.mkdir(exist_ok=True)
    with (STATE / f"{kind}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **fields}) + "\n")


def best_of(symbol: str) -> tuple[float, str | None]:
    row = query_one("SELECT best_score,best_source FROM functions WHERE project_id=1 AND name=?", symbol)
    return (float(row[0]), row[1]) if row else (0.0, None)


def known_name(symbol: str) -> str | None:
    """The name the project knows a function by, when it is not the symbol's own spelling."""
    found = re.search(r"func_([0-9A-Fa-f]{8})", symbol)
    if not found:
        return None
    return (functions().get("names") or {}).get("func_" + found.group(1).upper())


def definition_span(source_text: str, symbol: str, name_hint: str | None = None):
    """Where the function's definition is in `source_text`.

    A constructor or destructor is spelled `Class::Class` / `Class::~Class`, not `func_0040D615`, so the symbol alone
    often finds nothing even though the definition is right there: fall back to the name the project knows it by.
    """
    from . import csource
    span = csource.find(source_text, [name_hint] if name_hint else [], symbol)
    if span[1] >= 0:
        return span
    hint = name_hint or known_name(symbol)
    if hint:
        return csource.find(source_text, [hint], symbol)
    return span


_STALE: dict | None = None
_STALE_MTIME: float = -1.0


def stale() -> dict:
    """Functions whose recorded best score the current tree cannot reproduce (`outmatch.sh stale`)."""
    global _STALE, _STALE_MTIME
    path = STATE / "stale.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _STALE, _STALE_MTIME = {}, -1.0
        return {}
    if _STALE is None or mtime != _STALE_MTIME:
        try:
            _STALE = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _STALE = {}
        _STALE_MTIME = mtime
    return _STALE


def function_by_name(name: str) -> dict | None:
    """One function's row, open or not, draft or not."""
    rows = query("SELECT id,name,unit,n_insns,size,best_score,best_source,permuted,meta FROM functions "
                 "WHERE project_id=? AND name=?", project_id(), name)
    if not rows:
        return None
    fid, sym, unit, n_insns, size, score, src, permuted, meta = rows[0]
    meta = json.loads(meta) if meta else {}
    return {"id": fid, "name": sym, "unit": unit or Path(meta.get("source", "")).stem, "n_insns": n_insns,
            "size": size, "score": score, "source": src, "permuted": permuted, "meta": meta}


def draftless(min_size: int = 0, limit: int | None = None, exclude_claims: bool = True) -> list[dict]:
    """Open functions with no C at all, biggest first - what a first draft would unlock."""
    sql = ("SELECT id,name,unit,n_insns,size,attempts,meta FROM functions "
           "WHERE project_id=? AND status='open' AND best_source IS NULL AND size>=?")
    args: list = [project_id(), int(min_size)]
    if exclude_claims:
        sql += " AND id NOT IN (SELECT function_id FROM claims)"
    sql += " ORDER BY size DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    out = []
    for fid, name, unit, n_insns, size, attempts, meta in query(sql, *args):
        meta = json.loads(meta) if meta else {}
        out.append({"id": fid, "name": name, "unit": unit or Path(meta.get("source", "")).stem,
                    "n_insns": n_insns, "size": size, "score": None, "source": None,
                    "attempts": attempts, "meta": meta})
    return out
