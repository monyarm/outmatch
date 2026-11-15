"""The MSVC6 trick catalogue, applied mechanically: one command that tries the skill's levers for you.

`.claude/skills/msvc-matching` explains *why* MSVC 6 emits one instruction rather than another. Most of those
explanations reduce to a small, local edit that can be enumerated without a human reading the diff:

* **argument order at a call** - the compiler materialises the argument temporaries in parameter order, so swapping
  two arguments of equal type moves several instructions at once (this is what fixed `@func_0043B414@4`);
* **local names and declaration order** - frame slots are ordered by reference count, and ties are broken by name;
* **condition direction** - `if (a == b)` vs `if (b == a)`, `if (x)` vs `if (x != 0)`, the `else` bodies swapped;
* **type width and signedness** - `int` vs `unsigned` changes `jl`/`jb`, `char` changes `shl r,2` to `[r*4]`;
* **value vs branch** - a ternary that the original lowered to `setcc`, or a `return` the original duplicated;
* **statement order and `volatile`** - tail merging and reload behaviour.

Everything is a pure `definition -> [definition]` rewrite, scored in scratch trees by `search.judge`, so nothing
touches the project until a variant reaches 100 or `--apply` asks for the improvement to be submitted.
"""

from __future__ import annotations

import re

from . import common, search
from .levers import COMPOUND, DECL, _body_span, _match

KEYWORDS = {"if", "for", "while", "switch", "return", "sizeof", "do", "else", "catch", "new", "delete"}
TYPES = ("unsigned char", "signed char", "unsigned short", "unsigned int", "unsigned long", "unsigned",
         "char", "short", "int", "long", "bool")
FLIP = {"==": "==", "!=": "!=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}


# ------------------------------------------------------------------ parsing helpers

def calls(definition: str) -> list[tuple[str, int, int, list[str]]]:
    """Every call in the definition: `(name, open_paren, close_paren, [argument texts])`."""
    out: list[tuple[str, int, int, list[str]]] = []
    for m in re.finditer(r"[A-Za-z_]\w*(?=\s*\()", definition):
        name = m.group(0)
        if name in KEYWORDS:
            continue
        p_open = definition.find("(", m.end())
        p_close = _match(definition, p_open, "(", ")")
        if p_close < 0:
            continue
        args, depth, start = [], 0, p_open + 1
        for i in range(p_open + 1, p_close - 1):
            ch = definition[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 0:
                args.append(definition[start:i].strip())
                start = i + 1
        args.append(definition[start:p_close - 1].strip())
        out.append((name, p_open, p_close, args))
    return out


def locals_of(definition: str) -> list[tuple[str, str]]:
    """`(type, name)` for each plain local declaration in the definition."""
    out = []
    for line in definition.splitlines():
        m = DECL.match(line)
        if m and m.group("name") not in KEYWORDS:
            out.append((m.group("type").strip(), m.group("name")))
    return out


def _replace_ident(definition: str, old: str, new: str) -> str:
    return re.sub(rf"\b{re.escape(old)}\b", new, definition)


# ------------------------------------------------------------------ transforms

def swap_arguments(definition: str) -> list[tuple[str, str]]:
    """Swap each adjacent pair of arguments at every call site (equal-type pairs are the common win)."""
    out = []
    for name, p_open, p_close, args in calls(definition):
        if not 2 <= len(args) <= 8:
            continue
        for i in range(len(args) - 1):
            if args[i] == args[i + 1]:
                continue
            swapped = list(args)
            swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
            out.append((f"args:{name}:{i}", definition[:p_open + 1] + ", ".join(swapped)
                        + definition[p_close - 1:]))
    return out


def rename_locals(definition: str) -> list[tuple[str, str]]:
    """Swap the names of two locals of the same type: the frame-slot tie-break is by name."""
    out, seen = [], set()
    decls = locals_of(definition)
    for i, (ta, na) in enumerate(decls):
        for tb, nb in decls[i + 1:]:
            if ta != tb or na == nb or (na, nb) in seen:
                continue
            seen.add((na, nb))
            text = _replace_ident(_replace_ident(definition, na, "\x00"), nb, na).replace("\x00", nb)
            out.append((f"rename:{na}<->{nb}", text))
    return out


def reverse_declarations(definition: str) -> list[tuple[str, str]]:
    """Reverse the order of two adjacent declarations of the same type."""
    out, lines = [], definition.splitlines()
    for i in range(len(lines) - 1):
        a, b = DECL.match(lines[i]), DECL.match(lines[i + 1])
        if a and b and a.group("type") == b.group("type"):
            new = list(lines)
            new[i], new[i + 1] = new[i + 1], new[i]
            out.append((f"decl-order:{a.group('name')}<->{b.group('name')}", "\n".join(new)))
    return out


def condition_forms(definition: str) -> list[tuple[str, str]]:
    """`if (x)` <-> `if (x != 0)`, `if (!x)` -> `if (x == 0)`, `if (a == b)` -> `if (b == a)`."""
    out = []
    for m in re.finditer(r"\b(if|while)\s*\(", definition):
        p_open = definition.find("(", m.start())
        p_close = _match(definition, p_open, "(", ")")
        if p_close < 0:
            continue
        cond = definition[p_open + 1:p_close - 1].strip()
        head = definition[:p_open + 1]
        tail = definition[p_close - 1:]
        if cond.startswith("!") and "?" not in cond:
            out.append((f"not->eq:{cond[:24]}", head + f"{cond[1:].strip()} == 0" + tail))
        elif re.fullmatch(r"[\w\.\[\]\->]+", cond):
            out.append((f"truthy->neq:{cond[:24]}", head + f"{cond} != 0" + tail))
            out.append((f"truthy->eq0:{cond[:24]}", head + f"{cond} == 0" + tail))
        else:
            m2 = re.fullmatch(r"(.+?)\s*(==|!=|<=|>=|<|>)\s*(.+)", cond)
            if m2:
                a, op, b = (s.strip() for s in m2.groups())
                out.append((f"cmp-swap:{a[:16]}{op}{b[:16]}", head + f"{b} {FLIP[op]} {a}" + tail))
    return out


def ternary_swap(definition: str) -> list[tuple[str, str]]:
    """`c ? a : b` -> `!(c) ? b : a` (nested ternaries skipped)."""
    out = []
    for m in re.finditer(r"([^?;\n]+?)\?([^?:\n]+):([^;,\n\)]+)", definition):
        cond, a, b = (s.strip() for s in m.groups())
        out.append((f"ternary-swap:{cond[:20]}", definition[:m.start()] + f"{cond} ? {b} : {a}"
                    + definition[m.end():]))
    return out


def return_forms(definition: str) -> list[tuple[str, str]]:
    """`if (c) return a; return b;` <-> `return c ? a : b;` (one line, no nesting)."""
    out = []
    m = re.search(r"(?m)^([ \t]*)if\s*\(([^()]*)\)\s*return\s+([^;]+);\s*\n[ \t]*return\s+([^;]+);[ \t]*$",
                  definition)
    if m:
        indent, cond, a, b = m.groups()
        out.append(("branch->ternary", definition[:m.start()] + f"{indent}return {cond} ? {a} : {b};"
                    + definition[m.end():]))
    m = re.search(r"(?m)^([ \t]*)if\s*\(([^()]*)\)\s*return\s+([^;]+);\s*else\s*return\s+([^;]+);[ \t]*$",
                  definition)
    if m:
        indent, cond, a, b = m.groups()
        out.append(("if-else->ternary", definition[:m.start()] + f"{indent}return {cond} ? {a} : {b};"
                    + definition[m.end():]))
    for m in re.finditer(r"\breturn\s+([^;?\n]+?)\s*\?\s*([^:;\n]+?)\s*:\s*([^;]+);", definition):
        cond, a, b = (s.strip() for s in m.groups())
        out.append(("ternary->branch", definition[:m.start()]
                    + f"if ({cond}) return {a};\nreturn {b};" + definition[m.end():]))
    return out


def init_split(definition: str) -> list[tuple[str, str]]:
    """`T x = e;` -> `T x; x = e;` (a temporary the original kept in one store, or the reverse shape)."""
    out = []
    for line in definition.splitlines():
        m = DECL.match(line)
        if not m:
            continue
        whole = re.fullmatch(r"([ \t]*)(.+?)\s+(\w+)\s*=\s*([^;{}]+);[ \t]*", line)
        if whole and "()" not in whole.group(4):
            indent, ty, name, expr = whole.groups()
            out.append((f"init-split:{name}", definition.replace(
                line, f"{indent}{ty} {name};\n{indent}{name} = {expr};", 1)))
    return out


def zero_init(definition: str) -> list[tuple[str, str]]:
    """`T x = {0};` -> `T x; memset(&x, 0, sizeof x);` (stores where the original zeroes a struct)."""
    out = []
    for m in re.finditer(r"(?m)^([ \t]*)([A-Za-z_]\w*(?:\s*\*)?)\s+(\w+)\s*=\s*\{\s*0\s*\};\s*$", definition):
        indent, ty, name = m.groups()
        out.append((f"zero-init:{name}", definition[:m.start()]
                    + f"{indent}{ty} {name};\n{indent}memset(&{name}, 0, sizeof {name});"
                    + definition[m.end():]))
    return out


def volatile_locals(definition: str) -> list[tuple[str, str]]:
    """Mark one local `volatile` - the usual cure for a duplicated epilogue / reordered tail."""
    out, lines = [], definition.splitlines()
    for i, line in enumerate(lines):
        m = DECL.match(line)
        if m and "volatile" not in line:
            new = list(lines)
            new[i] = line.replace(m.group("type"), "volatile " + m.group("type"), 1)
            out.append((f"volatile:{m.group('name')}", "\n".join(new)))
    return out[:6]


def type_widths(definition: str) -> list[tuple[str, str]]:
    """Change one local's declared type: signedness decides `jl`/`jb`, width decides `shl`/indexed addressing."""
    out, lines = [], definition.splitlines()
    swaps = {"int": ("unsigned",), "unsigned": ("int",), "char": ("unsigned char",),
             "unsigned char": ("char",), "short": ("unsigned short",), "unsigned short": ("short",),
             "long": ("unsigned long",), "long int": ("unsigned long",)}
    for i, line in enumerate(lines):
        m = DECL.match(line)
        if not m:
            continue
        ty = m.group("type").strip()
        for alt in swaps.get(ty, ()):
            new = list(lines)
            new[i] = line.replace(ty, alt, 1)
            out.append((f"type:{m.group('name')}={alt}", "\n".join(new)))
    return out[:12]


def statement_swaps(definition: str) -> list[tuple[str, str]]:
    """Swap two adjacent single-line statements that mention no common identifier."""
    out, lines = [], definition.splitlines()
    for i in range(len(lines) - 1):
        a, b = lines[i].strip(), lines[i + 1].strip()
        if not (a.endswith(";") and b.endswith(";")) or a.startswith(("return", "if", "for", "while", "}")):
            continue
        ida = set(re.findall(r"[A-Za-z_]\w*", a))
        idb = set(re.findall(r"[A-Za-z_]\w*", b))
        if ida & idb or "(" in a and "=" in a:
            continue
        new = list(lines)
        new[i], new[i + 1] = lines[i + 1], lines[i]
        out.append((f"stmt-order:{a.split('(')[0][:18]}<->{b.split('(')[0][:18]}", "\n".join(new)))
    return out[:8]


GROUPS = (
    ("args", swap_arguments),
    ("rename", rename_locals),
    ("decl-order", reverse_declarations),
    ("condition", condition_forms),
    ("ternary", ternary_swap),
    ("return", return_forms),
    ("init", init_split),
    ("zero-init", zero_init),
    ("volatile", volatile_locals),
    ("type", type_widths),
    ("stmt-order", statement_swaps),
)


def variants(definition: str, only: str = "", cap: int = 160) -> list[tuple[str, str, str]]:
    """Every trick that applies to this definition, in catalogue order, as `judge` candidates."""
    wanted = [k.strip() for k in only.split(",") if k.strip()] or [k for k, _ in GROUPS]
    out: list[tuple[str, str, str]] = []
    seen = {definition}
    for name, fn in GROUPS:
        if name not in wanted:
            continue
        for label, text in fn(definition):
            if text in seen:
                continue
            seen.add(text)
            out.append((label, text, ""))
            if len(out) >= cap:
                return out
    return out


def _body(fn: dict, definition: str | None) -> str:
    text = common.source_path(fn["unit"]).read_text(encoding="utf-8")
    own, start, end = search.common.definition_span(text, fn["name"])
    return definition if definition is not None else (text[start:end] if start >= 0 else "")


def ascend(fn: dict, only: str = "", cap: int = 160, jobs: int = common.JOBS, depth: int = 3,
          submit_match: bool = True, apply_best: bool = False, definition: str | None = None) -> dict:
    """Hill-climb the catalogue: the best single trick, then the catalogue again *on that body*, up to `depth` steps.

    A single pass only ever applies one edit, but real functions often need two that only pay off together (an
    argument swap plus a rename, a decl reorder plus a type change). Each step re-enumerates from the improved body,
    so independent fixes stack; `depth` bounds the work at `depth` catalogue passes per function.
    """
    symbol = fn["name"]
    base = _body(fn, definition)
    if not base:
        return {"symbol": symbol, "variants": 0, "best_pct": None, "error": f"no definition in src/auto/{fn['unit']}.cpp"}
    current, trail, seen, total = base, [], {base}, 0
    best_pct, best_label = None, None
    matched, submitted = False, {}
    for _ in range(max(1, depth)):
        cands = [c for c in variants(current, only=only, cap=cap) if c[1] not in seen]
        if not cands:
            break
        res = search.judge(fn, cands, jobs=jobs, submit_match=submit_match, write_best=False, current=current)
        total += res.get("variants") or 0
        matched, submitted = bool(res.get("matched")), res.get("submitted") or submitted
        best_pct, best_label = res.get("best_pct"), res.get("best_label")
        if matched:
            break
        rows = res.get("rows") or []
        pick = max(range(len(rows)), key=lambda i: rows[i]["pct"] if rows[i]["pct"] is not None else -1)
        floor = float(res.get("base_pct") or 0.0)
        if pick >= len(cands) or rows[pick].get("pct") is None or rows[pick]["pct"] <= floor + 1e-6:
            break
        label, text = cands[pick][0], cands[pick][1]
        if text in seen:
            break
        seen.add(text)
        current = text
        trail.append({"label": label, "pct": rows[pick]["pct"]})
    out = {"symbol": symbol, "unit": fn["unit"], "variants": total, "best_pct": best_pct, "best_label": best_label,
           "steps": trail, "depth": len(trail), "matched": matched, "submitted": submitted, "definition": current}
    if not matched and apply_best and best_pct is not None and best_pct > search.db_best_pct(fn) + 1e-6:
        try:
            helpers, body = search.csource.split_extra(current)
            out["won"] = search.submit(symbol, body, helpers, fn=fn, improve=True)
        except common.OutmatchError as e:
            out["note"] = str(e)[:300]
    return out


def run(fn: dict, only: str = "", cap: int = 160, jobs: int = common.JOBS, submit_match: bool = True,
        apply_best: bool = False, definition: str | None = None) -> dict:
    """Score the catalogue against `fn`'s current definition (or `definition`) and report the ranking."""
    symbol, unit = fn["name"], fn["unit"]
    text = common.source_path(unit).read_text(encoding="utf-8")
    own, start, end = search.common.definition_span(text, symbol)
    base = definition if definition is not None else (text[start:end] if start >= 0 else "")
    if not base:
        return {"symbol": symbol, "error": f"no definition in src/auto/{unit}.cpp"}
    cands = variants(base, only=only, cap=cap)
    return search.judge(fn, cands, jobs=jobs, submit_match=submit_match, apply_best=apply_best,
                        current=base if definition is not None else None)
