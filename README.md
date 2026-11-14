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
| `flags [--unit U \| --all] [--apply SYMBOL]` | sweep optimisation levels and codegen flags for a whole unit, scored per function. `--apply` keeps a clean set in `file_flags.json`, or splits the function into its own unit when the set regresses its unit mates |
| `pragma NAME` | `#pragma optimize/inline_depth/auto_inline` wrappers plus forward/extern/typedef declaration-run state carriers around the definition |
| `levers NAME [--only id,id]` | mechanical CMR2Decomp/brickpark source idioms (`if-else-swap`, `ternary-to-if`, `and-or-swap`, `compound-split`, `assign-to-compound`, `postinc`, `chain-assign`, `decl-hoist`) |
| `submit NAME FILE` | send a saved variant (e.g. from `.outmatch/best/`) through `/api/try` |
| `status` | DB status counts plus the app's `/api/state` |
| `release NAME` | give a claim back |

Every command prints one JSON object per event on stdout and appends to `.outmatch/*.jsonl`. Improvements below 100
are saved as C files under `.outmatch/best/` and are never written into `src/` unless they win.

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
