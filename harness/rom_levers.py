"""Mechanical rewrites for gcc-family compilers (agbcc, old_agbcc, ee-gcc, ...): what moves their output.

From decompdecomp's own agbcc guide (consoles/gba/guides/agbcc.md) — local declaration order and first use drive
register allocation; narrow types add truncation shifts; loop shape changes block layout; `if (a && b)` against a
nested if; temporaries; casts on subexpressions; `register` — each as a rewrite of the function's C. Every variant is
judged; none is assumed to keep the meaning (the judge decides, as for the permuter's rewrites).
"""

from __future__ import annotations

import re

from . import csource

DECL = re.compile(r"^(?P<ind>[ \t]+)(?P<reg>register\s+)?(?P<type>(?:const\s+)?(?:unsigned\s+|signed\s+)?\w+)"
                  r"(?P<ptr>\s*\**\s*)(?P<name>\w+)(?P<rest>\s*(?:=[^;]*)?;)\s*$")
NOT_TYPES = {"return", "goto", "break", "continue", "case", "else", "do"}
NARROW = ["u8", "s8", "u16", "s16"]
WIDE = ["int", "s32", "u32"]
CASTS = ["(s32)", "(u32)", "(u16)", "(s16)", "(u8)"]


def _decls(lines: list[str]) -> list[int]:
    out = []
    for i, ln in enumerate(lines):
        m = DECL.match(ln)
        if m and m.group("type").split()[-1] not in NOT_TYPES and "(" not in ln.split("=")[0]:
            out.append(i)
    return out


def _body(src: str) -> tuple[str, str, str] | None:
    """(head up to and with '{', body, closing '}' and after) of the first function definition in `src`."""
    masked = csource._blank(src)
    m = re.search(r"\)\s*\{", masked)
    if not m:
        return None
    start, depth = m.end(), 1
    for i in range(start, len(masked)):
        depth += {"{": 1, "}": -1}.get(masked[i], 0)
        if depth == 0:
            return src[:start], src[start:i], src[i:]
    return None


def _block_end(text: str, open_at: int) -> int:
    """Index of the '}' closing the '{' at open_at, or -1."""
    masked, depth = csource._blank(text), 0
    for i in range(open_at, len(masked)):
        depth += {"{": 1, "}": -1}.get(masked[i], 0)
        if depth == 0:
            return i
    return -1


def decl_order(lines: list[str]) -> list[list[str]]:
    """Each pair of adjacent local declarations swapped (allocation follows declaration order)."""
    d, out = _decls(lines), []
    for a, b in zip(d, d[1:]):
        if b == a + 1 and "=" not in lines[a] and "=" not in lines[b]:
            new = list(lines)
            new[a], new[b] = new[b], new[a]
            out.append(new)
    return out


def retype(lines: list[str]) -> list[list[str]]:
    """Each local's type widened (narrow types add truncation shifts) or narrowed."""
    out = []
    for i in _decls(lines):
        m = DECL.match(lines[i])
        t = m.group("type").split()[-1]
        if m.group("ptr").strip():
            continue
        for new_t in (WIDE if t in NARROW or t in ("char", "short") else NARROW if t in WIDE + ["unsigned", "long"] else []):
            new = list(lines)
            new[i] = lines[i][:m.start("type")] + new_t + lines[i][m.end("type"):]
            out.append(new)
    return out


def register(lines: list[str]) -> list[list[str]]:
    out = []
    for i in _decls(lines):
        m = DECL.match(lines[i])
        new = list(lines)
        new[i] = (lines[i].replace("register ", "", 1) if m.group("reg")
                  else lines[i][:m.start("type")] + "register " + lines[i][m.start("type"):])
        out.append(new)
    return out


def loops(body: str) -> list[str]:
    """for -> while, while -> guarded do/while (block layout differs)."""
    out, masked = [], csource._blank(body)
    for m in re.finditer(r"\bfor\s*\(", masked):
        close = csource._skip_parens(masked, m.end() - 1)
        parts = masked[m.end():close - 1].split(";")
        brace = masked.find("{", close)
        if close < 0 or len(parts) != 3 or brace < 0 or masked[close:brace].strip():
            continue
        end = _block_end(body, brace)
        inner = body[brace + 1:end]
        if end < 0 or re.search(r"\bcontinue\b", csource._blank(inner)):
            continue
        a, b, c = (body[m.end():close - 1].split(";") + ["", "", ""])[:3]
        init = f"{a.strip()};\n" if a.strip() else ""
        step = f"    {c.strip()};\n" if c.strip() else ""
        out.append(body[:m.start()] + init + f"while ({b.strip() or '1'}) {{" + inner + step + "}" + body[end + 1:])
    for m in re.finditer(r"\bwhile\s*\(", masked):
        if masked[:m.start()].rstrip().endswith("}"):  # the tail of a do/while
            continue
        close = csource._skip_parens(masked, m.end() - 1)
        brace = masked.find("{", close)
        if close < 0 or brace < 0 or masked[close:brace].strip():
            continue
        end = _block_end(body, brace)
        if end < 0 or re.search(r"\bcontinue\b", csource._blank(body[brace + 1:end])):
            continue
        cond = body[m.end():close - 1]
        out.append(body[:m.start()] + f"if ({cond}) {{ do {{" + body[brace + 1:end] + f"}} while ({cond}); }}"
                   + body[end + 1:])
    return out


def conditions(body: str) -> list[str]:
    """`if (a && b) {X}` -> `if (a) { if (b) {X} }` (no else), and back."""
    out, masked = [], csource._blank(body)
    for m in re.finditer(r"\bif\s*\(", masked):
        close = csource._skip_parens(masked, m.end() - 1)
        cond = masked[m.end():close - 1]
        depth, cut = 0, -1
        for i, ch in enumerate(cond):
            depth += {"(": 1, ")": -1}.get(ch, 0)
            if depth == 0 and cond[i:i + 2] == "&&":
                cut = i
                break
        brace = masked.find("{", close)
        if cut < 0 or brace < 0 or masked[close:brace].strip():
            continue
        end = _block_end(body, brace)
        if end < 0 or re.match(r"\s*else\b", masked[end + 1:]):
            continue
        real = body[m.end():close - 1]
        a, b = real[:cut].strip(), real[cut + 2:].strip()
        out.append(body[:m.start()] + f"if ({a}) {{ if ({b}) {{" + body[brace + 1:end] + "} }" + body[end + 1:])
    for m in re.finditer(r"\bif\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*\{\s*if\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*\{",
                         masked):
        outer = masked.find("{", m.start())
        end_o, inner_open = _block_end(body, outer), m.end() - 1
        end_i = _block_end(body, inner_open)
        if end_o < 0 or end_i < 0 or masked[end_i + 1:end_o].strip() or re.match(r"\s*else\b", masked[end_o + 1:]):
            continue
        out.append(body[:m.start()] + f"if (({m.group(1)}) && ({m.group(2)})) {{" + body[inner_open + 1:end_i] + "}"
                   + body[end_o + 1:])
    return out


def temps(body: str) -> list[str]:
    """Drop a temporary that is only returned; return a ternary as if/return and back."""
    out = []
    for m in re.finditer(r"^([ \t]*)(?:[\w\s\*]*?\s)?(\w+)\s*=(?!=)\s*([^;]+);\s*\n\s*return\s+\2\s*;", body, re.M):
        out.append(body[:m.start()] + f"{m.group(1)}return {m.group(3).strip()};" + body[m.end():])
    for m in re.finditer(r"if\s*\(([^;{}]+)\)\s*return\s+([^;]+);\s*\n(\s*)return\s+([^;]+);", body):
        out.append(body[:m.start()] + f"return ({m.group(1)}) ? ({m.group(2)}) : ({m.group(4)});" + body[m.end():])
    for m in re.finditer(r"return\s+\(?([^;?]+?)\)?\s*\?\s*\(?([^;:]+?)\)?\s*:\s*\(?([^;]+?)\)?\s*;", body):
        out.append(body[:m.start()] + f"if ({m.group(1)}) return {m.group(2)};\n    return {m.group(3)};" + body[m.end():])
    return out


def casts(body: str) -> list[str]:
    """A cast on an assignment's right-hand side (changes when truncation happens)."""
    out = []
    for m in re.finditer(r"(?<![=!<>+\-*/&|^])=(?!=)\s*([^;]+);", body):
        rhs = m.group(1)
        if rhs.lstrip().startswith("("):
            continue
        for c in CASTS:
            out.append(body[:m.start(1)] + f"{c}({rhs})" + body[m.end(1):])
    return out


def variants(src: str, family: str = "agbcc", cap: int = 240) -> list[str]:
    """Every single rewrite of the function's C; gcc-family levers (other families: none here yet)."""
    if family not in ("agbcc", "gcc"):
        return []
    parts = _body(src)
    if not parts:
        return []
    head, body, tail = parts
    out: list[str] = []
    lines = body.split("\n")
    for f in (decl_order, retype, register):
        out += [head + "\n".join(v) + tail for v in f(lines)]
    for f in (loops, conditions, temps, casts):
        out += [head + v + tail for v in f(body)]
    return list(dict.fromkeys(v for v in out if v != src))[:cap]


if __name__ == "__main__":  # self-check
    s = ("int f(u8 *p, int n) {\n    u8 a;\n    int b;\n    int i;\n    for (i = 0; i < n; i++) {\n        a += p[i];\n    }\n"
         "    if (a && n) {\n        b = a;\n    }\n    b = a + 1;\n    return b;\n}\n")
    v = variants(s)
    assert any("while (i < n)" in x for x in v), "for -> while"
    assert any("if (a) { if (n)" in x for x in v), "&& -> nested if"
    assert any("    int a;" in x for x in v), "u8 -> int"
    assert any("int i;\n    int b;" in x for x in v), "decl swap"
    assert any("return a + 1;" in x for x in v), "drop temp"
    assert any("(u16)(a + 1)" in x for x in v), "cast"
    print("ok", len(v))
