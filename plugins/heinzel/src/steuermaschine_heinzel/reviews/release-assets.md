# Review: `sm release assets`

This command writes on this machine and was registered `writes=False`; it is now a write command under the
review gate (Fable review 2026-11-13, F9). Unit: the module, its `_lib` package and every first-party file they
import (`sm self reviews` lists them and the digest).

What it writes: the probe cache file (setting release_assets_cache).

What the reviewer checks: only the cache file is written; the probes are GET requests.

## Independent re-review, campaign group 5 (integration/sm-complete at 479a8e8, 2026-11-13)

Reviewer wrote none of this code. Own worktree on review/campaign-g5; digest recomputed with `uv run sm self reviews`; every file of the unit diffed 1d69c43..479a8e8.
New write unit (was writes=False). release_read.py changed one line (writes=True, files=release_read_lib+release_lib);
read release_read.py:388-436 and release_read_lib/assets.py whole.

- Only the probe cache file is written; probes are GET with Range 0-100, 20 s timeout each; `--stable` is bounded at
  240 rounds; `--digest` writes nothing (it is a read now behind the write gate, harmless).
- N1 (minor): `assets.save` writes the cache with `json.dump(..., open(path, 'w'))`: not atomic, handle closed only by
  GC, no lock. Two concurrent runs or a crash mid-write leave a torn file; the reader then says `ASSETS: unknown`
  (ValueError caught), but a valid non-dict JSON would raise KeyError in digest_line. Prefer tmp + replace.
- N2 (minor): the unit lists `release_lib` without importing from it in the assets path; harmless (a wider unit).
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

REVIEWED: sha256=35b567a046dc5b2e77421061270a6893d0c7bcbadffb816544e67a4f6affb3a3
VERDICT: APPROVE

## Independent re-review, re-verdict campaign g5 (integration/sm-complete at 2d0877b, 2026-11-13)

Reviewer wrote none of this code. Own worktree on review/reverdict-g5; digest recomputed with `uv run sm self reviews`
(equals the campaign list); every file of the unit diffed 479a8e8..2d0877b.
- Follow-up confirmed: `release_read_lib/assets.py` `save` writes the probe cache atomically: a per-pid temp file in
  the cache's own directory, then `os.replace`; the temp file is removed on any failure, the old cache survives a
  failed replace (test `test_the_assets_cache_is_replaced_atomically`). It also closes the leaked file handle.
  Residual (not blocking): no fsync before the replace; a power loss can leave an empty cache, which the next probe
  rewrites (it is a cache, not a receipt).
- Shared release_lib/runner.py change: `has_tag` (see release-pr.md) and the rejection guard before --open-pr.
- queue_lib/hzlib.py: `gh_api_method` reads `-X`/`--method`/`--method=`/`-XPUT` and treats `gh api` with a body
  flag as POST, so such calls get the 300 s write bound (was 120 s). Read paths of this unit unaffected.
- Shared core files changed 479a8e8..2d0877b (engine.py, native.py): `_supersede` now takes the older run's own
  lock (`PIPELINE.KEY`, non-blocking flock; Busy is caught by the caller and reported as waiting, nothing half-marked
  matters since the marking is idempotent); `gate_problem` honours `exempt=` only for paths on the core allowlist
  `EXEMPT_PATHS` = {`gate run`} (narrows the gate, never widens it); `switch_pending` ignores a promote pid file only
  when `ps -o etime=` proves the pid started after the file's UTC time (+2 s), and holds (fails closed) when either
  time is unreadable; clock.now() is float per the Clock protocol, Commands.run returns Result(rc, out). Nothing
  here weakens a gate of this unit.
- Tests through `sm gate run -- uv run pytest` at 2d0877b (release, pipeline, rehearse, lock, queue, go, dispatch-focused,
  moved release/queue suites, core engine/native/runlock/window): 2496 collected, 0 failed, 0 errors, 25 skipped (N2 of the previous round is fixed at 2d0877b). NOT verified: any live GitHub write.

COVERS: release assets
REVIEWED: sha256=2d71578481d99932faf97c46c1a538f80e8b0ad03337e00501503d49a0ce6515
VERDICT: APPROVE

## Independent re-review, re-verdict campaign g5, release plugin fix (integration/sm-complete at 539b7f7, 2026-11-13)

Reviewer wrote none of this code. Own branch review/reverdict-g5-ovvg from 539b7f7 (contains fix/release-plugin-runid 234dc8d, bead
heinzel-management-ovvg; no code change between 60a83a5 and 539b7f7); digest recomputed with `uv run sm self reviews`; code diffed 2d0877b..60a83a5 (release.py,
release_lib/runner.py only). The 479a8e8..2d0877b delta of this shared unit is reviewed in release-pr.md (previous section).
- Moved only because release_lib/runner.py is in this unit; the fix touches only the plugin release path, which
  `release assets` never calls (see release-resume.md).
- Tests through `sm gate run -- uv run pytest` at 60a83a5 (code-identical to 539b7f7; the same 85 files as before): 2506 tests, 0 failed, 0 errors, 25 skipped; the 10 tests of test_native_release_plugin_runid.py all pass.
  NOT verified: a live dispatch or any live GitHub write.

COVERS: release assets
REVIEWED: sha256=c40d59696d54f5c080d5161587435c2aa2a8b4e107193ce2a45040fcbd91b7bd
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

COVERS: release assets
REVIEWED: sha256=ebea14b1c71d850701c56e786caf077e6a20ee51d598912db897970e889e9051
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
- Delta: `release_core_checkout` has no machine default any more; `sm release assets` (and `release pins`) refuse with
  code 2 and name the key when it is unset (`binding.required_core_checkout`); `--digest` needs no checkout. The probe
  cache defaults to assets-health.json in the state directory, the setting still wins. The machine values file sets
  release_core_checkout, so promotion does not break the command here.
- Non-blocking, outside this command's behaviour but inside its digest: release_read_lib/chores.py:78 (the checklist of
  `sm release after`) names `sm beads triage`, which does not exist (`sm beads triage --help`: No such command;
  `sm tools gaps` lists 0 missing). It should name the real triage command or say it is missing.

COVERS: release assets
REVIEWED: sha256=f6adda229f47ffb42db561e8358ef319917ec78b870750db52202c1e88ec46dc
VERDICT: APPROVE

## Re-verdict at abc64a4, group B (2026-11-14)

Independent reviewer; wrote none of this code. Findings, baselines and tests: ~/Code/packets/sm-reverdict-abc64a4/B.md (recorded verbatim by the integrator).

COVERS: release assets
REVIEWED: sha256=11b66645dfeea7b2e549b9140f80395a864e80f8c3449d266dd5e9c54692c5a1
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Delta: runner.py (release_pr, above; release assets does not call it), mini.py (updater hold stricter), sonar.py scheme guard, toolmap row, defaults/config/protocols. No behaviour change for release assets.
COVERS: release assets
REVIEWED: sha256=9a3b6f8cc984b3a3a8496468d8f63f16940053b0763a5eedfb54d5e592bc4e43
VERDICT: APPROVE
