# Review: `sm release repair-receipt [--apply|--dry-run]` (one-time receipt repair)

## Independent re-review, campaign group 5 (integration/sm-complete at 479a8e8, 2026-11-13)

Reviewer wrote none of this code. Own worktree on review/campaign-g5; digest recomputed with `uv run sm self reviews`; every file of the unit diffed 1d69c43..479a8e8.
New unit. Read: release.py:278-289, runner.py repair_receipt/read_json_object/plugin_keys, main() routing, mini.core_receipt.

- Default is a dry run: it prints old/new core and plugin receipts, the latest tail evidence and the backup path and
  writes nothing (only the shared release lock file is opened, LOCK_NB: a concurrent release command makes it stop).
- `--apply`, under the global release lock: backup copy of the core receipt first (copy2), then the plugin receipt
  (atomic write_state via a scoped RECEIPT_OVERRIDE, restored in finally), then the core receipt keeps every other key
  or is removed (+ dir fsync) when only stamps were in it. A crash between the two writes leaves the stamps in both
  with equal values; a rerun merges them (no clash) and finishes.
- Refusals write nothing: unreadable/non-object receipts stop and are preserved; a plugin stamp key present in both
  files with different values stops before any write. The key pattern only matches `plugin-heinzel-<dist>-X.Y.Z[_suffix]`;
  no core key can match. Tail receipts are only read. `--apply` with a version or --plugin is a usage error.
- N1 (minor): the help names `--dry-run` while the runner has no such flag for this path; the native option maps
  `--apply/--dry-run` to the presence of `--apply`, so behaviour is right.
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

COVERS: release repair-receipt
REVIEWED: sha256=9c5f2884b1eda8cf0398f4c18853f569a111eb978e6ad6bcb30fe08089384f48
VERDICT: APPROVE

## Independent re-review, re-verdict campaign g5 (integration/sm-complete at 2d0877b, 2026-11-13)

Reviewer wrote none of this code. Own worktree on review/reverdict-g5; digest recomputed with `uv run sm self reviews`
(equals the campaign list); every file of the unit diffed 479a8e8..2d0877b.
- `repair_receipt` unchanged; it routes first in main() under the global lock, before --open-pr and its new
  rejection guard. Shared-unit changes reviewed in release-pr.md.
- Shared core files changed 479a8e8..2d0877b (engine.py, native.py): `_supersede` now takes the older run's own
  lock (`PIPELINE.KEY`, non-blocking flock; Busy is caught by the caller and reported as waiting, nothing half-marked
  matters since the marking is idempotent); `gate_problem` honours `exempt=` only for paths on the core allowlist
  `EXEMPT_PATHS` = {`gate run`} (narrows the gate, never widens it); `switch_pending` ignores a promote pid file only
  when `ps -o etime=` proves the pid started after the file's UTC time (+2 s), and holds (fails closed) when either
  time is unreadable; clock.now() is float per the Clock protocol, Commands.run returns Result(rc, out). Nothing
  here weakens a gate of this unit.
- Tests through `sm gate run -- uv run pytest` at 2d0877b (release, pipeline, rehearse, lock, queue, go, dispatch-focused,
  moved release/queue suites, core engine/native/runlock/window): 2496 collected, 0 failed, 0 errors, 25 skipped (N2 of the previous round is fixed at 2d0877b). NOT verified: any live GitHub write.

COVERS: release repair-receipt
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

COVERS: release repair-receipt
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

COVERS: release repair-receipt
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
- Delta: the core receipt is state/release.json, the plugin receipt state/release-plugins.json, the backup
  release.json.bak-<UTC> (matches the `release.json.*` move pattern).
- Rerun safety checked in an isolated HOME: after migration, dry run, then `--apply` moved the stamps and kept a
  backup; a second `--apply` says "nothing to repair" and writes nothing; a clashing value still stops. The stamps the
  live tool moved today to ~/.cache/hz-release-plugins.json are a plain old-name file: the migration moves it to the
  new name and leaves a link; before the migration this release refuses (exit 70) rather than reading or creating a
  second plugin receipt. `release plugin` still refuses while the core receipt holds plugin stamps.

COVERS: release repair-receipt
REVIEWED: sha256=0d5c8f6f59530f3584e321ddb59738692cb3bdbff3728fd54ecd8b76fbb49d6e
VERDICT: APPROVE

## Re-verdict at abc64a4, group B (2026-11-14)

Independent reviewer; wrote none of this code. Findings, baselines and tests: ~/Code/packets/sm-reverdict-abc64a4/B.md (recorded verbatim by the integrator).

COVERS: release repair-receipt
REVIEWED: sha256=62dace46b549abcec99680095be4a302347cc24c548253bb3610a67ad0b9e549
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Own verdict (recut A8). repair_receipt and its argv checks are untouched; it shares release.py's digest, whose delta is the drop lock/archive and the runner's release_pr. No behaviour change for this command.
COVERS: release repair-receipt
REVIEWED: sha256=b1d5d5f5226d59fc09a60cad53a998ed18f01c71c852e1329d6401c1d6a0464a
VERDICT: APPROVE
