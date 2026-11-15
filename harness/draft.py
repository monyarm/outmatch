"""First-draft packets for functions that have no C at all.

A function with no draft cannot be permuted or improved: there is nothing to permute from. This collects what the
project already knows about one - its target assembly, the Ghidra pseudocode, the declarations around it, its
calling convention and the functions it calls - into a single markdown packet a model (or a person) can write C from.

    python3 -m harness.draft --limit 5 [--min-size 512] [--out DIR] [--print]

Write the C, then judge it locally with `outmatch.sh try SYMBOL FILE` and hand it to the app with `--submit`, or let
`outmatch.sh loop --apply-improvements` pick it up once it beats the (now -1) best.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from . import common

ADDR = re.compile(r"([0-9A-Fa-f]{8})")


def entry(unit: str, name: str) -> dict | None:
    info = common.functions()
    for fn in (info.get("units") or {}).get(unit, []):
        if fn.get("name") == name:
            return fn
    return None


def convention(symbol: str) -> str:
    """The calling convention and argument bytes MSVC's decoration spells out."""
    if symbol.startswith("@"):
        name, _, size = symbol[1:].rpartition("@")
        return f"fastcall, {size} bytes of arguments"
    if symbol.startswith("?") or "@?" in symbol:
        return "C++ member or mangled name"
    if symbol.startswith("_"):
        return "cdecl"
    _, at, size = symbol.rpartition("@")
    if at and size.isdigit():
        return f"stdcall, {size} bytes of arguments"
    return "cdecl (undecorated)"


def target_asm(unit: str, symbol: str) -> list[str]:
    try:
        target = common.target_obj(unit)
        data = common._diff(target, target, symbol)  # the target against itself: carry the original's instructions
    except Exception:
        return []
    for sym in (data.get("left") or {}).get("symbols", []):
        if sym.get("name") == symbol and sym.get("instructions"):
            out = []
            for i, row in enumerate(sym["instructions"]):
                text = (row.get("instruction") or {}).get("formatted") or ""
                if text:
                    out.append(f"{i:4d}  {text}")
            return out
    return []


def pseudocode(unit: str, symbol: str) -> str:
    found = ADDR.search(symbol) or ADDR.search(symbol.split("@")[0])
    if found is None:
        return ""
    key = found.group(1).upper()
    for path in sorted(Path(common.ROOT, ".decomp-wtf", "ghidra").glob(f"{unit}-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for name, text in data.items():
            if key in ADDR.findall(name.upper()):
                return text.strip()
    return ""


def prologue(unit: str, lines: int = 90) -> str:
    text = common.source_path(unit).read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(text[:lines])


def packet(fn: dict) -> str:
    symbol, unit = fn["name"], fn["unit"]
    info = entry(unit, symbol) or {}
    addr = info.get("addr")
    names = common.functions().get("names") or {}
    relocs = info.get("relocs") or []
    calls = []
    for _, _, target, _ in relocs:
        target = target or ""
        if "func_" not in target:
            continue
        key = "func_" + re.search(r"func_([0-9A-Fa-f]{8})", target).group(1).upper()
        calls.append(f"{target} = {names.get(key, 'unknown')}")
    asm = target_asm(unit, symbol)
    pseudo = pseudocode(unit, symbol)
    parts = [
        f"# {symbol}",
        "",
        f"- unit: `src/auto/{unit}.cpp`",
        f"- address: `0x{addr:08X}`" if addr else "",
        f"- size: {fn['size']} bytes, {fn['n_insns']} instructions, {fn.get('attempts', 0)} attempts",
        f"- calling convention: {convention(symbol)}",
        f"- source symbol: `{info.get('name', symbol)}`",
        "",
        "## Target assembly (delinked original)",
        "```",
        *(asm or ["(could not read the target object)"]),
        "```",
        "",
        "## Ghidra pseudocode",
        "```c",
        pseudo or "(none)",
        "```",
        "",
        "## Declarations at the top of the unit",
        "```c",
        prologue(unit),
        "```",
    ]
    if calls:
        parts += ["", "## Calls and data references"] + [f"- {c}" for c in sorted(set(calls))]
    parts += [
        "",
        "## How to test",
        f"```",
        f"../outmatch/outmatch.sh try '{symbol}' draft.c",
        f"```",
        "A compiling definition that beats the app's best is submitted by `loop --apply-improvements`; a 100 is "
        "committed by the app.",
        "",
    ]
    return "\n".join(p for p in parts if p != "")


def main() -> int:
    parser = argparse.ArgumentParser(description="write first-draft packets for functions with no C")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min-size", type=int, default=0)
    parser.add_argument("--out", default=str(Path(common.STATE) / "drafts"))
    parser.add_argument("--write", action="store_true", help="save the packets (default: print them)")
    parser.add_argument("--print", action="store_true", help="print each packet")
    args = parser.parse_args()
    todo = common.draftless(min_size=args.min_size, limit=args.limit)
    out = Path(args.out)
    if args.write:
        out.mkdir(parents=True, exist_ok=True)
    for fn in todo:
        text = packet(fn)
        if args.write:
            path = out / (fn["name"].replace("@", "_").replace("?", "_") + ".md")
            path.write_text(text, encoding="utf-8")
        print(json.dumps({"symbol": fn["name"], "unit": fn["unit"], "size": fn["size"],
                          "file": str(out / (fn["name"].replace("@", "_").replace("?", "_") + ".md"))
                          if args.write else None}))
        if args.print and not args.write:
            print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
