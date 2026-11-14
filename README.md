# outmatch — match functions outside decompdecomp, record them inside it

`decompdecomp`'s own permuter is capped: it works only on functions scoring within 6%, 64 variants a turn, 60 seconds a
turn, four turns (`permuted < 4`). Every one of this project's 176 near misses has used all four turns, so the app
will never look at them again. `outmatch` runs the same permuter generator (and more) outside the app, as long as you
like, in parallel scratch trees, and only tells the app about candidates that are exact matches.

Nothing is written into the project tree while searching. A match is submitted through the app's own
`POST /api/try`, which compiles it, diffs it, writes the definition into `src/auto/`, commits it on the fork's branch,
and updates the database — the same path the GUI's **Try** uses. No restart, no SQL.

```
tools/outmatch/outmatch.sh status
tools/outmatch/outmatch.sh search '@func_00411E5A@12' --seconds 300
tools/outmatch/outmatch.sh run --max-ratio 0.005 --limit 26 --seconds 120
```

## Requirements

- the decompdecomp app running with this project open (its `~/Decomp/.decomp/server.json` is read for the port and
  secret; submissions are credited to **you**),
- a configured build (`build.ninja`, target objects in `build/casefile1/target/`),
- `objdiff-cli` (found on the PATH or in `~/.local/share/decompnet/toolchains/objdiff-cli/`),
- the app's own Python for the permuter generator (taken from the ninja compile command, so it is always the right
  one).

Everything else is the Python standard library. Scratch trees live in `/tmp/opencode/outmatch` (override with
`OUTMATCH_ROOT`... see `harness/common.py`), and state/logs in `.outmatch/` (git-ignored).

## Commands

| command | what it does |
| --- | --- |
| `search NAME [--seconds 120] [--jobs 4] [--count 64] [--seed N] [--no-submit]` | hill-climb decomp-permuter variants for one function; submits the first exact match |
| `run [--max-ratio 0.06] [--limit 20] [--seconds 30] [--any] [--stop-on-match] [--no-submit]` | the same over the near-miss queue, worst-first; skips claimed functions. `--any` includes functions the app has not permuted 4 times |
| `loop [--max-ratio 0.06] [--min-pct 0] [--order fresh\|closest] [--limit 0] [--seconds 900] [--phases levers,pragma,permuter] [--unit U] [--again] [--forever] [--dry-run]` | works the queue with **no function named**: for each candidate it runs the phases in order, keeps the ones that help, submits a 100, and records what it tried in `.outmatch/ledger.jsonl`. `--min-pct 99.5` restricts the queue to the closest drafts (`--order closest` sorts by percentage instead of least-visited). Visits rotate, so a second run continues where the first stopped; a phase already run on unchanged source is skipped unless `--again`. `--dry-run` prints the queue; `--forever` waits for new candidates |
| `report [SYMBOL]` | the loop's ledger: functions, visits, matches and per-phase runs, or one function's whole history |
| `flags [--unit U \| --all] [--apply SYMBOL]` | sweep optimisation levels and codegen flags for a whole unit, scored per function. `--apply` keeps a clean set in `file_flags.json`, or splits the function into its own unit when the set regresses its unit mates |
| `pragma NAME` | `#pragma optimize/inline_depth/auto_inline` wrappers plus forward/extern/typedef declaration-run state carriers around the definition |
| `levers NAME [--only id,id]` | mechanical CMR2Decomp/brickpark source idioms (`if-else-swap`, `ternary-to-if`, `and-or-swap`, `compound-split`, `assign-to-compound`, `postinc`, `chain-assign`, `decl-hoist`) |
| `submit NAME FILE` | send a saved variant (e.g. from `.outmatch/best/`) through `/api/try` |
| `libmatch [--libs DIR] [--write] [--range-from HEX]` | match the project's functions against the VC6 static libraries' own objects (masked-relocation signatures), then `--write` records them in `set_aside.json` with the library, object and symbol as the reason, and adds their names |

| `status` | DB status counts plus the app's `/api/state` |
| `release NAME` | give a claim back |

Every command prints one JSON object per event on stdout and appends to `.outmatch/*.jsonl`. Improvements below 100
are saved as C files under `.outmatch/best/` and are never written into `src/` unless they win.

## Loop mode and its ledger

```
tools/outmatch/outmatch.sh loop --dry-run --limit 10     # what would be next
tools/outmatch/outmatch.sh loop --dry-run --min-pct 99.9 --order closest  # the closest drafts first
tools/outmatch/outmatch.sh loop --limit 5 --seconds 600  # levers, pragmas, permuter per function
tools/outmatch/outmatch.sh report                        # what it has tried so far
```

Each function is announced with its score and percentage, each phase prints a `start` line with its remaining budget,
and each phase result carries the percentage, variant count and elapsed seconds, e.g.

```
{"symbol": "@func_0043B414@4", "unit": "text_0043B124", "score": 2, "insns": 54, "pct": 99.963, "visits": 0}
{"symbol": "@func_0043B414@4", "phase": "pragma", "start": true, "budget": 894.2}
{"symbol": "@func_0043B414@4", "phase": "pragma", "pct": 99.965, "variants": 39, "seconds": 4.1}
```

`--seconds` is the budget for **one function** (default **900**), not for the run: `levers` and `pragma` finish in
seconds, the permuter gets the rest, so a single function can be worked for up to 15 minutes by default. Lower it
(e.g. `--seconds 120`) for a quick pass, or use `--phases levers,pragma` when the permuter has already stalled.

The phases are `levers`, `pragma` and `permuter` (add any with `--phases`; `all` adds `flags`). The optional `flags` phase
sweeps the function's unit once per unit + source + current flags (about 3 s, cached under `.outmatch/flags/`) and
applies a set only when it makes the function 100 without knocking any matched function in the unit off 100. A set
that would need a split is reported, never applied: run `flags --apply SYMBOL` for that. Every phase that reaches 100
submits the definition through `POST /api/try` immediately, so the app records and commits the match.

`--apply-improvements` also submits a phase's best draft when it is **not** 100 but beats the app's current best
(its `best_score` from the database). That is the only way a sub-100 draft reaches `src/`: the app re-judges it,
keeps it as `best_source` and updates `best_score`, so the GUI and the app's own permuter start from it. Equal or
worse drafts stay in `.outmatch/best/` and the ledger; each submitted improvement takes the `you` claim until the app
releases it, so run it when the agents are not competing for the same functions.

Before anything is submitted, the finalist is recompiled in a **freshly copied** tree and rescored; a local 100 that
does not survive (`fresh 99.868 below 100.000`, as happened once with a stale `include/`) is logged to
`.outmatch/gate.jsonl` and never reaches the app, so it cannot take the `you` claim. Worker trees also refresh changed
headers (`include/`, `src/*.h`) on reuse, so intermediate percentages track the live declarations.

The ledger is append-only JSONL, one row per visit and per phase run, with the source hash of the unit at the time.
That is what makes runs resumable and cheap: the next run picks the least-visited functions first (then worst
relative score), and a phase whose source hash has not changed is skipped. When an agent or you edit the unit, the
hash changes and the phases run again against the new baseline. Only one loop should run at a time: it uses the same
scratch trees as the other commands.

## How a candidate is judged

`harness/search.py` copies `src/`, `include/`, `config/` and `tools/` into one scratch tree per worker, replaces the
target function's definition in its unit, and runs the exact compile command ninja uses for that unit (with the same
include paths, the same flags, and `tools/names.py` symbol normalization). The object is scored with
`objdiff-cli diff` against the delinked original in `build/casefile1/target/`, using the same diff options as the
app's progress report (`functionRelocDiffs=none`, combined sections). One compile is ~0.3 s; four workers give
~9 candidates/s, about 20–50× the app's per-candidate build-and-diff.

A 100% score is not by itself a match in the app's sense — literals and relocation mappings are checked there — so
only 100s are submitted and the app has the final word.

## Flags, and splitting a function out

MSVC 6 flags are per file. `flags` compiles the whole unit once per candidate set and scores every function in it,
which answers both questions at once: did this set fix anything, and did it break any function that already matched?

- clean win for the target function → `--apply` writes the delta into `config/casefile1/file_flags.json` (e.g. `/O1`,
  `/Oy-`), lets ninja's configure rule regenerate `build.ninja`, rebuilds the unit, and the app's next check measures
  it;
- win but regressions → `--apply` moves the function into `src/auto/text_<addr>.cpp` with the unit's declaration
  prologue, moves its `functions.json` entry to the new unit, gives the new file the flag set, reconfigures and
  delinks. This is the manual equivalent of the app's `split_file`, which PC forks do not have.

`--apply` only applies a set that takes the function to a full 100. Anything less is reported and left alone.

## State carriers (pragma / declaration runs)

MSVC 6 keeps hidden state: optimisation pragmas in force at the definition, and the compiler's bookkeeping before it —
what the isle project calls *compiler entropy*. `pragma` tries `#pragma optimize("<letters>", off/on)` for the
letters MSVC 6 knows, `#pragma inline_depth`, `#pragma inline_recursion`, `#pragma auto_inline`, and runs of unused
`struct`/`extern int`/`typedef` declarations of 1–32 lines before the definition. All of these are exactly what the
app accepts beside a definition (`#pragma` lines and one-line declarations), so a winner goes in through `/api/try`
unchanged.

## Libraries already in the binary (`libmatch`)

The game links the C/C++ runtime statically (`/MT`: no `msvcr*.dll` import, `.tls` present), so a few hundred
functions in `orig/casefile1.exe` are not the game's code at all — they are Microsoft's. `libmatch` finds them by
compiling nothing: it unpacks the VC6 libraries' `.obj` members, extracts every function (they are built `/Gy`, one
section per function), and compares a signature of `(size, bytes with relocation fields zeroed, relocation
offsets/types)` against every function in `build/casefile1/target/*.obj`. The delivered image was linked with the
same library build, so a signature match is byte identity in everything the linker did not fill in.

- Only signatures unique in the library are accepted, and a signature shared by two project functions is rejected:
  compiler-generated bodies (`??_G...` scalar-deleting destructors, tiny stubs) coincide across unrelated code. The
  scan reports those under `ambiguous` rather than guessing.
- `--write` adds a `set_aside.json` entry per proven function naming the evidence, e.g.
  `"@func_00451E2B@12": "MSVC 6.0 static runtime — LIBCMT.LIB (stdexcpt.obj): ??0exception@@QAE@ABQBD@Z"`, plus one
  `ranges` entry for the whole CRT tail (`0x44E690-0x4595C6`), because the rest of that block is the same library
  compiled by a slightly different build and does not always match byte for byte. It also fills in the function names
  the library gives (`_onexit`, `_nh_malloc`, `RtlUnwind`, …) in `functions.json`'s `names`.
- Set-aside functions leave the progress denominator (`matched / (total - set aside)`) and are marked not-C; the
  rebuild already takes their bytes from your copy of the game, so nothing about the built program changes.

The libraries are not committed (43 MB, and Microsoft's): take `vc_studio/main/VC98/Lib` from the archive.org item
`visual-studio-6-0-sp5-portable` ("Visual Studio 6.0 SP5+PP5 portable") and point `--libs` at it (default
`/tmp/opencode/vs60/vc_studio/main/VC98/Lib`). This project's original compiler is cl 12.00.8943 (VC6 Processor
Pack, from the PE's Rich header); the archive's library build is 12.00.9044, which is why the big formatting/startup
routines match exactly but the `_malloc` family and `mainCRTStartup` do not.

## Skills

Two project-local skills drive the same tools:

- `.claude/skills/msvc-matching/` — the MSVC 6 lever catalogue distilled from CMR2Decomp and brickpark-decomp, with a
  symptom → lever index, and instructions for turning a diff into a source-level experiment.
- `.claude/skills/doc-driven-matching/` — give it a link (a decomp's write-up, a compiler manual, a blog post), and it
  extracts matching-relevant claims, turns the useful ones into experiments (`pragma`, `flags`, `levers`, or a manual
  edit + `/api/try`), and records what worked.

## Troubleshooting

- **Generator says "can't read this C"** — decomp-permuter's parser could not handle the definition. The function is
  still available for `pragma`/`levers`/manual work.
- **Everything compiles to the same score** — the mismatch is likely scheduling or register allocation, which source
  permutations rarely reach. Read the diff, pick a lever from the skill, and change the source by hand; use
  `submit` to verify.
- **`/api/try` refuses a function** — another agent has it claimed. `release NAME` only frees your own claim; wait for
  the other agent's TTL or work on a different function.
- **Don't run `ninja` in this tree while the app is building** — the app locks units inside itself, not against an
  external ninja. Searches are safe: they compile in scratch copies. `flags --apply` and splits do touch the real
  tree; run them when the app is idle.
