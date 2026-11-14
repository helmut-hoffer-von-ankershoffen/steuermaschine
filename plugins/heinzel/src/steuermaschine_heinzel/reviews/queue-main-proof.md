# Review: `sm queue main-proof`

New command (no old tool), Core ADR 0149 items i and g'. Unit: `native/merge_ledger.py`, `native/merge_tier.py`,
`tasks_lib`, `asks_lib`, `coord_lib`.

## What it writes

1. One commit status on the current main commit: context `hz/main-proof`, state success, only after main's own
   `scripts/focused_gate.py` (main mode, FOCUSED_MAIN_BASE) printed a `focused-main/v1` proof binding that commit and
   its push run. Core's `last_green_main` counts a main commit as green only with this status.
2. One store record per proven commit (`heinzel.merge_tier.main_proof`).
3. Flaky beads for the proof's `passed_on_rerun` lines (the same filing as `sm ci flake-ledger`), once per run attempt.

## The base

The last main commit before the tip whose push run passed `CI OK` (latest attempt) and that carries the status, the
same rule as Core's `last_green_main`; none yet: the tip's parent, and the judge then requires the full set itself.

## To check

- The judge runs in a scratch `git clone --shared` of `core_repo_dir` at the tip, origin set to GitHub, with the
  GitHub token and the five plan variables in its environment only; it is main's code, never a PR's.
- No status is posted when the run is not finished, not green, or the judge refuses; a rerun of a red main run is
  waited for, not refused.

## Campaign re-verdict g4 (2026-11-14, independent; reviewer wrote none of this code)

Reviewer: independent (sm-dod campaign group g4, branch review/campaign-g4-sm-dod); wrote none of this code. NEW unit,
reviewed in full at c36bbe1: native/merge_ledger.py (both commands), native/merge_tier.py, and its use of
tasks_lib.beads. Digest recomputed with `uv run sm self reviews` (25 files); it equals the campaign list. The unit does
not include queue_lib, so the g4 finding on main_batch.valid_run is not in it. Core's judge read at Core 64a7b0d2f
(scripts/focused_gate.py verify_main and main(), scripts/focused_plan.py last_green_main).

Against the approved Core review (packets/desktop-focused-0942/CODE-REVIEW.md):
- The judge recomputes the selection with main's selector: sm runs `uv run --frozen --no-build --script
  scripts/focused_gate.py` in a `git clone --shared --no-checkout` of core_repo_dir, checked out detached at the tip
  sha read from GitHub's refs/heads/main (content bound by the commit hash), origin set to GitHub. With
  FOCUSED_MAIN_BASE set, Core's main() calls verify_main, which calls resolve() with main's own manifest and slots
  (the five plan variables are passed). No PR code runs. The token is only in the child's environment.
- R4 (the base is never newer than the last accepted green main): sm's last_green mirrors Core's last_green_main
  (ci.yml push runs, status=success, 30 runs, ancestor of the tip, CI OK on the latest attempt, an hz/main-proof
  success). It is also enforced by the judge itself: verify_main requires `base` to be an ancestor of its own
  last_green_main and refuses otherwise. The bootstrap fallback (no green main: the tip's parent) therefore fails
  closed when sm's read missed a green main (an API error in last_green is ignored and yields the parent; the judge
  then refuses with "a main base must not be newer than the last accepted green main commit"), and with no green
  main the judge selects the full set. Also checked: verify_main fetches main and refuses when it moved.
- Reruns: a red or unfinished push run is `waiting` (no status, exit 0), never refused; the judge accepts a rerun of
  the main run (merge tier) and returns `passed_on_rerun`, which sm files as flaky beads once per run attempt (store
  key main-RUN-ATTEMPT). The release candidate is not touched here (Core's release_candidate.py keeps attempt 1).
- Gating: `_merge_tier` returns None for merge_tier_mode=off, and in auto requires main's focused_plan.py to define
  WHOLE_SUITES; main-proof additionally needs MAIN_PROOF_CONTEXT in it (proof_capable). With today's Core main it
  returns not-applicable and writes nothing (tested with plans lacking either marker).
- Writes: one status (hz/main-proof, success) only after the judge printed a focused-main/v1 proof whose head and run
  equal the tip and its push run; a non-zero exit, no JSON, or a proof for another head or run refuses before any
  post; `--sha` other than the tip refuses; `--dry-run` posts nothing. A failed POST refuses. All tested in
  plugins/heinzel/tests/test_native_merge_ledger.py.

Findings (none blocking):
1. merge_ledger.py:272-282: the status is posted and the proof stored before the flaky beads are filed. If `bd`
   fails after the post, the next tick returns `already` and that attempt's passed_on_rerun lines are never filed.
   Filing first (or filing from the stored proof on `already`) would close it.
2. merge_ledger.py:114-115: merge_tier_mode=on with an unreadable focused_plan.py proceeds to the judge (plan ""),
   which then fails on main's missing script and refuses. Forced by the operator; acceptable.
3. No test runs main-proof with merge_tier_mode=off (flake-ledger has it); the code path is the same `_merge_tier`.
4. The command is not wired into a loop (loops.py); until it runs, Core sees no green main and every main push runs
   the full set. Safe direction.

Tests: `sm gate run -- uv run pytest` over the 32 files plugins/heinzel/tests/test_native_{queue,ship,merge_ledger,merge_tier}*.py and moved/test_native_{queue,ship}*.py at c36bbe1, one run, exit 0, none failed. (`-n 2` is refused: no pytest-xdist in this repo, so the run was serial.)

COVERS: queue main-proof
REVIEWED: sha256=4b1fdd22cbb65b1122ae0c5ea20c0316c5c4168b534c9d8bbaabb767aed5ffc8
VERDICT: APPROVE

## Re-verdict at abc64a4, group B (2026-11-14)

Independent reviewer; wrote none of this code. Findings, baselines and tests: ~/Code/packets/sm-reverdict-abc64a4/B.md (recorded verbatim by the integrator).

COVERS: queue main-proof
REVIEWED: sha256=f13d1e7ee3302835baf1f2ce6bf44ffe710d4ea5957a6fdbc1e4cbaa5bd4c3d2
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does.
COVERS: queue main-proof
REVIEWED: sha256=29eee6ac6fd958f8e2948e7d26946fc95df0f2f0c0caee046f56e619457280a2
VERDICT: APPROVE
