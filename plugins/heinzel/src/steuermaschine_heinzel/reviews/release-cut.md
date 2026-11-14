# Review: `sm release cut` (engine pipeline)

Unit: `pipelines/release_cut.py` and every first-party file it imports, including core (`sm self reviews` lists
them and the digest). The steps it runs are other units (release pins, notes-audit, admit prepare|apply, resume,
verify); each keeps its own review and gate, checked again when the pipeline calls it.

What it writes: the run state of this version, the cut plan file under the review root; through the steps:
prepare.out (local); with --live: the release-main plan slot (release admit apply) and the whole release
(release resume: hold, PR, merge, tag, PyPI, compose, acceptance).

What the reviewer checks: the admission review passes exactly its digest to apply; the publish review covers the
inputs resume runs with (version, intro, snapshot, candidate gate) as canonical JSON; main moving before the apply
voids prepare and its review; a finished accepted receipt is adopted; a stopped resume is reported with the
runner's own reason and is never retried by the pipeline.

Known limit: there is no stop between a green candidate and the publishing merge; that needs the runner split
along release.CUT_STEPS.

## Independent re-review, campaign group 5 (integration/sm-complete at 479a8e8, 2026-11-13)

Reviewer wrote none of this code. Own worktree on review/campaign-g5; digest recomputed with `uv run sm self reviews`; every file of the unit diffed 1d69c43..479a8e8.
New unit. Read pipelines/__init__.py, common.py, release_cut.py whole; engine `_advance` whole; test_pipeline_admit_cut
and test_pipeline_cov_release_cut.

Holds: the pipeline itself runs only with its own unit approved (app.py:209, not in --plan); every composed step goes
through `native.call`/`gate_problem` (an OVERRIDE in tests never opens a shut gate); the admission digest handed to
apply is the one the reviewer approved (ctx.data from the stop); main moving before apply voids pins..apply; apply
adopts an existing receipt for the release head; a stopped resume is a refusal with the runner's reason, never retried
by the pipeline; verify not serving yet is a Wait.

- C1 (BLOCKING, write safety): the publish review is bypassed for changed inputs once `admit apply` is done. Engine
  `frozen` (any external step done) makes a done ReviewStop count as approved even when its digest changed
  (engine.py: `rec.get('digest') == digest or frozen`). `publish review` comes AFTER the external `admit apply`, so
  after it was approved once, `sm release cut V --set candidate_gate=yes --set intro=... --live` rewrites cut-plan.json
  (the approved bytes are lost on disk) and runs `release resume` with the unreviewed inputs. Reproduced in a scratch
  test on this head (not committed): approve publish review, resume stops, rerun with changed intro and candidate_gate
  -> status finished, resume called with intro 'Something else' and the plan file shows candidate_gate true. The
  module docstring says "a changed input is a new plan and a new verdict": it is not. Fix (either): the engine applies
  `frozen` only to stops before the last done external step (a stop after it is re-checked by digest), or
  publish_review refuses when an approved plan exists with another digest; and resume reads its inputs from the
  approved plan, not from ctx.inputs. Add the test.
- N1 (minor): neither the pipeline command (app.py checks only the review gate) nor `native.call` checks
  `switch_pending()` (a promote switching the live sm); only the native CLI wrapper does. A long `release resume` can
  start inside the pipeline while a promote is pending.
- N2 (doc): TRAPS['resume'] says a failed compose is re-adopted until a fresh compose runs; the runner now replaces a
  failed compose once when a plugin fix is on PyPI. Update the trap.
- Core files in the digest (engine, native, config, models, protocols, helpdoc, defaults, hookspecs, plugins, runlock,
  window): typing, the `Environment` model (only reader of os.environ), review-stop file/sha256 in the report, a
  `running` state with pid before a step acts, `_supersede` of an older run of the same argument (marked, never
  deleted), `native.call`/`gate_problem` (the same review gate for pipeline steps), a promote switch makes write
  commands exit 75 with nothing run, transitive import digests, `gate run --exclusive` (queues as priority 2, bounded
  by a 30 min default wait, starts only with no heavy slot busy). Nothing here weakens a gate of this unit.
- Tests through `sm gate run -- uv run pytest` at 479a8e8: tests/test_native_release_*, test_pipeline_*, test_native_rehearse*,
  test_native_lock*, test_native_queue_*, test_native_go*, tests/moved/*release*|*queue*|*rehearse*, core test_core_engine,
  _native, _runlock, _window: 1 failed, 2330 passed, 24 skipped, 1 xfailed. The one failure is
  moved/test_native_queue_recheck.py::test_a_green_needs_work_row_at_its_pinned_head_is_requeued_and_merges (stale kwargs
  expectation, see N2 in release-resume.md); no test of this unit's own paths fails. NOT verified: any live GitHub write.

Required changes:
- C1: a changed publish plan after its approval must need a new verdict (and resume must run with the approved inputs).

REVIEWED: sha256=f74f94614c1dfed226e50537a4f1d3d2d41597c474db45d05695fe6af32ea1ca
VERDICT: CHANGES


## Independent review of fix/pipelines-p1-p3 at fcad53c (base integration/sm-complete 479a8e8)

Reviewer: independent; wrote none of this code. Reviewed 161b9dd, then the deltas 803d811 and fcad53c. Unit and
digest recomputed with `uv run sm self reviews` at fcad53c. Paths under `pipelines/release_cut.py`.

- P2, prepare reused only at this main (`prepare_probe`): prepare.out is adopted only when it parses, its own
  `main` equals the subject's main (the real prepare.args.json names no main: `admission_lib/ops.py` writes schema,
  sha256, packet, ...), and prepare.args.json's `sha256` equals prepare.out's canonical digest (fcad53c), so the
  arguments belong to that transaction. The main-moved test fails on 479a8e8 and passes here.
- P3, resume verdict (`ok_or_stopped`): returns only on exit 0 AND a finished receipt AND accepted is True; finished
  and accepted False refuses with the absorb hint; exit 0 without that verdict refuses ("no ACCEPTED verdict") and
  the resume step stays open, so verify does not run. `resume_probe` adopts only finished + accepted True. All three
  P3 cases fail on 479a8e8 and pass here.
- C1, the publish plan is bound (`publish_review`, `_approved`, `approved_plan`): the plan is kept in the run state
  with its digest. Once that digest has an approving verdict, inputs that differ refuse (names the keys; the
  plan file is not rewritten). Resume takes version, intro, snapshot and candidate gate only from `approved_plan()`,
  which re-checks the digest, never from this call's `--set` values. So the engine keeping a done stop past the
  admission (an external step) no longer matters. A plan changed before any verdict gets a new digest, and an
  approval of the old digest does not carry over (approved_plan refuses when the stored digest has no verdict).
- g3 (1) shared: `identify` uses `common.core_repo` (empty, None, ".", "./" refuse).
- Tests: the C1 tests (changed inputs refuse, resume not run; the approved values again finish with the approved
  intro; a plan fixed before its verdict) pass. The sibling cov tests now build an approved plan before calling resume
  (`approved()`), and add a no-approved-plan refusal; prepare_probe tests bind args.sha256. Not weakened.
- Ran through `sm gate run`: test_pipeline_admit_cut.py, test_pipeline_cov_release_cut.py,
  test_pipeline_cov_head_admit.py, test_heinzel_plugin.py, tests/test_gate_structure.py: see the commit message.

Should-fix (not blocking):
1. `_approved` checks only `verdict == "approve"`, not that the reviewer is not the author; it relies on the engine's
   stop having enforced that when it recorded the stop as done. Keep the stop the only way a publish verdict counts.
2. The known limit above (no stop between a green candidate and the publishing merge) is unchanged.

COVERS: release cut
REVIEWED: sha256=baed7524e91658fb7527232fb92904f07a575316c67c785577a4db9f6e85ddf8

VERDICT: APPROVE

## Re-verdict (integration/sm-complete 2d0877b, frozen; campaign re-verdict pipelines)

Reviewer: independent; wrote none of this code. Digest recomputed with the worktree's sm (`self reviews`, 35 files):
d804b644f98ad9f805f9c3723edd1152d209fd929e067e45b9eefdc85ebe97fe, as in the campaign list.

Delta fcad53c..2d0877b of every unit file: `pipelines/*.py` and every native file of the unit are unchanged except
`queue_lib/hzlib.py` (`gh_api_method`: the timeout bound only) and `admission_lib/wait.py` (docstring). Core:
`engine.py` `_supersede` takes the older run's own lock; `native.py` `switch_pending` pid-reuse check (fail
closed), `EXEMPT_PATHS`. Nothing regressed: P2, P3 and C1 from the fcad53c review are byte-identical.

Should-fix 1 (`_approved` does not check the reviewer is not the author) is bead heinzel-management-554n (filed under
head admit). Should-fix 2 (no stop between a green candidate and the publishing merge) is unchanged.

Not blocking: `_supersede` now locks every other key of the pipeline before it checks arg and status, so a new subject
waits (exit 75) while any other subject of the same pipeline is advancing, even one for another worktree. Liveness
only (a retry passes; supersede is idempotent); checking arg and status before taking the lock (and again under it)
would avoid it.

Tests through `sm gate run -- uv run pytest`: test_pipeline_{admit_cut,cov_head_admit,cov_release_cut}.py,
test_native_admission_cov_a_focused_admit{,_run}.py, test_native_dispatch_{cov_a_focused,focused}.py,
test_native_preflight.py, tests/test_core_{engine,native,runlock,window,preflight}.py: 364 passed.

COVERS: release cut
REVIEWED: sha256=d804b644f98ad9f805f9c3723edd1152d209fd929e067e45b9eefdc85ebe97fe
VERDICT: APPROVE

## Independent re-review, re-verdict campaign g5, release plugin fix (integration/sm-complete at 539b7f7, 2026-11-13)

Reviewer wrote none of this code. Own branch review/reverdict-g5-ovvg from 539b7f7 (contains fix/release-plugin-runid 234dc8d, bead
heinzel-management-ovvg; no code change between 60a83a5 and 539b7f7); digest recomputed with `uv run sm self reviews`; code diffed 2d0877b..60a83a5 (release.py,
release_lib/runner.py only). Everything before the fix is covered by this file's previous verdict at 539b7f7.
- Moved only because native/release.py is in this unit. The ovvg change in release.py is limited to the `release plugin`
  command (help text, the `--redispatch` option, its pass-through). The cut pipeline uses `release.runner()` and
  `release.receipt_of()` and the resume/verify steps, all unchanged. The runner change touches only the plugin path and
  one argparse check that a cut never triggers. Cut pipeline tests passed in the same gated run (270 passed, 0 failed).
- Tests through `sm gate run -- uv run pytest` at 60a83a5 (code-identical to 539b7f7; the same 85 files as before): 2506 tests, 0 failed, 0 errors, 25 skipped; the 10 tests of test_native_release_plugin_runid.py all pass.
  NOT verified: a live dispatch or any live GitHub write.

COVERS: release cut
REVIEWED: sha256=ae76ea479c6d6d388ae6bbc78c1981de2e314c115d7ddd2025dd494351e16e88
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

COVERS: release cut
REVIEWED: sha256=283b93beb10006ac2e4084536f2841a2644390f8645cd027dc6f145550f1f6fa
VERDICT: APPROVE

## Campaign re-verdict g6 (2026-11-14, independent; reviewer wrote none of this code)

Target integration/sm-dod at c36bbe1, baseline 903a1b7. Digest recomputed with the worktree's sm (`uv run sm self reviews`, 203 files); it equals the campaign list. pipelines/release_cut.py is unchanged; read the delta of common.py, release_lib/{runner,binding,edge,rejection}.py, release_admit_lib/, queue_lib/hzlib.py and main_batch.py, state.py and the shared core (writers' lock, statemove.py). Tests through `sm gate run`: test_pipeline_{admit_cut,cov_release_cut}.py within the g6 run (268 passed, exit 0).

Attempt 1: the candidate gate is unchanged. main_candidate_gate (train path) and main_tip_green still refuse a main tip green only on a rerun ("a release needs a first-attempt full green main run", runner.py:1170-1173); the first-attempt candidate policy at the merge and the acceptance attempt binding (runner.py:553) are untouched. The one widening nearby, main_batch.valid_run accepting a rerun of a delegated final run (queue_lib/main_batch.py:112), carries the real attempt into the effective row, so main_release_gate still sees attempt != 1 and needs every attempt-1 failure KNOWN.
No auto-merge: the release PR is opened without auto-merge and refused when one is set (runner.py:1081, 1088, 1696); the merge still waits for the publish review in the pipeline.
State: the receipt, tail receipts, holds, freeze, waiver, main-gate memo, dropped and rejection records now live in the state directory under plain names (`state.path`, refusing while unmigrated); release_core_checkout lost its machine default and refuses with the key name when unset. `require(not environment.state_overrides().any())` (admission_lib/focused_slots.py:523, 700) keeps the old guard against overridden state paths.
Note, not blocking: the head-admit findings (reviews/head-admit.md, this campaign) are in this digest's files but on a path release cut does not run.

COVERS: release cut
REVIEWED: sha256=09425bcc3eb4a7a8d3810876342afc44d5c3408f0e42efe5e498ec11b0a291dd
VERDICT: APPROVE

## Re-verdict at abc64a4, group A (2026-11-14)

Independent reviewer; wrote none of this code. Baselines, shared core and tests: ~/Code/packets/sm-reverdict-abc64a4/A.md (recorded verbatim by the integrator).

Only change: `resume_probe` reaches `release resume`'s module through the registry (`common.command`) instead of an
import (R1: digest 203 -> 23 files); same function, same receipt read. The pipeline now holds the writers' lock and
waits out a switch (core above), and `release cut` is a busy step for promote/rollback and the floor.

COVERS: release cut
REVIEWED: sha256=34011e25cf4435c0f7bf04064f1e5d9f06809a78ca1f1377ad7eeeb6d485c543
VERDICT: APPROVE

## Re-verdict at feat/sm-next-admission 5fe179a (delta review 4, 2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-next-admission/REVIEW.md, delta review 4 (recorded verbatim by the integrator; COVERS from `self reviews`).

COVERS: release cut
REVIEWED: sha256=29741d07c38431e32514659c523ec3afca61ea39747cb3b12c5075649583db79
VERDICT: APPROVE

## Change for review: the pipeline builds the release-main-only/v1 packet (bead heinzel-management-asy6, branch feat/sm-release-recut)

Written by the builder; the reviewer adds the COVERS, REVIEWED and VERDICT lines. NOT approved.

New step `packet` between `notes` and `admit prepare` (binds main), helper functions in `pipelines/release_cut.py` (no new file: the digest budget of 23 files stands), new inputs
`commit_log` (relative to review_root; last line vX.Y.Z) and `release_review` (default RELEASE-REVIEW.md). It ports the
hand script `build-packet.py` of 0.9.40 to 0.9.42: the pair packet's `main_manifest` becomes `manifest`; `release` binds
version, head, parent (base), tree, the sha256 of this install's `release_admit_lib/release_scope.py` (the file the
route checks), the three hooks of the clone (path from `git rev-parse --git-path`, sha256) and the PREK interpreter
named by the hooks (path parsed from the hook, sha256), and the commit-hook transcript; `review` binds the review file,
the canonical digests of `release` and `manifest` and the CHANGELOG blob. Refuses: a merge commit, a manifest whose base,
tree or event differ from the commit, a transcript not ending in vX.Y.Z, review paths outside review_root. Written to
`release-reservation-packet.json` next to the pair packet; `admit prepare` and `admit apply` now read that file (a given
packet that is already `release-main-only/v1` is used as it is). The apply probe reads `release.head` of it.
Differences from the script: git runs through `providers.commands`; hook paths come from git, not from `.git/hooks`;
the interpreter path is read from the hook, not hard-coded. Output is byte-equal on the 0.9.42 fixture
(tests/fixtures/release/packet, test_release_packet_recut.py). No network, no push. Not verified: a live cut.
Core change shared with release drop: `Store.delete`.

Delta after the review of 1c8764a (A3), NOT approved: the packet is written to a temporary file and renamed. Not done, listed
for the reviewer: A1 (bind the packet inputs' digests in the step fingerprint; prepare_probe checks the packet sha of
prepare.args.json) and A4 (a git-fixture test from evidence() to render()); both fail closed today.

## Independent review of feat/sm-release-recut at 0cfb0ae (2026-11-14)

Independent reviewer; wrote none of this code. Findings, probes and tests: ~/Code/packets/sm-release-recut/REVIEW.md and REVIEW-2.md (R1, R2 fixed; advisories open).

COVERS: release cut
REVIEWED: sha256=78242cb4273cec83a77f5c02e764f6ee72b0b8a9f056a9f861775550919b6d3a
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Two branches: next-admission (focused_slots/ops/pr_owned in the digest; prepare/apply now take the release-window hold) and recut (the packet step builds release-reservation-packet.json from the pair packet, transcript and review; prepare, apply, the plan and _packet_head all read reservation(ctx)). Composition checked: the window hold names str(packet) and both prepare and apply get the same reservation path, so give_back still matches. Digest cap 24 (coordinator decision, test comment). Approve; X2 under release-drop.md.
COVERS: release cut
REVIEWED: sha256=18f05356120e35298573a2d9b315aad86704858c347f6eb964176519b84f2942
VERDICT: APPROVE

## Re-verdict at f346492 (bead heinzel-management-04ro + integration/sm-batch3, 2026-11-14)

Independent reviewer; wrote neither 04ro nor window admission. Review: ~/Code/packets/sm-04ro/REVIEW.md.
Stale through window admission: admit prepare and admit apply pass `wait_=True`; release_ops/ops apply wrapped in
`wait.tracked`. The cut still reaches the slots only through `release admit apply` (verdict required). Tests green.
COVERS: release cut
REVIEWED: sha256=d4bfc9d82c8f671d98cb50f4b10b1539c0853a0dab26630ac29bd282a8157cf6
VERDICT: APPROVE
