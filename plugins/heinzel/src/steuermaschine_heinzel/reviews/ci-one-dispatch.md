# Port review: `sm ci one dispatch` (also covers `ci one rerun`, `head validate`, `ci window run`)

Old: `tools/hz-ci-one` (702 lines) and `tools/hz_ci_one.py` (243 lines). New: `native/dispatch_lib/focused.py`,
`native/dispatch_lib/rules.py`, `native/dispatch.py`, `native/window.py`, core `steuermaschine/window.py`.

## What moved unchanged

- `rules.py`: 224 of 243 lines identical. Changed: command names in messages (`sm ci one ...`), and `sha_refusal` /
  `throwaway_branch` now call the core (`steuermaschine.window.sha_uses`, `throwaway_branch`) with the same result
  (the moved tests `test_sha_refusal_names_each_use_of_the_sha`, `test_throwaway_branch_and_option_rules` pass unchanged).
- `focused.py`: 519 of the 592 body lines identical (measured with difflib, line by line). Every guard, every
  inventory check, the exact-capability check with its two pinned digests, the pending-marker protocol and the order
  inside `admit` / `start` are among the unchanged lines.

## What changed, line by line (the 73 lines)

| Old | New | Risk to check |
|---|---|---|
| `subprocess.run(["gh", ...], timeout=60)` in `gh()` | `Gh(commands, repo, timeout).run(args, retry=False)` | `retry=False` on purpose: a POST must never be repeated. rc 124/127 map to "GitHub API unavailable" |
| `gh run view --log` without timeout in `tally_of` | the same call through `gh(..., ok=True, timeout=300)` | a log read that fails now yields an empty tally instead of hanging |
| `fcntl.flock` on `<state>.lock` (blocking) | `store.lock("heinzel.dispatch.admission")`, retried every 50 ms | same exclusion on this machine; **not shared with the old tool's lock file** (see below) |
| `time.time`, `time.sleep`, `time.strftime` | `clock.now`, `clock.sleep`, `stamp()` | none |
| `print(...)`, `print(..., file=sys.stderr)` | `out(...)`, `warn(...)` (collected into the result model / said on stderr) | text identical |
| `hz-note` process | `journal()`: one dated line in the store, kind `heinzel.journal` | the journal is not yet the page `sm note` writes (that tool is another slice's) |
| `hz-faillog RUN --no-hint` process in `wait` | `fail_log()`: failed jobs and the last 25 log lines each, through the core `GitHub` provider | **different text** in the fail log of a red run; the verdict (exit code, conclusion) is unchanged |
| `import hz_focused_slots` | `from ..admission_lib import focused_slots`; ImportError refuses ("the SHA guard cannot be decided") | fail closed |
| constants `R`, `STATE`, `CORE_CLONE` (`HZ_CORE_REPO`), `SCRATCH` | settings `repo`, `dispatch_state_file`, `dispatch_core_clone`, `dispatch_scratch_dir`, bound per call | the state file is a NEW path |
| `git` through `subprocess` with GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE removed | the same argv and the same cleaned environment through `commands.run(..., env=)` | none |
| argparse | Typer options in `dispatch.py`; `SystemExit(text)` becomes `Refuse(text)` (exit 1), `SystemExit(2)` exit 2 | option names unchanged; `--json` is the root `--format json` |

## What is new (not a move)

- `steuermaschine/window.py`: the line of heads (one at a time, one request per branch, cap, lease). 7 tests.
- `head validate`: the former `validate` of the wrapped tree, now calling the library in process instead of one
  process per lane: window -> throwaway `commit-tree` -> push through the run lock -> `cmd_dispatch` per lane ->
  `wait` per run -> `delete_throwaway` (only while the branch still points at its commit) -> leave the window.
  The branch is deleted through the API (`DELETE git/refs`), no longer with a gated `git push :ref`.

## Interplay with the old tool while both exist

- The 5-run cap holds across both: `_capacity` counts focused runs from GitHub's own run inventory, not only from
  local receipts.
- Receipts and the admission lock are NOT shared: `sm ci one status` does not list runs the old tool started, and an
  old and a new start can observe capacity at the same moment (two starts at 4 in flight could both pass).
  Mitigation: switch every caller in one step; the window (`head validate`) is new-only anyway.

## Tests

- 73 moved tests (`test_native_dispatch_focused.py`), assertions unchanged except one message that named the old
  command; 1 skipped until the admission area's decoder is merged (`focused_slots`); 3 dropped: two `--help` tests of
  the old script (replaced by the help contract lint) and one that tested another tool's status code.
- 13 new tests through the command tree on fakes (`test_native_dispatch.py`), among them the exact order of GitHub
  calls of one dispatch with exactly one POST as the last call, the SHA guard refusing a PR head with no write, the
  review gate (exit 70), and validation with a red lane, a busy window and an existing validation branch.

## For the independent reviewer

1. Read the 73 changed lines: `diff <(sed -n 56,647p tools/hz-ci-one) native/dispatch_lib/focused.py`.
2. Confirm `gh()` cannot retry a write and that no path reaches the POST before `_capacity` and `attachment_guard`.
3. Decide whether the unshared admission lock is acceptable for the switch-over (see above).
4. `head validate` has never run against GitHub: first use on a low-risk head, watched.

No verdict yet.

## Independent review (2026-11-12)

Reviewer: independent, wrote none of the code. Full findings with file:line and failure scenarios:
`REVIEW-INDEPENDENT-dispatch.md` at the repository root (untracked).

Reviewed commit: `0091e9d` (code `b3ac7ef`) on base `5f8118b`. Blobs: `native/dispatch.py` `253e8f7d`,
`native/dispatch_lib/focused.py` `1e09a088`, `native/dispatch_lib/rules.py` `6abb4ef1`, `native/window.py` `a6176962`,
core `steuermaschine/window.py` `e82208aa`. Method: own diff of both moved modules and of the moved tests against the
originals, every changed line read, the slice's 97 tests run, a differential run of the old and new SHA rule over
20 000 cases, experiments on fakes only. No dispatch, re-run, cancel, push or branch delete was made.

Result: 0 blockers, 12 should-fix, 12 notes.

Holds (proven by reading and on fakes):
- SHA guard: refuses an open PR head, a `pull_request:<sha>` key of the plan registry and a non-validation branch tip
  with exit 2 and no write; `validation/*`, `rehearsal/*`, `bisect/*` start as before; an unreadable PR list,
  registry, branch list or run inventory refuses with no POST. Old and new rule give identical reasons.
- One POST per dispatch, as the last call; `gh()` never retries; the pending-marker protocol is unchanged, so a crash
  on either side of the POST cannot dispatch twice.
- The cap of 5 is enforced on every start from GitHub's inventory plus the receipts, under the admission lock.
- `head validate`: commit-tree moves no branch and no HEAD; order push, one POST per lane, DELETE; a failed push
  starts nothing; a refused or red lane still removes the branch; an existing validation branch is not clobbered.
- The four write commands exit 70 without a verdict; no `__main__` entry; the slice reads no switch that opens the gate.

Should-fix (S1 to S6 before the first unattended `head validate`; they do not reach the four harms above):
- S1 `focused.py:163` `tally_of` raises on a log-read timeout: a GREEN `--wait --cleanup` run then exits 1 and
  leaves its validation branch.
- S2 `native/window.py:117` the window counts local receipts only, not GitHub's inventory: with foreign focused
  runs in flight it lets a head in and half its lanes are refused.
- S3 `native/window.py:188` parallel lanes no longer wait for a slot (the former validate did): half lane sets at
  the job ceiling, reported as a failure to fix.
- S4 core `window.py:146` and `native/window.py:205,212` a second caller for the same branch and head gets `go`, and
  its `leave` ends the first caller's turn; in the same second it can delete the first caller's branch.
- S5 a killed `head validate` leaves the branch and holds the window for every head for up to 180 minutes; no
  command releases it or names the removal.
- S6 `native/window.py:208` says "removed" when the branch was not removed.
- S7 `native/window.py:169` commits `HEAD`, read again, instead of the head it reported.
- S8 the exists-checks treat any API failure as "absent" (safe only because the push has no force).
- S9 receipts are a file at a setting's path, the lock is in the store: no longer paired.
- S10 the digest of `head validate` and `ci window run` is `native/window.py` alone: a later change to the dispatch
  library or to core `sha_uses` leaves them approved. After the rebase name the review unit with `files=`.
- S11 tests: the real chain of `head validate` is replaced by stand-ins in every test; `ci one watch`, a successful
  `ci one rerun` and S1, S4, S6 have none.
- S12 `ci one dispatch --throwaway` pushes outside the run lock (as the old tool did).

Conditions of this approval:
- Rebase onto main first: this branch's base still has the settings switch that opens every gate
  (`SM_ALLOW_UNREVIEWED_WRITES`); main `2913b2c` removed it.
- The switch from the old tool is ONE step. Receipts, pending marker and admission lock are not shared: side by side
  the cap of 5 can be exceeded (two starts at 4; the other tool's re-runs are not counted), a start in doubt pauses
  only its own tool, `watch` on the other tool's re-run reports the previous attempt, and the window is blind to the
  other tool's runs. Drain the old receipts first (two rows without a result at review time) and stop the old entry
  point for every caller in the same step, the rehearsal included.
- The digests below are of the code at `0091e9d` under the default review unit (the module plus its `_lib` package).
  Any change to these files, `review=` or `files=` on a registration included, makes them stale: a reviewer reads
  that delta and re-issues the lines. `COVERS` lists what this review read and approves; `ci one rerun`,
  `head validate` and `ci window run` open only once their registrations name this file.

COVERS: ci one dispatch
COVERS: ci one rerun
COVERS: head validate
COVERS: ci window run
REVIEWED: sha256=66ce6307b7423c366fc97ad0775c332cb2ab07c9b13379059c1739d7278b25ae
REVIEWED: sha256=45253d9a6db6ace423595dace229fff9ac5d1e1648acf5dbe6d46a6694fecb67

VERDICT: APPROVE

## Independent review of the fixes (2026-11-13)

Reviewer: independent, wrote none of the code. Full findings with file:line and failure scenarios:
`~/Code/tmp-sm-reviews/dispatch/REVIEW.md`. Reviewed: `bcae212` (fixes on `3dfc6b8`, `f7fe1ab`, `9dbe78a`), read from
an export of that commit. No dispatch, re-run, cancel, push, branch delete or GitHub call was made.

The approval above (2026-11-12, digests of `0091e9d`) is superseded by this block; its `COVERS` lines for
`head validate` and `ci window run` no longer apply (those commands name `reviews/head-validate.md`).

Delta read in this unit (`native/dispatch.py` + `native/dispatch_lib/`, 4 files):
- `focused.tally_of` catches the `SystemExit` of a log read that timed out and returns an empty tally: a finished run
  keeps its result and `--cleanup` still runs (previous S1, fixed).
- `focused.delete_throwaway` returns whether it deleted; `cmd_dispatch` ignores the value (no behaviour change).
- `admit(start, arm, getattr(a, "wait_for_slot", a.wait))`: the `ci one dispatch` arguments have no `wait_for_slot`,
  so `--wait` decides as before; only `head validate` sets it.
- `dispatch.py`: state file and scratch directory default to the project's state home (no machine path); the clone is
  the existing setting `core_repo_dir` (unset: the throwaway refuses at `git remote get-url`, before any push);
  `ci one rerun` names this file. `ci one rerun` reaches none of the changed lines except `tally_of` on the
  non-strict refresh path.

SHA guard re-proved: `attachment_guard` runs on the exact tip that is dispatched, before `admit`, the pending marker
and the POST; an unreadable PR list, registry or branch list refuses with no POST. `ci one rerun` dispatches nothing
(it re-runs an existing `ci.yml` run).

Open (should-fix, not blocking): the SHA-guard decoder `admission_lib/focused_slots.py` and the three admission files
it imports run on every dispatch but are outside this unit (they are inside the approved `head apply` unit); name them
with `files=`. Core `steuermaschine/github/` (`Gh` no-retry) is outside any plugin digest.

COVERS: ci one dispatch
COVERS: ci one rerun
REVIEWED: sha256=783e52d445e94a5c6b31cf905529c6754cd52b0be49434c2b5cadf9590f12411

VERDICT: APPROVE

## Independent review of the inventory retry (2026-11-13)

Reviewer: independent, wrote none of the code. Reviewed: `b9c32b1` on `fix/inventory-complete` (bead
heinzel-management-8elp). Delta in this unit: `dispatch_lib/focused.py` only. No dispatch, re-run, cancel, push,
branch delete or GitHub call was made.

Holds:
- Bounded: `focused.py:150` loops over `(2.0, 5.0, None)`: at most 3 reads, 2 sleeps on the clock provider (`:156`),
  then `Unreadable` (`:155`). Tests assert 3 calls and `slept == [2.0, 5.0]`.
- Each retry is a whole `_inventory_once` (`:130`): one `gh api --paginate --slurp` that returns count and list
  together, re-checked as before (`:138` count vs items, ids unique and positive). Only a self-consistent read is
  returned; a persistent mismatch still refuses. No silent acceptance.
- `Unreadable` subclasses `SystemExit` with the same `refusing: ...` text: every existing caller (`admit`, `jobs_of`,
  `window.in_flight`, `dispatch.py:143,241`, `queue.py`, `release.py`) refuses exactly as before.
- The cap cannot undercount: the count in `admit` and `in_flight` comes only from a consistent read, under the
  admission lock as before. No new GitHub write: the retried call is a GET; `gh()` still never retries the POST, and
  `inventory` is not called between the pending marker and the POST.
- SHA guard unchanged (`attachment_guard` before `admit`; the plan-registry listing at `:536-543` is not retried and
  still refuses).

Notes (not blocking):
- N1 every `SystemExit` of a read is retried, also rc 124/127 ("API unavailable"): up to 3 x 60 s inside the
  admission lock. Bounded, fail closed.

COVERS: ci one dispatch
COVERS: ci one rerun
REVIEWED: sha256=dc3b4c469ca0828872547e84c0d1eb5de1a930c64864a52c573beb75148ec579

VERDICT: APPROVE

## Independent review of fix/batch-validate at 623899e (2026-11-13; commits ccb9318, cc2513a)

COVERS: ci one dispatch
COVERS: ci one rerun
REVIEWED: sha256=aa1a68f39cc0833240af760615069461d3d1315625e2de6c7ba2af82a201f8dd

- `run_view` (dispatch_lib/focused.py:310) retries only the read `gh run view` (reads after 0, 2, 5, 10 s: 4 reads,
  bounded), checks the answer's shape, and raises `Unreadable` after the last one; `wait` (:327) uses it, still bounded
  by MAX_WAIT. The dispatch POST (:683) still goes through `gh` with retry=False: a start is never repeated.
- SHA guard unchanged (`attachment_guard` :587 before `admit`); ccb9318 adds tests that an admitted registry key and a
  PR-branch tip refuse, also with --force.
- Note: retries are not distinguished by error kind (a 401 is also read 4 times): at most 17 s more, fail closed.

VERDICT: APPROVE

## Independent review, campaign G2 (integration/sm-complete at 479a8e8, 2026-11-13)

Reviewer wrote none of this. Digest recomputed with `uv run sm self reviews` in a fresh worktree of 479a8e8.
Delta 1d69c43..479a8e8 in the unit: dispatch.py, dispatch_lib/ and the admission_lib files it imports are
unchanged; only core files changed: window.py (type hints only), defaults.py (typing; the pytest write refusal now
reads PYTEST_CURRENT_TEST / SM_TEST_ALLOW_LOCAL_PUSH through config.Environment, same rule), config.py (typed
Environment, same variables), engine.py (pipelines only: supersede of older runs, running state, spent digest),
native.py (digest follows imports, gate_problem, a write waits with exit 75 while a promote switch is pending),
app.py, models.py, hookspecs.py, plugins.py, protocols.py (typing, typed json output).

- The SHA guard, the pending-marker protocol, the admitted-key and PR-tip refusals and the bounded retries are the
  bytes approved before (aa1a68f3...). The new promote wait refuses before the command body: nothing is written.
- No force push, stash, auto-merge or hz-* call in the delta.
Tests (G2, `sm gate run -- uv run pytest` on the 15 files of this group: ci_classify*, ci_ops_*, dispatch*, rejudge, core github cli/forge, gate units, native, gate structure): 398 passed, 2 skipped, 1 failed: test_the_cores_own_writers_are_under_the_gate assumed `ci cancel` unapproved; fixed in the same branch to patch the review state (gate still asserted); then 8 passed.

COVERS: ci one dispatch
COVERS: ci one rerun
REVIEWED: sha256=fc1249446b8b16ece64561d2aea4c6e4430d393b69c450dd81f0a3c01f432e71
VERDICT: APPROVE

## Independent re-verdict, campaign G2 (integration/sm-complete frozen at 2d0877b, 2026-11-13)

Reviewer wrote none of this. Digest recomputed with `uv run sm self reviews` in a fresh worktree of 2d0877b; it
matches the campaign list. Delta read: 479a8e8..2d0877b on every file of the unit.

- Unit delta: dispatch_lib/focused.py `refused_by_github` + `start()`: the pending marker is removed only when the
  dispatch POST fails with a definite `(HTTP 4xx)` other than 408 in gh's error text; a `refusing:` error (timeout,
  gh missing, malformed reply), a 5xx or no reply re-raises and keeps the marker for reconcile, as before. A 4xx
  means GitHub started nothing, so clearing it is safe. Test
  test_dispatch_definite_refusal_clears_pending_ambiguous_keeps_it. The SHA guard, admitted-key and PR-tip refusals
  and the exact receipt check are unchanged.
- Not blocking, D1: gh's stderr is cut to 300 characters before the match; a long error message that hides
  `(HTTP 4xx)` keeps the marker (fail-safe, manual reconcile).
- Core delta shared by every unit: native.py `EXEMPT_PATHS`/`is_exempt` (an `exempt=` writer skips the gate only
  for `gate run`: the gate is stricter, not looser); `switch_pending(commands, now)` ignores a pending file whose pid
  was reused (process start from `ps -o etime=` later than the file's UTC time + 2 s) and holds when the start
  cannot be read (fail closed); the format matches bin/promote_lib.sh `mark_switch_pending` (`PID %Y-%m-%dT%H:%M:%SZ`);
  `NativeRuntime.commands` (read-only registry lookup). engine.py: `_supersede` marks an older run under that run's
  own store lock (non-blocking flock, Busy = exit 75), pipelines only.
Tests (`sm gate run -- uv run pytest` on 17 files: ci_classify*, ci_ops_*, dispatch*, rejudge, core github
cli/client/forge, gate units, core native, gate structure, queue hzlib): 479 passed, 2 skipped, 0 failed.

COVERS: ci one dispatch
COVERS: ci one rerun
REVIEWED: sha256=813c8f801add81e271d76e0ea83c4f53d152fa607aca3e3d0588ed33141ec681
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

COVERS: ci one dispatch
COVERS: ci one rerun
REVIEWED: sha256=4dd9b60d0e235998313a1fa706c6e3f4b345e169513e1f968877c93a16744820
VERDICT: APPROVE

## Campaign re-verdict g2 (2026-11-14, independent; reviewer wrote none of this code)

Target: integration/sm-dod frozen at c36bbe1 (baseline 903a1b7, where this unit was APPROVED). Digest recomputed with
`uv run sm self reviews --format json` in a fresh worktree of c36bbe1; it equals the campaign list.

- Merge tier (Core ADR 0149 d): `ci one rerun` passes a lazy `merge_tier` probe; cmd_rerun lifts the main refusal only
  for `branch == "main"` and only when the probe is true (short-circuit: no API call for other branches). release/*
  still needs --force. merge_tier.active: mode on/off forces; auto reads main's scripts/focused_plan.py by GET and needs
  `^WHOLE_SUITES =`; unreadable (API error, bad base64, OSError/RuntimeError/ValueError) means the older rules.
  Old Core checked: test_a_main_rerun_needs_no_force_only_in_the_merge_tier (probe False: refused as before; release/v1
  refused with the probe True). All other guards (CI workflow only, finished run, capacity) run unchanged.
- admission_lib: state paths move from ~/.cache/hz-* to the plugin state directory (`state.root()`, which refuses
  while any old path is unmigrated: fail closed); symlink/owner/mode checks kept; git refs renamed refs/sm-admission/*;
  the GIT_* scrub now goes through child_environment(drop_prefixes=("GIT_",)) (same set). `report_blockers` still
  requires the explicit ALLOWLIST and `.headBranch!="main"` markers before any cancel. state overrides check:
  `environment.state_overrides().any()` reads the same three variables with the same truthiness.
- dispatch_lib/focused.git: same GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE drop via child_environment.
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

COVERS: ci one dispatch
COVERS: ci one rerun
REVIEWED: sha256=be74cd3443d37a0adc9688c91e2bf5a5aaa8b3e5932cfdfc31aef8126af57c9c
VERDICT: APPROVE

## Re-verdict at abc64a4, group C (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-abc64a4/C.md (recorded verbatim by the integrator).

dispatch.py, dispatch_lib/*, window.py, statemove.py unchanged. Changed in its digest: admission_lib/focused_slots.py.
Dispatch uses only `slots.NAMES` and `slots.registry_text` (dispatch_lib/focused.py:559), both unchanged; the SHA guard
still fails closed if the decoder is missing or the registry undecodable. The focused_slots delta (queue label from
the setting `queue_service_label`, `queue_state` refuses when unset; `report_blockers` read-only, the script-calling
cancel path removed) is not on the dispatch/rerun path and only narrows writes. dispatch* tests green.
COVERS: ci one dispatch, ci one rerun
REVIEWED: sha256=d841418d20eea12ec94ce993dd711b9f915381988a175a025c2bc4c9312fb7d8
VERDICT: APPROVE

## Re-verdict at feat/sm-next-admission 5fe179a (delta review 4, 2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-next-admission/REVIEW.md, delta review 4 (recorded verbatim by the integrator; COVERS from `self reviews`).

COVERS: ci one dispatch
COVERS: ci one rerun
REVIEWED: sha256=7974f6ba7aa38b4e07e8e97cdee625fcc57d6510c7ed6b83677472461a657c0a
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Delta: focused_slots and pr_owned (next-admission m598), defaults.py (Store.delete), config/protocols. ci one dispatch/rerun unchanged; S15 (no release-window check here) still a follow-up.
COVERS: ci one dispatch
COVERS: ci one rerun
REVIEWED: sha256=696fbf9038716b93d4f97bdf094cdad28015f889d9119fef966e0f725ecd0358
VERDICT: APPROVE

## Re-verdict at f346492 (bead heinzel-management-04ro + integration/sm-batch3, 2026-11-14)

Independent reviewer; wrote neither 04ro nor window admission. Review: ~/Code/packets/sm-04ro/REVIEW.md.
Stale only through core window.py (holds, gone waiters). Dispatch does not use `Window`; its own code unchanged.
COVERS: ci one dispatch
COVERS: ci one rerun
REVIEWED: sha256=f83821b26dfc341775316341c918e77631392a181244878bfff0f6b49198eb79
VERDICT: APPROVE
