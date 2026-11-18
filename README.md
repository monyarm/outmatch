# outmatch — match functions outside decompdecomp, record them inside it

`decompdecomp`'s automatic permuter works in short background turns: only functions scoring within 6%, 64 variants a
turn, 60 seconds a turn, four turns (`permuted < 4`). Since 1.0.10-alpha.2 the app also has a **Deep permuter** tab
(decompdecomp #244): a user-started search of one function's saved C, limited to 1-120 minutes and up to 1,000,000
variants, at most two at a time on the app's shared workers, saving every improvement that passes the normal judge.

`outmatch` covers what neither does:

- **whole queues, unattended** — `loop` walks every open function in an order you choose and keeps a ledger, instead
  of one function per tab;
- **more than permutations** — MSVC source levers, `#pragma`/declaration state carriers, per-unit compiler flags, and
  a sweep of every installed compiler build (with splits when one function needs another build than its file);
- **its own CPU budget** — any number of scratch trees outside the app's worker pool, for as long as you like;
- **library code** — `libmatch` finds statically linked library functions (CRT, any `.lib` or locally built `.obj`)
  byte for byte and sets them aside;
- **drafts and repairs** — packets for functions with no C yet, restoring best attempts the tree lost, stale-record
  checks.

Nothing is written into the project tree while searching. A match is submitted through the app's own
`POST /api/try`, which compiles it, diffs it, writes the definition into `src/auto/`, commits it on the fork's branch,
and updates the database — the same path the GUI's **Try** uses. No restart, no SQL.

```
../outmatch/outmatch.sh status
../outmatch/outmatch.sh search '@func_00411E5A@12' --seconds 300
../outmatch/outmatch.sh run --max-ratio 0.005 --limit 26 --seconds 120
```

## Requirements

- the decompdecomp app running with this project open (its `~/Decomp/.decomp/server.json` is read for the port and
  secret; submissions are credited to **you**),
- a configured build (`build.ninja`, target objects in `build/<version>/target/`),
- `objdiff-cli` (found on the PATH or in `~/.local/share/decompnet/toolchains/objdiff-cli/`),
- the app's own Python for the permuter generator (taken from the ninja compile command, so it is always the right
  one).

Everything else is the Python standard library. Scratch trees live in `/tmp/outmatch/<project folder>` (override with
`OUTMATCH_WORK`), and state/logs in the project's `.outmatch/` (git-ignored there).

## Where it runs

outmatch is not part of any one project. Keep it beside the decompdecomp projects it works on and run it from inside
one of them:

```
~/Decomp/
  outmatch/                  this repository
  case-file-1-casefile1/     a decompdecomp project (fork)
  ...
```

```sh
cd ~/Decomp/case-file-1-casefile1
../outmatch/outmatch.sh status
```

It takes the project from the working directory: the nearest folder at or above it with
`config/<version>/functions.json`. The version is that folder's name, and the app's name for the project (its slug)
comes from decompdecomp's `projects` table by folder. Override any of them with `OUTMATCH_PROJECT`, `OUTMATCH_VERSION`
and `OUTMATCH_SLUG`. The Claude Code skills in `skills/` (`msvc-matching`, `doc-driven-matching`) are linked into a
project's `.claude/skills/`.

## Commands

| command | what it does |
| --- | --- |
| `search NAME [--seconds 120] [--jobs 4] [--count 64] [--seed N] [--no-submit]` | hill-climb decomp-permuter variants for one function; submits the first exact match |
| `run [--max-ratio 0.06] [--limit 20] [--seconds 30] [--any] [--stop-on-match] [--no-submit]` | the same over the near-miss queue, worst-first; skips claimed functions. `--any` includes functions the app has not permuted 4 times |
| `loop [--max-ratio 0.06] [--min-pct 0] [--order fresh\|closest] [--limit 0] [--seconds 900] [--phases msvc,levers,pragma,permuter,compiler] [--unit U] [--again] [--forever] [--dry-run] [--depth N] [--beam K] [--cap N] [--focus auto|off]` | works the queue with **no function named**: for each candidate it runs the phases in order, keeps the ones that help, submits a 100, and records what it tried in `.outmatch/ledger.jsonl`. `--min-pct 99.5` restricts the queue to the closest drafts (`--order closest` sorts by percentage instead of least-visited). Visits rotate, so a second run continues where the first stopped; a phase already run on unchanged source is skipped unless `--again`. `--dry-run` prints the queue; `--forever` waits for new candidates |
| `report [SYMBOL]` | the loop's ledger: functions, visits, matches and per-phase runs, or one function's whole history |
| `flags [--unit U \| --all] [--apply SYMBOL]` | sweep optimisation levels and codegen flags for a whole unit, scored per function. `--apply` keeps a clean set in `file_flags.json`, or splits the function into its own unit when the set regresses its unit mates |
| `pragma NAME` | `#pragma optimize/inline_depth/auto_inline` wrappers plus forward/extern/typedef declaration-run state carriers around the definition |
| `levers NAME [--only id,id]` | mechanical CMR2Decomp/brickpark source idioms (`if-else-swap`, `ternary-to-if`, `and-or-swap`, `compound-split`, `assign-to-compound`, `postinc`, `chain-assign`, `decl-hoist`) |
| `msvc NAME [--only g,g] [--file F] [--cap 160] [--depth N] [--beam K] [--focus auto\|off] [--apply]` | the MSVC catalogue in bulk: argument order, local rename, declaration order, condition/ternary form, init form, `volatile`, integer types, statement order, `x -= k; f(x)`, boolean value forms, if/else negation, index width, `++i`, compare constants, compound assignment, switch order; every variant is compiled and scored. `--depth` chains passes on the improved body and a tie may continue the chain; `--beam K` keeps K equally-good bodies per generation (1 = greedy hill-climb); `--focus auto` orders the families by the objdiff diff kinds actually observed |
| `compiler [--symbol NAME] [--unit U] [--limit N] [--apply]` | score a function (or a whole unit) under every installed CL (`6.0`, `7.0`, `7.1`) and report the best version and what it gains; `--apply` pins `file_compilers.json` when no unit mate regresses, and otherwise reports `needs_split` with the functions that would be lost so the unit can be divided |
| `stale [--symbol NAME]... [--write]` | functions whose recorded best the current tree cannot reproduce: re-measure the recorded body and the file body, and with `--write` put the measured percentage in `.outmatch/stale.json` so queueing and `report` use the truth |
| `submit NAME FILE` | send a saved variant (e.g. from `.outmatch/best/`) through `/api/try` |
| `libmatch [--libs DIR] [--write] [--range-from HEX]` | match the project's functions against the VC6 static libraries' own objects (masked-relocation signatures), then `--write` records them in `set_aside.json` with the library, object and symbol as the reason, and adds their names |
| `draft [--limit N] [--min-size B] [--symbol NAME] [--out DIR] [--print] [--write]` | functions with no C at all, biggest first: writes a packet per function (disassembly, Ghidra pseudocode, unit declarations, callees) to paste into a model; test the result with `try` |
| `restore [--symbol NAME] [--limit N] [--submit] [--force]` | functions the app has a best for but whose unit no longer defines: measure the recorded body against the current file, report neighbours it would disturb, and `--submit` re-measure it through the app (a large gap means the record is stale, not that the C is lost) |
| `twins --from DIR [--from DIR] [--no-submit] [--min-size N] [--jobs N]` | open functions whose target bytes (relocations masked) equal a matched function's in another decompdecomp project (another game on the same engine): its C renamed by the relocations (and the class/type names that share their addresses), the declarations it needs borrowed from its file or project, all twins of a unit compiled at once with this unit's own compiler and flags (no same-compiler rule), and only 100s sent to `/api/try`. Minutes, where the app's autopilot twin pass takes hours and skips files pinned to different compilers |
| `autopilot [--stop] [--status] [--all]` | start (default), stop or show the app's autopilot (`/api/autopilot`) for this project; `--all` does it for every project the app knows, from any folder, without looking for a project (or `OUTMATCH_PROJECT`) |

| `status` | DB status counts plus the app's `/api/state` |
| `release NAME` | give a claim back |

Every command prints one JSON object per event on stdout and appends to `.outmatch/*.jsonl`. Improvements below 100
are saved as C files under `.outmatch/best/` and are never written into `src/` unless they win.


### `refs`: references that name the wrong symbol

`./outmatch.sh refs [SYMBOL...] [--status open|matched|all] [--kind K]` asks the app's own diff (`/api/diff`, what the
UI shows under a function) for every function and lists each `refers to X where the original refers to Y`, classified:
`constructor` (an out-of-line `??0Class` where the original calls `func_X`: name the class `C_X`, typedef the old name to
it, declare the constructor without a body), `import` (`__imp__F@N` vs the delinked original's undecorated `__imp__F`: usually harmless; a `_imp__F` pointer fixes the name but can lose dllimport's register reuse),
`data_or_code` (the same address spelt `func_` on one side and `D_` on the other), `unnamed` (no address and no `names`
entry) and `other_callee` (the code calls a different address: a decompilation error). Exits 1 when it finds any.

## Loop mode and its ledger

```
../outmatch/outmatch.sh loop --dry-run --limit 10     # what would be next
../outmatch/outmatch.sh loop --dry-run --min-pct 99.9 --order closest  # the closest drafts first
../outmatch/outmatch.sh loop --limit 5 --seconds 600  # levers, pragmas, permuter per function
../outmatch/outmatch.sh report                        # what it has tried so far
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
`objdiff-cli diff` against the delinked original in `build/<version>/target/`, using the same diff options as the
app's progress report (`functionRelocDiffs=none`, combined sections). One compile is ~0.3 s; four workers give
~9 candidates/s, about 20–50× the app's per-candidate build-and-diff.

A 100% score is not by itself a match in the app's sense — literals and relocation mappings are checked there — so
only 100s are submitted and the app has the final word.

## Flags, and splitting a function out

MSVC 6 flags are per file. `flags` compiles the whole unit once per candidate set and scores every function in it,
which answers both questions at once: did this set fix anything, and did it break any function that already matched?

- clean win for the target function → `--apply` writes the delta into `config/<version>/file_flags.json` (e.g. `/O1`,
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
functions in `orig/<version>.exe` are not the game's code at all — they are Microsoft's. `libmatch` finds them by
compiling nothing: it unpacks the VC6 libraries' `.obj` members, extracts every function (they are built `/Gy`, one
section per function), and compares a signature of `(size, bytes with relocation fields zeroed, relocation
offsets/types)` against every function in `build/<version>/target/*.obj`. The delivered image was linked with the
same library build, so a signature match is byte identity in everything the linker did not fill in.

- Only signatures unique in the library are accepted, and a signature shared by two project functions is rejected:
  compiler-generated bodies (`??_G...` scalar-deleting destructors, tiny stubs) coincide across unrelated code. The
  scan reports those under `ambiguous` rather than guessing.
- `--write` adds a `set_aside.json` entry per proven function naming the evidence, e.g.
  `"@func_00451E2B@12": "MSVC static library — LIBCMT.LIB (stdexcpt.obj): ??0exception@@QAE@ABQBD@Z [builds 8168, 8447, 8797, ...]"`, plus one
  `ranges` entry for the whole CRT tail (`0x44E690-0x4595C6`), because the rest of that block is the same library
  compiled by a slightly different build and does not always match byte for byte. It also fills in the function names
  the library gives (`_onexit`, `_nh_malloc`, `RtlUnwind`, …) in `functions.json`'s `names`.
- Set-aside functions leave the progress denominator (`matched / (total - set aside)`) and are marked not-C; the
  rebuild already takes their bytes from your copy of the game, so nothing about the built program changes.

The libraries are not committed (Microsoft's). Several builds can be indexed at once, one `--libs BUILD=DIR` each;
the same object in several builds (or in both `LIBC.LIB` and `LIBCMT.LIB`) counts once, reported from the first
library in `--only` and the first build in `--libs`, and each hit lists the builds that carry it:

```sh
python -m harness.libmatch --only LIBCMT.LIB,LIBCPMT.LIB,OLDNAMES.LIB,LIBC.LIB,LIBCP.LIB \
  --libs 8797=<sp4>/enu/vc98/lib --libs 8966=<nolf>/msvc-6.00-8966/LIB --libs 8447=<nolf>/msvc-6.00-8447/LIB \
  --libs 9782=<nolf>/msvc-6.00-9782/VC98/Lib --libs 8168=<nolf>/msvc-6.00-8168/LIB [--write]
```

Where each build's libraries come from:

| build | what | source |
|---|---|---|
| 8797 | **VC6 SP4 — the game's CRT build** (Rich header: 126 C + 8 C++ objects at 8797; the `@comp.id` of `libcmt.lib`'s objects says 8797) | archive.org `microsoft-visual-studio-6-sp4-x05-78387-x05-78367d1-2000-microsoft-cd`: the zip holds `vs6sp4.mdf`, raw 2352-byte sectors (keep bytes 16-2064 of each for a plain ISO); `enu/vc98/lib`. The same CD's `enu/PP/vcpp.exe` is the SP4 Processor Pack |
| 8168, 8447, 8966, 9044, 9782, 7303, 3077 | VC6 RTM, SP3, SP5, SP5+PP (same libraries as SP5), SP6, VC5, VC7.1 | [nolffan/toolchains](https://github.com/nolffan/toolchains) (`msvc-*/LIB`, `COMPILERS.md` for the evidence) |
| SP5 | the old default (`--libs` with no argument) | archive.org `visual-studio-6-0-sp5-portable`, `vc_studio/main/VC98/Lib` |

Measured: 213 functions, all `LIBCMT.LIB`, all inside the CRT tail; 208 of them are in the 8797 build, and the
8797 libraries add none the others miss. So the rest of the tail (the `_malloc` family, `mainCRTStartup`) does not
fail for want of the right build. `--only all` (MFC, ATL, everything) finds only 10-23-byte setters in the game's
code that happen to equal MFC/ATL members: coincidences, never set aside.

## Which compiler built the unit

The exe's Rich header lists the builds that made it: 161 C++ objects at **8943** (Visual C++ 6.0 with the **Processor
Pack**, whose back end `C2.DLL` is a 13.00 file version behind VC6's 12.00 front end), 126 C + 8 C++ at 8797 (the
static CRT, VC6 SP4's libraries), 36 C at 8168 (VC6 RTM: decompdecomp's `6.0`) and one C object at 8830 (a VS2002
pre-release). Build 8943 itself is in no public archive; the Processor Packs on either side of it are (SP4's 8876,
SP5's 9044). A `return true` compiled to `xor eax, eax; inc eax` rather than `push 1; pop eax` is the Processor Pack
back end; plain `6.0` cannot produce it.

The project's default is the SP5 Processor Pack (`own-13.0.9044`, `MSVC_VERSION` in `tools/configure.py`); files
whose matched functions need another build are pinned in `config/<version>/file_compilers.json`.

```
../outmatch/outmatch.sh compiler --symbol '@func_00413AE5@4'          # per-function sweep, report only
../outmatch/outmatch.sh compiler --unit text_0041BFF4                # every open function in the unit
../outmatch/outmatch.sh compiler --symbol SYM --apply                # pin file_compilers.json if safe
```

`--apply` refuses when the version would regress a function the unit already matches, and prints `needs_split` with
the list of functions that would be lost. The app has no per-function compiler: a file has one, and the app's own
switch (Settings, `/api/compiler`) keeps a file's new compiler only when every function that matched still matches.
So a function that matches only under another build than its file's neighbours has to move into a file of its own,
pinned to that build (see "Flags, and splitting a function out"; the 2026-11-15 splits moved 38 functions this way).
The `compiler` phase of `loop` reports the sweep for every candidate it visits.

## Adding compilers

outmatch sweeps every compiler decompdecomp can build with: its own packs (`6.0`, `7.0`, `7.1`, `8.0`, `8.0SP1`) and
every "Microsoft Visual C++ (your own copy)" kit registered in Setup, which decompdecomp names `own-<C2.DLL build>`
(`own-13.0.9044` for decomp.me's `msvc6.5pp`). To add one: unpack it so `Bin/CL.EXE`, `Bin/c2.dll` and `Include/` sit
in one folder, then register it in Setup > Compilers & tools or with
`POST /api/toolchain {"id": "visualcpp", "folder": "<folder>", "accept_license": true}`.

Two gotchas, both measured:

- **After adding a kit, regenerate `build.ninja` once.** decompdecomp writes the kits into `build.ninja`'s `configure`
  rule (`--own`) only when it sets a project up, so a kit added later is unknown to the build: every file switched to it
  fails to build and the app's switch keeps the old compiler. From the project:

  ```sh
  PY=~/.cache/decompdecomp-nix/<version>/venv/bin/python   # the app's own interpreter
  eval "$PY tools/configure.py <version> $($PY -c 'import shlex; from decompnet.consoles.pc import scratch; print(shlex.join(scratch.tool_args(("visualcpp",))))')"
  grep -c -- --own build.ninja      # > 0
  ```

- **New units need their target objects.** The app's build runs `ninja all_source`, which compiles but never delinks; a
  unit added by hand (a split) has no `build/<version>/target/*.obj` until `tools/delink.py config/<version>/functions.json
  build/<version>/target` runs, and `/api/fork/check` fails with "Failed to open .../target/<unit>.obj" until then.

Sources for VC6 service packs and Processor Packs: the
[OmniBlade/decomp.me `msvcwin9x` release](https://github.com/OmniBlade/decomp.me/releases/tag/msvcwin9x) (6.0, 6.3, 6.4,
6.5, 6.5pp, 6.6, 7.0 as Bin+Include tarballs; decomp.me serves the same as `ghcr.io/decompme/compilers/win9x/*`), and
archive.org's original media (the VS6 SP4 CD's `enu/PP/vcpp.exe` is the SP4 Processor Pack, `c2.dll` 13.00.8876; VS.NET
2002 Beta 1 is 13.00.9030). Each project's own list of what it uses belongs in that project.

## Drafts for functions with no C at all

About a quarter of the game's bytes are in functions that have never had a single line of C written - the permuter
and `loop` cannot touch them, because both need a draft to start from. `draft` turns one into a work packet:

```
../outmatch/outmatch.sh draft --limit 5 --min-size 1024 --write   # .outmatch/drafts/<symbol>.md
../outmatch/outmatch.sh draft --symbol '@func_0042BD7C@16' --print
```

A packet holds the symbol, unit, address, size, instruction count and calling convention; the delinked target's
disassembly; the Ghidra pseudocode for that address; the unit's includes and class/struct declarations; and the
function's calls and data references with any names the project knows. Write the C from that, then
`../outmatch/outmatch.sh try SYMBOL draft.c` - it compiles the draft in the function's own unit and prints the
percentage. A first draft that compiles and beats the (nonexistent) best can be pushed through the app with `try
--submit` or `loop --apply-improvements`, after which the app's permuter can take it from there.

## Skills

Two Claude Code skills in `skills/` drive the same tools (link them into a project as `.claude/skills/<name>`):

- `skills/msvc-matching/` — the MSVC 6 lever catalogue distilled from CMR2Decomp and brickpark-decomp, with a
  symptom → lever index, and instructions for turning a diff into a source-level experiment.
- `skills/doc-driven-matching/` — give it a link (a decomp's write-up, a compiler manual, a blog post), and it
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
