# Review: `sm head prepare`

This command writes on this machine and was registered `writes=False`; it is now a write command under the
review gate (Fable review 2026-11-13, F9). Unit: the module, its `_lib` package and every first-party file they
import (`sm self reviews` lists them and the digest).

What it writes: prepare.out and prepare.args.json next to the packet (batch: in --out). Admission binds these bytes: `head apply` recomputes and requires byte equality, and the reviewer of the apply approves their canonical sha256.

What the reviewer checks: it writes only those two files, reads GitHub only (no cancel probe), and `--wait` only waits.

State: open. Waiting for an independent review.

## Independent review (campaign g3, 2026-11-13, Claude Opus 5.5; wrote none of this code)

Unit: `admission.py`, `admission_lib/*` and the imported core (19 files, same digest as `head apply`). Diff
1d69c43..479a8e8: only `writes=False` -> `writes=True` on `head prepare`, plus the core changes listed in
head-apply.md (typing, Environment, import-following digest, review-gate helpers); `admission_lib` is unchanged
since the approved head-apply reviews, which read prepare's logic as its dependency.

Checked: single mode writes only `prepare.out` and `prepare.args.json` next to the packet, batch only in `--out`;
`ops.prepare`/`prepare_batch` default `probe=False`, and `head prepare` never passes `probe`, so no cancel POST is
sent (GitHub reads only); `--wait` polls the read-only inventory and is bounded by `admission_wait_max_minutes`
(45) with exit 75 (`StillWaiting` -> `Wait`); a refusal raises before any write; `head apply` trusts neither file
(recomputes and requires byte equality). Tests through `sm gate run`: `test_native_admission*.py` green.

Findings (S3, not blocking):
1. `admission_lib/wait.py:5` docstring says "a prepare sends one cancel probe per stuck-queued receipt"; with
   `probe=False` it does not. Correct the comment.
2. `--wait` sleeps `admission_wait_poll_seconds` (90 s) per poll with no lock held; two prepares of one packet race
   on the same two files. Harmless (apply recomputes), but the pipeline's per-subject lock is the only guard.

REVIEWED: sha256=8a9ba37cd818b423c214917029bfac972422df1b6feac1061c5cadffcbb847e0
VERDICT: APPROVE

## Re-verdict (campaign g3, 2d0877b, 2026-11-13, Claude Opus 5.5; independent, wrote none of this code)

Same 19 files and digest as head-apply.md; that section lists the core delta (supersede lock, `EXEMPT_PATHS`,
promote pid reuse) and none of it changes what prepare writes. Unit delta: the `head prepare` help now says a
repair ends in a fresh attempt-1 push run and `queue main-rerun` refuses (matches `queue_main.py`), and the
follow-up from my earlier finding 1 is done: `wait.py` now says prepare sends no cancel probe (`probe=False`; only
apply probes before its write). Verified: `ops.prepare`/`prepare_batch` default `probe=False`, `admission.py`
never passes `probe`, and apply's batch path (`ops.py:353`) uses the default `probe=True` of `focused_slots`.
Tests: as in head-apply.md, green.

Findings (S4, not blocking): `focused_slots.py:818` (`stuck_queued_receipts` docstring) still says the cancel probe
"is left to prepare/apply"; only apply sends it. Finding 2 of the earlier review (two `--wait` prepares of one
packet race on the same two files; harmless) stands.

COVERS: head prepare
REVIEWED: sha256=ff3d914c4ea8c449a3191cf5e1d641034e4cd68bc842a9982ef3954eaffb501b
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

COVERS: head prepare
REVIEWED: sha256=6140790ceece5d9e86650b45c11eb3c6e18a59887462dd444ae903275212c8f2
VERDICT: APPROVE

## Campaign re-verdict g3 (2026-11-14, independent; reviewer wrote none of this code)

Target integration/sm-dod frozen at c36bbe1; baseline 903a1b7 (every unit APPROVED there). Digests recomputed with
`uv run sm self reviews` in a fresh worktree at c36bbe1; they equal the campaign list. Delta read with
`git diff 903a1b7 c36bbe1 --` over the unit files and every imported core file.

Unit delta (`admission.py`, `admission_lib/`, `environment.py`, `state.py`):
- `admission_nonconsumers_file` setting: used only when --nonconsumers is absent; default None (no real-path
  default). The receipts are still validated by the unchanged consumer checks; only the source of the path changed.
- State paths move from the scripted era's `~/.cache` names to the project state directory (`state.root()`): registry
  lock, receipts, validator cache, work root, queue/train/release locks, merge-freeze, release-holds. The symlink, owner
  and mode checks are kept on the new root. `root()` refuses while any old path still holds state, so the freeze and
  hold checks in `global_guard` cannot silently read an empty new directory while the state sits at the old name.
- The old state-override refusal (queue state, train state dir, release dir) is now
  `environment.state_overrides().any()`, the same three variables. GIT_* is still dropped for every git child
  (`child_environment(drop_prefixes=...)`).
- Disposable snapshot refs renamed to refs/sm-admission/* (scratch clones only). QUEUE_LABEL keeps the live launchd
  label on purpose. Message renames only otherwise.
- No change to the approval digest binding, the live-main identity check, the registry write or the locks' order.

Findings: none blocking. Note: `root()` caches a passed migration check per process; a long-lived process does not
see an old-name file that an older release writes later. Low risk, because the migration leaves a link at each old
name, so an old writer writes through it into the new location.

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

COVERS: head prepare
REVIEWED: sha256=7f1db6950c00396e3d71d584614da80f347be13f10524edf1459f1415556c2e6
VERDICT: APPROVE

## Re-verdict at abc64a4, group A (2026-11-14)

Independent reviewer; wrote none of this code. Baselines, shared core and tests: ~/Code/packets/sm-reverdict-abc64a4/A.md (recorded verbatim by the integrator).

- R3: `QUEUE_LABEL` literal gone. `queue_label()` reads `queue_service_label` from the native runtime's
  `settings.of('heinzel')`; outside a native command (argv `main`) or unset -> None, `Runtime.service/plist` None,
  and `queue_state()` refuses first thing (focused_slots.py:162) naming the key. `exclusions()` calls `queue_state()`
  before the `try` with bootout, so nothing is paused or written when unset. Verified end to end: a user config
  `[heinzel] queue_service_label = "x"` resolves to "x" through `app.runtime()`; an empty config to None.
  migrate-values.toml on the Mini carries the live label.
- R6: `report_blockers` is read-only; the `--cancel-blocking-pr-runs` argv option and the `hz-ci-defer` exec path are
  gone. No caller passed cancel. Stronger.
- NOTE: `head prepare` (and `release admit prepare`) also refuse while the label is unset (`fresh_guard` ->
  `global_guard` -> `queue_state`, focused_slots.py:532); the setting's description and docs/SWITCH-queue-daemon.md
  name only the two applies. Fail-closed; fix the wording.

COVERS: head prepare
REVIEWED: sha256=7dcae9f27a9291763e55f6403a7c14b093ecee06cf5203e2bd9f00dfd135fee6
VERDICT: APPROVE

## Re-verdict at feat/sm-next-admission 5fe179a (delta review 4, 2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-next-admission/REVIEW.md, delta review 4 (recorded verbatim by the integrator; COVERS from `self reviews`).

COVERS: head prepare
REVIEWED: sha256=93f0b2c71b74c1c3f74eebd93689dba1d1f0620a318907d77667c54effe0fcab
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

next-admission only (lftt nonconsumers re-authenticated by prepare; m598 expected remote head; admission.py settings conflict kept all three fields). config/protocols. Matches the review file.
COVERS: head prepare
REVIEWED: sha256=c6f2b129d16767c2d1c7ca6a9b0314eb6e8119b6c2b8dda6625990ac2192ca52
VERDICT: APPROVE

## Change: admission hold, waiting (2026-11-14)

Change (branch feat/sm-window-admission; beads heinzel-management-qji9, heinzel-management-5zb9). Builder note for the re-verdict; no verdict is written here.

Admission hold. `sm head prepare` now runs inside `admission_hold` (native/admission_lib/hold.py): it registers a hold in the validation window
(`steuermaschine.window.Window.hold`, state key `holds` in the window's own store kind) and ends it in a `finally`. While a live hold exists,
`Window.enter` answers `wait` to every head that does not hold the window already: a validation that runs finishes, no new one starts.
A hold whose pid is gone, or whose lease ran out (window_lease_minutes), is ignored, so a killed admission never blocks the line for good.
Registering the hold is the first step; the admission code itself is unchanged except for what is listed below.
`--wait` is unchanged (it already waited on the inventory); a validation running is such an uncovered run, and the hold keeps new ones from starting, so the wait ends.

Check: the hold is released on every exit (tests/test_native_admission_hold.py: success, AdmissionError, the 75 wait); the hold is
registered before the first read of the run inventory; no write of the registry moved or changed.

Awaiting an independent re-verdict: COVERS, REVIEWED and VERDICT lines follow from the reviewer.

## Re-verdict at 5b48447: window admission merged onto integration/sm-batch2 (2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-window-admission/REVIEW.md, round 3 (delta 8927d34..5b48447: admission hold, release-window order, drop of gone waiters, release-hold; withdraw and PrunedWindow removed). Shared core changed by integration/sm-batch2 (runlock, usage log, gate) is covered by its own reviews, not by this one.

COVERS: head prepare
REVIEWED: sha256=5950d14cc4d5ef7f1df2b8ff7a1dd718529f1fcd71cf13df313619ab34f9d7e9
VERDICT: APPROVE

## Re-verdict at f346492 (bead heinzel-management-04ro + integration/sm-batch3, 2026-11-14)

Independent reviewer; wrote neither 04ro nor window admission. Review: ~/Code/packets/sm-04ro/REVIEW.md.
Registration after single, waiting and batch prepare in `_prepare`, unchanged by the merge; window hold code as in sm-batch3.
COVERS: head prepare
REVIEWED: sha256=ea20838a13a7aa5f9cd43e106edc5ae5ee45ac1b20a1b63fecf9f264c003e24e
VERDICT: APPROVE
