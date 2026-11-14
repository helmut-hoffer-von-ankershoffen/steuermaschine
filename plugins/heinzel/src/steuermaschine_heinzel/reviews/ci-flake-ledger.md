# Review: `sm ci flake-ledger`

New command (no old tool), Core ADR 0149 item g. Unit: `native/merge_ledger.py`, `native/merge_tier.py`, `tasks_lib`,
`asks_lib`, `coord_lib`.

## What it writes

- One bead per job, title `flaky: JOB`, label `flaky`, type bug, through `tasks_lib.beads.Tracker.add`; when an open bead
  with that exact title exists, one note through `Tracker.note` instead.
- One store record per harvested artifact (`heinzel.merge_tier.flakes`, key = artifact name), so a ledger is filed once.

## What it reads

- `merge_tier_mode`, else main's `scripts/focused_plan.py` (contents API, GET): nothing happens without WHOLE_SUITES.
- The newest completed runs of `merge-gate.yml`, their artifacts, and each `flake-ledger-RUN-ATTEMPT` artifact
  (`gh run download` into a scratch directory). Only lines of the exact ledger form are filed.

## To check

- An artifact whose download fails refuses before any bead of it is filed; a partially filed artifact is not recorded
  and is filed again on the next tick (a duplicate note, never a lost line).
- No GitHub write.

## Campaign re-verdict g2 (2026-11-14, independent; reviewer wrote none of this code)

Target: integration/sm-dod frozen at c36bbe1 (baseline 903a1b7, where this unit was APPROVED). Digest recomputed with
`uv run sm self reviews --format json` in a fresh worktree of c36bbe1; it equals the campaign list.

- NEW unit, read in full: native/merge_ledger.py (the `ci flake-ledger` half; `queue main-proof` shares the file and is
  g3/g4's), native/merge_tier.py, and the tasks_lib/asks_lib/coord_lib deltas (state paths, child_environment).
- Gating: `_merge_tier` returns None for mode off, and in auto unless main's focused_plan.py has WHOLE_SUITES;
  then it returns Harvest(state=not-merge-tier) before any listing or write. Old Core: nothing changes (test
  test_harvest_does_nothing_without_the_merge_tier_and_refuses_unreadable_runs, also merge_tier_mode=off).
- Writes: only `bd create` (title `flaky: JOB`, label flaky, priority 2, type bug, description = the ledger line) or
  `bd update --append-notes` on the open bead with the same title, through tasks_lib.Tracker (argv lists, no shell; bead
  id checked against the prefix and suffix form); and one store record per artifact name. No GitHub write, no cancel,
  rerun, push or merge. Only lines matching FLAKE_LINE exactly are filed; other text is ignored.
- Fail-closed: a run listing, artifact listing or download error refuses (exit 1) before that artifact is recorded;
  expired artifacts are skipped and not recorded. A partly filed artifact is filed again next tick (a duplicate note,
  never a lost line), as the file's own To check says. Output: one Model (Harvest), one JSON document.
- Note (not blocking): no per-command lock beyond the shared writers' lock, so two overlapping runs could both file
  `flaky: JOB` for a new job (a duplicate bead). It is not a daemon job today (no launchd entry); the worst case is a
  duplicate bead to close by hand. A store lock around the harvest would remove it.
- Shared core delta (every unit): native.py takes `writers.lock` in the project's state directory around each run of a
  non-exempt write command (shared flock; `sm self migrate-state` takes it exclusively, non-blocking with a bounded
  wait, so it never moves state under a writer). The promote-switch check now runs inside the lock: a single run still
  exits 75, a loop still skips the tick; `guarded(True) or 0` keeps the old exit codes. config.child_environment gains
  drop/drop_prefixes (same filtering the callers did by hand), `search_path()`, `variable()`; plugins.old_variable lets
  an old env variable set a setting only when no file or SM_ variable does (source named in `sm self config`).
  helpdoc lint letters O/X, models StateMigration, hookspec sm_state_moves: no runtime effect on this unit.
- Safety: no force push, stash, auto-merge, --admin or hz-* call added; the cancel and rerun allowlists are unchanged.
Tests (`sm gate run -- uv run pytest`, by file; pytest-xdist is not installed in this repo, so without -n 2):
plugin: test_native_merge_ledger, merge_tier_rules, ci_classify, ci_classify_jev, ci_ops_defer, ci_ops_quarantine,
ci_ops_rerun, ci_ops_sonar, dispatch, dispatch_cov_a_focused, dispatch_focused, rejudge, admission_cov_a_focused_admit,
admission_cov_a_focused_admit_run: 451 tests, 2 skipped, 0 failed. core: test_core_github_cli, client, forge, logs, sonar,
core_native, core_statemove, gate_structure: 126 passed, 0 failed.

COVERS: ci flake-ledger
REVIEWED: sha256=4b1fdd22cbb65b1122ae0c5ea20c0316c5c4168b534c9d8bbaabb767aed5ffc8
VERDICT: APPROVE

## Re-verdict at abc64a4, group C (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-abc64a4/C.md (recorded verbatim by the integrator).

Changed code: merge_ledger.py `file_once` (a1aa7e8). Checked: the filing lock `heinzel.merge_tier.filing` is a
non-blocking flock (defaults.py:59); SEEN re-checked inside the lock, so a tick and a manual call (or the main judge)
cannot file the same ledger twice; patience 0 for the harvest: Busy -> `Wait` at once (75, loop skips); the
main judge waits up to 120 s. Lock order JUDGE -> FILING only, harvest takes FILING only: no deadlock. A `Busy` from
inside the body is not swallowed (the try wraps only `enter_context`). Same as before: a tracker failure midway leaves
SEEN unsaved, so the next tick notes (not re-adds) the beads already added, because `file_flakes` notes an open bead
with the same title. coord_tool_path: see shared. merge_ledger tests green.
COVERS: ci flake-ledger
REVIEWED: sha256=f13d1e7ee3302835baf1f2ce6bf44ffe710d4ea5957a6fdbc1e4cbaa5bd4c3d2
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does.
COVERS: ci flake-ledger
REVIEWED: sha256=29eee6ac6fd958f8e2948e7d26946fc95df0f2f0c0caee046f56e619457280a2
VERDICT: APPROVE
