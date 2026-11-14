# Port review: dropping a failed candidate (`sm release drop`)

Slice heinzel-native-release. Written by the porting agent. The `REVIEWED:` line and the verdict line are the
independent reviewer's and are NOT there yet. Same review unit as `release-resume.md` (read its sections 1 and 2
first): `native/release.py`, `native/release_lib/` with the two reviewed source files, `native/queue_lib/`.

## 1. Where the code comes from

`sm release drop VERSION --reason TEXT [--close-pr]` is the reviewed, not yet applied patch of the old step runner:
`research/hz-release-drop-candidate.patch` next to the old tools (sha256 658d19ea8ac3...17743), verdict
`research/hz-release-drop-candidate.verdict.md` (APPROVE WITH ONE FIX; the fix, refusing while a run on the release
branch is active, is in the patch). The patch is not copied into this repository: `release_lib/moved_map.py` names it
with its sha256 (REVIEWED_PATCHES), and `port/move.py verify` checks the sha256, applies the patch to the old file in
a scratch directory and compares the moved `runner.py` and the moved test `test_native_release_drop_candidate.py`
(59 tests) with that result. Once the patch is applied to the old tools the old file alone is the source.

What the patch adds to the runner: `dropped_dir`, `DROP_QUERY`, `drop_active_runs`, `drop_identity`, `drop_unmet`,
`archive_dropped`, `drop_candidate`, the options `--drop-candidate` and `--close-pr` with their argument checks, the
refusal of any other action while a drop is recorded but unfinished, and the drop's own STOPPED line. No line of it is
changed by the move.

## 2. What is new here

`release_drop` in `native/release.py`: the typed command builds `[VERSION, --drop-candidate, --reason, TEXT,
--close-pr]`, runs the moved `main` in process and returns `ReleaseDrop` (dropped, the archive path, which hold,
closed PRs, branch deleted, whether other holds remain), read from the runner's line and from the archived receipt.

## 3. Side effects, in order (recorded from the patched old script, replayed on the native command)

Without `--close-pr` (`drop`): the release lock; PyPI must answer 404; one GraphQL read (tag, GitHub release, branch,
release PRs); five run listings of the release branch (queued, in_progress, waiting, requested, pending) that must
all be empty; the drop record is written into the receipt; ONLY the hold named by the receipt's token, whose content
is this version, is unlinked (pinned by inode before the proof); the receipt moves to
`~/.cache/hz-release-dropped/vX.Y.Z-<UTC>.json`. No GitHub write.

With `--close-pr` (`drop-close-pr`): the same proof first (an active run refuses before any write), then
`gh pr close N`, then `gh api --method DELETE .../git/refs/heads/release/vX.Y.Z`, then the whole proof again, then
the record, the hold, the archive. Another release's hold in the same directory is still there afterwards (asserted).

Refusal (`drop-published-refused`): PyPI answers 200: STOPPED, no `gh` call, receipt and hold untouched.

Tests: `test_native_release_writes.py` (parity on the three transcripts; `test_drop_of_the_real_pretag_receipt_...`
on a copy of the real pre-tag receipt of 0.9.40 with an incident hold that must survive;
`test_a_resume_is_refused_while_a_drop_is_recorded_but_unfinished`) and the 59 moved tests.

## 4. Open, not verified

- The verdict of the patch was given on the old script. This review must confirm that the move changed nothing in it
  (run the verifier) and that the native wrapper passes nothing but the three options.
- Accepted residuals of the patch's verdict stand: `--close-pr` closes the PR before the durable drop record (a
  failed second proof leaves the PR closed and nothing dropped; retry-safe), and a stuck queued run on the release
  branch blocks the drop until someone cancels it.
- Never run against the live state: the two drops of 0.9.40 on 2026-11-12 were done by hand and left
  `hz-release.json.dropped-*` files in another shape than this command's archive.

## Independent review (2026-11-13, a reviewer who wrote none of this code)

Reviewed: the unit as merged on main (merge 1e5ace3 of e911b38; `native/release.py`, `native/release_lib/` with
`approved_sources/ci.yml` and `approved_sources/release_candidate.py.txt`, `native/queue_lib/` of main), 34 files.
The branch alone (e911b38, digest 7b3b6e94...) is NOT approved: its `queue_lib/edge.py` sends the merge call's
lenient decoding to the strict core runner; main's `queue_lib/edge.py` does not, and this digest contains main's.
Full findings with file and line: `REVIEW-INDEPENDENT-release.md`, section "Phase 2" (P2-0 to P2-11).

Confirmed:
- The move is verbatim (`port/move.py verify` rc 0 again after 061fb50 and e911b38; an independent diff in phase 1).
  Pins equal the old literals and Core main. 691 = 691 tests, no assertion changed.
- The reviewed candidate sources are in the digest: one byte changed in either file makes all three verdicts stale
  (tried in a scratch copy). No other data file under the two libraries is read at run time.
- Every option of the old command line maps one to one; the runner's own argument checks, lock, receipt check,
  unfinished-drop check and rejection guard run unchanged; an empty option value is passed on and refused by the
  runner as before (tried). `resume` cannot reach the drop, `drop` cannot reach resume, plugin or absorb (tried).
- Process calls on the release path: a timeout raises again, a missing program raises again, the merge call decodes
  leniently again; each call checked, table in the findings.
- Run for real in a sandbox (real core runner, stub `gh`/`git`/`bash`, throwaway HOME with copies of the state files
  of 2026-11-13): `resume 0.9.40` up to the merge stop, and the release PR preparation through the child
  `sm gate run -- bash scripts/release.sh patch` in the configured release checkout. The copied state files were
  byte-equal afterwards; only the new receipt, the new hold and the lock file were added.
- The gate: without a verdict each of the three commands exits 70 through the real command tree and writes nothing;
  no module entry reaches a write.

`release drop`, additionally confirmed: the code equals the reviewed patch (the verifier applies it, sha256
658d19ea...); the native command passes exactly VERSION, `--drop-candidate`, `--reason TEXT` and `--close-pr`.
`--close-pr` cannot drop a candidate with an active run (the proof runs before any close and again after it), a
merged, tagged, released or published version, a finished release, a tail, or another version than the receipt's.
On today's state (no receipt; hand-moved leftovers of the two candidates of 0.9.40) it refuses and touches nothing.

Open, not blocking:
- The residuals of the patch's verdict stand: the PR is closed before the durable drop record; a stuck queued run
  blocks the drop until it is cancelled.
- P2-9 of the findings: after a drop the closed PR still satisfies "release PR open" of the next resume; open the
  new release PR before resuming.

## Re-review 2026-11-13: ci.yml re-pin (Core 53c4e88)

The only change in the review unit since the previous verdict (sha256=715a1eff...) is
`release_lib/approved_sources/ci.yml`, re-pinned to the exact bytes of Core `.github/workflows/ci.yml` at 53c4e88
(identical to the shipped head 02ae550c8; file sha256 edccd5b1...). Its diff is one line: the lint job's
`timeout-minutes: 5` -> `10`. No job removed or renamed, no condition or concurrency change. The unit digest at
the previous commit recomputes to 715a1eff..., at this commit to cefa96ac... (34 files).

COVERS: release drop

Previous verdict: approved at sha256=cefa96ac1680a8ef2d7039457ca05adc48aefa1529d6979c64b2b51d0ce740bb.

## Re-review 2026-11-13: fix/batch-release at 61dcce0 (35 files)

Independent reviewer (wrote none of it). The drop path is unchanged in this unit change: `drop_candidate` and its
proof are not touched; `main` still routes `--drop-candidate` before anything else and refuses every other action,
now including `--open-pr`, while a drop is recorded but unfinished (runner 2585-2591). `--open-pr` cannot be combined
with `--drop-candidate` (2499-2503). The other changes of the unit are reviewed in `release-resume.md` (re-review
of 61dcce0) and `release-pr.md`.

REVIEWED: sha256=9af0b9aac389ab91f3273ae70b19c338f072811cf351cb6d596a6e550221d2fd
## Independent review (feat/repair-main 7fc817a, base main 5c87f14)

Reviewer: independent; wrote none of this code. Unit recomputed by `sm self reviews`. Stale only because it shares
queue_lib/queue_daemon.py: diffed every file of the unit against 5c87f14; the only change is the `sm queue main-rerun`
repaired-plan path (queue_daemon.py:501-507, :514-519, :579-583, :650-669, :679-682; see reviews/queue-main-rerun.md).
No queue add/drop, daemon tick, ship, PR, release or train code path changed; the restored-by-rerun result keeps its
shape (one optional `repaired` key). Related suites and `sm self lint` pass through `sm gate run`.

REVIEWED: sha256=5cf634bca48beb8e76c53e487465b4ae875a248d0a2bb69605939701b6bb3340

VERDICT: APPROVE

## Independent re-review, campaign group 5 (integration/sm-complete at 479a8e8, 2026-11-13)

Reviewer wrote none of this code. Own worktree on review/campaign-g5; digest recomputed with `uv run sm self reviews`; every file of the unit diffed 1d69c43..479a8e8.
- `drop_candidate` and the `release drop` command are unchanged. Shared unit with release resume / pr /
  repair-receipt (same digest); the new runner paths are reviewed in each of those files. For drop: the open-pr path
  refuses while a drop is recorded but unfinished (main(): the drop check precedes --open-pr).
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

COVERS: release drop
REVIEWED: sha256=9c5f2884b1eda8cf0398f4c18853f569a111eb978e6ad6bcb30fe08089384f48
VERDICT: APPROVE

## Independent re-review, re-verdict campaign g5 (integration/sm-complete at 2d0877b, 2026-11-13)

Reviewer wrote none of this code. Own worktree on review/reverdict-g5; digest recomputed with `uv run sm self reviews`
(equals the campaign list); every file of the unit diffed 479a8e8..2d0877b.
- `drop_candidate` and `release drop` unchanged. main(): the unfinished-drop check still precedes --open-pr, which now
  also runs `rejection_guard` (see release-pr.md).
- Shared core files changed 479a8e8..2d0877b (engine.py, native.py): `_supersede` now takes the older run's own
  lock (`PIPELINE.KEY`, non-blocking flock; Busy is caught by the caller and reported as waiting, nothing half-marked
  matters since the marking is idempotent); `gate_problem` honours `exempt=` only for paths on the core allowlist
  `EXEMPT_PATHS` = {`gate run`} (narrows the gate, never widens it); `switch_pending` ignores a promote pid file only
  when `ps -o etime=` proves the pid started after the file's UTC time (+2 s), and holds (fails closed) when either
  time is unreadable; clock.now() is float per the Clock protocol, Commands.run returns Result(rc, out). Nothing
  here weakens a gate of this unit.
- Tests through `sm gate run -- uv run pytest` at 2d0877b (release, pipeline, rehearse, lock, queue, go, dispatch-focused,
  moved release/queue suites, core engine/native/runlock/window): 2496 collected, 0 failed, 0 errors, 25 skipped (N2 of the previous round is fixed at 2d0877b). NOT verified: any live GitHub write.

COVERS: release drop
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

COVERS: release drop
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

COVERS: release drop
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
- Delta in runner.py: state paths (release-dropped/, release.json, release-holds/, merge-freeze) move to the state
  directory; the drop still removes only its own hold and never the incident sentinel
  (test_drop_of_the_real_pretag_receipt_in_text_and_json, now in the state directory). Old hz-* hints became sm names.
- FINDING (wrong recovery hint): plugins/heinzel/src/steuermaschine_heinzel/native/release_lib/runner.py:2775 tells the
  operator to finish an unfinished drop with `sm release resume VERSION --drop-candidate --reason ...`. `sm release
  resume` has no `--drop-candidate` option (typer refuses it); the command is `sm release drop VERSION --reason ...`.
  plugins/heinzel/tests/test_native_release_writes.py:290 asserts that wrong text, so the test pins the defect. This is
  the only way out of the "drop recorded but unfinished" stop. Fix the hint and the test.

COVERS: release drop
REVIEWED: sha256=0d5c8f6f59530f3584e321ddb59738692cb3bdbff3728fd54ecd8b76fbb49d6e
VERDICT: CHANGES

## Re-verdict at abc64a4, group B (2026-11-14)

Independent reviewer; wrote none of this code. Findings, baselines and tests: ~/Code/packets/sm-reverdict-abc64a4/B.md (recorded verbatim by the integrator).

COVERS: release drop
REVIEWED: sha256=62dace46b549abcec99680095be4a302347cc24c548253bb3610a67ad0b9e549
VERDICT: APPROVE

## Change for review: drop resets the cut state (bead heinzel-management-5j8d, branch feat/sm-release-recut)

Written by the builder; the reviewer adds the COVERS, REVIEWED and VERDICT lines. NOT approved.

What changed: after a successful drop (the runner printed `DROPPED`), `release_drop` calls `reset_cut_state(version)`: it
reads the state of `sm release cut VERSION` (store kind `runs.release.cut`, key VERSION), copies it to the kind
`runs.release.cut.dropped` under `VERSION-<UTC stamp>`, then deletes the original. A later `sm release cut VERSION`
finds no state and starts fresh (0.9.42 recut b adopted the dropped run's prepare and failed reading its prepare.out).
`ReleaseDrop.cut_state_archived` names the archive key; a note line says so. No cut state: nothing happens.
Core: `Store.delete(kind, key)` (protocol, FileStore, MemoryStore; the read-only store refuses). It deletes state only;
receipts have no delete.

Check: the reset runs only after the runner's own drop succeeded (a refused drop leaves the cut state, tested); only
the dropped version's key is touched (tested with 0.9.41); the archive keeps every field of the old state; the drop
logic in `release_lib/runner.py` (`drop_candidate`) is unchanged. Residual: the reset takes no `release.cut.VERSION`
lock; a cut advancing at that moment could save its state again (the drop itself refuses while a run is active).
Not verified: a live drop. Tests: test_native_release_writes.py (two new), test_release_packet_recut.py.

Delta after the review of 1c8764a (R1, R2, A2, A3), written by the builder, NOT approved: `release drop` now holds
`store.lock("release.cut.VERSION")` for the whole drop (Busy: exit 75, nothing dropped; tested). `drop_identity`'s query
asks `isCrossRepository` and ignores fork PRs (the recorded drop transcripts carry the new query text; the 59 moved tests pass).
A failed archive of the cut state after a real drop is reported in `lines`, not raised.

## Independent review of feat/sm-release-recut at 0cfb0ae (2026-11-14)

Independent reviewer; wrote none of this code. Findings, probes and tests: ~/Code/packets/sm-release-recut/REVIEW.md and REVIEW-2.md (R1, R2 fixed; advisories open).

COVERS: release drop
REVIEWED: sha256=0bb6db776bfdfb9124f579b46488741dcda38a8b77a1466f3cad43f96cf5dcad
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Recut R1/R2 hold in the merged tree: drop holds `release.cut.VERSION` (Busy -> exit 75, nothing dropped), forks filtered in DROP_QUERY/drop_identity, cut state archived to runs.release.cut.dropped then deleted via the new Store.delete. A5 still open: parity/release-drop.md has no "Differs on purpose" line for isCrossRepository and the exit-75 wait (doc only, not in the digest). Advisory X2 (cross-branch): drop does not give back the release-window hold that the dropped candidate's `release admit prepare` took (next-admission), so `sm head validate` can wait up to release_window_minutes after a drop; fails closed.
COVERS: release drop
REVIEWED: sha256=b1d5d5f5226d59fc09a60cad53a998ed18f01c71c852e1329d6401c1d6a0464a
VERDICT: APPROVE
