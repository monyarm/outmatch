"""Mechanical source idioms that move MSVC 6's code (the CMR2Decomp / brickpark lever catalogues, automated part).

Only rewrites with an exact, local meaning are here: they change how the compiler sees a statement without changing
what it does.  The larger levers -- stack-slot reference counts, separate globals instead of a blob, `volatile` for
tail duplication, `char` indices for `shl r,2`, switch case order -- need a human/agent reading the diff, and live in
`.claude/skills/msvc-matching/`.  Candidates are scored in scratch trees and only a full 100 is submitted.
"""

from __future__ import annotations

import re

from . import common, search

DECL = re.compile(
    r"^(?P<indent>[ \t]*)(?P<type>(?:unsigned\s+|signed\s+)?"
    r"(?:int|char|short|long|float|double|bool|void|BYTE|WORD|DWORD|BOOL|UINT|LPVOID|HANDLE|HWND|[A-Z]\w*)"
    r"(?:\s*\*)?)\s+(?P<name>\w+)(?P<array>\[\d*\])?\s*;\s*$")
COMPOUND = {"+=": "+", "-=": "-", "|=": "|", "&=": "&", "^=": "^", "<<=": "<<", ">>=": ">>"}


def _match(text: str, at: int, open_ch: str, close_ch: str) -> int:
    """Index just past the bracket that closes the one at `at` (-1 if unbalanced), strings/comments ignored roughly."""
    depth, i, n = 0, at, len(text)
    while i < n:
        ch = text[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        elif ch in "\"'":
            quote, i = ch, i + 1
            while i < n and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
        i += 1
    return -1


def _body_span(definition: str) -> tuple[int, int]:
    at = definition.find("{")
    if at < 0:
        return -1, -1
    return at, _match(definition, at, "{", "}")


def swap_if_else(definition: str) -> str | None:
    """`if (c) {A} else {B}` -> `if (!(c)) {B} else {A}`."""
    m = re.search(r"\bif\s*\(", definition)
    if not m:
        return None
    p_open = definition.index("(", m.start())
    p_close = _match(definition, p_open, "(", ")")
    if p_close < 0:
        return None
    a_open = definition.find("{", p_close)
    if a_open < 0 or definition[p_close:a_open].strip():
        return None
    a_close = _match(definition, a_open, "{", "}")
    if a_close < 0:
        return None
    e = re.match(r"\s*else\s*", definition[a_close:])
    if not e:
        return None
    b_open = a_close + e.end()
    if definition[b_open:b_open + 1] != "{":
        return None
    b_close = _match(definition, b_open, "{", "}")
    if b_close < 0:
        return None
    cond = definition[p_open + 1:p_close - 1]
    a, b = definition[a_open:a_close], definition[b_open:b_close]
    return (definition[:m.start()] + f"if (!({cond})) {b} else {a}" + definition[b_close:])


def ternary_to_if(definition: str) -> str | None:
    """`x = c ? a : b;` -> `if (c) x = a; else x = b;` (one line, no nested `?`)."""
    for line in definition.splitlines():
        m = re.fullmatch(r"[ \t]*([\w\.\[\]\->]+)\s*=\s*([^?;]+?)\s*\?\s*([^:;]+?)\s*:\s*([^;]+);[ \t]*", line)
        if m and "?" not in m.group(2) and "?" not in m.group(3):
            lhs, cond, a, b = m.groups()
            new = f"if ({cond}) {lhs} = {a}; else {lhs} = {b};"
            return definition.replace(line, new, 1)
    return None


def and_or_swap(definition: str) -> str | None:
    """Swap the two sides of the last top-level `&&`/`||` in the first `if` condition."""
    m = re.search(r"\bif\s*\(", definition)
    if not m:
        return None
    p_open = definition.index("(", m.start())
    p_close = _match(definition, p_open, "(", ")")
    if p_close < 0:
        return None
    cond = definition[p_open + 1:p_close - 1]
    depth, cut, op = 0, -1, ""
    for i, ch in enumerate(cond):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 0:
            two = cond[i:i + 2]
            if two in ("&&", "||"):
                cut, op = i, two
    if cut < 0:
        return None
    left, right = cond[:cut].strip(), cond[cut + len(op):].strip()
    return definition[:p_open + 1] + f"{right} {op} {left}" + definition[p_close - 1:]


def compound_split(definition: str) -> str | None:
    """`x += y;` -> `x = x + y;`."""
    changed = False

    def repl(m: re.Match) -> str:
        nonlocal changed
        changed = True
        return f"{m.group(1)} = {m.group(1)} {COMPOUND[m.group(2)]} {m.group(3)};"

    out = re.sub(r"\b(\w+)\s*(\+=|-=|\|=|&=|\^=|<<=|>>=)\s*([^;]+);", repl, definition)
    return out if changed else None


def assign_to_compound(definition: str) -> str | None:
    """`x = x + y;` -> `x += y;` (and the other arithmetic/bitwise forms)."""
    rev = {v: k for k, v in COMPOUND.items()}
    changed = False

    def repl(m: re.Match) -> str:
        nonlocal changed
        changed = True
        return f"{m.group(1)} {rev[m.group(2)]} {m.group(3)};"

    out = re.sub(r"\b(\w+)\s*=\s*\1\s*(\+|-|\||&|\^|<<|>>)\s*([^;]+);", repl, definition)
    return out if changed else None


def standalone_incdec(definition: str) -> str | None:
    """`x++;` -> `++x;` (only where the value isn't used)."""
    out, n = re.subn(r"(?m)^([ \t]*)([\w\.\[\]\->]+)\+\+;[ \t]*$", r"\1++\2;", definition)
    if n:
        return out
    out, n = re.subn(r"(?m)^([ \t]*)([\w\.\[\]\->]+)--;[ \t]*$", r"\1--\2;", definition)
    return out if n else None


def split_chain_assign(definition: str) -> str | None:
    """`a = b = c;` -> `b = c; a = b;`."""
    m = re.search(r"(?m)^([ \t]*)(\w+)\s*=\s*(\w+)\s*=\s*([^;]+);[ \t]*$", definition)
    if not m:
        return None
    indent, a, b, c = m.groups()
    return definition[:m.start()] + f"{indent}{b} = {c};\n{indent}{a} = {b};" + definition[m.end():]


def hoist_declarations(definition: str) -> str | None:
    """Move plain local declarations to the top of the body (MSVC orders frame slots by use, not declaration)."""
    start, end = _body_span(definition)
    if start < 0:
        return None
    body = definition[start + 1:end - 1]
    lines, decls = [], []
    for line in body.splitlines():
        m = DECL.match(line)
        if m and m.group("name") not in ("return", "else", "case"):
            decls.append(m.group(0).strip())
        else:
            lines.append(line)
    if not decls:
        return None
    inner = "\n".join(lines).strip("\n")
    return (definition[:start + 1] + "\n" + "\n".join("    " + d for d in decls) + "\n" + inner + "\n"
            + definition[end - 1:])


TRANSFORMS = {
    "if-else-swap": (swap_if_else, "original's branch bodies are the other way round"),
    "ternary-to-if": (ternary_to_if, "setcc/arithmetic where the original has a branch"),
    "and-or-swap": (and_or_swap, "short-circuit order changes which side is tested first"),
    "compound-split": (compound_split, "`add r,-k` where a compound statement folds"),
    "assign-to-compound": (assign_to_compound, "the reverse: a folded add where the original reads x twice"),
    "postinc": (standalone_incdec, "`inc` before/after a use that only needs the side effect"),
    "chain-assign": (split_chain_assign, "chained stores to two variables of one value"),
    "decl-hoist": (hoist_declarations, "frame-slot order / a local declared where it is used"),
}


def variants(definition: str, only: str = "") -> list[tuple[str, str, str]]:
    wanted = [k.strip() for k in only.split(",") if k.strip()] or list(TRANSFORMS)
    out: list[tuple[str, str, str]] = []
    seen = {definition}
    for name in wanted:
        fn = TRANSFORMS.get(name)
        if fn is None:
            continue
        new = fn[0](definition)
        if new and new not in seen:
            seen.add(new)
            out.append((name, new, ""))
    # a combination of everything that applied, in catalogue order: cheap and occasionally wins where none does
    combined = definition
    for name in wanted:
        fn = TRANSFORMS.get(name)
        if fn is None:
            continue
        new = fn[0](combined)
        if new:
            combined = new
    if combined not in seen:
        out.append(("all", combined, ""))
    return out


def apply(name: str, only: str = "", jobs: int = common.JOBS, submit_match: bool = True,
          apply_best: bool = False) -> dict:
    fn = {"name": name, "unit": common.unit_of(name)}
    text = common.source_path(fn["unit"]).read_text(encoding="utf-8")
    own, start, end = search.csource.find(text, [], name)
    if start < 0:
        return {"symbol": name, "note": "no definition", "matched": False}
    return search.judge(fn, variants(text[start:end], only), jobs=jobs, submit_match=submit_match,
                        apply_best=apply_best)
