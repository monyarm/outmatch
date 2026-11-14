"""Variants of one function's C, made by decomp-permuter's generator.

Runs under the app's own Python (it imports decompnet so the header is prepared exactly as the app's permuter does it);
the harness talks to it over JSON lines.  It is a separate process so the harness itself needs nothing but stdlib.

  {"op": "prepare", "unit": "text_0043B124", "symbol": "@func_0043B414@4"}
      -> {"header": ..., "name": "C_0043B4EE::func_0043B414", "cxx": false}
  {"op": "variants", "header": ..., "source": ..., "function": ..., "count": 64, "seed": 123}
      -> {"variants": ["...", ...]}  (empty when the generator can't read the C)
  {"op": "close"} -> {"done": true}
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("OUTMATCH_ROOT", Path.cwd()))

_QUOTED = re.compile(r'^\s*#\s*include\s+"([^"]+)"', re.M)


def _without_bodies(text: str) -> str:
    """The file's declarations only: each function body cut to ';' (same idea as the app's)."""
    from decompnet.forks import csource

    clean, out, i, depth, start = csource._blank(text), [], 0, 0, None
    for k, c in enumerate(clean):
        if c == "{":
            if depth == 0 and clean[:k].rstrip().endswith(")"):
                start = k
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                out += [text[i:start], ";"]
                i, start = k + 1, None
    return "".join(out) + text[i:]


def prepare(unit: str, symbol: str) -> dict:
    from decompnet.app import permute
    from decompnet import seeds
    from decompnet.forks import csource

    from . import csource as ours  # noqa: F401  (harness copy defines the same find)

    path = ROOT / "src" / "auto" / f"{unit}.cpp"
    text = path.read_text(encoding="utf-8", errors="replace")
    own, start, end = ours.find(text, [], symbol)
    if start < 0:
        raise SystemExit(f"{symbol}: no definition in {path}")
    definition = text[start:end]

    seen: set[Path] = set()

    def headers(body: str, folder: Path) -> list[str]:
        out: list[str] = []
        for name in _QUOTED.findall(body):
            for base in (ROOT / "include", folder, ROOT / "src", ROOT):
                h = (base / name).resolve()
                if h.is_file():
                    if h not in seen and len(seen) < 64 and h.is_relative_to(ROOT.resolve()):
                        seen.add(h)
                        own_text = h.read_text(encoding="utf-8", errors="replace")
                        out += [*headers(own_text, h.parent), own_text]
                    break
        return out

    raw = "\n".join([*headers(text, path.parent), _without_bodies(text)])
    cxx = permute.is_cxx(definition)
    header = permute.permuter_header(seeds.c_only(raw, cxx=cxx), definition)
    return {"header": header, "name": own, "cxx": cxx}


def main() -> int:
    from decompnet.app import permute

    gen = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        job = json.loads(line)
        op = job.get("op")
        try:
            if op == "prepare":
                out = prepare(job["unit"], job["symbol"])
            elif op == "variants":
                if gen is None:
                    gen = permute.Generator()
                variants = gen.batch({"header": job["header"], "source": job["source"],
                                      "function": job["function"], "family": job.get("family", "base"),
                                      "seed": int(job["seed"]), "count": int(job.get("count", 64))})
                out = {"variants": variants or []}
            elif op == "close":
                if gen is not None:
                    gen.close()
                print(json.dumps({"done": True}), flush=True)
                return 0
            else:
                out = {"error": f"unknown op {op}"}
        except Exception as e:  # the harness turns this into "no variants", like the app does
            out = {"error": f"{type(e).__name__}: {e}"}
        print(json.dumps(out), flush=True)
    if gen is not None:
        gen.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
