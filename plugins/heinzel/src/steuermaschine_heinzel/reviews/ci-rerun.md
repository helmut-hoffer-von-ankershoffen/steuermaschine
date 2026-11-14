# Port review: `sm ci rerun`

Old: `tools/hz-rerun-batch` (81 lines), `tools/hz_rerun_batch.py` (83 lines), the function `classify_pr` of
`tools/hz_digest.py`, and `--resume [--force]` of `tools/hz-ci-defer`. New: `native/ci_ops_lib/rerun_plan.py`,
`native/ci_ops_lib/checks.py`, `native/ci_ops_lib/rerun.py`, the edge in `native/ci_ops.py`.

## What moved unchanged

- `rerun_plan.py`: 64 of 67 lines identical (difflib, blank lines ignored). Changed: the docstring's first line, the
  import of `classify_pr`, one `noqa`. Every rule of the plan is among the unchanged lines.
- `checks.py`: `REAL_BAD`, `is_focused`, `classify_pr` copied from the digest module, 17 of 20 lines identical
  (two docstring lines named the old single-lane tool; one `noqa`).
- `rerun.py: batch()`: the body of the old `main()` in the same order: list -> open PRs -> runs -> queue items ->
  capacity -> main jobs queued -> macOS busy -> plan -> print -> re-run each run -> rewrite the list -> note.

## What changed, edge by edge

| Old | New | Risk to check |
|---|---|---|
| `subprocess.run(["gh", ...], check=True)` | `Gh.json(args)` for reads; a failed read refuses (exit 1) instead of a traceback | none |
| `subprocess.run(rerun_argv(...))` | `Gh.run(rerun_argv(...)[1:], retry=False)` | a re-run is a write: never tried twice (asserted) |
| the jobs of a run fetched up to 3 times with 3 jq filters | fetched once per call and asked three questions; a failed read fails for each question as before | fewer API calls; same decisions |
| a refusal printed `refusing: ...` and exited 0 | the same line, state `waiting`, **exit 75** | callers that tested for exit 0 |
| `~/.cache/hz-ci-deferred.txt` read and rewritten | the store list (`heinzel.ci_ops.deferred`); the old file is only read, and only until the store has a list | see [ci-defer.md](ci-defer.md), interplay |
| `HZ_QUEUE_STATE` or `~/.cache/hz-queue.json` | setting `ci_ops_queue_state_file`, read-only (`legacy.queue_items`) | to be replaced by the queue area's own function |
| `hz-note` process | one line in the journal (store kind `heinzel.journal`), as the single-lane commands do | not yet the tracking document |
| `hz-ci-defer --resume [--force]` | `sm ci rerun --all [--force]`; `--dry-run` shows the runs | as before it empties the list, also of the runs GitHub refused |
| no lock | a live re-run holds the store lock `heinzel.ci_ops.deferred` | none |
| argparse | Typer; `--n`, `--dry-run` unchanged | none |

## Tests

- 10 moved tests of `tools/tests/test_rerun_batch.py`: assertions unchanged, only the import renamed.
- 11 new tests through the command tree: the exact calls in order (queued-for-merge PR first, `--failed` only for a run
  with a green job), dry run with no write, the review gate, waiting with exit 75, one jobs read for three questions,
  a refused re-run stays on the list and is not retried, `--all` and its refusal, the lock, the queue file, the checks.

## For the independent reviewer

1. `diff tools/hz_rerun_batch.py native/ci_ops_lib/rerun_plan.py` (3 lines).
2. Read `rerun.batch` beside lines 27-78 of the old script: the order of reads and the conditions of `main_q`,
   `busy`, `macos_busy` (each falls back as before when the jobs cannot be read).
3. Confirm that nothing is re-run in a dry run, while waiting, or after a failed read.
4. `checks.classify_pr` is a second copy until the anomaly digest is native: one of the two must import the other.

## Changes after the independent review of 2026-11-12 (REVIEW-INDEPENDENT-ops.md)

Unit for all four write commands: `native/ci_ops.py` + `native/ci_ops_lib/`.

- G1: no setting has a path default any more. `ci_ops_legacy_deferred_file`, `ci_ops_queue_state_file`,
  `ci_ops_sonar_closures_file`, `ci_ops_beads_dir` are `None` until the configuration sets them; `ci_ops_bead_prefix`
  is empty. `ci sonar-close` refuses (exit 2) without a manifest; `ci quarantine flag` refuses without a tracker
  provider or a beads directory (a dry run still works). No private repository name and no old state path remains.
- G2: `ci rerun` declares `replaces=("hz-rerun-batch",)`; G3: `sonar_close.run` is now `close_reviewed`, so the
  name-following net no longer sends `ci quarantine list` there.
- C1: `ci rerun`'s journal line is written by `ci_ops.journal` (same store kind `heinzel.journal`); the unit no
  longer executes code of another area.
- C2: `ci defer --keep` holds for one call (said in the help); PRs that every call keeps go in the keep file
  (setting `ci_ops_keep_file`, no default). An unreadable or malformed keep file refuses (exit 2) before any call; a
  closed or unknown PR in it is skipped with a note.
- C3: a full bead id is used as it is (`quarantine.full_bead_id`); only a short id gets the prefix.
- C4: a note the tracker or `bd` refuses is not marked for the day, is reported `bead note NOT written` with what
  it said, and exits 1; a registry that cannot be read is `QUARANTINE-UNREADABLE` with exit 1 (a 404 is still
  "no entries").
- C5: the main route of `ci sonar-close` checks project, open status and type (bug or vulnerability) per planned
  issue and refuses the whole plan on a mismatch; a rule matches exactly (full rule equal, or bare key equal to the
  key part), no longer by suffix.
- C7: `ci_ops.quarantine_digest()` takes no flag and never writes; notes are `ci quarantine flag` only.
- G5: the gate tests assert the current refusal wording.

## Independent review (export of 1d0a65b)

Reviewer: independent; wrote none of this code. Findings in full: `~/Code/tmp-sm-reviews/ops2/REVIEW.md`.
Unit `ci_ops.py` + `ci_ops_lib/` (10 files) recomputed by `sm self reviews` in the export. The earlier review's
findings G1, C1-C5, C7, T1, T2 are fixed and verified.

Writes traced: under the store lock, only ids on the deferred list are re-run (gh run rerun, never retried), the
list rewritten without the done ids, one journal line. The batch refuses (exit 75, nothing re-run) while a release/*
run is active, main CI is queued, a main run has queued jobs or more than 2 macOS jobs are busy; the 14-of-20 ceiling
(rerun_plan.py verbatim, 4 jobs per PR) is kept, --n and --all override it as documented. The list is fed only by
ci defer (allowlisted), so the first-attempt rule of a candidate is untouched.
Open, not blocking: S1 (no branch check at re-run time, defence in depth), S2 (release/main detection reads the newest
100 runs unfiltered, as before), N2 (unreadable job counts fall back to 5 PRs).

COVERS: ci rerun
REVIEWED: sha256=718129193acab756892e3d434f79d6e5ade7abc2f81e577d904f2eb9627feb79
VERDICT: APPROVE

## Independent review of `--supersede-main` (fix/batch-gate at d1c7bbe, 2026-11-13)

Reviewer did not write the change (commit 9017e10, bead oyh). Read: ci_ops.py:160-230, ci_ops_lib/defer.py:70-115,
GitHub.cancel_run (core forge.py:350-366), test_native_ci_ops_defer.py. The other ci_ops files are unchanged since the
last approval (718129...).

- Only `gh run list --branch main --status queued`, filtered again to headBranch == "main", event push, workflow
  "CI" (defer.py:79-83): no release/*, bump/*, PR, tag or other workflow run is ever a candidate.
- Sorted newest first; the newest MAIN_KEEP = 2 are kept (defer.py:84, 95-96); runs in progress are never listed.
- Each older run is read again; any status outside queued/waiting/pending, or unreadable, is spared (defer.py:99-103).
- Dry run without --cancel; the live run holds the deferred store lock (ci_ops.py:220-223); every attempt audited; not
  recorded for `ci rerun`. --supersede-main refuses every other option (ci_ops.py:165-166).
- Not blocking, S1: a run that starts between the re-read (defer.py:100) and cancel_run's own GET is still cancelled:
  cancel_run refuses only `completed` (forge.py:358). Sub-second window; the API has no conditional cancel.
- Not blocking, N1: a release candidate pinned to an older main SHA whose run was superseded would see `cancelled`;
  the command is manual only, keep it out of the release train.
Tests: affected suites at d1c7bbe 290 passed, 2 skipped; `sm self lint` green.

COVERS: ci rerun
REVIEWED: sha256=08b7d7b600987b446e4cdc3ff0b2f61dcc5a25f0286726620617a87dfd7baf7d
VERDICT: APPROVE

## Independent review, campaign G2 (integration/sm-complete at 479a8e8, 2026-11-13)

Reviewer wrote none of this. Digest recomputed with `uv run sm self reviews` in a fresh worktree of 479a8e8.
Delta 1d69c43..479a8e8 in the unit: ci_ops.py (+48: `--supersede-main`, `rejudge_after`, setting ci_ops_rejudge_after_close), ci_ops_lib/defer.py (+48: `supersede_main`), new rejudge.py (302, now imported by ci_ops.py, so in this digest), core: github/cli.py (env via config.prefixed/secret), app.py (typed emit, core writers gated), native.py (digest follows imports, gate_problem, promote switch wait), config.py (typed Environment), engine.py (supersede of older runs, running state, spent digest), the rest typing only.

- `--supersede-main` (defer.py:190-235): lists only queued push runs of workflow CI on main, keeps the newest 2,
  re-reads each older run and spares any that started; dry without --cancel; live under the deferred store lock;
  audited; not recorded for `ci rerun`. Refuses every other option (exit 2) before any read. Earlier notes S1 (a
  sub-second start between re-read and cancel) and N1 (keep out of the release train) still hold; nothing calls it.
- `ci sonar-close --apply --pull-request N` now ends with `rejudge(N)` only after at least one issue is closed;
  Wait and Refuse are caught and reported, the closure's own result stays. The re-judge is a bounded wait (15 min,
  30 s polls); it re-runs only the head's Merge gate run, never another suite, and refuses on unclean Sonar before
  any POST. Not blocking, R1: any other exception from the re-judge (not Wait/Refuse) would end the command after
  the closures were written and lose their report; Gh and Sonar reads return (data, why), so none is expected.
- Not blocking, R2: the re-judge inside sonar-close is covered by this unit (files= names rejudge.py), not by the
  gate of `ci merge-gate rejudge`; both units approve the same bytes here.
- defer, rerun and quarantine flag: no behaviour change in their own paths; core changes are typing, the env layer
  (same variables) and the review gate. No force push, stash, auto-merge, PR head validation or hz-* call.
- json: every path returns one model (Deferral, Rerun, QuarantineFlag, SonarClose) or one Error document.
Tests (G2, `sm gate run -- uv run pytest` on the 15 files of this group: ci_classify*, ci_ops_*, dispatch*, rejudge, core github cli/forge, gate units, native, gate structure): 398 passed, 2 skipped, 1 failed: test_the_cores_own_writers_are_under_the_gate assumed `ci cancel` unapproved; fixed in the same branch to patch the review state (gate still asserted); then 8 passed.

COVERS: ci rerun
REVIEWED: sha256=8adabca2f0c2445fb1f02676fc58ce82149a5fab7a70d822bc04f63de8a648d4
VERDICT: APPROVE

## Independent re-verdict, campaign G2 (integration/sm-complete frozen at 2d0877b, 2026-11-13)

Reviewer wrote none of this. Digest recomputed with `uv run sm self reviews` in a fresh worktree of 2d0877b; it
matches the campaign list. Delta read: 479a8e8..2d0877b on every file of the unit.

- Unit delta: ci_ops.py and rejudge.py (ci_ops_lib/ unchanged). Earlier R1 fixed: `rejudge_after` now returns
  (lines, failed); any exception other than Wait/Refuse is reported as `re-judge: FAILED: TYPE: message` with NEXT
  `sm ci merge-gate rejudge PR`, after the closures, and `SonarClose.exit_code` is 1; the closure rows and calls
  stay in the report (test test_sonar_close_reports_the_closures_when_the_rejudge_breaks_and_exits_1).
- rejudge.py (in this digest through ci_ops.py) gains the head_sha, not-red and done-without-checks guards: each
  refuses before the POST, so a sonar-close re-judge writes less, never more. defer, rerun and quarantine flag:
  no change in their own paths. No force push, stash, merge, PR head validation or hz-* call.
- Related follow-up, landed outside this unit (queue_lib/hzlib.py, group g4): `gh_api_method` reads `gh api` with
  -f/-F/--field/--raw-field/--input and no -X/--method as POST, so `gh_timeout` bounds it as a write.
- Core delta shared by every unit: native.py `EXEMPT_PATHS`/`is_exempt` (an `exempt=` writer skips the gate only
  for `gate run`: the gate is stricter, not looser); `switch_pending(commands, now)` ignores a pending file whose pid
  was reused (process start from `ps -o etime=` later than the file's UTC time + 2 s) and holds when the start
  cannot be read (fail closed); the format matches bin/promote_lib.sh `mark_switch_pending` (`PID %Y-%m-%dT%H:%M:%SZ`);
  `NativeRuntime.commands` (read-only registry lookup). engine.py: `_supersede` marks an older run under that run's
  own store lock (non-blocking flock, Busy = exit 75), pipelines only.
Tests (`sm gate run -- uv run pytest` on 17 files: ci_classify*, ci_ops_*, dispatch*, rejudge, core github
cli/client/forge, gate units, core native, gate structure, queue hzlib): 479 passed, 2 skipped, 0 failed.

COVERS: ci rerun
REVIEWED: sha256=56fb724e2b65f92f4dfdfae8abd8b10dbf8cccd74126dc4f0e6feb463726555a
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

COVERS: ci rerun
REVIEWED: sha256=a4056f6524543d38f1acb57b96600ae4fc5988e09f6eb23b549f6630fdd9f978
VERDICT: APPROVE

## Campaign re-verdict g2 (2026-11-14, independent; reviewer wrote none of this code)

Target: integration/sm-dod frozen at c36bbe1 (baseline 903a1b7, where this unit was APPROVED). Digest recomputed with
`uv run sm self reviews --format json` in a fresh worktree of c36bbe1; it equals the campaign list.

- Unit delta: none of ci_ops.py, ci_ops_lib/* or rejudge.py changed (`git diff 903a1b7 c36bbe1` is empty for them).
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

COVERS: ci rerun
REVIEWED: sha256=1480f844dbcab00e5d392012fc6931ac405dcce9336d9d209bae3bd9a905d8c0
VERDICT: APPROVE

## Re-verdict at abc64a4, group C (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-abc64a4/C.md (recorded verbatim by the integrator).

ci_ops.py, ci_ops_lib/* (rerun.py, rerun_plan.py, checks.py), rejudge.py, github/* unchanged since c36bbe1; only
native.py/models.py moved. Rerun allowlist and plan untouched; ci_ops_rerun tests green.
COVERS: ci rerun
REVIEWED: sha256=ab0c3ffb9c5b47b11926a3b7fc2c5fe96259eca38d00fe2f3c5fba761648ce96
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does. github/sonar.py now returns (0, "unsupported URL scheme") for a non-http(s) URL instead of opening it (leak guards): stricter, the callers already treat status 0 as unreadable.
COVERS: ci rerun
REVIEWED: sha256=01fcf631ec489e92842a2dd0efee287dab60b65ccebca5f9a7bacaa189a9c11d
VERDICT: APPROVE
