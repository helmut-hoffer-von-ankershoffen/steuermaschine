# Port review: `sm release admit prepare` and `sm release admit apply` (the release-main route)

For the independent reviewer. Both commands are write commands under this one file: `prepare` sends one cancel probe
per stuck-queued receipt (the route's guard has no probe-free form) and stores the transaction `apply` depends on;
`apply` performs the one registry write of the route. The author of the port writes no verdict line.

Source: the reviewed isolated operator copy of the former tool (`operator-nonconsumers`, used for the 0.9.39 release),
ported after `research/release-admission-port-plan.md` (8 conflicts, 2 semantic).

## 1. Where the code is

| File | Content | Check |
|---|---|---|
| `native/release_admit_lib/release_scope.py` | operator `hz_release_scope.py` | byte-identical (no rule applies); its sha256 is what release packets pin as `validator_sha256`, so that value does not change. Test: `test_every_bound_source_exists_and_the_validator_is_the_reviewed_file` |
| `native/release_admit_lib/focused_release.py` | operator `hz_focused_release.py` | `admission_verbatim.py diff`: 2 hunks, change R1 |
| `native/release_admit_lib/release_slots.py` | six blocks of the operator's `hz_focused_slots.py`: `Runtime.__init__`, `.run`, `.set_variable` (lines 66-77, 79-90, 147-150), `check_pr_holds` (411-424), `read_release_nonconsumers` (362-373), the release block `release_snapshot`, `release_guard`, `prepare_release`, `apply_release` (603-745); plus a new header and the argv form `main` | `admission_verbatim.py diff` prints each block as VERBATIM (substring check against the operator file) |
| `native/release_admit_lib/release_identity.py` | `release_holds`, `release_holds_present`, `incident_frozen`, `merge_frozen` from the operator's `hzlib.py` (four blocks, byte-checked); identity and `split_key` imported from the PR admission | same |
| `native/release_admit_lib/release_ops.py` | NEW: typed entry (stored arguments, isolated start) | section 3 |
| `native/release_admit.py` | NEW: the two registrations | |
| `tests/test_native_release_admit.py`, `_stuck.py` | operator `tests/test_focused_release.py`, `_stuck.py` | 1 hunk each: the import of `s` now names `release_slots` (and, until the frozen hunks land, a skip mark) |

## 2. The eight conflicts of the port plan

| Plan | Resolution here | Test |
|---|---|---|
| 3.1 `apply_slots`: `pairs` (installed) against `release_main` (operator), two hunks, SEMANTIC | Both parameters. `singleton = resulting_main or release_main` (both given: refused); `pairs` must be 1 whenever a singleton is given; the non-singleton branch keeps the installed exact pair-count check (`2 * pairs` keys, exactly `pairs` PR keys and `pairs` push-tree keys); the singleton branch and the three later tests use the operator's `singleton`; a release returns the receipt dict (written by the route after the queue is restored), every other mode writes its receipt as before. This is hunk set 1 of `reviews/release-admit-frozen.patch` (frozen file `focused_admit.py`). | `test_release_main_with_a_pair_count_other_than_one_refuses`, `test_release_main_and_resulting_main_together_refuse`, `test_release_main_needs_its_own_mode_and_the_exact_manifest`; the moved batch tests; the moved release tests |
| 3.2 blank line | not applicable: `focused_slots.py` is not touched | |
| 3.3 `check_consumers` defined twice after a merge, SEMANTIC | Only one exists: the installed one in `admission_lib.focused_slots` (with `active_runs`, `blocking_runs`, the refusal that names the runs). The route imports it. Only `read_release_nonconsumers` came over from the operator. | `test_the_route_uses_the_one_consumer_check_of_the_pr_admission`; moved `test_native_release_admit_stuck.py` |
| 3.4 batch block against release block | The release block lives in its own module; nothing is adjacent. | |
| 3.5 `main` dispatch | The PR admission's `main` is unchanged. `release_slots.main` is release-only and refuses `--pr`, `--reviewed-protected-producer`, `--packets`, `--core-repos`, `--prs`, `--stuck-queued`, `--cancel-blocking-pr-runs`. The commands have no such options at all. | `test_argv_form_excludes_every_other_admission_mode`, `test_argv_form_is_release_only` |
| 3.6 `Runtime(github_host=)`, `check_pr_holds(require_owner=)`, `focused_resulting` request shape | `Runtime` is a subclass in `release_slots` with the operator's three methods; the PR admission's `Runtime` is unchanged. `check_pr_holds` with `require_owner` is the route's own (operator text); the PR admission keeps its own. `focused_resulting`: the operator's change (`required` map, `release_registry_proof`), a clean three-way merge, is hunk set 2 of the patch (frozen file). | `test_runtime_pins_the_github_host`, `test_runtime_is_constructed_like_the_pr_admission_runtime`; moved `test_native_admission_resulting.py` (unchanged, green with the patch) |
| 3.7 `SOURCES` | change R1 | `test_every_bound_source_exists_...` |
| doc conflict | not applicable (no manual moved) | |

## 3. Changes and additions

| # | Change | Test |
|---|---|---|
| R1 | `focused_release.SOURCES` lists the route's and the PR admission's modules by their paths under `native/` (was: the tool's file names) and the base directory is `native/`; the validator key is `release_admit_lib/release_scope.py`. Every module of the route must be listed (tested). Consequence, as the plan says: every source hash in a transaction changes; a transaction prepared by the operator copy cannot be applied here and the reverse. | `test_every_bound_source_exists_and_the_validator_is_the_reviewed_file` |
| R2 | Isolated start. The operator required `sys.flags.isolated` (`python3 -I` on its entry point). The argv form keeps exactly that. The commands accept an isolated interpreter OR the same properties checked one by one: no `PYTHONPATH`, `PYTHONSTARTUP`, `PYTHONHOME`, `PYTHONINSPECT`; user site disabled; neither `''` nor the working directory on `sys.path`. **This is a judgment call for the reviewer**: an installed `sm` is not started with `-I`. | `test_isolated_startup`, `test_prepare_and_apply_refuse_a_start_that_is_not_isolated` |
| R3 | `prepare` stores `prepare.out` and `prepare.args.json`; `apply PACKET --approved SHA` verifies before anything runs: route marker, digest, `core_repo` (a field of the release transaction), packet sha256, nonconsumer receipts; then recomputes and requires canonical equality (operator text), then `apply_release`. | `test_apply_refuses_changed_stored_arguments_before_anything_runs`, `test_apply_refuses_changed_packet_bytes_a_pr_transaction_and_a_wrong_digest`, `test_apply_recomputes_compares_and_applies`, `test_prepare_stores_transaction_and_arguments` |
| R4 | No module entry (`__main__`) in any file of the route. | `test_module_has_no_entry` |

## 4. The two changes to the PR admission's frozen modules

`reviews/release-admit-frozen.patch` (7 hunks): `focused_admit.apply_slots` (5 hunks, plan 3.1) and
`focused_resulting` (2 hunks, plan 3.6). Listed in reviews/head-apply.md as change 3. They change the digest of
`head apply`. With `release_main` absent `apply_slots` takes the same branches as before: `singleton` is
`resulting_main`, `mode` is the resulting-main mode, the return is `_write_receipt(...)`. Evidence: all moved PR tests
and the replayed applies of the recorded packets pass unchanged with the patch.

Change C of 2026-11-13 (list-row receipt guard, one full proof before the write) touches `release_slots.py`
(`release_guard`, `prepare_release`, `apply_release`; block 603-745 of the operator copy is no longer verbatim) and
`release_admit.py` (help text). The hunks and tests are listed once, in reviews/head-apply.md section 2c.

## 5. Operating limits carried over unchanged (plan section 6)

- The cancel probe is a real `POST .../cancel` per receipt per guard, including the guard after the write
  (`release_guard` has no `probe` parameter). A refusal there leaves the slot written and no receipt: verify the
  five-value readback by hand, do not re-apply. The PR admission solved this with `probe=not written()`; the route
  was not changed in the port.
- A zombie run that became cancellable is cancelled by the probe and the guard refuses: reprepare without it.
- Admission runs before the release branch is pushed and before its PR is opened.

## 6. Deviations from docs/INTEGRATION.md section 2

The same as in reviews/head-apply.md section 4 (processes, `gh` argv, time, `os.environ`, paths under `~/.cache`
including `hz-focused-release-admission-attempts`, `hz-release.json`, `hz-release-holds`, `hz-merge-freeze`; locks;
receipts), for the same reasons, plus: `focused_release._pypi_absent` opens `https://pypi.org/pypi/heinzel/<version>/json`
with `urllib` (moved).

## 7. Evidence and what is unverified

- Moved operator tests: 119 passed, 1 skipped (`fresh release form only`, as at the source), with the patch applied.
- New tests: `tests/test_native_release_admit_ops.py`.
- UNVERIFIED: no run against live GitHub, launchd, PyPI or the registry. No replay of a recorded release transaction
  yet (parity/release-admit-apply.md). Mutation checks of the plan's step 6 are not run yet. The reviewer's ten
  mutations of the nonconsumer patch were not rerun.

## 8. What the verdict is bound to

The core binds a verdict to the code: `sm self reviews` prints the sha256 over `native/release_admit.py` and every
`.py` file under `native/release_admit_lib/` and `native/admission_lib/` (16 files). An approval is one `COVERS:` line
per command, the line `REVIEWED: sha256=<that digest>` and the exact last line `VERDICT: APPROVE`; any later change to
one of those files voids it. The author of the port writes none of these lines.

## 9. Independent review (2026-11-13, reviewer wrote none of the code)

Reviewed: commit `216c11b` (9380c25, 44e4152, 216c11b on ac1b3e1), exported with `git archive`; every run below used
my own export, never the builder's working tree. No GitHub call, no registry write, no real apply. Full notes with
file:line: `~/Code/tmp-sm-reviews/admission/REVIEW-INDEPENDENT-admission-route.md`.

COVERS: release admit prepare
COVERS: release admit apply
REVIEWED: sha256=53b56f03b63da8015ca0c4fbf56531bc4c4afaa1a27880a924ed398abc0ca209

That digest is `sm self reviews` on the export and my own computation from the commit. It does not cover
`native/admission.py`. This verdict approves the two release commands only; `sm head apply` on its new digest and the
batch commands are not approved by it (see the notes: one blocker there, in `native/admission.py`).

What I checked myself:

- Moved code against the operator copy: `release_scope.py` byte-identical (sha256 bc4ab426..., the value all four
  recorded release packets pin as `validator_sha256`); `focused_release.py` differs only by R1; the nine moved blocks
  of `release_slots.py` and the four of `release_identity.py` are identical; everything the route imports from the PR
  admission is source-identical to the operator's copy except `check_consumers`, which is the installed one.
- The two semantic points: one singleton proof at a time, `pairs` must be 1 for it, the installed pair-count check is
  kept; one `check_consumers`.
- Replay of three recorded release admissions through `release_ops.prepare` and `release_ops.apply` (0.9.40, 0.9.40b,
  0.9.39 recut as paused recovery; GitHub, launchd and PyPI answered in process, Git and the trusted-main codec real):
  each prepared transaction equals the recording in every field except the tool source hashes (R1); each receipt
  equals the real receipt except `admission_id`, `transaction_sha256` and those hashes; one write, the recorded slot
  value; the attempt and observed journals equal the real ones. Every `gh` call carried the pinned host.
- Refusals in that replay, each with no write and the queue running afterwards: uncovered active run, truncated
  inventory, main moved, wrong digest, changed `prepare.out`, five changed stored arguments, changed packet bytes,
  changed registry variable, variable over 49,152 bytes, total over 262,144, merge freeze, release hold, existing
  attempt journal, held recovery PR.
- Gate: both commands exit 70 through the real entry point, plain, with `--format json`, and with the two
  environment variables that once opened it. No module of the route has a `__main__` block.
- Unit: complete (the route imports `admission_lib`, which is in `files=`); no data file.
- R2 (isolated start): accepted. The installed `sm` starts a console script of its own venv and passes; `PYTHONPATH`,
  `python -m` or a working directory on the import path refuse.
- Mutants: 31 run, 24 killed, 2 equivalent, 5 lines no test reaches (listed below).

Not verbatim equal to the recordings, by design: the bound source list (R1). Not replayed: 0.9.39 fresh (its clone
moved on) and five earlier attempts (their packet revisions no longer exist). Nothing ran against live GitHub,
launchd, PyPI or the registry: watch the first live apply.

Open, none blocks these two commands:

- `focused_release.SOURCES` does not list `admission_lib/ops.py` (executed by the route) or the two `__init__.py`.
  The review digest covers them; the transaction does not bind them. Add them with the next change of the unit.
- The guard after the write sends the cancel probes again (section 5). A refusal there leaves the slot written and
  no receipt. Operator behaviour, unchanged; the PR admission's `probe=not written()` is the known remedy.
- No test reaches: the validator pin (`focused_release.py:159`), the freeze check of `release_guard`
  (`release_slots.py:119`), the slot/bytes check in `write_slot` (`release_slots.py:211`), and in `apply_slots` the
  in-lock append proof and "already admitted" refusal (`focused_admit.py:698,690`). Each line is correct as read.
- `isolated_startup` names four `PYTHON*` variables; refuse all but an allowlist.
- A failure after the write is printed as "refused".

VERDICT: APPROVE

## 10. Independent review of change C, 7522853 (2026-11-13, reviewer wrote none of the code)

COVERS: release admit prepare
COVERS: release admit apply
REVIEWED: sha256=5efa319bd8a3eb0ab08ec613d9223527d3fd0f8479a8e92aa85f9ea56d566f67

Findings (paths under `native/`):

- `prepare_release` (`release_admit_lib/release_slots.py:156`) and `guard(paused=False)` (`:192`) are list-only; no
  write follows them. In `apply_slots` the write (`admission_lib/focused_admit.py:703`) follows the second
  `check_fresh`, which is `guard(full=pre_write())` (`release_slots.py:233`) = full proof with cancel probe (the
  `check_consumers` default `probe=True` via `release_guard:126`). The `pre_write` counter is only fed by that lambda.
- No cancel POST after the write: before, the post-write guard re-probed every receipt; section 9 above already named
  that as a risk (a refusal there strands a written slot without receipt). Dropping it matches the PR route's
  `probe=not written()`; a post-write probe cannot protect a write already made. Safe.
- `check_consumers`, `authenticate_run`, threading, receipt changes: see `head-apply.md` section 11 (same code).
- Tests: `test_native_release_admit.py` and `_stuck.py` one-line fake edits (`full=` passthrough, `updated_at=`);
  `test_native_admission_receipt_guard.py` checks one probe pass directly before the write and jobs caught pre-write.
  Suites pass; `sm self lint` passes. Docs string in `release_admit.py:3-4,33` matches the behaviour.

VERDICT: APPROVE

## 11. Independent review of repair-main, 7fc817a (2026-11-13, reviewer wrote none of the code)

COVERS: release admit prepare
COVERS: release admit apply
REVIEWED: sha256=723b9940967cbc4a90bc5e63da30584bb38f56a1af3fc3aedb716b72d389913f

Findings (paths under `native/admission_lib/`):

- The release route is untouched apart from shared code: `apply_slots` (`focused_admit.py:661-666`) selects
  `release_main` first and its mode string as before; the new `sum(...) > 1` refusal contains the old
  release+resulting conflict. `release_slots.py` does not pass `repair_main`, so its singleton path is byte-for-byte
  the old one. The new module `repair_main.py` is reached only via `focused_slots.prepare(repair_main=True)`.
- Repair route itself: see `head-apply.md` section 12.
- Run: release-admit suites (`test_native_release_admit*.py`) pass with the admission suites through `sm gate`;
  `sm self lint` passes.

VERDICT: APPROVE

## Independent re-review, campaign group 5 (integration/sm-complete at 479a8e8, 2026-11-13)

Reviewer wrote none of this code. Own worktree on review/campaign-g5; digest recomputed with `uv run sm self reviews`; every file of the unit diffed 1d69c43..479a8e8.
- The unit's own files (release_admit.py, release_admit_lib/, admission_lib/) are byte-identical to 1d69c43; the digest
  moved only because it now follows imports into core.
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

COVERS: release admit prepare
COVERS: release admit apply
REVIEWED: sha256=1d3ef81575d0d6c43bedffbc76f1e5707696b29c0f31f0529521d5547b93201e
VERDICT: APPROVE

## Independent re-review, re-verdict campaign g5 (integration/sm-complete at 2d0877b, 2026-11-13)

Reviewer wrote none of this code. Own worktree on review/reverdict-g5; digest recomputed with `uv run sm self reviews`
(equals the campaign list); every file of the unit diffed 479a8e8..2d0877b.
- Unit-own change: `admission_lib/wait.py` docstring only (prepare sends no cancel probe; checked: `ops.py` defaults
  `probe=False` for prepare, only apply probes). No code line of release_admit*, admission_lib changed otherwise.
- Shared core files changed 479a8e8..2d0877b (engine.py, native.py): `_supersede` now takes the older run's own
  lock (`PIPELINE.KEY`, non-blocking flock; Busy is caught by the caller and reported as waiting, nothing half-marked
  matters since the marking is idempotent); `gate_problem` honours `exempt=` only for paths on the core allowlist
  `EXEMPT_PATHS` = {`gate run`} (narrows the gate, never widens it); `switch_pending` ignores a promote pid file only
  when `ps -o etime=` proves the pid started after the file's UTC time (+2 s), and holds (fails closed) when either
  time is unreadable; clock.now() is float per the Clock protocol, Commands.run returns Result(rc, out). Nothing
  here weakens a gate of this unit.
- Tests through `sm gate run -- uv run pytest` at 2d0877b (release, pipeline, rehearse, lock, queue, go, dispatch-focused,
  moved release/queue suites, core engine/native/runlock/window): 2496 collected, 0 failed, 0 errors, 25 skipped (N2 of the previous round is fixed at 2d0877b). NOT verified: any live GitHub write.

COVERS: release admit prepare
COVERS: release admit apply
REVIEWED: sha256=ed9dc184730c1b22dd5d292e7ae0c6eccdcd73796e9817760ceffa6ce886b4d0
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

COVERS: release admit prepare
COVERS: release admit apply
REVIEWED: sha256=e1d98ce7bf0f0b19b559276aba6ff9087634b56add7fc1e8c899bf894813d203
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
- Delta: the admission runtime's `cache` is `state.root()` (refuses on any unmigrated path of the plugin), and the
  journal, receipt, registry lock and paused-receipt names lose the `hz-` prefix, matching state.NAMES. The journal-dir
  safety check (owner, no group/world write) now applies to the state root. `verify_account` still refuses while
  HZ_QUEUE_STATE, HZ_TRAIN_STATE_DIR or HZ_RELEASE_DIR is set (`environment.state_overrides().any()`, the same three
  variables). `isolated_startup` reads the same four PYTHON* variables through the typed model. gh children get
  `child_environment(GH_HOST=...)`, which is the old `{**os.environ, ...}`. No behaviour change beyond the paths.

COVERS: release admit prepare
COVERS: release admit apply
REVIEWED: sha256=1a1fd451c3f3d71dbf4234fb0c0a642171ecdfce2dd0f2c2daa429728a7754fd
VERDICT: APPROVE

## Re-verdict at abc64a4, group A (2026-11-14)

Independent reviewer; wrote none of this code. Baselines, shared core and tests: ~/Code/packets/sm-reverdict-abc64a4/A.md (recorded verbatim by the integrator).

release_slots.Runtime uses `_slots.queue_label()`, same refusal via the inherited `queue_state`; `release_guard` and
apply call it before any pause. NOTE: release_slots.py:263 still declares `--cancel-blocking-pr-runs` in its argv
parser only to refuse it (line 274); dead, harmless, delete it.

COVERS: release admit prepare
COVERS: release admit apply
REVIEWED: sha256=841da2a14aa3da6ae28fb8f7fcf5cea29ad07b5a448d203d8900e5fc7ca7f52e
VERDICT: APPROVE

## Re-verdict at feat/sm-next-admission 5fe179a (delta review 4, 2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-next-admission/REVIEW.md, delta review 4 (recorded verbatim by the integrator; COVERS from `self reviews`).

COVERS: release admit prepare
COVERS: release admit apply
REVIEWED: sha256=3b0fd0141912e77593faa2226a326a4a163f061adc08e8a6b9f9e1cbd5965a74
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

next-admission only (release-window hold around prepare/apply, give_back only its own record, S13). The recut cut passes the reservation packet path to both, so the hold's packet name is consistent. Matches the review file.
COVERS: release admit prepare
COVERS: release admit apply
REVIEWED: sha256=297f22cc429b185b9c0bef3379f716c8355e5bf19cd72db5e93ec18c1998adfc
VERDICT: APPROVE

## Change: admission hold, waiting (2026-11-14)

Change (branch feat/sm-window-admission; beads heinzel-management-qji9, heinzel-management-5zb9). Builder note for the re-verdict; no verdict is written here.

Admission hold. `sm release admit prepare and sm release admit apply` now runs inside `admission_hold` (native/admission_lib/hold.py): it registers a hold in the validation window
(`steuermaschine.window.Window.hold`, state key `holds` in the window's own store kind) and ends it in a `finally`. While a live hold exists,
`Window.enter` answers `wait` to every head that does not hold the window already: a validation that runs finishes, no new one starts.
A hold whose pid is gone, or whose lease ran out (window_lease_minutes), is ignored, so a killed admission never blocks the line for good.
Registering the hold is the first step; the admission code itself is unchanged except for what is listed below.
`--wait` (new on `sm release admit prepare|apply`): on a refusal that names an uncovered active run, or a truncated or drifting inventory, wait
admission_wait_poll_seconds and run the identical command again (`wait.retry_when_clear`, built on the existing `prepare_when_clear`), up to
admission_wait_max_minutes, then exit 75. Both refusals are raised by the guard BEFORE the registry write, so a retry never repeats a write;
the reviewer should confirm that for apply (focused_slots.prepare -> require(not blocking) precedes apply_prepared in ops.apply).
`sm release cut` passes `wait_=True` to both (pipelines/release_cut.py).

Check: the hold is released on every exit (tests/test_native_admission_hold.py: success, AdmissionError, the 75 wait); the hold is
registered before the first read of the run inventory; no write of the registry moved or changed.

Merge with next-admission: `release admit prepare|apply` run inside `release_window.held` (the release-window lock: from prepare to apply `sm head validate` waits, checked at every look) and, inside it, `admission_hold` (the hold is atomic in the validation line: a validation that passed the lock check a moment ago still cannot start). Order: lock outside, hold inside; the lock is given back by a successful apply only. `--wait`/`WrittenWithoutReceipt` as above.

Awaiting an independent re-verdict: COVERS, REVIEWED and VERDICT lines follow from the reviewer.

## Re-verdict at 5b48447: window admission merged onto integration/sm-batch2 (2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-window-admission/REVIEW.md, round 3 (delta 8927d34..5b48447: admission hold, release-window order, drop of gone waiters, release-hold; withdraw and PrunedWindow removed). Shared core changed by integration/sm-batch2 (runlock, usage log, gate) is covered by its own reviews, not by this one.

COVERS: release admit prepare
COVERS: release admit apply
REVIEWED: sha256=5fc1cb6c0c11c533bb92b61ebdb67a0f1e2280a08a668cf772b3a8a29fa4603f
VERDICT: APPROVE

## Change: apply needs a recorded independent verdict (bead heinzel-management-04ro)

`release admit apply` calls `admission.require_verdict(approved)` before `release_ops.apply`, the same check as `head apply`
(approve receipt, reviewer not an author of the digest); `release admit prepare` registers its digest. Tests:
`test_native_admission_apply_needs_verdict.py`. The previous verdict line and digest are void; re-review.

After merging integration/sm-batch3 (window admission): the check runs BEFORE the release-window lock and the admission hold, so a missing verdict never holds either; `release admit prepare` registers inside its lock and hold, after `release_ops.prepare` (the one path of that command, with or without --nonconsumers).

## Re-verdict at f346492 (bead heinzel-management-04ro + integration/sm-batch3, 2026-11-14)

Independent reviewer; wrote neither 04ro nor window admission. Review: ~/Code/packets/sm-04ro/REVIEW.md.
`require_verdict` before `release_window.held` and `admission_hold`; `register_prepared` inside `once()`, so plain,
--wait and --nonconsumers prepares register (inside the lock and hold; a Busy registration fails closed: no verdict
can be recorded until a re-prepare). Lock and hold code as in sm-batch3.
COVERS: release admit prepare
COVERS: release admit apply
REVIEWED: sha256=7073ba86d270cffeba480a550f05d5d5d332b52168c355b5d2c6d26dfbb45fe2
VERDICT: APPROVE
