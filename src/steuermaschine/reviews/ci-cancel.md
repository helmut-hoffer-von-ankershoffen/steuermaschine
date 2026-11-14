# Review: `sm ci cancel`

COVERS: ci cancel

Unit: `src/steuermaschine/github/cli.py` and every core file it imports (`sm self reviews` lists them and the digest).

What it writes: with `--live`, one cancel of one run on the CI host; without `--live`, nothing.

What the reviewer checks: the dry run sends no write; `--live` sends exactly one cancel for exactly the named run;
a refused or failed cancel is reported as such; the command refuses (exit 70) while this file does not approve the digest.

State: reviewed (see below).

## Independent review, campaign G2 (integration/sm-complete at 479a8e8, 2026-11-13)

Reviewer wrote none of this. Digest recomputed with `uv run sm self reviews` in a fresh worktree of 479a8e8.
Read: github/cli.py (whole), `GitHub.cancel_run` (forge.py:350-366), app.py `gate`/`core_units`, native.py
`gate_problem`, and the core diff 1d69c43..479a8e8.

- The gate runs first, before any read, for the dry run too: an unapproved unit exits 70 with no call (test
  test_core_gate_units.py).
- Without --live: one GET of the run, no write, state `dry_run` with the call it would make.
- With --live: one GET, then exactly one POST `repos/R/actions/runs/RUN/cancel` for the named run, never retried; a
  completed run is `not_cancellable` (no POST); an unreadable run or repo is `unknown: ...` with no POST; a failed
  POST is `unknown: ...` with its reason. The exit code follows the model.
- json: one CancelResult document (or one Error).
- Not blocking, C1: RUN is not checked to be digits; a non-numeric value builds a different API path. The GET of the
  same path must first return a run with a non-completed status, so a write is not expected; a digit check is cheap.
- Not blocking, C2: unlike native write commands, this core writer does not wait while a promote switches the live
  sm (`switch_pending`); a single cancel does not depend on the install, so it does not matter for this command.
- No audit line and no branch rule (main and release/* runs can be cancelled): by design a manual, dry-by-default
  command; the help says to check branch and event first.
Tests (G2, `sm gate run -- uv run pytest` on the 15 files of this group: ci_classify*, ci_ops_*, dispatch*, rejudge, core github cli/forge, gate units, native, gate structure): 398 passed, 2 skipped, 1 failed: test_the_cores_own_writers_are_under_the_gate assumed `ci cancel` unapproved; fixed in the same branch to patch the review state (gate still asserted); then 8 passed.

COVERS: ci cancel
REVIEWED: sha256=dff4f87be49567b736a4e5b5615905d3ac17d08372ed86f28584a2239cd93636
VERDICT: APPROVE

## Independent re-verdict, campaign G2 (integration/sm-complete frozen at 2d0877b, 2026-11-13)

Reviewer wrote none of this. Digest recomputed with `uv run sm self reviews` in a fresh worktree of 2d0877b; it
matches the campaign list. Delta read: 479a8e8..2d0877b on every file of the unit.

- Earlier C1 fixed: github/cli.py refuses RUN unless `run.isascii() and run.isdigit()`, exit 2 (USAGE), before the
  gate and before any call (test test_ci_cancel_refuses_a_run_that_is_not_digits_before_any_call: no call, no write).
  Dry run and --live paths are otherwise unchanged: one GET, at most one POST cancel of the named run.
- app.py: `locked()` wraps focus add/done and alarm ack in the store lock (Busy: exit 75); `sm self reviews` uses
  `is_exempt`. C2 (no promote-switch wait for this core writer) still holds and still does not matter.
- Core delta shared by every unit: native.py `EXEMPT_PATHS`/`is_exempt` (an `exempt=` writer skips the gate only
  for `gate run`: the gate is stricter, not looser); `switch_pending(commands, now)` ignores a pending file whose pid
  was reused (process start from `ps -o etime=` later than the file's UTC time + 2 s) and holds when the start
  cannot be read (fail closed); the format matches bin/promote_lib.sh `mark_switch_pending` (`PID %Y-%m-%dT%H:%M:%SZ`);
  `NativeRuntime.commands` (read-only registry lookup). engine.py: `_supersede` marks an older run under that run's
  own store lock (non-blocking flock, Busy = exit 75), pipelines only.
Tests (`sm gate run -- uv run pytest` on 17 files: ci_classify*, ci_ops_*, dispatch*, rejudge, core github
cli/client/forge, gate units, core native, gate structure, queue hzlib): 479 passed, 2 skipped, 0 failed.

COVERS: ci cancel
REVIEWED: sha256=46ccf74bea3f20507e32f29995fb4c398aa22c4de5f6a1c7031ed18b0bfd6365
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

COVERS: ci cancel
REVIEWED: sha256=6a9d4bd4291037ec2b7e860ee22ff56255867c2d3bab056cc1d44293caab037c
VERDICT: APPROVE

## Campaign re-verdict g2 (2026-11-14, independent; reviewer wrote none of this code)

Target: integration/sm-dod frozen at c36bbe1 (baseline 903a1b7, where this unit was APPROVED). Digest recomputed with
`uv run sm self reviews --format json` in a fresh worktree of c36bbe1; it equals the campaign list.

- Unit delta: github/cli.py unchanged; the digest moved through the shared core and the new core commands package
  (commands/doc, gate, lim, window: read-only or exempt, imported by app.py) and app.py (`self migrate-state`, `natives()`
  merging core and plugin native commands, a later entry wins). `ci cancel` keeps one GET and at most one POST cancel of
  the named, digit-only run.
- Shared core delta (every unit): native.py takes `writers.lock` in the project's state directory around each run of a
  non-exempt write command (shared flock; `sm self migrate-state` takes it exclusively, non-blocking with a bounded
  wait, so it never moves state under a writer). The promote-switch check now runs inside the lock: a single run still
  exits 75, a loop still skips the tick; `guarded(True) or 0` keeps the old exit codes. config.child_environment gains
  drop/drop_prefixes (same filtering the callers did by hand), `search_path()`, `variable()`; plugins.old_variable lets
  an old env variable set a setting only when no file or SM_ variable does (source named in `sm self config`).
  helpdoc lint letters O/X, models StateMigration, hookspec sm_state_moves: no runtime effect on this unit.
- Safety: no force push, stash, auto-merge, --admin or hz-* call added; the cancel and rerun allowlists are unchanged.
Tests (`sm gate run -- uv run pytest`, by file; pytest-xdist is not installed in this repo, so without -n 2):
plugin: test_native_merge_ledger, merge_tier_rules, ci_classify, ci_classify_jev, ci_ops_defer, ci_ops_quarantine,
ci_ops_rerun, ci_ops_sonar, dispatch, dispatch_cov_a_focused, dispatch_focused, rejudge, admission_cov_a_focused_admit,
admission_cov_a_focused_admit_run: 451 tests, 2 skipped, 0 failed. core: test_core_github_cli, client, forge, logs, sonar,
core_native, core_statemove, gate_structure: 126 passed, 0 failed.

COVERS: ci cancel
REVIEWED: sha256=815f9b79aaf59d84f5260f88f14c83ead2caf96f8376b5efbbf7f79a066a136d
VERDICT: APPROVE

## Re-verdict at abc64a4, group C (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-abc64a4/C.md (recorded verbatim by the integrator).

Own code (github/cli.py, client.py, forge.py, base.py, models.py, settings.py, sonar.py, commands/*, window.py,
dispatch.py, statemove.py, config.py, engine.py) unchanged since c36bbe1. Checked `cancel`: digits-only run id before
any API path, `gate(rt, "ci cancel")` before the call, dry unless `--live`. Shared delta above; nothing reaches the cancel.
Observation (pre-existing, not a finding of this delta): core `ci cancel` goes through `app.gate`, not
`native.writing`, so it does not take the writers' lock nor wait out a pending switch. Harmless for a one-call API write.
COVERS: ci cancel
REVIEWED: sha256=9e2e73b1f2c3c6ca6af34634ed361fb0316ad1f6a2d716e5ae8df2e4459482d0
VERDICT: APPROVE

## Delta review: feat/sm-gate-lanes d94de45 (independent reviewer)

Changed in this unit since the last verdict: src/steuermaschine/runlock.py, src/steuermaschine/commands/gate.py (light lane, rerun refusal). Reviewed in full in two rounds (e6db9d9, d94de45),
probes now tests; see ~/Code/packets/sm-gate-lanes/REVIEW-2.md. Nothing in this command got weaker.

REVIEWED: sha256=3087ff8e410410e15cb134d5d41dc62256da32d3103f9b8d7769d4ac3053b204
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged; its digest takes in app.py, commands/gate.py, runlock.py, usagelog.py, statsreport.py, config/protocols/defaults and sonar.py, all reviewed under core-local-writers.md. `ci cancel` runs no gated command and is logged like every command.
COVERS: ci cancel
REVIEWED: sha256=8e9dae4e67153860beea399bca5b746426d35f8219dfd6c7bf508bf53b1a8d5d
VERDICT: APPROVE

## Re-verdict at f346492 (bead heinzel-management-04ro + integration/sm-batch3, 2026-11-14)

Independent reviewer; wrote neither 04ro nor window admission. Review: ~/Code/packets/sm-04ro/REVIEW.md.
Stale through shared core (app.py review verdict, verdicts.py, window.py). `ci cancel` code unchanged.
COVERS: ci cancel
REVIEWED: sha256=c65a9aa23391afe92fb11fcf62c26b7923404280448713fddc4cd5b980bebbe2
VERDICT: APPROVE
