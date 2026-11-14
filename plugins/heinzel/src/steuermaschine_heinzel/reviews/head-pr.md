# Port review: `sm head pr`

The port review of this command is one file with the other commands of its unit: [head-ship.md](head-ship.md)
(sections 1 to 5). Independent review (second round, export of 11e5de1): section 6 of head-ship.md and `~/Code/tmp-sm-reviews/ship2/REVIEW.md`.

## Re-issued verdict (2026-11-13, branch chore/sm-clean, reviewer wrote none of the code)

Help text only since the previous verdict (sha256=9cf87db5a3413912dfe6bcdbe623a051a7e314bb3af4c8a6466063b3e5bac7ac, approved on main at f9134cc).
The whole unit diff from the merge base f9134cc to 6acd0b4 is native/ship.py: `help=` added to the PR argument of `head_ship` (same unit as head ship).
No default, type, metavar, option name or body line changed; the digest at f9134cc recomputes to the old
REVIEWED value, so nothing else in the unit moved.

COVERS: head pr
REVIEWED: sha256=549a3c6ee97c32d90b95498338ab93a9944a316f2b508dc955e14e9af2b18e38
VERDICT: APPROVE

## Independent re-review of fix/batch-release at 61dcce0 (against f9134cc)

Reviewer did not write any of the change. Files of this unit that changed since f9134cc (`sm self reviews --format json`
files x `git diff --name-only f9134cc..61dcce0`): `queue_lib/job_proof.py` (new) and `queue_lib/main_checkpoint.py`
(+3 lines). Nothing else in the unit moved, so queue/ship behaviour is otherwise unchanged.

- `job_proof.needs_log` (`queue_lib/job_proof.py:24-37`) adds a step only for a dict job with int `id` > 0, the same
  `run_id`, `run_attempt == 1`, `status == "completed"`, `conclusion == "success"` (so cancelled, skipped, failure,
  neutral are left alone), int `runner_id` > 0 (0, None, "7" are left alone) and `steps == []` exactly.
  `log_proven` (`:40-50`) then requires a non-empty log after `strip()`; a `ValueError`/`OSError` (the gate's
  `CandidateError` is a ValueError, `release_candidate.py:44`; `main_checkpoint.require` raises it, `:51-53`) leaves
  the job unchanged. Any other exception (e.g. a subprocess timeout) propagates and refuses the whole gate: fail closed.
- A job left unchanged still meets the pinned check `job["runner_id"] > 0 and job.get("steps")`
  (`release_candidate.py:434-437`, file not changed); a proven job still passes all other checks of `validate`
  (inventory names, run, attempt, head SHA, conclusion) unchanged.
- Used in exactly two places: `main_checkpoint.py:302` (merged candidate anchor of the queue's main checkpoint,
  reads `actions/jobs/{id}/logs` through `download` with the 128 MiB bound `:48`) and
  `release_lib/candidate_policy.py:58` (release gate, `job_log` `:73-78`). The isolated focused reader
  (`main_checkpoint_reader.py`) does not import it.
- Not blocking: a non-empty log is a weak proof on its own (GitHub writes runner setup lines before any step);
  the weight is carried by runner_id > 0 + success + attempt 1, as bead 7d8e specifies. `candidate_policy.job_log`
  reads the log with `capture_output` and no size bound (memory only, not a gate weakening).
- Tests run (`sm gate run -- uv run pytest -q ...`): go live-next/views/workload-stale, queue commands/edge/main
  commands, ship commands, train commands, candidate empty-steps, compose refresh, plugin PyPI, release pr open,
  run binding, plus 18 moved queue/train/checkpoint/ship/candidate/go tests: exit 0, no failures. `sm self lint`: pass.

COVERS: head pr
REVIEWED: sha256=39626f0355ec1d63e787f05bf3f714ffafb37d6ce7d55e8b43254145287a8680

## Independent review of the batch-gate changes (fix/batch-gate at d1c7bbe, 2026-11-13)

Reviewer did not write them. Commits 9f758f6 (bead 7et), ba73009 (jvgn, the ship side), 0f94953 (cdx9).

- Freeze WAIT (push_gate.py:40-48, 302-319): only when the moved gate said OK, no waiver (`minimum`) and
  `merge_frozen()` (incident file or any release hold; an unreadable hold directory counts as a hold, hzlib.py:35-47).
  The freeze is local, so this part fails closed; a branch whose PR lookup fails is not a fix (WAIT). The old gate
  runs first unchanged: nothing that was WAIT before becomes OK.
- Not blocking, S1: `is_fix` uses `startswith(NEVER_HELD)` with NEVER_HELD = ("release/", "bump/", "main")
  (push_gate_rules.py:95), so a branch named `main-x` or `maintenance/x` passes the freeze. Use exact "main".
- head pr (pr.py:410-411, ship.py:170-174): a focus worktree or a release/* branch pushes with
  `gate run --max-wait 1800 --priority 2 --exclusive`; the wait stays bounded by head_pr_lock_max_wait_seconds and
  75 still means nothing pushed. The checks before the push are unchanged; the push is still SHA:refs/heads/BRANCH.
- cdx9 (ship.py:44): the old `hz-gate make pr` now reads `sm head pr`. The core repository's guard_bash.py:17,21
  still suggest make pr (not in this repository).
Tests: affected suites at d1c7bbe 290 passed, 2 skipped; `sm self lint` green.

COVERS: head pr
REVIEWED: sha256=7f5f08e9b3e6501546235a587cf943dcb75f83685e9ad806590cef3239cac8d2
## Independent review (feat/repair-main 7fc817a, base main 5c87f14)

Reviewer: independent; wrote none of this code. Unit recomputed by `sm self reviews`. Stale only because it shares
queue_lib/queue_daemon.py: diffed every file of the unit against 5c87f14; the only change is the `sm queue main-rerun`
repaired-plan path (queue_daemon.py:501-507, :514-519, :579-583, :650-669, :679-682; see reviews/queue-main-rerun.md).
No queue add/drop, daemon tick, ship, PR, release or train code path changed; the restored-by-rerun result keeps its
shape (one optional `repaired` key). Related suites and `sm self lint` pass through `sm gate run`.

COVERS: head pr
REVIEWED: sha256=237fa21ec1117daf028d1cc8bab0da5534e7011771340a1441e040100afba2cf
VERDICT: APPROVE

## Independent review of integration/sm-complete at 479a8e8 (against live 1d69c43, campaign 2026-11-13)

COVERS: head pr
REVIEWED: sha256=81c100085154012f24f81c4896db2cb98216d84cc62e52f7b2f757305fede0d2

Reviewer: independent (campaign group 4, branch review/campaign-g4); wrote none of this code. Digest recomputed with
`uv run sm self reviews` at 479a8e8. Diffed every unit file 1d69c43..479a8e8. Ship-specific changes: ship.py (help
text, the make-pr name now translates to `sm head pr`, is_focus), ship_lib/ship.py, pr.py, deps.py, push_gate.py,
push_gate_rules.py; queue_lib and core as read in reviews/daemon-queue.md (same date).
- ship: an unreadable queue state refuses (exit 1) instead of being read as an empty queue. CLEARED only when the same
  repo's row was queued at the PR's current head (guard_cleared binds repo + reviewed/guarded head; an unreadable head
  is not cleared, the guard runs). PASS -> `queue add`, which guards again and binds the verdict to the head it read
  (F2 closed) and refuses a core PR without a focused plan on current main; nothing is queued on a refusal.
- head pr: a focus head or a release/* branch pushes via `gate run --priority 2 --exclusive` (bounded by --max-wait and
  the run lock's 30-min exclusive default); the push is still `SHA:refs/heads/BRANCH`, never forced; every check runs
  before any push or PR as before.
- head push (read-only): during a merge freeze a non-fix PR WAITs (local freeze file, keep list, known-failures fix
  PRs); `main` is now matched exactly (main-x is held). The branch lookup has a 60 s timeout and an error means "not
  a fix" (WAIT): fails closed.
Findings, not blocking: ship's `gh run list` in-flight count still has no timeout (pre-existing; a read that only
prints a NOTE); is_focus reads the focus list from the store without error handling (a broken store raises before
anything is pushed).

Tests (through `sm gate run`): test_native_ship_commands, moved/test_native_ship_push_gate, test_native_preflight*,
test_native_queue_commands, moved/test_native_queue_guard_and_neighbours, test_native_lock, test_native_admission_ops:
pass. The one red test on the branch (moved/test_native_queue_recheck.py) is the queue tick's (daemon-queue).

VERDICT: APPROVE

## Independent re-verdict of integration/sm-complete at 2d0877b (against 479a8e8, campaign 2026-11-13)

COVERS: head pr
REVIEWED: sha256=39d657593e0c6eee207c48fe38fa51b2c40f47268f56c89fe2bd00e3fa1e11c0

Reviewer: independent (re-verdict campaign group 4, branch review/reverdict-g4); wrote none of this code. Digest
recomputed with `uv run sm self reviews` at 2d0877b (integration/sm-complete, frozen). Diffed every file of the unit
479a8e8..2d0877b (the previous campaign review). Changed in the unit: queue_lib/queue_daemon.py, merge_train.py,
merge_gate.py, hzlib.py, flakes_read.py, native/queue_main.py, ship_lib/ship.py; core engine.py and native.py (shared).

Follow-ups of the 479a8e8 review, verified:
- Recheck test: moved/test_native_queue_recheck.py now strips `pre_merge_guard` from the compared kwargs, still
  asserts `{queue_core: True, expected_head: PIN}`, and asserts the guard is callable on every merge. Passes.
- Pin to the plan: `plan_guard` calls `main_plan.gap(pr, reviewed_head or guarded_head)` (the same pin the tick sets
  as expected_head); `queue add` calls `gap(number, guarded_head)`, the head read before and after the diff guard.
  Covered by tests/test_native_queue_pin_bounds.py (both cases).
- Timeouts: ship's run-list count goes through `hzlib.gh` (120 s, exit 124 -> counted as 0, as before); train's
  freeze wait is bounded at FREEZE_POLLS=60 (then `NOT merged`, the row turns needs work with "requeue once it
  lifts", lock released); `merge_gate.write_ruleset` PUT has GH_WRITE_TIMEOUT and returns False on timeout (state
  "unknown, read it again"); `flakes_read.gh` has GH_READ_TIMEOUT and returns "". `gh_timeout` now classifies
  `gh api` with -f/-F/--field/--raw-field/--input and no -X as POST (300 s), and reads `--method=X` / `-XPUT`.
- main-rerun (bead 4yen): `main_rerun_plan` refuses before any write when `rerun_refusing_jobs` names a focused job
  that would run again (any non-green focused job, or every focused job of a repaired main), and the refusal names
  `sm queue add --fix-forward --for "JOB=BEAD" PR`. The help text says the same. Dry run and live refuse alike; no
  `gh run rerun` is called (test_native_queue_repaired_main).
- Core: the supersede pass now takes the older run's own lock before it reads and writes it (Busy -> WAITING);
  switch_pending ignores a pid reused after the promote wrote its file and fails closed when the start is unreadable;
  `exempt=` counts only for `gate run`. None loosens the review gate for this unit.

Findings (none blocking):
1. rerun_refusing_jobs treats a `skipped` focused job as green unless the main is repaired. If a KNOWN failure sits in
   a job a focused job depends on, `rerun --failed` would start that skipped focused job on attempt 2 and it would
   refuse: one wasted rerun (bounded: never twice per run, nothing merges on it). Could refuse skipped focused jobs
   whose needs include a failed job.
2. A freeze that outlives 60 min turns the row needs work, and recheck_needs_work requeues only "checks ..." details:
   the operator re-ships it. Fails closed; acceptable.
3. Residual race main-moves-between-gap-and-merge (finding 2 of 479a8e8) stays accepted; GitHub has no base pin.
This path: ship_lib/pr.py and preflight are unchanged in the delta; only shared queue_lib/ship.py/core moved.

Tests (through `sm gate run`, one pytest run at 2d0877b): every plugins/heinzel/tests/test_native_{queue,ship,
preflight,rejudge,admission_stuck}*.py, moved/test_native_{queue,ship}*.py (recheck included), tests/test_core_engine,
test_core_native, test_core_preflight: exit 0, about 970 tests, 23 skipped, none failed.

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

COVERS: head pr
REVIEWED: sha256=891cdff57b562e251f8f3de505303fda5eb1792c9e4e6ec9dc9f78b594a22c94
VERDICT: APPROVE

## Campaign re-verdict g4 (2026-11-14, independent; reviewer wrote none of this code)

Reviewer: independent (sm-dod campaign group g4, branch review/campaign-g4-sm-dod); wrote none of this code. Target
integration/sm-dod frozen at c36bbe1; baseline 903a1b7 (live, every unit approved). The digest was recomputed with
`uv run sm self reviews` at c36bbe1 and equals the campaign list. Read: `git diff 903a1b7 c36bbe1` of native/queue.py,
queue_main.py, ship.py, every queue_lib and ship_lib file, merge_tier.py, state.py, environment.py, and the core
statemove.py; Core's judge read at Core 64a7b0d2f (ci/desktop-focused-0942) and at Core origin/main.

What changed in this unit since 903a1b7:
- State paths: queue.json/.lock/.log, alarm.json, merge-freeze, release*, ci-deferred.txt, note.lock move from
  ~/.cache/hz-* into the state directory (state.path). A path whose state is still at its old name raises
  MigrationError (statemove.require_migrated), and so does an old path that an older release writes after the move;
  the freeze check and the train's FREEZE therefore fail closed (the tick stops), never open.
- HZ_* environment reads become typed settings with the old variable as the compatibility source (queue_state_file,
  queue_known_failures_file, queue_main_batch_publish, queue_interventions_log, queue_release_flow_dir,
  queue_release_tag_on_main, alarm_*, head_push_minimum). The two-state-files refusal now reads
  environment.state_overrides(). Equivalent behaviour; the push waiver keeps the old meaning (0 waives, as `isdigit`).
- Old names in printed lines (hz-queue, hz-ship, ...) are now sm commands in the moved code; `_translate` became
  the identity. hzlib.load takes module names (`diff_guard`, `merge_train`, `merge_gate`, `flakes_read`).
- `queue add/drop` refuse a word that is no PR (exit 2) before the moved code; tested.
- Merge tier (Core ADR 0149), commit 13f5f72: hzlib.merge_tier() reads main's scripts/focused_plan.py through
  merge_tier.active(MERGE_TIER_MODE) (bound from the setting merge_tier_mode, default auto; an unreadable main keeps
  the older rules). main_state: a cancelled run replaced by a newer push is not red, and a green rerun of the pinned
  red run repairs main; both are gated by tier(). main-rerun itself only changed its note text ("releases still need a
  first-attempt full green"); restored_by_rerun lifts the hold for merges only, and the train's release gate still
  needs attempt 1 (hzlib's train gate classifies attempt N>1 by KNOWN failures). The release candidate check is
  Core's release_candidate.py (`run_attempt == 1`), unchanged.

Also in ship.py/ship_lib: the setting head_push_minimum (old HZ_PUSH_GATE_MIN) waives the release hold; the default
of head_pr_lock_max_wait_seconds is read from the model field; push_gate_rules.auto_defer_plan now names
`sm ci defer --if-main-starved --minutes N` (no caller in src; without --cancel that command does not cancel, a note
only). Nothing in these changes loosens the push check, the guard or the queue pin.

Finding (blocking for this verdict):
1. plugins/heinzel/src/steuermaschine_heinzel/native/queue_lib/main_batch.py:112: `valid_run` now accepts
   `run["run_attempt"] >= 1` without any merge-tier gate. The commit message says the change is gated on the detected
   capability, but it is not: with the Core at main today (origin/main scripts/main_batch.py:110 still requires
   `run_attempt == 1`), hzlib.resolve_main_runs (hzlib.py:371) now resolves a delegated batch admission to a final
   run that is green only on a rerun, where the older rule left it `batch_unknown` (main_state "unknown", merges
   held). That changes queue behaviour with an old Core, which the brief and Core ADR 0149's rollout rule exclude.
   The train gate still classifies attempt N>1 by KNOWN failures, so the impact is bounded, but the invariant is
   broken and the tests pin the ungated rule (tests/test_native_queue_cov_a_main_batch.py:88,
   tests/test_native_merge_tier_rules.py:94). Fix: accept attempt > 1 only when hzlib.merge_tier() (or the merge-tier
   mode passed in) holds, keep `== 1` otherwise, and test both Cores.

This unit's digest carries the same queue_lib bytes (UNIT includes queue_lib; head pr ends in the ship step, which
queues through queue add). The finding is shared, not specific to this command; its fix changes this digest anyway.

Tests: `sm gate run -- uv run pytest` over the 32 files plugins/heinzel/tests/test_native_{queue,ship,merge_ledger,merge_tier}*.py and moved/test_native_{queue,ship}*.py at c36bbe1, one run, exit 0, none failed. (`-n 2` is refused: no pytest-xdist in this repo, so the run was serial.)

COVERS: head pr
REVIEWED: sha256=92742c3f02eb8f4eac190d1678a0c881fda3afa0b0fb58a686c0151b8d03a204
VERDICT: CHANGES (finding 1: main_batch.valid_run accepts reruns without the merge-tier gate)

## Re-verdict at abc64a4, group A (2026-11-14)

Independent reviewer; wrote none of this code. Baselines, shared core and tests: ~/Code/packets/sm-reverdict-abc64a4/A.md (recorded verbatim by the integrator).

- g4 finding 1 closed: `main_batch.valid_run(..., reruns=False)` accepts only `run_attempt == 1`; `reruns=True` is
  passed only from hzlib.resolve_main_runs when `merge_tier()` holds (read once, lazily). The other callers
  (main_batch.py:296, queue_daemon.py:1373) keep attempt 1.
- R5: `queue_records_dir` defaults to None; `edge.records_dir()` refuses (Refuse naming the key) instead of a home
  path. Fail-closed; the Mini's migrate-values sets it.
- queue.py: `_main_proof` after each tick starts the gated `queue main-proof` detached once per green main commit
  (daemon-queue's concern; it does not touch the pr/ship path). edge.py plugin lookup by name (R1). Preflight delta
  as in head-admit (incl. the NOTE). ship.py/ship_lib unchanged.

COVERS: head pr
REVIEWED: sha256=3880ded1cd92ead5cb9d3e5c888a96160c44afd580cb53ace3f1fd8dae9a1d9f
VERDICT: APPROVE

## Re-verdict at feat/sm-next-admission 5fe179a (delta review 4, 2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-next-admission/REVIEW.md, delta review 4 (recorded verbatim by the integrator; COVERS from `self reviews`).

COVERS: head pr
REVIEWED: sha256=88659aec311198f030d55a1f00b84d91f0dc025d78150762e1ee05d297d5d4ca
VERDICT: APPROVE

## Delta review: feat/sm-gate-lanes d94de45 (independent reviewer)

Changed in this unit since the last verdict: src/steuermaschine/runlock.py (light lane, rerun refusal); plugin native/lock.py (passes git_tree_state, setting text). Reviewed in full in two rounds (e6db9d9, d94de45),
probes now tests; see ~/Code/packets/sm-gate-lanes/REVIEW-2.md. Nothing in this command got weaker.

REVIEWED: sha256=1f954fb45a11590bdf95f49e37cc1b1066487919631a100e4395399e41397271
VERDICT: APPROVE

## Re-verdict at a28f1a7 (preflight parity, bead 98bw)

Independent reviewer; wrote none of this code. Delta: native/preflight.py and preflight_lib/head.py gained the steps
`types`, `standards` and `related` (head.py, ~230 lines); ship.py, ship_lib, queue_lib, queue.py are unchanged.
Read in full, three rounds of findings (~/Code/packets/sm-preflight-parity/REVIEW.md): the commands run as argument
lists through the run lock; the type checks are the hook's own (TYPES of its script, whole project, also for config
changes); the unit selection is Core's own related_tests.py, read from standard output only, ALL runs the unit suite or
fails, any other line fails, no fall-open (round 2 R5 reproduced and closed); an empty or unmatched standards set
fails; every fallback (no related_tests.py, unparsable TYPES, unmatched default glob, web head) warns by name.
Run for real against origin/main of Core: standards 9 files, related 18 hook tests plus 4 extras, mypy and pyright
clean, pyproject.toml answered ALL. Tests (parity, preflight, preflight_head) green through sm gate.
Waived by the coordinator (R6): a head touching web/ or desktop/ is warned, not failed.

COVERS: head pr
REVIEWED: sha256=3a7d4dfe3e1b934bd3c54081a7f7578581cfe56c0116100307a4cfd6aaa26bf8
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Three branches: next-admission (queue hold refusal, adopted PR ownership), gate lanes (lock.py), preflight parity (types, standards, related steps). Merge resolution of preflight_lib/head.py was imports only (shlex and tempfile both kept). The R3c frozen selection survives: new steps read c.sel (parsed from sel_bytes) only, scoped coverage still gets the frozen copy. Under gate lanes a failed related/types run is refused on an unchanged rerun (X1; preflight shows it as the step's failure with the gate's reason). Approve.
COVERS: head pr
REVIEWED: sha256=5f8c7119a1df2835544e4a2c198a242a3735dacfa1c0aba9b5042cb201719d49
VERDICT: APPROVE
