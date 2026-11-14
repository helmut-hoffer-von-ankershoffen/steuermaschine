# Review: `sm lim` (core)

COVERS: lim

Unit: `src/steuermaschine/commands/lim.py` and every core file it imports (`sm self reviews` lists them and the digest).

Moved from the plugin (`steuermaschine_heinzel/native/guards.py` `lim_command` and `guards_lib/lim.py`, approved there as
reviews/lim.md of the plugin). `condense` and the command body are unchanged; what changed is where the log directory
comes from: `-o FILE`, else the value a plugin hands in under `Providers.extra["lim.directory"]` (the heinzel plugin
passes its setting `guards_lim_dir`), else `lim` below the project's state directory (the plugin's former fallback).

What it writes: when the output was cut, one file with the full output (`-o FILE` or a new file in the directory
above); nothing else. Short output writes nothing. The command it runs does what it does.

What the reviewer checks: the diff of `condense` and the command body against the plugin's former code (git show
origin/main:plugins/heinzel/src/steuermaschine_heinzel/native/guards.py); the log goes only where the three rules say;
the exit code is the command's; the command refuses (exit 70) while this file does not approve the digest.

State: open (needs an independent review; the author writes no verdict).

## Campaign re-verdict g1 (2026-11-14, independent; reviewer wrote none of this code)

Target: integration/sm-dod c36bbe1 (frozen), delta read against the live baseline 903a1b7, where this unit was approved (`git diff 903a1b7 c36bbe1 -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/campaign-g1-sm-dod); it equals the unit list of the sm-dod campaign.

Unit change: `sm lim` moved from the plugin (`native/guards.py` `lim_command`, `guards_lib/lim.py`) to core `commands/lim.py`. `condense`, `Limited` and the command body are unchanged apart from the log directory: `-o FILE`, else `Providers.extra["lim.directory"]` (the plugin passes `guards_lim_dir`), else `<state>/lim`. Writes only the full-output file when something was cut; the exit code is the command's.
- NOTE: with no plugin value and no project, `log_directory` would fail on `rt.project.home`; the review gate already needs a project, so not reachable in practice.

Core part of the unit (read as diffs 903a1b7..c36bbe1): `config.py` (`child_environment` gains `drop`/`drop_prefixes`, `search_path`, `variable` for a plugin setting's old environment name; defaults keep the old behaviour), `plugins.py` (`old_variable` metadata: the old variable sets a field only when no file or SM_ variable does, a bool only on its exact `on` value; `state_moves` hook), `hookspecs.py` (`sm_state_moves`), `helpdoc.py` (lint letters O and X; `sm self lint` passes), `models.py` (`StateMigration`), `native.py` (every gated write run, and every tick of a loop, holds the shared `writers.lock` in the state directory; readers, exempt writers and runs without a project do not; the switch-pending behaviour is unchanged: a single run exits 75, a loop skips the tick). No force push, stash, auto-merge, --admin or old hz-* call was added; `--format json` stays one document.
Tests (one gated command each, by file; this repo has no xdist, so without `-n 2`): `sm gate run -- uv run pytest tests/test_core_statemove.py tests/test_core_migrate.py tests/test_core_gate_units.py tests/test_core_commands.py tests/test_core_native.py tests/test_gate_structure.py`: all passed; `sm gate run -- uv run pytest` on plugin coord asks/briefs/docs/handoff/journal/skills/tasks/velocity/velocity_moved, guards_lim, lock, lock_refuse_matrix: exit 0 (all passed, 1 xfailed).

COVERS: lim
REVIEWED: sha256=22e0755e11f0041dce87b3b8389585e78a6131e01f108e3b1ad4b74912544970
VERDICT: APPROVE

## Re-verdict at abc64a4, group C (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-abc64a4/C.md (recorded verbatim by the integrator).

lim.py, config.py, engine.py, helpdoc.py, protocols.py unchanged; only native.py/models.py. guards_lim tests.
COVERS: lim
REVIEWED: sha256=21db575159f78f4effd5ee4b9cb6363f7690720e21fa36d32b6bfc893a3d0f7d
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does.
COVERS: lim
REVIEWED: sha256=57d1e8ae643b3b95e19f32cd5fa1dc55f5ebee0d0b9c82105b7c8d87a23c1902
VERDICT: APPROVE
