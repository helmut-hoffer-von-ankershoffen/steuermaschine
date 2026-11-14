# Review: `sm ci merge-gate rejudge PR` (new unit)

Unit: `native/rejudge.py` and every core file it imports (`sm self reviews` lists 14 files and the digest).

What it writes: one POST `actions/runs/GATE_RUN/rerun` on the core repository (a new attempt of the head's Merge gate
run); with `--dry-run` nothing. Also called by `ci sonar-close --apply --pull-request N` after a closure.

## Independent review, campaign G2 (integration/sm-complete at 479a8e8, 2026-11-13)

Reviewer wrote none of this. Digest recomputed with `uv run sm self reviews` in a fresh worktree of 479a8e8.
Read: the whole of rejudge.py, its callers (ci_ops.py `rejudge_after`, ci_read.py help), the core diff 1d69c43..479a8e8.

Order before the one write, each a refusal with no write: PR readable and open (exit 1); CI run of the head exists
(exit 1), is completed (Wait, exit 75), is not cancelled (exit 1); a Merge gate run is found through the gate checks'
details URL, else the newest gate run of the head (none: exit 1); the gate run is completed (Wait, exit 75); Sonar
gate OK with all three new-code ratings A and nothing unread (else exit 1). Then one POST, never retried; a refused
POST is exit 1. The wait after it is bounded (`--wait`, default 15 min, 30 s polls; 0 = no wait) and ends `running`
(exit 0) or `done` with the gate checks' verdicts (exit 1 when one is not success).

- Correct target: only the Merge gate run is re-run, never CI or the Claude review suite. No push, no force push,
  no stash, no merge, no dispatch: the re-run reuses its original event, so no PR head SHA is used for validation.
- No lock: a second concurrent call sees the gate run in progress (Wait) or GitHub refuses the re-run (exit 1).
- json: one `Rejudge` document, or one Error document on a refusal/wait.
- Not blocking, J1: `done` with no gate check found after the re-run gives exit 0 and NEXT `sm queue why`
  (`any()` over an empty list); better state `done` only with both GATE_CHECKS present, else `running`.
- Not blocking, J2: a gate run found through a details URL is not checked for `head_sha == head`; the URL comes from
  the check runs of that head, so a mismatch is not expected.
- Not blocking, J3: it does not check that merge-gate or coverage-floor is red first; a re-run of a green gate costs
  one Merge gate run, nothing else.
- No hz-* call; `sonar_project` and `repo` empty refuse before any read.
Tests (G2, `sm gate run -- uv run pytest` on the 15 files of this group: ci_classify*, ci_ops_*, dispatch*, rejudge, core github cli/forge, gate units, native, gate structure): 398 passed, 2 skipped, 1 failed: test_the_cores_own_writers_are_under_the_gate assumed `ci cancel` unapproved; fixed in the same branch to patch the review state (gate still asserted); then 8 passed.

COVERS: ci merge-gate rejudge
REVIEWED: sha256=bfbc20d36b5fddf63cbbbd63f442a2e39ae2d5d4fa3c1bb49a57fba92197012a
VERDICT: APPROVE

## Independent re-verdict, campaign G2 (integration/sm-complete frozen at 2d0877b, 2026-11-13)

Reviewer wrote none of this. Digest recomputed with `uv run sm self reviews` in a fresh worktree of 2d0877b; it
matches the campaign list. Delta read: 479a8e8..2d0877b on every file of the unit.

- Earlier J1 fixed: `done` with no gate check exits 1 and prints `VERDICT  none: ...` (test
  test_rejudge_done_without_any_gate_check_is_not_success).
- Earlier J2 fixed: a gate run whose `head_sha` is not the PR head refuses before any write (exit 1; test
  test_rejudge_refuses_a_details_url_run_of_another_head asserts no write).
- Earlier J3 fixed: after the gate run is completed, `is_red` refuses when no gate check ended outside
  success/neutral/skipped, or (no gate check) the gate run's own conclusion is green or empty; no write (tests
  test_rejudge_refuses_when_the_gate_is_not_red, test_rejudge_without_gate_checks_follows_the_gate_run_conclusion).
  Order: head_sha, then completed (Wait), then red, then Sonar, then the one POST.
- Not blocking, J4: `done` with only one of the two GATE_CHECKS present and green exits 0; a missing second check
  is not named. Not blocking, J5: when the check runs cannot be read, `is_red` falls back to the gate run's
  conclusion and the refusal text does not show the read error.
- Core delta shared by every unit: native.py `EXEMPT_PATHS`/`is_exempt` (an `exempt=` writer skips the gate only
  for `gate run`: the gate is stricter, not looser); `switch_pending(commands, now)` ignores a pending file whose pid
  was reused (process start from `ps -o etime=` later than the file's UTC time + 2 s) and holds when the start
  cannot be read (fail closed); the format matches bin/promote_lib.sh `mark_switch_pending` (`PID %Y-%m-%dT%H:%M:%SZ`);
  `NativeRuntime.commands` (read-only registry lookup). engine.py: `_supersede` marks an older run under that run's
  own store lock (non-blocking flock, Busy = exit 75), pipelines only.
Tests (`sm gate run -- uv run pytest` on 17 files: ci_classify*, ci_ops_*, dispatch*, rejudge, core github
cli/client/forge, gate units, core native, gate structure, queue hzlib): 479 passed, 2 skipped, 0 failed.

COVERS: ci merge-gate rejudge
REVIEWED: sha256=5336d0011354b266b517349bde38b3428b87d9275aa98f2fe17d27370f7d7978
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

COVERS: ci merge-gate rejudge
REVIEWED: sha256=569ae2c3173c7a7597c1c35a22ba148ba3a509ca2d211463f5ae5769d71682fe
VERDICT: APPROVE

## Campaign re-verdict g2 (2026-11-14, independent; reviewer wrote none of this code)

Target: integration/sm-dod frozen at c36bbe1 (baseline 903a1b7, where this unit was APPROVED). Digest recomputed with
`uv run sm self reviews --format json` in a fresh worktree of c36bbe1; it equals the campaign list.

- Unit delta: rejudge.py did not change (`git diff 903a1b7 c36bbe1` is empty for them).
  The digest moved only through the shared core files above.
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

COVERS: ci merge-gate rejudge
REVIEWED: sha256=3a3513f4152bf8a9bb7551bbd8942a3699ad052992d8112282b7c19e20870e12
VERDICT: APPROVE

## Re-verdict at abc64a4, group C (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-abc64a4/C.md (recorded verbatim by the integrator).

rejudge.py, github/* unchanged; only native.py/models.py. rejudge tests green.
COVERS: ci merge-gate rejudge
REVIEWED: sha256=042e29620b4ca9554ed8a522d851edd5d1f342d5772c2cdd121e558e51e30665
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does. github/sonar.py now returns (0, "unsupported URL scheme") for a non-http(s) URL instead of opening it (leak guards): stricter, the callers already treat status 0 as unreadable.
COVERS: ci merge-gate rejudge
REVIEWED: sha256=bc3a5ece0c07d5dd4c8e062e598f55050474b0fe28367c8e052e0501c6ee3e20
VERDICT: APPROVE
