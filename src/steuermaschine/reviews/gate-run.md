# Review: `sm gate run` (core)

COVERS: gate run

The run lock for heavy local commands. Registered `writes=True, exempt=...` on the core's allowlist
(`native.EXEMPT_PATHS`): the gate cannot hold it (the tests of every review run through it), so this verdict is
advisory for running it, but `bin/promote` refuses a candidate whose `gate run` is not approved here.

Unit: `src/steuermaschine/commands/gate.py` and every core file it imports, `runlock.py` among them (`sm self reviews`
lists them and the digest).

Moved from the plugin (`steuermaschine_heinzel/native/lock.py`, approved there as reviews/gate-run.md of the plugin,
campaign g1). The command body, its options and the `GateRun` model are unchanged; what changed is where the policy
comes from: the plugin hands in its policy factory under `Providers.extra["gate.policy"]` (the heinzel plugin passes
`native/lock.py` `policy`, with the refusals of `lock_lib/refuse.py`), else `default_policy` (files below the
project's state directory, the core limits of `runlock.Policy`, no refusals). The help names "the project's policy"
instead of the plugin's refusals; the refusal texts are the policy's and unchanged.

What it writes: a lock slot while the command runs and one line of the gate history; the command does the rest.

What the reviewer checks: the diff of the command against the plugin's former code (git show
origin/main:plugins/heinzel/src/steuermaschine_heinzel/native/lock.py); that the plugin's policy reaches the core
command unchanged (`plugins/heinzel/tests/test_native_lock.py`, `test_native_lock_refuse_matrix.py`); the default
policy writes only below the state directory (`tests/test_core_commands.py`).

State: open (needs an independent review; the author writes no verdict).

## Campaign re-verdict g1 (2026-11-14, independent; reviewer wrote none of this code)

Target: integration/sm-dod c36bbe1 (frozen), delta read against the live baseline 903a1b7, where this unit was approved (`git diff 903a1b7 c36bbe1 -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/campaign-g1-sm-dod); it equals the unit list of the sm-dod campaign.

Unit change: `gate run`/`gate status` moved from the plugin (`native/lock.py`) to core `commands/gate.py`. Read against `git show 903a1b7:plugins/heinzel/src/steuermaschine_heinzel/native/lock.py`: the body, options, `GateRun`, `execute` and the exempt declaration are unchanged; `environ=child_environment()` equals the former `dict(os.environ)`; the policy comes from `Providers.extra["gate.policy"]` (the plugin passes `lock.policy` with the `lock_lib/refuse.py` refusals, so make pr and full suites stay refused), else `default_policy` (files in `<state>/gate`, core limits, no refusals). `runlock.py` unchanged. `gate run` stays on `EXEMPT_PATHS` and outside the writers' lock, which is right: its files keep their inode when they move.
- NOTE: the plugin's policy (`native/lock.py`) is outside this digest. Its defaults moved from `~/.cache/hz-gate*` and `~/Library/Logs/hz-gate-history.jsonl` to the state directory (`gate.*`, `gate-history.jsonl`); `sm self migrate-state` hard-links the lock files, so an old-release gate and a new one exclude each other through both names. Before the migration the plugin's policy refuses (exit 70) rather than opening a second lock: fail-closed.
- NOTE (carried): the small exclusive/normal slot race noted at 479a8e8 is unchanged.

Core part of the unit (read as diffs 903a1b7..c36bbe1): `config.py` (`child_environment` gains `drop`/`drop_prefixes`, `search_path`, `variable` for a plugin setting's old environment name; defaults keep the old behaviour), `plugins.py` (`old_variable` metadata: the old variable sets a field only when no file or SM_ variable does, a bool only on its exact `on` value; `state_moves` hook), `hookspecs.py` (`sm_state_moves`), `helpdoc.py` (lint letters O and X; `sm self lint` passes), `models.py` (`StateMigration`), `native.py` (every gated write run, and every tick of a loop, holds the shared `writers.lock` in the state directory; readers, exempt writers and runs without a project do not; the switch-pending behaviour is unchanged: a single run exits 75, a loop skips the tick). No force push, stash, auto-merge, --admin or old hz-* call was added; `--format json` stays one document.
Tests (one gated command each, by file; this repo has no xdist, so without `-n 2`): `sm gate run -- uv run pytest tests/test_core_statemove.py tests/test_core_migrate.py tests/test_core_gate_units.py tests/test_core_commands.py tests/test_core_native.py tests/test_gate_structure.py`: all passed; `sm gate run -- uv run pytest` on plugin coord asks/briefs/docs/handoff/journal/skills/tasks/velocity/velocity_moved, guards_lim, lock, lock_refuse_matrix: exit 0 (all passed, 1 xfailed).

COVERS: gate run
REVIEWED: sha256=5ff55def69192080464c7bcb77e9842c55f6fa2beef4f92173c6d77d96ca1cf9
VERDICT: APPROVE

## Re-verdict at abc64a4, group A (2026-11-14)

Independent reviewer; wrote none of this code. Baselines, shared core and tests: ~/Code/packets/sm-reverdict-abc64a4/A.md (recorded verbatim by the integrator).

Delta since the g1 APPROVE at c36bbe1: `commands/gate.py` and `runlock.py` unchanged; only the shared core
(`native.writing()`, models.py docstring). `gate run` stays on `EXEMPT_PATHS`, so `writers_lock` skips it (as before:
its lock files keep their inode when they move, and the tests of every review run through it); the switch-pending
check is computed as before (`cmd.writes`), so a single `gate run` still exits 75 during a switch. Runlock: per-slot
`flock` LOCK_EX|LOCK_NB in the policy directory, unchanged. It is a lock, not an allowlist runner: it runs the given
argv (no shell) and refuses what the project's policy refuses (heinzel: `native/lock.py` with `lock_lib/refuse.py`:
make pr/preflight/check, full suites, coverage; `--allow-full` cannot admit a never-admit rule). That policy file is
outside this digest (carried NOTE). Nothing got weaker.

COVERS: gate run
REVIEWED: sha256=759654946f31153c5520fc5848b05535dc5578866dc821b774cf1e3573ad04e2
VERDICT: APPROVE

## Delta review: feat/sm-gate-lanes d94de45 (independent reviewer)

Changed in this unit since the last verdict: src/steuermaschine/runlock.py, src/steuermaschine/commands/gate.py (light lane by whole argv or keyed p50, rerun refusal on tree+dirty hash, --force-rerun reason). Reviewed in full in two rounds (e6db9d9, d94de45),
probes now tests; see ~/Code/packets/sm-gate-lanes/REVIEW-2.md. Nothing in this command got weaker.

REVIEWED: sha256=dd489acbe5513772e2172e4a4189c9207dac5c373206be90d9fcce4c33b75105
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Exempt unit, re-read anyway. Light lane (fullmatch on shlex.join, learned p50 keyed by argv+cwd+scope env), rerun refusal on HEAD^{tree}+dirty hash, --force-rerun with bad_reason checked in the CLI and in RunLock.run; git commands and pushes never refused; refusals happen before any slot is taken and are not logged. Composition: the plugin's policy now sets tree_state, so every internal caller of lock_of() inherits the refusal (see X1 under head-junit.md and plugin-gallery.md). gate-lanes A7 (exit 2 counted as usage, so make/pytest collection failures stay rerunnable) is still open: fails open.
COVERS: gate run
REVIEWED: sha256=831672de1ccbd71875762d1f67f3ba5c41d9b0d32d56784aa0894a157202a3b3
VERDICT: APPROVE
