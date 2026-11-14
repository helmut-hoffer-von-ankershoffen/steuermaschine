# Port review: `sm ci sonar-close`

Old: `tools/hz-sonar-close` (165 lines). New: `native/ci_ops_lib/sonar_close.py` (173 lines) and the edge in `native/ci_ops.py`.

## What moved unchanged

110 of the 144 old lines are identical (difflib, blank lines ignored): `Stop`, `open_findings`, `required_text`,
`reviewed_rows`, `exact_plan`, the scope checks and the selection in `run`, the order "validate the complete plan,
then per issue comment, then transition, then count again". The 34 others are the script header and imports, the
process call in `api`, argparse, the prints and `main`.

## What changed, edge by edge

| Old | New | Risk to check |
|---|---|---|
| `subprocess.run(["sonar", "api", ...], timeout=60)` | `commands.run(["sonar", "api", ...], timeout=60)`: same argv, same timeout | the transport is still the signed-in `sonar` CLI; the core has `SonarSettings` but no Sonar client for writes |
| argparse (`allow_abbrev=False`) | Typer options with the same names; a typo is a usage error (exit 2) as before | none (the 6 typo cases are moved tests) |
| `LIST` beside the tools directory | setting `ci_ops_sonar_closures_file` (default: the same file), `--config` as before | none |
| `print` | lines collected into the result model; the plan and every successful write are kept on the edge | none |
| `refused: <why>` on stderr, exit 1 | `Refuse(<why>)`, exit 1; after a failure half way the message also says which writes already happened, in order | new information, same exit code |
| no token handling | none either: no token is read, stored, printed or put in a model | confirm |

## Tests

- 16 moved tests (52 cases) of `tools/tests/test_sonar_close.py`: every test body and assertion unchanged. Only the fixture
  `setup` changed: `close.main(argv)` runs the command through the whole command tree, and every `sonar` process goes
  through the `Commands` provider to the same mock.
- 5 new tests: preview in text and json, the review gate (exit 70, no call), the exact calls of `--apply` in order
  (one read, then comment and transition per issue, one read), a failure half way, a missing or broken manifest.

## For the independent reviewer

1. `diff tools/hz-sonar-close native/ci_ops_lib/sonar_close.py`: the checks must be byte-identical.
2. Confirm no `post` can happen without `--apply` and before `exact_plan` returned.
3. No call against the real Sonar was made for this port, not even a read.

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

Writes traced: none without --apply. With --apply the whole plan is validated first: PR route exact reviewed keys
with project, file, full rule, PR, open status and BUG/VULNERABILITY type; main route each issue checked for project,
open status and type, exact rule match; any mismatch refuses before the first write. Per issue: comment with the
reviewed reason, then the transition (falsepositive or accept); a failure half way names what was written.
Open, not blocking: N3 (the main route closes every open bug/vulnerability of a reviewed file and rule, including one
found after the review; the preview lists each key).

COVERS: ci sonar-close
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

COVERS: ci sonar-close
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

COVERS: ci sonar-close
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

COVERS: ci sonar-close
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

COVERS: ci sonar-close
REVIEWED: sha256=40967e671bd993dd9aba3ac4663349f0ea732196b8213069612094418fb24690
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

COVERS: ci sonar-close
REVIEWED: sha256=439c720c490e98684416fa50511f623c874d6b97ebb2beabc1d5acadaeb3d1d8
VERDICT: APPROVE

## Re-verdict at abc64a4, group C (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-abc64a4/C.md (recorded verbatim by the integrator).

ci_ops_lib/sonar_close.py, github/sonar.py, rejudge.py unchanged since c36bbe1; only native.py/models.py.
Close reasons and refusals untouched; ci_ops_sonar tests green.
COVERS: ci sonar-close
REVIEWED: sha256=f31373f1d63d22c431df77cca5f4be382fd0ed2b742bf27ad25c9eb8241b5dfb
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does. github/sonar.py now returns (0, "unsupported URL scheme") for a non-http(s) URL instead of opening it (leak guards): stricter, the callers already treat status 0 as unreadable.
COVERS: ci sonar-close
REVIEWED: sha256=4154124ef44878da8479eb368d9ec0d7ebab1eaf1761c51bafde6d7c3971d8fb
VERDICT: APPROVE
