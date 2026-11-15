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


def bool_values(definition: str) -> list[tuple[str, str]]:
    """`c ? 1 : 0` <-> `c ? 0xff : 0` <-> `c ? -1 : 0`: the setcc/dec/and shapes CMR2 lists."""
    out = []
    for m in re.finditer(r"([^?;\n]+?)\?\s*(-?(?:0x[0-9a-fA-F]+|\d+))\s*:\s*(-?(?:0x[0-9a-fA-F]+|\d+))",
                         definition):
        cond, a, b = (s.strip() for s in m.groups())
        for alt_a, alt_b in (("0xff", "0"), ("1", "0"), ("-1", "0"), ("0", "0xff"), ("0", "1")):
            if (a, b) == (alt_a, alt_b):
                continue
            out.append((f"bool-val:{cond[:18]}?{alt_a}:{alt_b}",
                        definition[:m.start()] + f"{cond} ? {alt_a} : {alt_b}" + definition[m.end():]))
    return out[:8]


def negate_branches(definition: str) -> list[tuple[str, str]]:
    """`if (c) {A} else {B}` -> `if (!(c)) {B} else {A}` (fall-through is chosen by branch shape)."""
    out = []
    for m in re.finditer(r"\bif\s*\(", definition):
        p_open = definition.find("(", m.start())
        p_close = _match(definition, p_open, "(", ")")
        if p_close < 0:
            continue
        cond = definition[p_open + 1:p_close - 1].strip()
        brace = definition.find("{", p_close)
        if brace < 0 or definition[p_close:brace].strip():
            continue
        end = _match(definition, brace, "{", "}")
        if end < 0:
            continue
        body_a = definition[brace + 1:end - 1]
        rest = definition[end - 1:]
        em = re.match(r"\s*else\s*\{", rest)
        if not em:
            continue
        brace_b = end - 1 + em.end() - 1
        end_b = _match(definition, brace_b, "{", "}")
        if end_b < 0:
            continue
        body_b = definition[brace_b + 1:end_b - 1]
        new = (definition[:p_open + 1] + f"!({cond})" + definition[p_close - 1:brace + 1]
               + body_b + "}" + definition[end - 1:brace_b] + body_a + definition[end_b - 1:])
        out.append((f"negate-branch:{cond[:18]}", new))
    return out[:6]


def index_forms(definition: str) -> list[tuple[str, str]]:
    """`a[i]` -> `a[(unsigned char)i]` (one `shl r,2`), and `a[i]` <-> `*(a + i)`."""
    out = []
    for m in re.finditer(r"\b([A-Za-z_]\w*(?:\s*->\s*\w+)?)\s*\[\s*([A-Za-z_]\w*)\s*\]", definition):
        base, idx = m.group(1), m.group(2)
        for cast in ("unsigned char", "unsigned short", "int"):
            out.append((f"index-cast:{base}[{idx}]={cast}",
                        definition[:m.start()] + f"{base}[({cast}){idx}]" + definition[m.end():]))
        out.append((f"index-ptr:{base}[{idx}]",
                    definition[:m.start()] + f"*({base} + {idx})" + definition[m.end():]))
    for m in re.finditer(r"\*\s*\(\s*([A-Za-z_]\w*(?:\s*->\s*\w+)?)\s*\+\s*([A-Za-z_]\w*)\s*\)", definition):
        base, idx = m.group(1), m.group(2)
        out.append((f"ptr->index:{base}+{idx}",
                    definition[:m.start()] + f"{base}[{idx}]" + definition[m.end():]))
    return out[:10]


def increment_forms(definition: str) -> list[tuple[str, str]]:
    """`i++` <-> `++i` in a `for` header (post/pre increment changes the loop's tail shape)."""
    out = []
    for m in re.finditer(r"\bfor\s*\(", definition):
        p_open = definition.find("(", m.start())
        p_close = _match(definition, p_open, "(", ")")
        if p_close < 0:
            continue
        inside = definition[p_open + 1:p_close - 1]
        parts = inside.split(";")
        if len(parts) != 3:
            continue
        third = parts[2]
        for im in re.finditer(r"(?<![+])(\+\+|--)\s*([A-Za-z_]\w*)|([A-Za-z_]\w*)\s*(\+\+|--)", third):
            pre, a, b, post = im.groups()
            if pre:
                alt = f"{a}{b}"
            else:
                alt = f"{post}{b}"
            new_third = third[:im.start()] + alt + third[im.end():]
            out.append((f"inc-form:{alt}",
                        definition[:p_open + 1] + ";".join(parts[:2] + [new_third]) + definition[p_close - 1:]))
    return out[:6]


def compare_constants(definition: str) -> list[tuple[str, str]]:
    """`x < k` <-> `x <= k-1` (an off-by-one compare shape the original may have used)."""
    out = []
    for m in re.finditer(r"([A-Za-z_][\w\.\[\]\->]*)\s*(<=|>=|<|>)\s*(0x[0-9a-fA-F]+|\d+)\b", definition):
        var, op, lit = m.groups()
        base = int(lit, 0)
        alts = {"<": ("<=", base - 1), "<=": ("<", base + 1),
                ">": (">=", base + 1), ">=": (">", base - 1)}
        alt_op, alt_val = alts[op]
        out.append((f"cmp-const:{var}{op}{lit}",
                    definition[:m.start()] + f"{var} {alt_op} {alt_val}" + definition[m.end():]))
    return out[:10]


def compound_assign(definition: str) -> list[tuple[str, str]]:
    """`a = a + b` <-> `a += b` (and the `-` forms)."""
    out = []
    for m in re.finditer(r"(?m)^([ \t]*)([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\s*([\+\-])\s*([^;]+);", definition):
        indent, lhs, ref, op, rhs = m.groups()
        if lhs != ref:
            continue
        out.append((f"compound:{lhs}{op}=",
                    definition[:m.start()] + f"{indent}{lhs} {op}= {rhs};" + definition[m.end():]))
    for m in re.finditer(r"(?m)^([ \t]*)([A-Za-z_]\w*)\s*([\+\-])=\s*([^;]+);", definition):
        indent, lhs, op, rhs = m.groups()
        out.append((f"expand:{lhs}{op}=",
                    definition[:m.start()] + f"{indent}{lhs} = {lhs} {op} {rhs};" + definition[m.end():]))
    return out


def split_call_args(definition: str) -> list[tuple[str, str]]:
    """`f(x - k);` -> `x -= k; f(x);` - the `x -= k; f(x)` ordering CMR2 records."""
    out = []
    for name, p_open, p_close, args in calls(definition):
        if not args:
            continue
        m = re.fullmatch(r"([A-Za-z_]\w*)\s*-\s*(0x[0-9a-fA-F]+|\d+)", args[0].strip())
        if not m:
            continue
        var, lit = m.groups()
        start = definition.rfind("\n", 0, p_open) + 1
        end = definition.find("\n", p_close)
        end = len(definition) if end < 0 else end
        line = definition[start:end]
        if not re.fullmatch(r"[ \t]*(?:[A-Za-z_]\w*\s*=\s*)?[A-Za-z_]\w*\s*\(.*\)\s*;\s*", line):
            continue
        indent = line[:len(line) - len(line.lstrip())]
        new_args = list(args)
        new_args[0] = var
        call_text = f"{name}(" + ", ".join(new_args) + ");"
        prefix = line.strip()
        if "=" in prefix.split("(")[0]:
            target, _, _ = prefix.partition("=")
            call_text = f"{target.strip()} = {call_text}"
        out.append((f"split-call:{name}", definition[:start]
                    + f"{indent}{var} -= {lit}; {call_text}" + definition[end:]))
    return out[:6]


def switch_order(definition: str) -> list[tuple[str, str]]:
    """Reverse the `case` blocks of a simple switch (MSVC may lay cases out in reverse source order)."""
    out = []
    m = re.search(r"\bswitch\s*\(", definition)
    if not m:
        return out
    p_open = definition.find("(", m.start())
    p_close = _match(definition, p_open, "(", ")")
    if p_close < 0:
        return out
    brace = definition.find("{", p_close)
    end = _match(definition, brace, "{", "}")
    if brace < 0 or end < 0:
        return out
    body = definition[brace + 1:end - 1]
    blocks = re.findall(r"([ \t]*case\s+[^:]+:\s*(?:[^{}]*?break\s*;\s*))", body)
    if len(blocks) < 2:
        return out
    rest = body
    for b in blocks:
        rest = rest.replace(b, "", 1)
    out.append(("switch-order:reverse", definition[:brace + 1] + rest + "".join(reversed(blocks))
                + definition[end - 1:]))
    return out[:2]


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
    ("split-call", split_call_args),
    ("bool-val", bool_values),
    ("negate-branch", negate_branches),
    ("index", index_forms),
    ("inc-form", increment_forms),
    ("cmp-const", compare_constants),
    ("compound", compound_assign),
    ("switch-order", switch_order),
)


# ------------------------------------------------------------------ diff-directed choice

# Which family can explain which objdiff symptom. Weights, not filters: a family that cannot explain the observed
# diff still runs (the cause may be one edit away), it just runs after the ones that can. This is the difference
# between "try 160 rewrites" and "try the 40 that fit the shape of the mismatch".
KIND_WEIGHTS = {
    "DIFF_ARG_MISMATCH": {"rename": 3, "args": 2, "type": 2, "decl-order": 1, "stmt-order": 1, "index": 1},
    "DIFF_OP_MISMATCH": {"condition": 3, "type": 2, "ternary": 1, "cmp-const": 2},
    "DIFF_INSERT": {"condition": 2, "return": 2, "init": 2, "stmt-order": 1, "ternary": 1, "compound": 1,
                    "volatile": 1},
    "DIFF_DELETE": {"condition": 2, "return": 2, "init": 2, "stmt-order": 1, "volatile": 1, "compound": 1},
    "DIFF_REPLACE": {"condition": 2, "type": 2, "init": 1, "zero-init": 1, "return": 1, "compound": 1,
                     "bool-val": 1, "index": 1, "inc-form": 1, "split-call": 1},
    "DIFF_REGISTER": {"rename": 2, "decl-order": 1, "stmt-order": 1},
}


def diff_kinds(fn: dict, definition: str | None = None) -> dict[str, int]:
    """How the current body differs from the target, as counts of objdiff's `diff_kind`s."""
    unit = fn["unit"]
    try:
        root = common.make_scratch(unit, 100, fresh=True)
        text = common.source_path(unit).read_text(encoding="utf-8")
        if definition is not None:
            text = search.source_with(text, fn["name"], definition)
        obj = common.scratch_compile(root, unit, text, f"build/{common.VERSION}/src/auto/{unit}.obj")
        data = common._diff(common.target_obj(unit), obj, fn["name"])
    except (common.OutmatchError, KeyError, OSError):
        return {}
    kinds: dict[str, int] = {}
    for sym in (data.get("left") or {}).get("symbols", []):
        if sym.get("name") != fn["name"]:
            continue
        for row in sym.get("instructions") or []:
            kind = row.get("diff_kind")
            if kind:
                kinds[kind] = kinds.get(kind, 0) + 1
    return kinds


def order_groups(kinds: dict[str, int]) -> list[str]:
    """The catalogue ordered by how well each family explains the observed diff."""
    names = [k for k, _ in GROUPS]
    score = dict.fromkeys(names, 0)
    for kind, count in kinds.items():
        for family, weight in KIND_WEIGHTS.get(kind, {}).items():
            if family in score:
                score[family] += weight * min(count, 6)
    if not any(score.values()):
        return names
    return sorted(names, key=lambda n: -score[n])


def variants(definition: str, only: str = "", cap: int = 160,
             order: list[str] | None = None) -> list[tuple[str, str, str]]:
    """Every trick that applies to this definition, in catalogue order (or `order`), as `judge` candidates."""
    wanted = [k.strip() for k in only.split(",") if k.strip()] or list(order or [k for k, _ in GROUPS])
    out: list[tuple[str, str, str]] = []
    seen = {definition}
    by_name = dict(GROUPS)
    for name in wanted:
        fn = by_name.get(name)
        if fn is None:
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


def ascend(fn: dict, only: str = "", cap: int = 160, jobs: int = common.JOBS, depth: int = 3, beam: int = 1,
          submit_match: bool = True, apply_best: bool = False, definition: str | None = None,
          focus: bool = True) -> dict:
    """Chain the catalogue: from each surviving body, one pass per step, up to `depth` steps.

    `beam` is how many bodies survive a generation - 1 is the greedy hill-climb, more keeps the equally-good ones so
    two edits that only pay off together have a chance to meet. A *tie* is allowed to continue the chain: it costs a
    pass and is often where the next lever starts (a rename that changes nothing yet makes the swap legal).
    """
    symbol = fn["name"]
    base = _body(fn, definition)
    if not base:
        return {"symbol": symbol, "variants": 0, "best_pct": None, "error": f"no definition in src/auto/{fn['unit']}.cpp"}
    order = order_groups(diff_kinds(fn, base)) if focus else None
    frontier, seen, trail, total = [base], {base}, [], 0
    best_pct, best_label, best_body = None, None, base
    matched, submitted = False, {}
    for _ in range(max(1, depth)):
        cands = []
        for body in frontier:
            for label, text, _ in variants(body, only=only, cap=cap, order=order):
                if text in seen:
                    continue
                seen.add(text)
                cands.append((label, text, ""))
        if not cands:
            break
        res = search.judge(fn, cands, jobs=jobs, submit_match=submit_match, write_best=False)
        total += res.get("variants") or 0
        matched = matched or bool(res.get("matched"))
        submitted = res.get("submitted") or submitted
        if matched:
            best_body = cands[0][1] if cands else base
            break
        scored = []
        for i, row in enumerate(res.get("rows") or []):
            if i < len(cands) and row.get("pct") is not None:
                scored.append((float(row["pct"]), cands[i][1], cands[i][0]))
        if not scored:
            break
        scored.sort(key=lambda t: -t[0])
        if best_pct is None or scored[0][0] > best_pct + 1e-6:
            best_pct, best_body, best_label = scored[0]
        trail.append({"label": scored[0][2], "pct": scored[0][0]})
        # survive: the best `beam` bodies that are at least as good as the best seen so far (a tie may continue)
        top = [s for s in scored[:max(beam, 1)] if s[0] >= best_pct - 1e-6]
        if not top:
            break
        frontier = [s[1] for s in top]
    out = {"symbol": symbol, "unit": fn["unit"], "variants": total, "best_pct": best_pct, "best_label": best_label,
           "steps": trail, "depth": len(trail), "beam": beam, "matched": matched, "submitted": submitted,
           "definition": best_body}
    if not matched and apply_best and best_pct is not None and best_pct > search.db_best_pct(fn) + 1e-6:
        try:
            helpers, body = search.csource.split_extra(best_body)
            out["won"] = search.submit(symbol, body, helpers, fn=fn, improve=True)
        except common.OutmatchError as e:
            out["note"] = str(e)[:300]
    return out


def run(fn: dict, only: str = "", cap: int = 160, jobs: int = common.JOBS, submit_match: bool = True,
        apply_best: bool = False, definition: str | None = None, focus: bool = True) -> dict:
    """Score the catalogue against `fn`'s current definition (or `definition`) and report the ranking."""
    symbol, unit = fn["name"], fn["unit"]
    text = common.source_path(unit).read_text(encoding="utf-8")
    own, start, end = search.common.definition_span(text, symbol)
    base = definition if definition is not None else (text[start:end] if start >= 0 else "")
    if not base:
        return {"symbol": symbol, "error": f"no definition in src/auto/{unit}.cpp"}
    order = order_groups(diff_kinds(fn, base)) if focus else None
    cands = variants(base, only=only, cap=cap, order=order)
    out = search.judge(fn, cands, jobs=jobs, submit_match=submit_match, apply_best=apply_best,
                       current=base if definition is not None else None)
    if order:
        out["focus"] = order[:5]
    return out
