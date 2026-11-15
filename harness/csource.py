"""Find and replace one function definition in a source file (standalone, ~the app's csource.find).

The app's own parser lives in decompnet/forks/csource.py; this is a small independent twin so the harness works on any
tree without importing AGPL code.  It handles what these units contain: free functions, out-of-class C++ methods,
K&R-ish MSVC declarations, comments and strings.
"""

from __future__ import annotations

import re

_MASK = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.S)
_RETURNISH = re.compile(r"^[\w\s\*&:<>,~]+$")
_NOT_RETURN = re.compile(r"[;{}()]")


def _blank(text: str) -> str:
    """`text` with comments and literals replaced by spaces, positions preserved."""
    return _MASK.sub(lambda m: " " * len(m.group(0)), text)


def _skip_parens(masked: str, at: int) -> int:
    """Index just past the ')' balancing the '(' at `at`, or -1."""
    depth = 0
    for i in range(at, len(masked)):
        c = masked[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _names(symbol: str, hint: str | None) -> list[str]:
    out = []
    if hint:
        out.append(hint)
    raw = symbol
    if raw.startswith("?"):  # mangled: take the inner name if obvious (MSVC C++ these units don't have yet)
        m = re.match(r"\?+([A-Za-z_]\w*)@", raw)
        if m:
            out.append(m.group(1))
    out.append(raw)
    # @func_0043B414@4 / _func_00419847 / func_0043B414 -> func_0043B414
    base = re.sub(r"^_?@?", "", raw)
    base = re.sub(r"@\d+$", "", base)
    out += [base, base.split("::")[-1]]
    return list(dict.fromkeys(n for n in out if n))


def find(text: str, hints: list[str] | None, symbol: str | None = None) -> tuple[str, int, int]:
    """(the definition's own name, start, end) for the first definition matching any hint/symbol spelling."""
    masked = _blank(text)
    for name in _names(symbol or "", (hints or [None])[0]):
        pat = re.compile(rf"(?<![\w:~])((?:[A-Za-z_]\w*\s*::\s*)*~?{re.escape(name)})\s*\(")
        for m in pat.finditer(masked):
            open_at = m.end() - 1
            close = _skip_parens(masked, open_at)
            if close < 0:
                continue
            # skip qualifiers and attributes to the body's '{'
            i = close
            while i < len(masked):
                c = masked[i]
                if c in " \t\r\n":
                    i += 1
                    continue
                if c == "{":
                    break
                if c == ";":
                    i = -1
                    break
                if masked.startswith("const", i) or masked.startswith("volatile", i) or masked.startswith("throw", i):
                    i += 6
                    continue
                if masked.startswith("noexcept", i) or masked.startswith("__declspec", i):
                    j = masked.find("(", i)
                    if j < 0:
                        i = -1
                        break
                    i = _skip_parens(masked, j)
                    continue
                if c == ":":
                    # a constructor's initialiser list: the body is the next '{' outside parentheses
                    k, depth = i, 0
                    while k < len(masked):
                        if masked[k] == "(":
                            depth += 1
                        elif masked[k] == ")":
                            depth -= 1
                        elif masked[k] == "{" and depth == 0:
                            i = k
                            break
                        elif masked[k] == ";" and depth == 0:
                            i = -1
                            break
                        k += 1
                    else:
                        i = -1
                    if i < 0:
                        break
                    continue
                # anything else here (a ')', an operator, a call argument...) is not a definition:
                # `if (x.func_00436F2F(&a)) {` is a call site, not the function
                i = -1
                break
            if i == -1:
                continue
            if i < 0 or i >= len(masked):
                continue
            start = m.start()
            # pull in the return type: preceding lines that are all type-ish
            line_start = start
            while True:
                prev_end = line_start - 1
                if prev_end <= 0:
                    break
                prev_start = masked.rfind("\n", 0, prev_end) + 1
                line = text[prev_start:prev_end]
                if not line.strip():
                    break
                if not _RETURNISH.match(line.strip()) or _NOT_RETURN.search(line):
                    break
                if line.lstrip().startswith("#"):
                    break
                start = prev_start
                line_start = prev_start
            depth = 0
            end = -1
            for j in range(i, len(masked)):
                c = masked[j]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        end = j + 1
                        break
            if end < 0:
                continue
            if end < len(text) and text[end] == "\n":
                end += 1
            return m.group(1).replace(" ", ""), start, end
    return "", -1, -1


def replace(text: str, definition: str, hints: list[str] | None = None, symbol: str | None = None) -> str:
    """`text` with the function `definition` names replaced by `definition` (same spelling)."""
    own, start, end = find(text, hints, symbol)
    if start < 0:
        raise KeyError(f"no definition of {hints or symbol} in the file")
    return text[:start] + definition.rstrip("\n") + "\n" + text[end:]


def extract(text: str, hints: list[str] | None = None, symbol: str | None = None) -> str:
    own, start, end = find(text, hints, symbol)
    if start < 0:
        raise KeyError(f"no definition of {hints or symbol} in the file")
    return text[start:end]


def split_extra(variant: str) -> tuple[str, str]:
    """(declarations/helpers beside the variant, the function itself) for a permuter variant.

    decomp-permuter sometimes writes a static helper above the function; the app refuses those through /api/try, so
    the harness puts them into the unit file and submits only the definition.
    """
    masked = _blank(variant)
    first = masked.find("{")
    if first < 0:
        return "", variant
    depth, end = 0, -1
    for i in range(first, len(masked)):
        if masked[i] == "{":
            depth += 1
        elif masked[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    tail = masked[end:]
    if "}" in tail and "{" in tail:  # more than one top-level body: helpers above
        head = variant[:end]
        rest = variant[end:]
        # the function is the last top-level body: find its start by the last ';' or '}' before the final body
        start = max(rest.rfind("}"), rest.rfind(";"))
        return variant[:end + start + 1], variant[end + start + 1:]
    return "", variant
