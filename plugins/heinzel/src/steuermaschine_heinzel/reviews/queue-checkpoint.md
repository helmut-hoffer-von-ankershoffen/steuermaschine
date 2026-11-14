# Port review: `sm queue checkpoint`

The port review of this command is one file with `sm queue main-rerun`: [queue-main-rerun.md](queue-main-rerun.md).
The independent reviewer adds `COVERS:`, `REVIEWED: sha256=...` and the verdict line below.

## Independent review (main f9134cc)

Reviewer: independent; wrote none of this code. Unit recomputed by `sm self reviews` (21 files, shared with main-rerun).

- Continuity before evidence: `establish` refuses under a merge freeze or release hold (main_checkpoint.py:668), builds
  the full proof first (`verify_configured`, :676; registry snapshot re-checked at the end, :649-654), re-checks the
  hold (:677-678), and only then with `--apply` writes the one config file atomically (temp + `os.replace`, symlinks
  refused, :679-692). Without `--apply` nothing is written but temp downloads (bounded, :126-141).
- Needs the idle queue lock, released in `finally` (queue_daemon.py:1551-1562). The written config grants nothing by
  itself: `permits` re-verifies all evidence on each use (:698-).
- No hz-* script; the reader is `uv run --no-project ... python -I main_checkpoint_reader.py` with a 180 s timeout.
- Findings, not blocking: queue_main.py:47 catches ValueError/OSError/KeyError only; a reader timeout
  (`subprocess.TimeoutExpired`), `TypeError` or `BadZipFile` gives exit 70 with a traceback (still one JSON Error
  document) instead of a refusal; the success path and `--apply` are untested here (the release slice's test file).

## Independent review (feat/repair-main 7fc817a, base main 5c87f14)

Reviewer: independent; wrote none of this code. Unit recomputed by `sm self reviews`. Stale only because it shares
queue_lib/queue_daemon.py: diffed every file of the unit against 5c87f14; the only change is the `sm queue main-rerun`
repaired-plan path (queue_daemon.py:501-507, :514-519, :579-583, :650-669, :679-682; see reviews/queue-main-rerun.md).
No queue add/drop, daemon tick, ship, PR, release or train code path changed; the restored-by-rerun result keeps its
shape (one optional `repaired` key). Related suites and `sm self lint` pass through `sm gate run`.

COVERS: queue checkpoint
REVIEWED: sha256=a0dd1a161498729cb3a2e613bc97e10aa400b9c613d5779613ebc8846c6794a2
VERDICT: APPROVE

## Independent review of integration/sm-complete at 479a8e8 (against live 1d69c43, campaign 2026-11-13)

COVERS: queue checkpoint
REVIEWED: sha256=91a527b14bc305ccfa6e165148c013df98d9832cf7989b194ba904163b3495df

Reviewer: independent (campaign group 4, branch review/campaign-g4); wrote none of this code. Digest recomputed with
`uv run sm self reviews` at 479a8e8. native/queue_main.py is unchanged since 1d69c43; the repaired-plan main-rerun path
(main_rerun_plan, _main_rerun, the `repaired` result) is unchanged since its review at 7fc817a. The unit changed only
through the shared queue_lib and core files; the full reading is in reviews/daemon-queue.md (same date). Effects here:
- An unreadable queue state refuses (moved main() -> exit 1 REFUSED, alarm) instead of reading as empty. main-rerun's
  first state write (update(main_reruns=...), under the state lock) comes before `gh run rerun`, so a corrupt file
  never starts a rerun; checkpoint reads main_failure through load() and refuses the same way.
- `gh run rerun` is a write bounded at 300 s (hzlib.gh_timeout); a timeout is exit 124: the request entry is removed
  unless it carries an infra receipt (unchanged rule), so an uncertain scoped rerun is never sent twice.
- main_checkpoint: a success job with empty steps is proven by a non-empty log (job_proof, MAX_JOB_LOG 128 MiB, bounded
  download); reviewed in the batch-release round; any other error fails closed.
- --dry-run starts nothing; checkpoint still needs the idle queue lock and writes its config only with --apply.
- Old finding stays, not blocking: queue_main.py catches ValueError/OSError/KeyError only (a reader timeout is exit 70
  with one JSON error document, not a refusal).

Tests (through `sm gate run`): test_native_queue_main_commands, test_native_queue_repaired_main,
moved/test_native_queue_main_rerun, moved/test_native_queue_main_cancelled_timeout, test_native_queue_cov_a_checkpoint,
test_native_queue_cov_a_checkpoint_chain, test_native_queue_state_safety: pass. The one red test on the branch
(moved/test_native_queue_recheck.py) is the tick's, not on this path.

VERDICT: APPROVE

## Independent re-verdict of integration/sm-complete at 2d0877b (against 479a8e8, campaign 2026-11-13)

COVERS: queue checkpoint
REVIEWED: sha256=d7544665005d70c6c99077798d737dc866be133d207a10541ea6e0b4b1418772

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
This path: main_checkpoint* unchanged; the shared queue_lib delta above was read for its effect on it.

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

COVERS: queue checkpoint
REVIEWED: sha256=0bbaafc4d43a8333687ad2aa167e00223dd64c9f55d05ae23e1a0c6ea9d85183
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

Tests: `sm gate run -- uv run pytest` over the 32 files plugins/heinzel/tests/test_native_{queue,ship,merge_ledger,merge_tier}*.py and moved/test_native_{queue,ship}*.py at c36bbe1, one run, exit 0, none failed. (`-n 2` is refused: no pytest-xdist in this repo, so the run was serial.)

COVERS: queue checkpoint
REVIEWED: sha256=77a7a02cd72c9e5cc95e1388559a2d52da40e36a96e9b2c333e8d1936e5d2660
VERDICT: CHANGES (finding 1: main_batch.valid_run accepts reruns without the merge-tier gate)

## Re-verdict at abc64a4, group B (2026-11-14)

Independent reviewer; wrote none of this code. Findings, baselines and tests: ~/Code/packets/sm-reverdict-abc64a4/B.md (recorded verbatim by the integrator).

COVERS: queue checkpoint
REVIEWED: sha256=ca3cde30bf0175f78e319d01511a850f5c916eb631c26bc31a429ac189e90adf
VERDICT: APPROVE

## Re-verdict at feat/sm-next-admission 5fe179a (delta review 4, 2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-next-admission/REVIEW.md, delta review 4 (recorded verbatim by the integrator; COVERS from `self reviews`).

COVERS: queue checkpoint
REVIEWED: sha256=6e9614ed7681abc0f43d23a33db26fea945bfe24b1fb6c6e68c3a2474f1247db
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Delta: queue.py and queue_hold.py (next-admission gtco: the hold stops the tick; --except owner/repo#N), defaults.py (FileStore.delete), config/protocols. No other branch touches these files; the merged queue.py equals 5fe179a's. Matches the review file.
COVERS: queue checkpoint
REVIEWED: sha256=114bf381b51672e06d13f59848e9f1c34a9ce859e0f509ad3aa7f5628772721e
VERDICT: APPROVE
