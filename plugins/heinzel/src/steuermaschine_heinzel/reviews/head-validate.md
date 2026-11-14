# Port review: `sm head validate`, `sm ci window run`, `sm ci window release`

One review unit for the three commands (stated on their registrations with `files=`): `native/window.py`,
`native/dispatch.py`, `native/dispatch_lib/`, `native/lock.py`, `native/lock_lib/`. The core modules they call
(`steuermaschine/window.py`, `steuermaschine/runlock.py`, `steuermaschine/github/`) cannot be named in a plugin's
review unit; they are covered by the core's own tests (`tests/test_core_window.py`, `tests/test_core_runlock.py`).

The first independent review (in `ci-one-dispatch.md`, of commit 0091e9d) approved `ci one dispatch` and listed what
`head validate` did not yet deliver. This file records what changed since, finding by finding.

| Finding | Change | Test |
|---|---|---|
| S1 a log read that times out failed a finished run | `focused.tally_of` returns an empty tally when the log cannot be read; the result of the run stands, cleanup runs | `test_a_log_that_cannot_be_read_in_time_does_not_fail_a_finished_run` |
| S2 the window counted only local receipts | `window.in_flight` counts as the start does: GitHub's inventory of active runs of the focused workflow plus tracked receipts; an uncertain inventory, a start in doubt (`.pending`) refuse before any push; an active release run or a full job ceiling waits (exit 75) before any push | `test_runs_in_flight_are_counted_from_githubs_inventory_and_a_full_set_waits_before_any_push` |
| S3 parallel lanes did not wait for a slot; half lane sets | every lane starts with `wait_for_slot=True` (the library waits up to 30 minutes per lane); a lane that still cannot start ends the set: started runs are cancelled, the branch is removed, the result is `incomplete` (exit 75, "nothing is wrong with the head"), never NOT VALIDATED | `test_a_lane_that_cannot_start_ends_the_set_...`, `test_validate_pushes_...` (asserts `wait_for_slot`) |
| S4 a second caller for the same head got `go` and released the first caller's window | a turn has an owner token (pid + random); only the owner is answered `go` again and only the owner leaves; a second command for the same head waits; cleanup is armed only after the push succeeded | core: `test_a_second_command_for_the_same_head_waits_...`; plugin: `test_a_second_command_for_the_same_head_waits_and_leaves_the_first_ones_turn_and_branch_alone`, `test_cleanup_reports_what_really_happened_and_is_armed_only_after_the_push` |
| S5 a killed holder kept the window for 180 minutes | (1) SIGTERM is turned into an exception for the time of a validation: runs cancelled, branch removed, window freed, exit 143; (2) liveness: a holder whose process is gone on this machine is passed at the next `enter`, and the passer is told which branch it left; (3) `sm ci window release [--force] [--remove] [--remove-branch NAME]`; (4) the "already exists" refusal names the removal command | `test_a_stopped_validation_...`, `test_a_holder_whose_command_is_gone_is_passed_and_release_ends_a_turn_by_hand`, `test_remove_branch_deletes_a_leftover_validation_branch_only_when_no_run_is_active_on_it`, core `test_release_...`, `test_process_alive_...` |
| S6 "removed" was reported when nothing was removed | `delete_throwaway` returns whether it deleted; the note says "did NOT remove" otherwise | `test_cleanup_reports_what_really_happened_...` |
| S7 the throwaway commit read HEAD a second time | `commit-tree <head>^{tree} -p <head>` with the head read at the start | `test_validate_pushes_...` (asserts the argv) |
| S8 "absent" for any failure of the exists check | `branch_state`: only a clear HTTP 404 is absent; anything else refuses | `test_an_existing_or_unreadable_validation_branch_refuses_...` |
| N8 the push had no time limit | 7200 seconds, as before the port | - |
| N9 a start in doubt was found only after the push | see S2 | see S2 |
| unit not stated | `files=("dispatch.py", "dispatch_lib", "lock.py", "lock_lib")` on the three registrations; `ci one rerun` names `review="ci-one-dispatch.md"` (same unit as dispatch) | `test_the_review_units_and_files_of_the_write_commands` |

Delta inside the unit that `ci-one-dispatch.md` approved (it is stale until the reviewer re-reads these lines):
- `dispatch_lib/focused.py`: `tally_of` (try/except, 4 lines), `delete_throwaway` (returns True/False, 2 lines),
  `cmd_dispatch` (`admit(start, arm, getattr(a, "wait_for_slot", a.wait))`, 1 line), two comments naming the setting.
- `dispatch.py`: settings `dispatch_state_file` and `dispatch_scratch_dir` default to the project's state home instead
  of a path of this machine; the core clone is the existing setting `core_repo_dir`; `ci one rerun` names its review file.

Not changed (the reviewer's S9-S12 and notes N4-N7, N10): the lease is wall-clock time and is not extended by a live
holder; a waiter that gave up keeps its place for `window_waiting_minutes`; `ci window run --run` validates heads
again after an exit 75; `ci one dispatch --throwaway` pushes outside the run lock (as the old tool did).

Switching from the old tool (the reviewer's condition): old and new must not run side by side. The old receipts file
holds rows without a result. The native side does not adopt them, and does not need to: its count of runs in flight
comes from GitHub's inventory, so runs the old tool started are counted while they are active, and they need no local
row once they finished. To close them on the old side before the switch: wait until `sm ci one status` and GitHub
show no active focused run, then stop every caller of the old entry point in the same step (the rehearsal included).

Never run against GitHub: no dispatch, re-run, cancel, push or branch delete was made by the port.

No verdict yet.

## Independent review (2026-11-13)

Reviewer: independent, wrote none of the code. Full findings with file:line and failure scenarios:
`~/Code/tmp-sm-reviews/dispatch/REVIEW.md`. Reviewed: `bcae212` (fixes on `3dfc6b8`, `f7fe1ab`, `9dbe78a`), read from
an export of that commit; the slice's tests pass there. No dispatch, re-run, cancel, push, branch delete or GitHub call
was made.

Unit (8 files): `native/window.py`, `native/dispatch.py`, `native/dispatch_lib/` (3), `native/lock.py`,
`native/lock_lib/` (2). Outside it, stated plainly: core `steuermaschine/window.py` (the line, owner token, liveness,
`release`, `sha_uses`), `steuermaschine/runlock.py` (the push), `steuermaschine/github/` (`Gh`, `GitHub.cancel_run`),
`steuermaschine/defaults.py`; and the plugin's SHA-guard decoder `admission_lib/focused_slots.py` with
`focused_admit.py`, `focused_resulting.py`, `focused_update.py` (inside the approved `head apply` unit, not this one).

Holds:
- No PR head SHA is dispatched: each lane goes through `cmd_dispatch` and its SHA guard on a fresh `commit-tree` child
  (never the head); an unreadable PR list, registry or branch list refuses before the pending marker and the POST, and
  a lane that does not start ends the set.
- (a) a log read that times out no longer fails a finished run.
- (b) runs in flight are counted from GitHub's inventory plus open receipts, fail closed; a `.pending` marker or an
  active release run stops before any push; every lane waits for its slot; a lane that cannot start cancels the
  started runs, removes the branch and exits 75. Only ids this command started are cancelled (see S5 for the one
  text-parsing gap in `--serial`).
- (c) a turn has an owner token; a second command for the same head waits and can neither leave the turn nor delete
  the branch; cleanup is armed only after a successful push.
- (d) SIGTERM cancels the known runs, removes the branch, frees the window, exit 143; a gone holder is passed;
  `ci window release` refuses a live holder without `--force` (a lease that ran out is released); only `validation/*`
  names are removed, and only while no run is active on them.

Should-fix (not blocking; S2 to S4 before unattended use):
- S1 `window.py:145` the job ceiling and the ARM limit are NOT checked before the push (only the release run is);
  the table above claims they are. A full ceiling costs a push, up to 30 minutes and exit 75.
- S2 `window.py:406-420` `--remove-branch NAME` never reads the window and drops the admission lock before the DELETE:
  it can delete a LIVE validation's branch between its push and its first start (or between serial lanes); the next
  POST then fails after the pending marker, pausing all focused starts until reconciled.
- S3 `window.py:290-299` one failed `gh run view` while waiting makes the head NOT VALIDATED ("fix in ONE new
  commit") and deletes the branch under a running run.
- S4 `window.py:76`, `:302` "cancelled" is printed whether or not the cancel happened; a SIGTERM between POST and
  receipt leaves an uncancelled run and a pending marker while saying the runs were cancelled.
- S5 `window.py:157` the run id is the LAST `: run N` match of the lane's output; in `--serial` a fail-log tail can
  supply a foreign id that a later cancel would act on. Take the first match or return the id.
- S6 name the decoder files in `files=` (changes the digest).
- S7 `window.py:383` an incomplete head is labelled NOT VALIDATED in `ci window run`.

COVERS: head validate
COVERS: ci window run
COVERS: ci window release
REVIEWED: sha256=ad508bcc2cccc173aa211097588fd4181fb0a9e90b981cbd5224fc35afa090b9

VERDICT: APPROVE

## Independent review of the inventory retry and kept lanes (2026-11-13)

Reviewer: independent, wrote none of the code. Reviewed: `b9c32b1` on `fix/inventory-complete` (bead
heinzel-management-8elp). Delta in this unit: `native/window.py`, `dispatch_lib/focused.py`. Tests of dispatch,
validate and admission pass (`test_native_dispatch*.py`, `test_native_admission_*.py`, `test_native_ci_read.py`,
`test_native_packet.py`); `sm self lint` exit 0. No dispatch, re-run, cancel, push, branch delete or GitHub call.

Holds:
- Retry: bounded to 3 whole reads of count and list together (`focused.py:130,150-156`); a persistent mismatch
  refuses as `Unreadable`. `in_flight` (`window.py:143`) only counts a consistent read; the window is now held while
  kept lanes are watched (`window.leave` in `finally`, `:321`), so the ceiling never undercounts.
- Only an `Unreadable` refusal keeps started lanes (`:284`, `:291-295`); any other refusal of the last lane resets the
  flag and cancels as before. A not-started row is skipped in the watch (`:297`).
- INCOMPLETE is never validated: `:324` yields `validated` only when not incomplete and every lane succeeded;
  `ci window run` maps anything else to NOT VALIDATED (`:390`); exit 75 / 1 (`:72`).
- Cleanup per the old contract: the branch is deleted in `finally` after the watch (`:314`); SIGTERM still cancels
  every started run (`:306-307`). Tests assert no `/cancel` call on the kept path and the delete after the watch.
- No new write path (the change removes cancels in one case); lanes still dispatch only on the `commit-tree` child
  through `cmd_dispatch` and its SHA guard, never on a PR head SHA.

Notes (not blocking):
- N1 `:323` `red` counts a kept lane whose watch failed to read (empty conclusion) as a verdict: a read error then
  reports NOT VALIDATED ("fix the head") instead of INCOMPLETE. Fails safe (never validated); the pre-existing S3.
- N2 `:76` `kept` says "kept" for a run whose cancel did not happen only if `cancel_runs` sets a conclusion other
  than `cancelled`; it always sets `cancelled` (`:201`), so the text is right today.

COVERS: head validate
COVERS: ci window run
COVERS: ci window release
REVIEWED: sha256=84d7f6d1ce78086ab0ef6f2fbb11762018d246b35bb55af1c5a69fd224533607

VERDICT: APPROVE

## Independent review of the make pr refusal (2026-11-13)

Reviewer: independent, wrote none of the code. Reviewed: `08ab268` on `fix/refuse-head-pr` against main `a7160ac`.
The diff touches only `native/lock.py`, `native/lock_lib/refuse.py` and `tests/test_native_lock.py`; nothing else in
the unit changed. `test_native_lock.py` and `test_native_guards_floor.py` pass; `sm self lint` exit 0.

Holds:
- Exact target match: `refuse.py:80-83` takes make words that are neither options nor assignments and refuses only
  when one equals `pr` (set membership, no prefix test). Probed: `make printenv`, `make pre-commit`, `make pr-check`,
  `make preview`, `make prs`, `make check-fast` stay allowed; `make pr`, `make -j2 pr`, `make NO_PREFLIGHT=1 pr`,
  `env NO_PREFLIGHT=1 make pr` and NO_PREFLIGHT=1 in the caller's environment are refused.
- `--allow-full` never admits it: `lock.py:96` skips the admit-and-log path when the reason is `NEVER_MAKE_PR`; other
  full suites still pass with `--allow-full` and are logged (test: `make preflight` admitted and logged).
- The route (`refuse.py:8-10`) names `sm head pr BEAD TITLE` then `--live`, never `make pr`; tests assert it.
- Tests cover plain, NO_PREFLIGHT=1 in the environment, and `--allow-full` with `env NO_PREFLIGHT=1 make pr` (refused,
  not in the allow-full log).
- No internal caller of `make pr` remains: `lock.py:72` and `ship_lib/push_gate_rules.py:91` only classify a command;
  `ship_lib/pr.py` states it does not run it. In heinzel `origin/main` only the Makefile target and hint text remain.

Notes (not blocking):
- N1 `ship.py:44` still rewrites old text `hz-gate make pr` to `sm gate run -- make pr`, a route that is now refused;
  better `sm head pr`. Outside this unit.
- N2 heinzel `.claude/hooks/guard_bash.py:17,21` advises `make pr TITLE=...` (contributor hint in the public repo).
- N3 `--allow-full` with plain `make pr` (no NO_PREFLIGHT) is not tested by itself; it is the same reason string and branch.

COVERS: head validate
COVERS: ci window run
COVERS: ci window release
REVIEWED: sha256=63ed44d760860394a656a71648f3fff44fe5229759c589b65f03d04b883fb357

VERDICT: APPROVE

## Delta for re-review: one ref per lane (bead heinzel-management-omyb, 2026-11-13)

Symptom: Core's `focused-job.yml` has `concurrency: focused-job-${ref}` without cancel-in-progress, and GitHub keeps
one PENDING run per group, so parallel lanes on ONE validation ref cancelled each other while queued (update-all lost
web-2 and cli). Core's workflow is unchanged; the fix is in `native/window.py`:
- each lane gets its own child commit (`commit-tree <head>^{tree} -p <head>`, the lane in the message keeps the SHAs
  apart, never the head) on its own ref `validation/<name>-<lane>-<sha8>` (`lane_branch`); every ref is checked absent
  first; all refs go out in ONE `git push --atomic` through the run lock; cleanup deletes each (`remove_lane_branches`);
- the window records the refs space separated; `ci window release --remove` and the "gone holder" note handle each;
- the window and cap accounting is unchanged (`enter(..., len(lanes), in_flight(...))`, one run per lane);
- a run cancelled by someone else (concurrency group or by hand) is `cancelled: queued|running` and makes the result
  INCOMPLETE (exit 75), never NOT VALIDATED; a real failure next to it is still a verdict (`take_result`, `verdict`);
- `--serial` uses the same per-lane refs.

Tests: `test_four_lanes_run_on_four_refs_of_their_own_and_cleanup_removes_every_one[False|True]`,
`test_a_run_cancelled_while_queued_is_no_verdict_on_the_head`, `test_release_removes_every_lane_branch_a_gone_holder_recorded`.

### Independent review of 4d8ede9 (base bb56886), 2026-11-13

COVERS: head validate
COVERS: ci window run
COVERS: ci window release
REVIEWED: sha256=bd08c543c0f29d5e2fca568e2ff1a637d1f7b51ca2bcaf38ba42ccd395d43636

Checked (native/window.py unless named):
- No lane on a PR head or a non-validation ref: names come only from `lane_branch` -> `throwaway_branch` (validation/ prefix,
  core window.py:119) at :266; the work branch may not be main, release/* or validation/* (:262); each ref is checked absent
  (:299); each commit is a fresh child of the head, never the head, all distinct (:230-238); every dispatch goes to
  `row.branch` (:326) through `branch_refusal` and `attachment_guard` (focused.py:627, :644).
- Cleanup: one `git push --atomic` through the run lock (:313), `pushed` only after it succeeded (:320); the finally removes
  every lane ref (:355, `remove_lane_branches` :241, each failure noted, the loop goes on); SIGTERM cancels the started runs
  and reaches the same finally (:348); `delete_throwaway` deletes only validation/* at the recorded commit (focused.py:614).
  A failed atomic push leaves nothing and refuses; the refs are recorded in the window before the push (:307).
- Accounting: `window.enter(..., len(lanes), in_flight(len(lanes), arm))` unchanged (:281), one run per lane, counted by
  workflow path, so the 5-run cap counts each lane once.
- Verdict (`verdict` :369, `take_result` :363): VALIDATED needs every lane started and none failed or cancelled; a run
  cancelled from outside is INCOMPLETE only when no other lane really failed; an unstarted lane is INCOMPLETE.
- The bb56886 `make pr` run-lock refusal is untouched (diff touches only window.py, this file and test_native_dispatch.py).

Findings (non-blocking, follow-ups):
1. `throwaway_branch` truncates the flattened name to 80 characters BEFORE the lane suffix is safe: a work branch of about
   75+ characters gives every lane the same ref (verified: `feat/` + 75 x). The atomic push then fails locally (duplicate
   destination) and refuses with nothing pushed, but the message blames the pre-push hook. Fix: truncate the branch part,
   then append the lane.
2. Pre-existing, not introduced here: with `--serial`, if a later lane cannot start, `cancel_runs` (:337) also overwrites an
   earlier lane's completed real failure with conclusion `cancelled`, so the result is INCOMPLETE instead of NOT VALIDATED.
3. Pre-existing: a SIGTERM during the push (before `pushed`) or during the cleanup loop can leave refs; the window record is
   dropped by `leave`, so only `sm ci window status`/a manual `--remove-branch` finds them.

Tests: test_native_dispatch.py, test_native_dispatch_focused.py, tests/test_core_window.py green (117); `sm self lint` green.

VERDICT: APPROVE

## Independent review of fix/batch-validate at 623899e (2026-11-13; commits d331d29, 3f3024c, ccb9318, cc2513a, 623899e)
## Independent review of `gate run --exclusive` (fix/batch-gate at d1c7bbe, 2026-11-13)

Reviewer did not write it (ba73009, bead jvgn). In this unit only lock.py changed (one option, passed to the core
Request); the mechanism is core runlock.py, read in full for this.

- The exclusive request queues as level 2 (runlock.py:378-381): normal and priority-1 runs do not start while it
  waits; it starts only when no heavy slot is locked, takes the exclusive file, counts again, then slot 0
  (runlock.py:416-431); while it runs every non-exclusive heavy start is refused (runlock.py:400). Never light
  (runlock.py:461). The load bar and the hold still apply (test pinned).
- No deadlock beyond the wait bound: flock is released on exit, so no stale exclusive file; a gated command under the
  exclusive holder runs at once (held env). If a heavy holder starts a gated child without the held env while an
  exclusive request waits, both wait until the exclusive request's --max-wait (head pr: 1800 s), then it gives up (75).
  With --max-wait 0 by hand that wait is unbounded: S1, recommend a default cap for --exclusive.
- Not blocking, S2: a non-exclusive priority-2 request still competes (turn=True) and can take slot 1 between the
  exclusive request's second count and its slot-0 lock (runlock.py:389-406, 423-426): exclusivity is best effort
  against explicit priority-2 runs, and a stream of them can starve the exclusive one until its bound.
- Not blocking, N1: runlock.py:396 and :400 call on_hold twice; if the hold disappears between the two calls an
  exclusive request falls into the normal branch and runs non-exclusively. Guard the elif with `not request.exclusive`.
Tests: test_core_runlock.py, test_native_lock.py, dispatch and window suites at d1c7bbe green (290 passed in the
batch, 2 skipped); `sm self lint` green.

COVERS: head validate
COVERS: ci window run
COVERS: ci window release
REVIEWED: sha256=bd9ac0e9832e97ed1d6d00eddb18043fd2aee836ec689847e2f5f7779c56773f

Checked against window.py at 623899e:
- INCOMPLETE is never VALIDATED: `verdict` (:422) returns validated only when no lane `failed`, `incomplete` is false and
  every lane's conclusion is `success`; an unread lane (`unread` set, :382, :393, :397) has no conclusion or keeps a
  non-success one, so it lands in incomplete (:427).
- A real failure never becomes INCOMPLETE: `LaneResult.failed` (:62) is status completed, conclusion not success or
  cancelled, result read; `verdict` checks it first (:425). `cancel_runs` (:264) now skips completed runs, so a
  `--serial` red lane survives a later lane that cannot start (old finding 2 fixed; test
  test_serial_keeps_a_real_failure_when_a_later_lane_cannot_start).
- Lane refs: `lane_branch` (:184) cuts the name to 80 - len(lane) - 1 before appending the lane, and validate refuses a
  clash before anything is pushed (:307), also for a lane given twice (old finding 1 fixed). Every ref is
  `validation/...` (core `throwaway_branch`, prefix fixed); the push message no longer blames only the hook (:362).
- SIGTERM: the push is shielded (:355) and `pushed` is set inside the shield (:358), so a landed push is always known and
  removed; the cleanup is shielded (:403) and finishes. Dispatch is never repeated: a deferred stop raises after the push,
  before the dispatch loop, and Terminated only cancels (:398). Old finding 3 fixed.
- No PR head SHA: lanes dispatch throwaway children (`make_commits` :271 refuses a commit equal to the head) through
  `cmd_dispatch` with force=False, which runs `attachment_guard`; ccb9318 proves the guard refuses an admitted registry
  key and a PR-branch tip, also with --force.

Findings (non-blocking, follow-ups):
1. `focused.wait` reads jobs, artifacts, tally and the fail log after `run_view` saw the run complete; if one of those
   reads refuses, a run already seen red is reported INCOMPLETE (unread), not NOT VALIDATED. Fail-safe (never
   validated), but the known red conclusion is lost.
2. Pre-existing: a SIGTERM inside `cmd_dispatch` after the POST but before its "run N" line leaves `row.run` None, so
   `cancel_runs` cannot cancel that run; its ref is removed, the run finishes on its own and holds a slot until then.
3. A second SIGTERM while `except Terminated` cancels runs escapes as a bare `Terminated` (BaseException); the shielded
   cleanup still runs, so no ref is left, only the exit message is lost.

Tests: test_native_dispatch.py, test_native_dispatch_focused.py, test_native_tick_watch.py, test_native_coord_select.py,
test_native_packet*.py green through `sm gate run`; `sm self lint` green.

REVIEWED: sha256=802b029ae138013984ed0d5834ca238fbb761fba713e380fd45c7d4a0142c937
VERDICT: APPROVE

## Re-review (campaign g3, 2026-11-13, Claude Opus 5.5; independent, wrote none of this code)

Why stale: the digest now follows imports into core and across `_lib` packages (30 files, including
`admission_lib/*` that `dispatch_lib/focused.py` imports, and core `window.py`, `runlock.py`, `github/`). Diff
1d69c43..479a8e8 of the unit: `window.py`, `dispatch.py`, `dispatch_lib/*`, `lock_lib/*`, `admission_lib/*`,
core `github/{base,client,forge,models}.py`: unchanged. `lock.py`: `gate run` gains `--exclusive` and is
`writes=True` with an `exempt` reason (the run lock cannot be gated: every review's tests run through it).
Core: `window.py` typing only; `runlock.py` exclusive lane (bounded 30 min default wait, priority 2, starts only on
no busy heavy slot, holds `<prefix>.exclusive.lock` for the run, non-exclusive heavy runs wait meanwhile);
`defaults.py`, `config.py`, `engine.py`, `native.py`, `models.py`, `helpdoc.py`, `protocols.py` as in head-apply.md.
Nothing changes the validate flow (throwaway child commit, lane refs, window turn, cleanup).
Tests through `sm gate run`: `test_native_dispatch*.py`, `test_native_lock*.py`, `tests/test_core_window.py`,
`tests/test_core_runlock.py`: green.

Findings:
1. (S3, core) runlock exclusive lane: a normal request that probed `exclusive_held()` false just before the
   exclusive request locks the exclusive file and slot 0 can still lock slot 1 and run beside it. Narrow, bounded;
   re-check `exclusive_held()` after a normal slot is locked and release on a hit.
2. Still open from the Fable review, not regressions: F11 (a failed poll reads as NOT VALIDATED in some paths) and
   F17 (lane ref truncation and leaks).

COVERS: head validate
COVERS: ci window run
COVERS: ci window release
REVIEWED: sha256=0ee6ed43d34f3e53a43f7869f2e14fad5250ff7fd599a1d45838cb05c8842f92
VERDICT: APPROVE

## Re-verdict (campaign g3, 2d0877b, 2026-11-13, Claude Opus 5.5; independent, wrote none of this code)

Diff 479a8e8..2d0877b of the 30 digest files: `window.py`, `dispatch.py`, `lock.py`, `lock_lib/*`, `admission_lib/*`,
core `window.py`, `runlock.py`, `defaults.py`, `github/*` unchanged. Changed:
- `dispatch_lib/focused.py`: `refused_by_github` + the dispatch POST. On a definite HTTP 4xx (not 408) from `gh api`
  the `.pending` marker is removed (inside `admission_lock`, so no other start observes it) and the command
  refuses "nothing started"; timeouts (rc 124/127 -> "refusing: ..."), 5xx and unparseable errors keep the marker
  (ambiguous start, reconcile first). `gh()` never retries (`retry=False`), so a 4xx cannot follow an accepted
  first attempt. If `gh` stderr is longer than the 300-character cut and loses "(HTTP 4xx)", the marker stays:
  the fail-safe direction. Test `test_dispatch_definite_refusal_clears_pending_ambiguous_keeps_it` covers 422,
  404, 502, timeout, EOF and that the next start is not blocked.
- core `native.py`: `exempt=` now counts only for paths in `EXEMPT_PATHS` (`gate run`), so no other command can
  declare itself out of the review gate; promote pid-reuse check (fails closed); `NativeRuntime.commands`.
- core `engine.py`: supersede under the older run's lock (see head-apply.md).
Follow-up, runlock `--exclusive` race (bead heinzel-management-qi1j, P2): still present in `runlock.py:405-410`
(a normal request that read `exclusive_held()` false can lock slot 1 after the exclusive request took the
exclusive file and slot 0). It cannot cause a write without the gate: the review gate (`gate_problem`) runs in
`native._command` before any runlock call, `gate run` is the only exempt path and gives no write rights to the
command it runs (an `sm` writer inside it meets its own review gate), and both runs still hold a run-lock slot.
The effect is two heavy runs at once for one exclusive run (timing/load only). The earlier N1 (an exclusive
request falling into the normal branch when the hold vanished) is fixed: the `elif` checks `not request.exclusive`.
Tests through `sm gate run` at 2d0877b: `test_native_dispatch_focused.py`, `test_native_dispatch.py`,
`test_native_lock.py`, `tests/test_core_{window,runlock,dispatch,native,gate_units}.py`: green.

Findings: none new and blocking. Open: the exclusive race (bead above); F11 and F17 from the Fable review.

COVERS: head validate
COVERS: ci window run
COVERS: ci window release
REVIEWED: sha256=43d83bbbe15e7a25f5d0cab0aa4cdd9e3c3cbf2436e83a8c74c3c994bec394b8
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

COVERS: head validate
COVERS: ci window run
COVERS: ci window release
REVIEWED: sha256=1ffefdc0084e843df57c8bb9b9dc4bf64471298b05655dd5474594fbf5da4ebb
VERDICT: APPROVE

## Campaign re-verdict g3 (2026-11-14, independent; reviewer wrote none of this code)

Target integration/sm-dod frozen at c36bbe1; baseline 903a1b7 (every unit APPROVED there). Digests recomputed with
`uv run sm self reviews` in a fresh worktree at c36bbe1; they equal the campaign list. Delta read with
`git diff 903a1b7 c36bbe1 --` over the unit files and every imported core file.

Unit delta (`window.py`, `dispatch.py`, `dispatch_lib/focused.py`, `lock.py`, `merge_tier.py`; admission_lib as in
head-apply.md):
- `lanes_of`: e2e-web-1/2 alias web-1/2 (deduplicated); desktop, consumers and managed refuse with exit 2 (no focused
  job kind runs them). Validation is still only on a throwaway child commit; the atomic push to validation/* is
  unchanged apart from `environ=child_environment()` (the same as dict(os.environ)).
- `sm ci window status` moved out of this file (read-only, not in this unit).
- Gating (merge_tier_mode=auto): `merge_tier.active` honours on/off, otherwise reads main's scripts/focused_plan.py
  through the GitHub contents API (GET) and requires a `^WHOLE_SUITES =` line. Unreadable, a non-base64 answer or an
  error means the older rules (fail closed). The only behaviour it changes in this unit is `ci one rerun`
  (dispatch_lib/focused.py:443): a rerun of a finished CI-workflow run on main no longer needs --force when the tier is
  active. release/* still always needs --force. The capacity, workflow and run-match guards still apply. The
  callable is evaluated only when the branch is main.
- `lock.py`: `sm gate run|status` moved to core (`commands/gate.py`, its own review); the policy is unchanged. Lock
  and history files default to the state directory (None default, no real path).

Findings: none blocking. Note: `active()` catches OSError/RuntimeError/ValueError only. Any other exception from the
Gh client propagates and makes the command fail, which still refuses the rerun (fail closed).

Shared core delta 903a1b7..c36bbe1 (in every g3 digest): `config.child_environment` gains `drop`/`drop_prefixes`
(callers that dropped `GIT_*` by hand now use it; same result), `search_path`, `variable`; `native._command` runs each
write under `writers_lock` (shared flock in the project state directory; `sm self migrate-state` takes it exclusively),
the switch-pending check moved inside it and still fails 75 for a single run and skips a loop tick; the review gate
(`gate_problem`) is unchanged and still runs first. `statemove.require_migrated` refuses while an old `~/.cache/hz-*`
path still holds state or both paths hold state, so the state-path move fails closed. helpdoc adds lint letters only.
`models` adds StateMigration only. No force push, stash, auto-merge or new write path without the digest gate.

Tests through `sm gate run` at c36bbe1, by explicit file (39 files: `test_native_admission_*` (14),
`test_native_combine{,_cov_b_plan}.py`, `test_native_coord_merges.py`, `test_native_dispatch{,_cov_a_focused,_focused}.py`,
`test_native_lock{,_refuse_matrix}.py`, `test_native_merge_tier_{rules,validate}.py`, `test_native_packet*` (9),
`test_pipeline_head_admit_merge_tier.py`, `tests/test_core_{native,runlock,statemove,window,gate_units}.py`): exit 0,
about 1006 passed, 26 skipped, 0 failed. Serial run: this repository has no pytest-xdist, so pytest refuses `-n 2`.

COVERS: head validate
COVERS: ci window run
COVERS: ci window release
REVIEWED: sha256=92ad4da7f0d03e0b59e9bbf125d47e0fd2a1f6ab4d5ec8393558e4ba9c5ca131
VERDICT: APPROVE

## Re-verdict at abc64a4, group A (2026-11-14)

Independent reviewer; wrote none of this code. Baselines, shared core and tests: ~/Code/packets/sm-reverdict-abc64a4/A.md (recorded verbatim by the integrator).

Delta: focused_slots (R3/R6; validate uses it only as the registry decoder, dispatch_lib/focused.py:559) and the core.

COVERS: head validate
COVERS: ci window run
COVERS: ci window release
REVIEWED: sha256=709c2ab33e92ecda73633ead7e4b74898b080686955c326245e80b34c17c9b35
VERDICT: APPROVE

## Re-verdict at feat/sm-next-admission 5fe179a (delta review 4, 2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-next-admission/REVIEW.md, delta review 4 (recorded verbatim by the integrator; COVERS from `self reviews`).

COVERS: head validate
COVERS: ci window run
COVERS: ci window release
REVIEWED: sha256=27e8c300945b7e10f8091e990d7a32f14be36cbebc561e7007139dfa3019776c
VERDICT: APPROVE

## Delta review: feat/sm-gate-lanes d94de45 (independent reviewer)

Changed in this unit since the last verdict: src/steuermaschine/runlock.py (light lane, rerun refusal); plugin native/lock.py (passes git_tree_state, setting text). Reviewed in full in two rounds (e6db9d9, d94de45),
probes now tests; see ~/Code/packets/sm-gate-lanes/REVIEW-2.md. Nothing in this command got weaker.

REVIEWED: sha256=fac6301b5e14289301c92c4135827069a205e49b62249609cf423d97e6816172
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

next-admission p0hc/b5vp (detached validate, exit 1 for a finished waiting/incomplete child, drop bound to worktree and start time, release-window wait) plus gate lanes' lock.py: the push runs `git ... push`, which is never refused as a rerun and never learned light by a unique SHA argv. Composition adds nothing. S7 (drop TOCTOU) still open.
COVERS: head validate
COVERS: ci window run
COVERS: ci window release
REVIEWED: sha256=db1a83dd655a5781a65af6b87909c836a705caceddb76f7daefd8509b9785454
VERDICT: APPROVE

## Change: dead waiters, admission hold, one drop verb (2026-11-14)

Builder note for the re-verdict (beads heinzel-management-qji9, heinzel-management-5zb9); no verdict is written here.

1. Core `steuermaschine/window.py`: `Window._expire` drops a waiting request whose pid is gone (`alive(turn) is False`, same host), so it never gets the turn and
   never sits ahead of a live head; `status` lists it separately as `gone` (marked, not numbered) with the command that removes it. Entries on another machine
   (alive None) are kept as before.
2. `Window.enter`: while a live admission hold exists a head that does not hold the window waits (reason names the pending admission); the holder's own repeated
   call still answers `go`. Holds are written by `sm head prepare|apply` and `sm release admit prepare|apply` (reviews/head-apply.md).
3. ONE verb for taking a waiting head out of the line: next-admission's `sm ci window drop` (reviews/ci-window-drop.md). My `ci window withdraw` is removed (nothing called it); `drop` also handles a `gone` waiter, and it never drops a live or unknown (another machine) waiter without ownership proof, so the R2 guarantee holds. `PrunedWindow` (plugin) is removed: the core Window drops dead waiters itself (same rule, and `status` marks them).
   NEW command `sm ci window release-hold OWNER` removes a stale admission hold (a live or unknown owner needs --force); it needs `COVERS: ci window release-hold`.
4. `head validate` / `ci window run` themselves are unchanged; their TRAP text now says the line is held during an admission.

Check: tests/test_core_window.py (hold, dead waiter, withdraw), plugins/heinzel/tests/test_native_admission_hold.py (through the command tree).

Awaiting an independent re-verdict: COVERS, REVIEWED and VERDICT lines follow from the reviewer.

## Re-verdict at 5b48447: window admission merged onto integration/sm-batch2 (2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-window-admission/REVIEW.md, round 3 (delta 8927d34..5b48447: admission hold, release-window order, drop of gone waiters, release-hold; withdraw and PrunedWindow removed). Shared core changed by integration/sm-batch2 (runlock, usage log, gate) is covered by its own reviews, not by this one.

COVERS: head validate
COVERS: ci window run
COVERS: ci window release
COVERS: ci window release-hold
REVIEWED: sha256=f42d33d9bf73be9c76d4acd4e69dbeaa4d9b01cf4917b89235af1c63bb267148
VERDICT: APPROVE
