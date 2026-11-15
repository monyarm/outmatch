"""Compiler-state carriers around one function: #pragma switches and declaration runs.

MSVC 6's output for a function depends on state no C construct describes: the optimisation pragmas in force at its
definition, the pack of the declarations the compiler has already seen, and the incidental bookkeeping that a run of
unused declarations perturbs.  The isle project calls this *compiler entropy* and ReproBit certifies the "required
compiler conditions" as state carriers; CMR2Decomp records the same effect for this compiler (an unrelated inline
member in a header moved code in other TUs).

Everything here is expressible as the app accepts it -- `#pragma` lines and one-line declarations beside the
definition (`forkengine._around`) -- so a carrier that wins can go straight in through `/api/try`.  Wrappers are
scored in the scratch trees first; only a full 100 reaches the live app.
"""

from __future__ import annotations

from . import common, search

# `#pragma optimize("letters", off|on)`: letters are MSVC6's own optimisation switches (g global, s size, t speed,
# y frame-pointer omission, a/w aliasing, i intrinsics, o loop, p precision, n FP).  Unknown letters are ignored.
OPTIMIZE = ("", "g", "s", "t", "y", "a", "w")
DEPTHS = ("0", "1", "2", "3", "6", "255")


def _off(name: str, letters: str) -> tuple[str, str]:
    """A pragma that turns `letters` off for the definition and back on after it."""
    return name, f'#pragma optimize("{letters}", off)\n', f'#pragma optimize("{letters}", on)\n'


def carriers(definition: str) -> list[tuple[str, str, str]]:
    """(label, definition with pragma wrappers, one-line declarations to place before it)."""
    out: list[tuple[str, str, str]] = []
    body = definition.rstrip()
    for letters in OPTIMIZE:
        for state in ("off", "on"):
            name = f'optimize("{letters}", {state})'
            if letters == "" and state == "on":
                continue  # the project's own state: no variant
            before, after = _off(name, letters)[1], _off(name, letters)[2]
            if state == "on":
                before = f'#pragma optimize("{letters}", on)\n'
                after = ""
            out.append((name, before + body + ("\n" + after.rstrip() if after else ""), ""))
    out.append(("auto_inline(off)",
                '#pragma auto_inline(off)\n' + body + '\n#pragma auto_inline(on)', ""))
    for depth in DEPTHS:
        out.append((f"inline_depth({depth})", f'#pragma inline_depth({depth})\n' + body, ""))
    out.append(("inline_recursion(on)", "#pragma inline_recursion(on)\n" + body, ""))
    out.append(("inline_recursion(off)", "#pragma inline_recursion(off)\n" + body, ""))
    # declaration runs: ReproBit's forward-declaration / extern / typedef state carriers (all one-line declarations,
    # which `_around` accepts).  Sizes are the useful part of its 1..32 grid.
    for n in (1, 2, 4, 8, 16, 32):
        out.append((f"forward_run({n})", body,
                    "\n".join(f"struct OmFwd{i};" for i in range(n))))
    for n in (1, 2, 4, 8, 16, 32):
        out.append((f"extern_run({n})", body,
                    "\n".join(f"extern int OmExt{i};" for i in range(n))))
    for n in (1, 2, 4, 8, 16):
        out.append((f"typedef_run({n})", body,
                    "\n".join(f"typedef int OmTyp{i};" for i in range(n))))
    return out


def sweep(fn: dict, seconds: float = 120, jobs: int = common.JOBS, submit_match: bool = True,
          apply_best: bool = False) -> dict:
    """Every carrier against one function; returns the summary `search.judge` printed."""
    text = common.source_path(fn["unit"]).read_text(encoding="utf-8")
    own, start, end = search.common.definition_span(text, fn["name"])
    if start < 0:
        return {"symbol": fn["name"], "note": "no definition", "matched": False}
    definition = text[start:end]
    variants = carriers(definition)
    return search.judge(fn, variants, jobs=jobs, submit_match=submit_match, apply_best=apply_best)
