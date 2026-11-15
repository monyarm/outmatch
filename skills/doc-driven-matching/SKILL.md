---
name: doc-driven-matching
description: Turn a document, article, URL or another decompilation project into new matching leverage. Use when given a source of truth to mine (compiler documentation, a blog post, another decomp's notes or tooling), or when the existing lever catalogues have stalled and new hypotheses are needed. Extracts codegen-relevant claims, converts each into a one-line experiment, tests it with ../outmatch, and keeps only what the compiler proves.
---

# Matching from a source of truth

A "source of truth" is anything that claims something about how the original binary was produced: compiler
documentation, a blog post about MSVC internals, another decompilation project's notes, an old build script, a
debug symbol dump. The job is not to read it and nod. The job is to convert its claims into **falsifiable
experiments** against this binary, and to keep the ones that produce bytes.

## Workflow

1. **Capture with provenance.** Save the raw source once, outside the conversation, so every later claim can be
   re-read without re-fetching:

   ```bash
   mkdir -p ../outmatch/docs/sources
   curl -sSL '<url>' -o ../outmatch/docs/sources/<slug>.md
   ```

   Put the URL, retrieval date, upstream revision if known, and what it is at the top of the file (or in a sibling
   `.prov` file). Never paraphrase from memory later; quote the file.

2. **Extract a claims table.** Read for statements that could change codegen, and write them down in this shape:

   | # | Claim (quote or close paraphrase) | Mechanism | Test |
   |---|---|---|---|
   | 1 | "stack slots are ordered by reference count" | allocation order | rename/reorder locals, compare slot offsets |
   | 2 | "`/Oy-` keeps a frame pointer" | flag | `outmatch.sh flags --unit <unit>` |
   | 3 | "unused inline members change later codegen" | TU state | declaration-run carrier before the function |

   Only claims with a plausible mechanism on **this** compiler (MSVC 6.0 x86, `/O2 /W3 /GX` plus per-file flags)
   go in the table. Note explicitly what you are discarding and why (MIPS-only, linker-only, MSVC 7+ only, C# ...).

3. **Triage to experiment types.** Every claim becomes one of:
   - a **source rewrite** (an idiom) -> write the candidate by hand, score with `outmatch.sh try`;
   - a **state carrier** (pragma, declaration run, include shape) -> `outmatch.sh pragma`, or add a variant;
   - a **flag/pragma set** -> `outmatch.sh flags`, then `--apply`;
   - a **unit/placement change** (split, move a declaration, reorder definitions) -> `flags --apply` split path or
     a manual `functions.json`/`src` move followed by `POST /api/fork/check`;
   - a **measurement** (what the original actually did: object count, Rich header, section order).

4. **Run the experiment on one function, one change at a time.** Use a function where the symptom matches the
   claim, not a random one. `try` compiles the whole real unit with the candidate spliced in, so the measurement is
   the same one the app will make. Record: candidate, pct, and the exact instruction window that changed.

5. **Write the result back.** In the source note, replace the claim's "Test" cell with the measured outcome
   (function, before -> after, kept/rejected). A claim that fails on one function may still hold on another — say
   which. Confirmed, broadly useful findings belong in
   `.claude/skills/msvc-matching/references/findings.md` (create it if absent), with the evidence, not just the rule.

6. **Promote to automation only when mechanical.** If a confirmed lever can be expressed as a text transform, add it
   to `../outmatch/harness/levers.py` (`TRANSFORMS`), so every future function gets it for free. If it is a
   carrier shape, add it to `harness/pragma.py`. Keep the prose in the skill for the judgement calls.

## What actually pays

- **Primary compiler behaviour**: frame pointer defaults, `/O` interactions, `#pragma optimize` letters, inlining
  thresholds, COMDAT/`/Gy` effects, x87 vs integer register allocation, string/literal pooling.
- **Measured notes from decomps of the same compiler**: CMR2 (MSVC6 SP3), brickpark (VC6 SP3), isle/ReproBit
  (MSVC 4.2/5.0, but the state-carrier method transfers). These are field reports with evidence.
- **The original build's artefacts**: Rich header (tool versions, object count), section sizes/order, import list,
  strings, debug paths, PDB names. Each one constrains compiler state.
- **The disassembly itself** as the ultimate source of truth: derive the C from the instruction schedule, not from
  what the function "should" look like.

## What rarely pays

- Claims about other architectures or other compiler families (decomp-permuter's IDO/GCC idioms, m2c output for
  MIPS/PPC). They can inspire a hypothesis, but MSVC6 x86 will not reproduce them.
- Linker-level tricks (ICF, ordering, section padding) when the judge compares one function's object code.
- "Rewrite it more cleanly" — the goal is the original's codegen, including its awkwardness.
- Byte surgery / patching objects. The app only accepts C that compiles to the bytes; anything else is refused or
  meaningless for the decompilation.

## Sources worth mining (start here)

- `https://github.com/pablocpas/CMR2Decomp` — MSVC6 SP3; `CLAUDE.md`, `scripts/` (match, permute_batch, slots,
  split_tu, rename_search).
- `https://github.com/adamXbot/brickpark-decomp` — VC6 SP3; `docs/LEVERS.md` (symptom-indexed), `tools/match.py`.
- `https://github.com/isledecomp/isle` + `https://github.com/isledecomp/reprobit` — compiler entropy, state
  carriers, `docs/interventions.md`, `docs/discovery.md`, intervention cost model.
- `https://github.com/gibbo101/ra2-decomp`, `https://github.com/SDW-Community/sdw-decomp`,
  `https://github.com/mech4decomp/Mech4MercDecomp` — more VC6-era matching projects.
- MSVC 6.0 documentation (`cl /?`, `/O` reference, `#pragma optimize`, `/Ob`, `/Oy`, `/GX`) — flag semantics.
- `https://github.com/encounter/objdiff` — how the measurement works (and what "100%" ignores).
- `https://github.com/n-delic/decompdecomp` — what the judge accepts and how units/matches are recorded.

Copies of the main catalogues live in `../msvc-matching/references/`; refresh them with its
`fetch-upstream.sh`. Record any new source you mine in `references/sources.md` with one line on what it was good for.

## Rules

- **One claim, one experiment, one measurement.** Do not bundle three rewrites and call the result evidence.
- **Quote the source; do not trust your summary of it.** Especially for "the compiler does X" claims.
- **A negative result is a result.** Write it down: it stops the next session from repeating it.
- **Do not let a document's authority override the object code.** The binary is the source of truth; documents are
  hypotheses.
- **Keep the project buildable.** Experiments go through `try`/scratch trees; only a 100 or an explicitly accepted
  improvement touches `src/` (and the app records it).
