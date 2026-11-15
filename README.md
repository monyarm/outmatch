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

The exe's Rich header says the game's C++ was built by build 8943: Visual C++ 6.0 with the **Processor Pack**, whose
back end (`C2.DLL`) is a 13.00 file version behind VC6's 12.00 front end. Build 8943 itself (the pack's first, SP4
era) is in no public archive. The nearest held one is decomp.me's `6.5pp` (SP5 + Processor Pack, `C2.DLL`
13.00.9044). A `return true` compiled to `xor eax, eax; inc eax` rather than `push 1; pop eax` is that back end; plain
`6.0` (12.00.8168) cannot produce it, which is why some functions only matched under `7.0`/`7.1` before the
Processor Pack compilers were installed. `compiler` sweeps every version `installed()` finds (see the next section).

```
tools/outmatch/outmatch.sh compiler --symbol '@func_00413AE5@4'          # per-function sweep, report only
tools/outmatch/outmatch.sh compiler --unit text_0041BFF4                # every open function in the unit
tools/outmatch/outmatch.sh compiler --symbol SYM --apply                # pin file_compilers.json if safe
```

`--apply` refuses when the version would regress a function the unit already matches, and prints `needs_split` with
the list of functions that would be lost — that is the signal to divide the unit (the app's `split_unit` keeps the
compiler setting per file). The `compiler` phase of `loop` reports the sweep for every candidate it visits so the
choice is recorded even when nothing is applied.

## Adding the OmniBlade compilers (6.3-6.6, 6.5pp)

decompdecomp's Visual C++ pack installs `6.0`, `7.0` and `7.1` only. The other VC6 service packs are published by
decomp.me's maintainers as Bin+Include tarballs in the
[OmniBlade/decomp.me `msvcwin9x` release](https://github.com/OmniBlade/decomp.me/releases/tag/msvcwin9x) (the same
release decompdecomp takes its 6.0 and 7.1 from; decomp.me also serves them as `ghcr.io/decompme/compilers/win9x/*`):

| version | asset | CL / C1 / C1XX / C2 file versions | sha256 |
|---|---|---|---|
| `6.3` (SP3) | `msvc6.3.tar.gz` | 12.00.8168 / 8472 / 8472 / 8447 | `84f73e718b3671bfd5de3b7764622b07633b572ee826ca3b77602d224c128608` |
| `6.4` (SP4) | `msvc6.4.tar.gz` | 12.00.8804 / 8867 / 8867 / 8799 | `6d4ef930390ca7481ae5b63ea3bf00d62e0993cd95f1d6f42ba06bb30f993c57` |
| `6.5` (SP5) | `msvc6.5.tar.gz` | 12.00.8804 / 8867 / 8964 / 8966 | `16f639b7961ca74b938532d912d2bb655d5ea08b01dc2f50e4a180774d138169` |
| `6.5pp` (SP5 + Processor Pack) | `msvc6.5pp.tar.gz` | 12.00.8804 / 8867 / 8964 / **13.00.9044** | `0de4c5dc848d59b8af842eb157ffe8b1d24fba6621b18be67ea9932e219ce14e` |
| `6.6` (SP6) | `msvc6.6.tar.gz` | 12.00.8804 / 9782 / 9782 / 9782 | `0a4cd7628a79067ee62dd90d40d2c027221cc46f530e40d92245ec46d550dd4d` |

**Install one locally.** Extract it as its own version folder beside the ones decompdecomp installed:

```sh
v=6.5pp
mkdir -p ~/.local/share/decompnet/toolchains/msvc/$v
curl -L https://github.com/OmniBlade/decomp.me/releases/download/msvcwin9x/msvc$v.tar.gz \
  | tar xz -C ~/.local/share/decompnet/toolchains/msvc/$v          # gives $v/Bin/CL.EXE, $v/Include, ...
```

Then pin a file to it like any other version: `"src/auto/text_00413AE5.cpp": "6.5pp"` in
`config/casefile1/file_compilers.json` (or the app's per-file compiler setting / `set_file_compiler`), or make it the
project's default in the app's Settings.

**Why the app picks it up.** decompdecomp gives `tools/configure.py` every installed pack folder as `--msvc`
(`scratch.tool_args`: `toolchains/msvc` and `toolchains/msvc2005`), and `configure.py`'s `msvc(version)` takes the
first `<folder>/<version>/Bin/CL.EXE`. Nothing checks the version name against a list, so a folder is all it takes;
`/api/try` judges with it like any other. The harness looks the same way (`common.cl_exe`), so `8.0`/`8.0SP1` from
`msvc2005` are swept too.

**Measured on this game (2026-11-15, every unit under every version):** exact functions with the whole project on one
version: `6.0` 1926, `6.3` 1926, `6.4`/`6.5`/`6.6` 1913, **`6.5pp` 1995**, `7.0` 1105, `7.1` 1066, `8.0` 1023.
`6.5pp` as the default with 27 files pinned elsewhere (25 to `6.0`, mostly the CRT tail; one `7.0`) gives 2089.
The SP4 Processor Pack (`c2.dll` 13.00.8876, `enu/PP/vcpp.exe` on the archive.org "Microsoft Visual Studio 6 SP4"
CD) brackets build 8943 from below but never beats `6.5pp`: it loses three functions and gains none.

**The app's own route for a version it doesn't ship** is a "Microsoft Visual C++ (your own copy)" kit (Setup >
Compilers & tools, or `POST /api/toolchain {"id": "visualcpp", "folder": ...}`): decompdecomp reads the copy's
`c2.dll` version and names it `own-<build>` (`own-13.00.9044` for 6.5pp), passes it to `configure.py` as `--own`, and
lists it in Settings, where the per-file and whole-game compiler switches (which keep only files whose matches hold)
accept it. A plain folder under `toolchains/msvc` builds and judges, but Settings and `set_file_compiler` refuse a
version outside `MSVC_VERSIONS`, so pins to it can only be written into `file_compilers.json` by hand.

**Caveats.** The folder is not part of decompdecomp's pack: reinstalling the pack in Setup can remove it, and every
machine that builds or judges this project needs it too (a file pinned to a missing version fails `configure.py`
with "Visual C++ 6.5pp isn't in ..."). The durable fix is to add the archives to decompdecomp's
`src/decompnet/toolchains.toml`, next to its existing 6.0 entry:

```toml
[[msvc.parts]]
url = "https://github.com/OmniBlade/decomp.me/releases/download/msvcwin9x/msvc6.5pp.tar.gz"
sha256 = "0de4c5dc848d59b8af842eb157ffe8b1d24fba6621b18be67ea9932e219ce14e"
into = "6.5pp"
```

(one stanza per version, with the sha256 from the table), plus `"MSVC/6.5pp"` etc. in `consoles/pc/__init__.py`'s
`MSVC_VERSIONS` so Settings offers them.

## Drafts for functions with no C at all

About a quarter of the game's bytes are in functions that have never had a single line of C written - the permuter
and `loop` cannot touch them, because both need a draft to start from. `draft` turns one into a work packet:

```
tools/outmatch/outmatch.sh draft --limit 5 --min-size 1024 --write   # .outmatch/drafts/<symbol>.md
tools/outmatch/outmatch.sh draft --symbol '@func_0042BD7C@16' --print
```

A packet holds the symbol, unit, address, size, instruction count and calling convention; the delinked target's
disassembly; the Ghidra pseudocode for that address; the unit's includes and class/struct declarations; and the
function's calls and data references with any names the project knows. Write the C from that, then
`tools/outmatch/outmatch.sh try SYMBOL draft.c` - it compiles the draft in the function's own unit and prints the
percentage. A first draft that compiles and beats the (nonexistent) best can be pushed through the app with `try
--submit` or `loop --apply-improvements`, after which the app's permuter can take it from there.

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
