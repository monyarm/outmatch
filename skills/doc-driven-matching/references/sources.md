# Sources mined for matching leverage

Add one entry per source: what it is, what it was actually good for, and where (if anywhere) its content was
captured. Keep the raw captures under `../outmatch/docs/sources/` with a URL + date header.

| Source | What it is | Good for | Captured |
|---|---|---|---|
| https://github.com/pablocpas/CMR2Decomp (`CLAUDE.md`, `scripts/`) | Colin McRae Rally 2.0, MSVC 6 SP3 | Stack-slot ordering and name ties, cross-TU contamination, ~60 concrete idioms, flag sets, `match.py`/`slots.py`/`split_tu.py` | `.claude/skills/msvc-matching/references/cmr2-claude.md` |
| https://github.com/adamXbot/brickpark-decomp (`docs/LEVERS.md`) | LEGOLAND, VC6 SP3 | ~194 symptom-first rules with evidence and measured floors; best "why is this instruction different" lookup | `.claude/skills/msvc-matching/references/brickpark-levers.md` |
| https://github.com/isledecomp/isle (`CONTRIBUTING.md`) | LEGO Island, MSVC 4.20, 100% | Compiler-entropy framing; Beta debug DLL used as an oracle for names/uninlined bodies | `.claude/skills/msvc-matching/references/isle-contributing.md` |
| https://github.com/isledecomp/reprobit (`docs/`) | ReproBit | State carriers, declaration-shape discovery, intervention costs, authenticity rules — the justification for pragma/declaration sweeps | `reprobit-*.md` in the same folder |
| https://github.com/gibbo101/ra2-decomp | Red Alert 2, MSVC6 | Levers for a large VC6 codebase; cross-check idiom catalogue | not yet mined |
| https://github.com/SDW-Community/sdw-decomp | Sheep Dog 'n' Wolf, VC6-exact | Small, exact VC6 project; compares well against CMR2 notes | not yet mined |
| https://github.com/mech4decomp/Mech4MercDecomp | MechWarrior 4, byte-identical track | Oracle/binary-analysis workflow | not yet mined |
| MSVC 6.0 `cl` reference (`cl /?`, `/O`, `/Ob`, `/Oy`, `/GX`, `#pragma optimize`) | Compiler documentation | Exact flag semantics, defaults, interactions the decomps describe empirically | local toolchain docs |
| https://github.com/encounter/objdiff | Diff tool used by the app | What "100%" compares and what it ignores (relocation names, pool relocations) | in use |
| https://github.com/n-delic/decompdecomp (`docs/GUIDE.md`) | The harness app | How attempts are judged, units, claims, sync, `/api/try` | `/tmp/opencode/decompdecomp` clone |

## How to find more

- GitHub code search for `FUNCTION:` / `WIP-FUNCTION:` markers, `reccmp`, `objdiff.json`, `permuter` in repos whose
  target is a 1990s/2000s Windows game (the compiler, not the game, is what matters).
- Project `CLAUDE.md` / `AGENTS.md` / `CONTRIBUTING.md` / `docs/LEVERS.md` / `NOTES.md` in those repos usually hold
  the measured knowledge; the tooling alone rarely explains *why*.
- The original build tree, if it ever surfaces (PDBs, map files, build logs, Rich header) outranks all secondary
  sources.
