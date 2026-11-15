---
name: msvc-matching
description: Make a decompiled function compile to the original's exact bytes under MSVC 6 (and 4.2/5.0). Use when a function sits at 95-99.9%, differs in a handful of instructions, registers, stack slots or frame layout, or when the permuter has stalled. Carries the measured lever catalogues from CMR2Decomp, brickpark-decomp and isle/ReproBit, and drives the outmatch harness (../outmatch beside the project) that tests each lever and submits real matches to decompdecomp.
---

# Matching MSVC output

MSVC 6 is deterministic **for a fixed compiler state**, not for a fixed function body. The same C compiled in
another translation unit, after another declaration, under another flag or pragma, or with differently named locals,
can emit different code. Matching is therefore two jobs:

1. **Explain the diff** — which single instruction, register, stack slot or frame detail is wrong, and what in the
   source or the compiler state produces it.
2. **Change the state and re-measure** — cheaply, one hypothesis at a time.

Never guess-and-commit. Every candidate is compiled and byte-compared before it is allowed near the project.

## Quick start

```bash
# from inside the decompdecomp project; outmatch lives beside it
../outmatch/outmatch.sh search '@func_0043B414@4' --seconds 120   # external permuter, submits a 100
../outmatch/outmatch.sh pragma  '@func_0043B414@4'                # #pragma + declaration-run state carriers
../outmatch/outmatch.sh levers  '@func_0043B414@4'                # mechanical CMR2/brickpark rewrites
../outmatch/outmatch.sh flags   --unit text_0043B124               # per-unit flag sweep (all functions at once)
../outmatch/outmatch.sh flags   --apply '@func_0043B414@4'         # keep the winning flags (splits the unit if needed)
../outmatch/outmatch.sh try     '@func_0043B414@4' cand.c [--submit]   # score a hand-written definition locally
../outmatch/outmatch.sh status
```

`try` prints `pct` (100 = exact). Only `--submit` (or a 100 from `search`) goes to decompdecomp's `/api/try`, where
the app's own judge re-checks it, writes it into the unit, commits on a match and records it live.

## Order of attack

1. **Look at the diff.** `objdiff-cli diff -1 build/<version>/target/<unit>.obj -2 <scratch>.obj '<symbol>'`
   (or the app's diff view). Note the exact symptom: extra `push`, wrong `[ebp-4]` slot, `add` vs `sub`, an extra
   `mov`, a duplicated epilogue, a different branch shape, an extra reload.
2. **Cheap structural levers first** (`levers`): statement order, `if/else` direction, compound vs simple assignment,
   declaration hoisting, `++x` vs `x++`, call-vs-cached-value, global-vs-struct access.
3. **State carriers** (`pragma`): `#pragma optimize` letters, `auto_inline`/`inline_depth`, and declaration runs
   (forward-declaration, extern, typedef). These are legitimate in this project (the judge only rejects inline asm).
4. **Permuter** (`search`): thousands of variants per minute in scratch trees; let it hill-climb from the app's
   `best_source`.
5. **Per-unit flags** (`flags`): the original may have built this unit with another `/O` level or `/Oy-`, `/Ob1`,
   `/GX-`. If a flag set matches one function but breaks its neighbours, `--apply` splits that function into its own
   unit with its own flags.
6. **Rewriting the C by hand** from the disassembly, then `try --submit`. This is where decompilation, not
   permutation, is the work.

## Mental model (what actually changes the bytes)

- **Stack slots are ordered by reference count.** The most-referenced local sits nearest `ebp`; declaration order is
  ignored. Ties are broken by **variable name** — renaming locals has fixed real functions. Disjoint lifetimes may
  share one slot; two locals where the original reused one is a visible symptom.
- **A translation unit is shared compiler state.** Editing one function can shift register allocation in later
  functions; an earlier floating-point function can change later integer code; a struct declared in the first
  include, a 16th member, an unused inline function in a header — all have moved code in other TUs. If a function
  matches alone but not in its unit (or the reverse), the fix may be placement, not the body.
- **Declarations are state.** A run of unused forward declarations, externs or typedefs before a function can change
  codegen with no semantic effect. ReproBit calls this a *state carrier* and treats it as a first-class, low-cost
  intervention; `pragma` sweeps exactly these.
- **Inlining is a decision about the call site.** `auto_inline(off)`, `inline_depth`, `/Ob1`, and whether a helper is
  a macro, a real function or an inline function all change whether a body appears inline.
- **The original TU was a real file.** MSVC6's object count (Rich header) tells you how many TUs there were; moving a
  function into its own unit is a legitimate lever, not cheating (decompdecomp models units explicitly).
- **Some diffs are unreachable.** Register-allocation/scheduling noise that no source form produces should be parked,
  not forced. CMR2 keeps such functions at 100% fuzzy and moves on.

## Symptom -> lever

Diagnostic reading on the left; the first thing to try on the right. Full evidence and caveats are in
`references/` (see below).

### Frame and stack
| Symptom | Lever |
|---|---|
| Local is at the wrong `[ebp-N]` | Count references; the more-referenced local must be nearer `ebp`. Reorder by use count, then try renaming ties. |
| One local where the original had two (or vice versa) | Reuse a block-scoped temporary, or split one variable into two with disjoint lifetimes. |
| Frame too large/small by a few bytes | Array/struct size (`char x[6]` vs `[8]`), a temporary struct, or `{0}` initialisation instead of `memset`. |
| Original keeps the frame pointer (`push ebp; mov ebp,esp`) and you do not | `#pragma optimize("y", off)` around the function (retail builds often had it). |
| Extra/missing callee-saved `push ebx/esi/edi` | Prologue vs branch placement: a value live across a branch is saved in the prologue; one used only inside a branch is pushed there. `volatile` often fixes duplicated tails. |

### Instruction choice
| Symptom | Lever |
|---|---|
| `add reg,-k` where you emit `sub reg,k` | The subtraction is part of a larger expression, or the constant came from a different form (`x -= k; f(x)` vs `f(x - k)`). |
| `lea` vs `add`/`imul` for an address | Index width: a `BYTE`/`char` index emits `shl r,2` once; an `int` index emits `[r*4+table]` at each use. |
| `setcc`/`dec`/`and` sequence | Write `c ? 0xff : 0` (or `c ? 1 : 0`) rather than a branch. |
| `cdq; xor; sub` | `abs()` written out; match the exact expression form. |
| Extra `mov`/reload of a global | Aliasin: a store through a pointer forces reloads of unrelated globals; stores to a named global do not. Write the global directly, or keep the value in a local. |
| Load above a loop guard | `for (i = 0; i < n; i++) p[i*k] = ...` — the guard is checked before the first load. |
| A value recomputed that the original cached | The original held it in a local across the call; hoist it. Conversely, if the original calls twice, do not cache. |
| `dec; je; dec; jne` | A `switch` whose cases the compiler emitted in reverse source order; reorder cases. |
| Identical branch tails duplicated | `return` inside each branch (duplicated epilogue) vs one shared `return`; or a `volatile` local to stop tail merging. |

### Control flow and calls
| Symptom | Lever |
|---|---|
| `if (a) X else Y` opposite shape | Swap the branches and negate the condition (the compiler often prefers fall-through on the else). |
| Ternary lowered to a branch (or vice versa) | Statement vs expression context: assignment from `?:` becomes `setcc`/branch depending on use. |
| Argument computed differently at each call | Separate calls with constant arguments, not one call with a ternary argument. |
| Narrow argument pushed as a full dword | The callee parameter is `BYTE`/`char`, not `int`. |
| Loop uses `jl` where the original uses `jb` | Pointer/int loop index: use the original's type. |
| Two tests of the same condition | Write it twice on a value the compiler cannot prove stable; caching it changes the code. |
| `do/while` with a biased pointer | A plain `for` over an indexed array, or the reverse. |
| A tail jump past alignment padding | The original had two separate functions; split the unit. |

### Data and initialisation
| Symptom | Lever |
|---|---|
| Globals scattered in `.data` | Uninitialised C++ globals are communal; initialise (`= {0}`) to keep definition order. |
| `memset` vs stores | `T s = {0};` vs `memset(&s,0,sizeof s)`. |
| Copy order of a string table differs | Declaration order of the initialised array drives copy order. |
| One struct blob vs separate globals | MSVC must assume a store through one view changes the other; the original's layout decides which you can write. |

## Rules of the game

- **The judge is the app.** A local 100 is a hypothesis; `/api/try` is the verdict (it also applies the literal and
  relocation-mapping checks). Submit only 100s; a failed kept attempt holds a claim and dirties the unit.
- **No inline asm, no asm includes.** The app refuses them, and they would not be decompilation anyway.
- **Pragmas and declaration carriers are allowed.** ReproBit records them as real conditions of the retail build;
  they are not byte-faking.
- **One hypothesis per compile.** Change one thing, re-measure, keep the result. The harness caches by source hash,
  so repeats are free.
- **Beware unit-wide effects.** A change that fixes your function may regress a matched neighbour; the app checks
  protected neighbours, and `flags --apply` refuses a set that breaks any. `ninja` in the live tree while the app is
  building is a race: the harness compiles in scratch copies for that reason.
- **Park unreachable diffs.** If the only difference is register allocation or scheduling, no source form may exist.
  Leave it, note it, move on.
- **Check whether it is even the game's code.** MSVC 6 links the CRT statically; `outmatch.sh libmatch` identifies
  those functions byte for byte against `LIBCMT.LIB`/`LIBCPMT.LIB` and records them in `set_aside.json`. Do not write C
  for a library function: it leaves the progress denominator and the rebuild already uses the original bytes. The
  project's CRT tail starts at `0x44E690` (`_onexit`); the allocator cluster at `0x401000` is the same library but a
  different build and is not yet proven.

## References (measured catalogues, verbatim upstream copies)

- `references/cmr2-claude.md` — CMR2Decomp's MSVC6 SP3 notes: stack slots, cross-TU contamination, ~60 idioms,
  flag sets, tooling (`match.py`, `permute_batch.py`, `slots.py`, `split_tu.py`).
- `references/brickpark-levers.md` — LEGOLAND/VC6 SP3 `docs/LEVERS.md`, ~194 symptom-first rules with evidence and
  measured floors, indexed by symptom. The single best lookup table for "why is this instruction different".
- `references/isle-contributing.md` — isle's position on compiler entropy and its Beta-DLL oracle.
- `references/reprobit-concepts.md`, `reprobit-interventions.md`, `reprobit-discovery.md`, `reprobit-costs.md`,
  `reprobit-platforms.md` — ReproBit's state-carrier model, intervention costs, and the declaration-shape/grind
  search. Use it to justify and design pragma/declaration sweeps.
- `references/fetch-upstream.sh` — re-download the originals (they are copies, pinned by date; upstream may move).

Attribution: CMR2Decomp (pablocpas), brickpark-decomp (adamXbot), isle and ReproBit (isledecomp). Each file keeps
its upstream licence/notice in the repository it came from; these are reference copies for this project.
