# Review: `sm release pr VERSION --head SHA [--dry-run]` (open the reviewed release PR)

Independent review (2026-11-13, a reviewer who wrote none of this code) of branch fix/batch-release at 61dcce0
(commits on f9134cc). Same review unit as `release-resume.md` and `release-drop.md` (35 files:
`native/release.py`, `native/release_lib/` with the two reviewed sources, `native/queue_lib/`); the unit's only
changes since the last verdict (cefa96ac...) are the five files of commits 8f6156c, 5de092f, 0cf8190, eb642f7 and
61dcce0; the findings on the other four are in `release-resume.md`.

COVERS: release pr

## What the command does (file:line, runner = `native/release_lib/runner.py`)

- `native/release.py:268-275`: VERSION must be X.Y.Z (exit 2 otherwise); passes exactly `VERSION --open-pr
  --reviewed-head SHA [--dry-run]` to the runner. Nothing else reaches the runner.
- runner `main` 2499-2505: `--open-pr` takes only `--reviewed-head` and `--dry-run`, needs an exact version and a
  head; `--reviewed-head`/`--dry-run` without `--open-pr` are refused. So the PR action can never combine with
  resume, absorb, reject, plugin or drop.
- runner 2572-2590: the action runs after the global release lock, after the receipt corruption check and after the
  unfinished-drop refusal.
- runner 962-1007 `open_reviewed_release_pr`: full 40-hex head (971); no tag (973); no release PR in ANY state (975,
  `release_pr` 647-649 lists `--state all`); a foreign unfinished receipt is preserved (980); the release checkout
  passes the existing reviewed `verify_release_pr_scope` (732-778: on release/vX, clean, one commit directly on
  origin/main, only the six release files, version and lock checks); its head must equal `--head` (984, refuses
  drift); the notes audit (986); the dry run stops here (991) and pushes nothing.
- The push is the existing reviewed `publish_release_pr` (917-956): the head is re-verified before notes, before the
  push and after it; notes must be non-empty (932, new); the remote branch must be absent (939-942); the push is
  `sm gate run -- git push --quiet origin <SHA>:refs/heads/release/vX` (943) with `RELEASE_PR_PUSH_ENV` (679-684, the
  release-only hook selectors): the exact SHA, no `--force`, no `--no-verify`; the remote branch must then be exactly
  that SHA (948-951) before `gh pr create --base main` with the checked title and body file.
- After creation (996-1001): open, base main, head = SHA, title `chore(release): vX`, non-empty body, no auto-merge;
  any difference stops and preserves the PR. No merge, no auto-merge, no branch deletion anywhere on the path
  (asserted by `test_the_reviewed_branch_is_pushed_under_the_run_lock_...`). Merging stays with `release resume`
  and its existing gates (unchanged merge path).

## Findings

- R1 (minor, not blocking): `--dry-run` does not run `scripts/release_notes.py` nor the remote-branch-absent probe;
  both are checked on the real run before the push, so a dry run that passes can still stop before pushing. The help
  says "Check everything"; it checks everything up to the notes generation.
- R2 (minor): `verify_release_pr_scope` compares against the local `origin/main` without a fetch; a stale base only
  yields a PR on an older main, which the merge train re-checks. Unchanged reviewed behaviour.
- R3 (observation): the receipt records `release_pr_opened` only when the receipt is this version's; with no receipt
  nothing is written. Fine: resume finds the PR by branch.

Tests (through `sm gate run`): `test_native_release_pr_open.py` and the rest of the release suites, see the commit
message of this verdict. NOT verified: a live push or PR creation (no GitHub writes in this review).

REVIEWED: sha256=9af0b9aac389ab91f3273ae70b19c338f072811cf351cb6d596a6e550221d2fd

VERDICT: APPROVE

## Independent re-review, campaign group 5 (integration/sm-complete at 479a8e8, 2026-11-13)

Reviewer wrote none of this code. Own worktree on review/campaign-g5; digest recomputed with `uv run sm self reviews`; every file of the unit diffed 1d69c43..479a8e8.
- Same code as the approval at 9af0b9 (R1-R3 there still hold); the digest changed with the shared unit and core.
- R4 (minor, not blocking): `--open-pr` returns before `rejection_guard(version)` in main(); a published rejected
  release is still refused by the tag check, but a version with rejection history and no tag is not checked here.
  Cheap to add the guard before `open_reviewed_release_pr`.
- Core files in the digest (engine, native, config, models, protocols, helpdoc, defaults, hookspecs, plugins, runlock,
  window): typing, the `Environment` model (only reader of os.environ), review-stop file/sha256 in the report, a
  `running` state with pid before a step acts, `_supersede` of an older run of the same argument (marked, never
  deleted), `native.call`/`gate_problem` (the same review gate for pipeline steps), a promote switch makes write
  commands exit 75 with nothing run, transitive import digests, `gate run --exclusive` (queues as priority 2, bounded
  by a 30 min default wait, starts only with no heavy slot busy). Nothing here weakens a gate of this unit.
- runner.py `bound_run`/`dispatch`: an unmarked dispatch is bound to repo, workflow, event workflow_dispatch and the
  head SHA resolved before the dispatch; two candidates stop (never guessed); rejects and the adoption are recorded;
  an adopted run is re-read by ID and its identity re-checked. Receipts from before the change (no `_dispatch`) keep
  the old path. N1 (minor): if the ref moves between the SHA read and `gh workflow run`, the run is rejected and the
  step waits to its timeout; a resume then dispatches again (the compose queue wait keeps it serial). Visible, bounded.
- compose replacement (`compose_fix_landed`): only when plugins main moved past the failed run's head (compare
  status ahead) AND a plugin whose pyproject version changed is on PyPI; once per failed run (compose_replaced),
  the failed run kept in the receipt; waits for the compose queue. Fail-closed on every read error (returns None,
  the old Stop).
- `release plugin`: stamps go to their own receipt (`hz-release-plugins.json`); refuses before any write while the
  core receipt still holds stamps; a success run without the version on PyPI is polled for at most 5 minutes.
- hzlib.gh / merge_train.gh: every gh call bounded (120 s reads, 300 s writes, exit 124 on timeout); a failed
  `gh pr merge` is read back (merged / not merged / unknown), unknown keeps the row queued with an alarm and stops
  the batch; the check-settle wait is bounded (80 x 45 s). Release merge path unchanged otherwise (its candidate
  guard stays the pre-merge guard).
- queue_daemon: flock around every load-change-save, per-process tmp file, an unreadable state file refuses (never
  read as empty), the main-plan check before a core merge and at `queue add`, needs-work re-check only at the
  unchanged pinned head; `__main__` blocks removed (no second way in). job_proof: a success job with empty steps is
  accepted only with run id, attempt 1, a runner id and a non-empty log of that exact job.
- N2 (test, not code; owner: the queue unit): `tests/moved/test_native_queue_recheck.py::test_a_green_needs_work_row_
  at_its_pinned_head_is_requeued_and_merges` FAILS at 479a8e8: it expects the train kwargs without the
  `pre_merge_guard` that the merged main-plan check (fix/stale-main-plan) now adds for core. The code is right; the
  test's expectation must take the guard. The integration head is not green until it does.
- Tests through `sm gate run -- uv run pytest` at 479a8e8: tests/test_native_release_*, test_pipeline_*, test_native_rehearse*,
  test_native_lock*, test_native_queue_*, test_native_go*, tests/moved/*release*|*queue*|*rehearse*, core test_core_engine,
  _native, _runlock, _window: 1 failed, 2330 passed, 24 skipped, 1 xfailed. The one failure is
  moved/test_native_queue_recheck.py::test_a_green_needs_work_row_at_its_pinned_head_is_requeued_and_merges (stale kwargs
  expectation, see N2 in release-resume.md); no test of this unit's own paths fails. NOT verified: any live GitHub write.

COVERS: release pr
REVIEWED: sha256=9c5f2884b1eda8cf0398f4c18853f569a111eb978e6ad6bcb30fe08089384f48
VERDICT: APPROVE

## Independent re-review, re-verdict campaign g5 (integration/sm-complete at 2d0877b, 2026-11-13)

Reviewer wrote none of this code. Own worktree on review/reverdict-g5; digest recomputed with `uv run sm self reviews`
(equals the campaign list); every file of the unit diffed 479a8e8..2d0877b.
- Follow-up confirmed: `rejection_guard(args.version)` runs before `open_reviewed_release_pr` (runner main(), after
  the global lock, the receipt check and the unfinished-drop check, before any gh call; also on --dry-run). Test
  `test_a_rejected_release_is_refused_before_the_pr_is_opened`: exit 1, the rejection message, no recorded call.
- `has_tag` now answers True only on gh rc 0 and False only on a 404/Not Found JSON body on stdout (checked against a
  real `gh api .../git/ref/tags/MISSING`: rc 1, that body on stdout); a timeout (124), empty, non-JSON or 5xx reply
  raises Stop. Before, a timed-out read meant "no tag" and the step acted. N3 (minor): the `tag` wait step and
  the `X or has_tag` checks now stop on one transient 5xx instead of polling on; a resume continues (fail-safe).
- Shared queue_lib changes in the digest (queue unit owns them): bounded gh reads in flakes_read, a bounded ruleset
  PUT in merge_gate (timeout = unknown, returns False), a bounded merge-freeze wait in merge_train (60 polls, then
  NOT merged, the row requeues), queue_daemon: main-rerun refuses when Core's focused jobs would run on attempt 2, and
  the main-plan gap is checked at the pinned head (`guarded_head`, defined before use). None touches the release PR.
- Shared core files changed 479a8e8..2d0877b (engine.py, native.py): `_supersede` now takes the older run's own
  lock (`PIPELINE.KEY`, non-blocking flock; Busy is caught by the caller and reported as waiting, nothing half-marked
  matters since the marking is idempotent); `gate_problem` honours `exempt=` only for paths on the core allowlist
  `EXEMPT_PATHS` = {`gate run`} (narrows the gate, never widens it); `switch_pending` ignores a promote pid file only
  when `ps -o etime=` proves the pid started after the file's UTC time (+2 s), and holds (fails closed) when either
  time is unreadable; clock.now() is float per the Clock protocol, Commands.run returns Result(rc, out). Nothing
  here weakens a gate of this unit.
- Tests through `sm gate run -- uv run pytest` at 2d0877b (release, pipeline, rehearse, lock, queue, go, dispatch-focused,
  moved release/queue suites, core engine/native/runlock/window): 2496 collected, 0 failed, 0 errors, 25 skipped (N2 of the previous round is fixed at 2d0877b). NOT verified: any live GitHub write.

COVERS: release pr
REVIEWED: sha256=738b0d91f8df9c3ee710a54f63218d1afb16c3c4e54508d1d6b52cfd342edc97
VERDICT: APPROVE

## Independent re-review, re-verdict campaign g5, release plugin fix (integration/sm-complete at 539b7f7, 2026-11-13)

Reviewer wrote none of this code. Own branch review/reverdict-g5-ovvg from 539b7f7 (contains fix/release-plugin-runid 234dc8d, bead
heinzel-management-ovvg; no code change between 60a83a5 and 539b7f7); digest recomputed with `uv run sm self reviews`; code diffed 2d0877b..60a83a5 (release.py,
release_lib/runner.py only). The 479a8e8..2d0877b delta of this shared unit is reviewed in release-pr.md (previous section).
- This unit shares release-resume's 50 files and digest. Its own path is unchanged by the fix (the fix touches only
  `release_plugin` and its new helpers, and main()'s `--redispatch` argument check); details in release-resume.md.
- Tests through `sm gate run -- uv run pytest` at 60a83a5 (code-identical to 539b7f7; the same 85 files as before): 2506 tests, 0 failed, 0 errors, 25 skipped; the 10 tests of test_native_release_plugin_runid.py all pass.
  NOT verified: a live dispatch or any live GitHub write.

COVERS: release pr
REVIEWED: sha256=a941feb6c2bcb671ac90d401c0fc84ff61333b715b8ed97fb279380c192cfe8d
VERDICT: APPROVE

## Digest-order re-verdict (integration/sm-complete at 7704ac6, 2026-11-13)

Independent reviewer; wrote none of this code. The only code change since this file's previous verdict is
`git diff 186f7d0 7704ac6 -- src plugins/heinzel/src`: `_imported` in src/steuermaschine/native.py now sorts the
imported files by `_package_path` (the path below the directory holding the top-level package) instead of by
absolute path. 539b7f7..186f7d0 changes no code (review files, plus `review:` in the commit check's allowed prefixes in
pyproject.toml). The bytes and labels of this unit are unchanged; only the order of imported files in the digest moved.
Checked: every package directory in both trees has `__init__.py`, and each top-level package has exactly one root, so
two files never share a key. The order is total and does not depend on set iteration. A git-archive, non-editable
install in a scratch venv gives the same digest as the checkout for all 74 commands, also under PYTHONHASHSEED 1 and
77. The previous verdict stands.

COVERS: release pr
REVIEWED: sha256=a217e6dee8135cefef79a04267448a15b6d64a54d3b497a43e5ac47bc639f121
VERDICT: APPROVE

## Campaign re-verdict g5 (2026-11-14, independent; reviewer wrote none of this code)

Target integration/sm-dod at c36bbe1, against the live baseline 903a1b7. `uv run sm self reviews` in a fresh worktree
gives the digest of the campaign list. Read `git diff 903a1b7 c36bbe1` for the unit's files and the shared core
(src/steuermaschine: statemove.py, config.py child_environment, native.py, plugins.py; plugin state.py, environment.py,
queue_lib edge/hzlib/queue_daemon/merge_train/release_flow).
- State cutover: every release state path now goes through `state.path(NAME)` (or `state.root()` for the admission
  runtime's `cache`). Checked in an isolated HOME (scratch HOME and STEUERMASCHINE_HOME, never the live state): an old
  ~/.cache/hz-release-plugins.json and hz-release.json refuse with MigrationError (exit 70, names
  `sm self migrate-state`); after `statemove.forward(state.moves(...))` the new names are read and the old names are
  links; an old name that is written again as a real file (split state) refuses at the next read.
- Tests through `sm gate run -- uv run pytest` by explicit file (45 files: the release, release-admit, rehearse,
  release_cut pipeline and admission-receipt tests, plus tests/test_core_statemove.py and tests/test_core_migrate.py;
  this repo has no xdist, so without `-n 2`): 1188 tests, 1185 passed, 3 skipped, 0 failed, exit 0.
  NOT verified: any live release, rehearsal, dispatch or GitHub write; the live `sm self migrate-state` pass.
- Delta: `publish_release_pr` and `run_release_script` hand children `child_environment(...)` (same content as
  `os.environ | ...`); temp dirs renamed. The opened PR is still checked for no auto-merge request
  (runner.py:1084-1088); the merge goes through merge_train with `full_candidate_guard` (attempt 1 of the full
  candidate run); no `--admin` and no auto-merge anywhere in release_lib or merge_train. merge_train reads FREEZE and the
  tail directory from the state directory. The runner.py:2775 hint defect (see release-drop) is not on this command's
  path.

COVERS: release pr
REVIEWED: sha256=0d5c8f6f59530f3584e321ddb59738692cb3bdbff3728fd54ecd8b76fbb49d6e
VERDICT: APPROVE

## Re-verdict at abc64a4, group B (2026-11-14)

Independent reviewer; wrote none of this code. Findings, baselines and tests: ~/Code/packets/sm-reverdict-abc64a4/B.md (recorded verbatim by the integrator).

COVERS: release pr
REVIEWED: sha256=62dace46b549abcec99680095be4a302347cc24c548253bb3610a67ad0b9e549
VERDICT: APPROVE

## Change for review: which PR is the release PR (bead heinzel-management-5j8d, branch feat/sm-release-recut)

Written by the builder; the reviewer adds the COVERS, REVIEWED and VERDICT lines. NOT approved.

`release_pr(tag)` in `release_lib/runner.py` took `gh pr list --state all` entry [0]. Now: the OPEN PR; with more than
one open PR it refuses (`Stop`, naming the numbers); otherwise the MERGED one; a CLOSED PR (of a dropped candidate) is
no release PR and gives None. Effects: `release resume` opens a fresh release PR after a drop ("release PR open" is
false for a closed-only branch) and `sm release pr` no longer refuses on a closed one (its message no longer
points to `sm release drop`); the merged check reads MERGED as before; `merge_release_pr` no longer indexes None
after a failed train. Hazard 1 of docs/RUNBOOK-release-cut.md (stale closed PR) is gone. Check: no caller needs a
CLOSED PR (all six callers read; the drop has its own GraphQL query). Tests: test_release_packet_recut.py.

Delta after the review of 1c8764a (R2), NOT approved: `release_pr` requests `isCrossRepository` and ignores cross-repository
PRs, so a fork PR named release/vX is never the release PR (test_release_packet_recut.py).

## Independent review of feat/sm-release-recut at 0cfb0ae (2026-11-14)

Independent reviewer; wrote none of this code. Findings, probes and tests: ~/Code/packets/sm-release-recut/REVIEW.md and REVIEW-2.md (R1, R2 fixed; advisories open).

COVERS: release pr
REVIEWED: sha256=0bb6db776bfdfb9124f579b46488741dcda38a8b77a1466f3cad43f96cf5dcad
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Recut R2: release_pr filters cross-repository PRs and ignores CLOSED ones (a dropped candidate's), so `sm release pr` can open the PR of a recut; the old hint to drop first is gone because drop now archives. Shares release.py's digest with drop/resume/repair-receipt. Approve.
COVERS: release pr
REVIEWED: sha256=b1d5d5f5226d59fc09a60cad53a998ed18f01c71c852e1329d6401c1d6a0464a
VERDICT: APPROVE
