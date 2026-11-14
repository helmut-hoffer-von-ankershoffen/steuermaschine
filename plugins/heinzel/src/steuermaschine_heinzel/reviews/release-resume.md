# Port review: the release step runner (`sm release resume`, `sm release plugin`)

Slice heinzel-native-release. Written by the porting agent. The `REVIEWED:` line and the verdict line are the
independent reviewer's and are NOT there yet. `release-drop.md` reviews `sm release drop`, which runs the same unit.

COVERS: release resume
COVERS: release plugin

Review unit (`sm self reviews` prints its digest; 34 files): `native/release.py`, every `.py` of
`native/release_lib/`, the two reviewed source files `release_lib/approved_sources/ci.yml` and
`release_lib/approved_sources/release_candidate.py.txt` (named one by one in `release.UNIT`, from `approved.FILES`),
and `native/queue_lib/` (frozen under the queue's review; the runner merges through its merge train and reads GitHub
through its shared library). The read commands live in `native/release_read.py` + `release_read_lib/` and are not part of this unit.

## 1. What moved, verbatim

`python3 plugins/heinzel/port/move.py verify OLD_TOOLS_DIR` recomputes every moved file from its source, prints every
changed line and fails on any other difference, or when the number of changed lines is not the pinned one
(`release_lib/moved_map.py`: EDGE_LINES 48 in modules, TEST_EDGE_LINES 35 in tests). Sources: the working tree of the
old tools on 2026-11-12 23:00 CET.

| Old file | Module in `native/release_lib/` | Lines | Changed lines |
|---|---|---|---|
| the release step runner | runner.py | 2237 + 207 (reviewed drop patch, see release-drop.md) | 32 |
| release_rejection.py | rejection.py | 568 | 2 |
| release_candidate_policy.py | candidate_policy.py | 202 | 6 |
| release_candidate_capacity.py | candidate_capacity.py | 542 | 5 |
| release_snapshot.py | snapshot.py | 134 | 1 |
| release_notes_audit.py | notes_audit.py | 42 | 2 |

18 test files moved with them (listed in `release_lib/moved_map.py`), assertions unchanged; four fixtures are
copied byte for byte (verified).

## 2. Every semantic change, with its test

1. **Imports** of moved siblings are relative; `sys.path` is not touched (8 lines in the runner).
2. **Processes** (`subprocess.run(` -> `edge.run(`, 19 lines in the runner): inside a native command a text-capturing
   call (`git`, `gh pr create`, `uv run ... release_notes.py`, the run lock) goes through the core's one process
   runner; outside one it is the plain call, so the moved tests still patch `module.subprocess.run`. GitHub reads and
   writes of the runner go through `queue_lib.hzlib.gh`, which is that same edge. Test: the parity tests see every
   `gh` call in order on `FakeGh`.
3. **The run lock is `sm gate run --`**, not the path of a script (4 lines: two definitions, the push, the hint in a
   refusal). The lock command is several words, so it is spliced into the argument list. Tests: moved
   `test_native_release_pr_gate.py` (13 changed lines: 10 name the native lock command instead of the script path, 3
   give its fake checkout the two files the cut reads since the notes audit and the `--ref` probe were added).
   NOT VERIFIED: priority and hold exemption were passed to the old lock through the environment by the train; whether
   `sm gate run` honours them is the run lock's contract (slice ops).
4. **Shipping the bump PR is `sm head ship PR`** (1 line; tag-on-green-main only, the flag is off by default). That
   command does not exist on main yet: with the flag on, `bump PR merged` stops with the ship's failure.
5. **The release checkout** is the setting `release_dir` (1 line); a moved function called directly keeps the old
   environment variable and default.
6. **The pins have one source of truth** (1 line in candidate_policy.py): `APPROVED_SOURCES = edge.approved_pins()`,
   the sha256 of the reviewed files in `release_lib/approved_sources/` (`release_lib/approved.py`). The verifier prints
   `pin ...: old literal ..., reviewed file ...: EQUAL` for both. A re-pin is the replacement of a file there. Both
   files are in the review unit, so a re-pin changes this unit's digest and needs a new verdict here, as a re-pin of
   the literals was a reviewed code change before (reviewer finding B1: the first unit covered `.py` files only).
   Test: `test_a_repin_voids_every_release_verdict_because_the_reviewed_sources_are_in_the_review_unit`. Tests: moved
   `test_native_release_candidate_capacity.py`, `..._candidate_policy.py` (50 tests that failed on a stale copy of the
   workflow now read these files), `test_the_pins_are_the_hashes_of_the_reviewed_files_and_the_policy_uses_them`.
7. **No `__main__` block** (runner: 2 lines removed): the runner is reached only through the native commands.
8. **`native/release.py` is new**: typed options become the runner's argument list (only what was typed is passed:
   the runner's own checks scan the raw list), the runner's `main` runs in process on a fresh copy of the module, its
   waits sleep on the clock provider, its standard output is shown line by line as progress (stderr) and returned as
   `lines`; the result in text form is the STOPPED line or the runner's last statement. `--absorb` is a flag on the
   version (`sm release resume X --absorb --reason ...`); `--plugin NAME` is `sm release plugin NAME`. A combination
   the runner's parser refuses is exit 2 with its message. Tests: `test_native_release_writes.py`
   (`test_every_typed_option_becomes_the_step_runners_argument`, `test_a_combination_the_step_runner_refuses_...`).
9. **Output**: old command names in hints are printed as `sm` commands (`release_lib/names.py`); file paths are
   printed as they are on disk.
10. **State**: unchanged. The receipt, its lock and sidecars, the tail receipts, the owned holds, the shared freeze
    file and the flow records are the files under `~/.cache` the runner has always used, in the same format (the
    queue reads the same holds and freeze file, so they cannot be moved by a setting of one side). Tests: parity
    (state files compared as data), `test_resume_of_a_real_finished_tail_...` on copies of real receipts of 2026-11-12.
11. **Write gate**: exit 70 until this file is approved for the current digest. Test:
    `test_a_write_command_without_an_approved_review_refuses`.

## 3. Parity on recorded transcripts

`tests/record_release_transcripts.py` ran the OLD script on a copy of the old tools in a throwaway HOME with a
scripted `gh` and PyPI and recorded exit code, both streams, every `gh` call and every state file. The native command
replays each scenario on `FakeGh`: `resume-main-red` (lock, receipt, flow record, freeze file, six reads, STOPPED),
`absorb`, `absorb-accepted-refused`. Equal: exit code, every line (old names translated), the `gh` calls in order,
the state files as data. See `parity/release-resume.md`.

## 4. Open, not verified

- The long path (release PR open -> merged -> tag -> PyPI -> compose -> acceptance -> verdict) has no recorded
  transcript: it is covered only by the moved unit tests of its steps. A parity recording needs a scripted candidate,
  snapshot and acceptance, or a rehearsal on a low-risk head.
- `import subprocess` stays in runner.py, candidate_policy.py, candidate_capacity.py, notes_audit.py (structural gate:
  4 findings to accept or to remove with the tests' patch points).
- `time.time`, `os.environ` (`HZ_RELEASE_TAG_ON_MAIN`, `HZ_RELEASE_FLOW_DIR`, `HZ_KNOWN_FAILURES`), `fcntl` and
  `~/.cache` paths are used by the moved code as before, against section 2 of docs/INTEGRATION.md and on purpose
  (adoption of the live state). PyPI and the public snapshot are read with `urllib` directly.
- The release train still starts the old script; it becomes native in the train piece of this slice.
- ruff and mypy were not run on the moved files (unstyled on purpose).

## Independent review (2026-11-13, a reviewer who wrote none of this code)

Reviewed: the unit as merged on main (merge 1e5ace3 of e911b38; `native/release.py`, `native/release_lib/` with
`approved_sources/ci.yml` and `approved_sources/release_candidate.py.txt`, `native/queue_lib/` of main), 34 files.
The branch alone (e911b38, digest 7b3b6e94...) is NOT approved: its `queue_lib/edge.py` sends the merge call's
lenient decoding to the strict core runner; main's `queue_lib/edge.py` does not, and this digest contains main's.
Full findings with file and line: `REVIEW-INDEPENDENT-release.md`, section "Phase 2" (P2-0 to P2-11).

Confirmed:
- The move is verbatim (`port/move.py verify` rc 0 again after 061fb50 and e911b38; an independent diff in phase 1).
  Pins equal the old literals and Core main. 691 = 691 tests, no assertion changed.
- The reviewed candidate sources are in the digest: one byte changed in either file makes all three verdicts stale
  (tried in a scratch copy). No other data file under the two libraries is read at run time.
- Every option of the old command line maps one to one; the runner's own argument checks, lock, receipt check,
  unfinished-drop check and rejection guard run unchanged; an empty option value is passed on and refused by the
  runner as before (tried). `resume` cannot reach the drop, `drop` cannot reach resume, plugin or absorb (tried).
- Process calls on the release path: a timeout raises again, a missing program raises again, the merge call decodes
  leniently again; each call checked, table in the findings.
- Run for real in a sandbox (real core runner, stub `gh`/`git`/`bash`, throwaway HOME with copies of the state files
  of 2026-11-13): `resume 0.9.40` up to the merge stop, and the release PR preparation through the child
  `sm gate run -- bash scripts/release.sh patch` in the configured release checkout. The copied state files were
  byte-equal afterwards; only the new receipt, the new hold and the lock file were added.
- The gate: without a verdict each of the three commands exits 70 through the real command tree and writes nothing;
  no module entry reaches a write.

Open, not blocking (none is a difference to the old tool that can merge, tag or publish):
- P2-9 (old behaviour): a CLOSED release PR of a dropped candidate counts as "release PR open". On today's state
  `sm release resume 0.9.40`, run before the new release PR exists, takes a new hold and stops at the merge with
  "release PR #794 not merged: skip: CLOSED"; merges stay held until `sm release drop 0.9.40 --reason ...`.
  Open the new release PR first, or change the runner by a reviewed patch.
- P2-5: `sm gate run` is called without priority and without hold exemption, which the old train set by
  environment; release.sh or the push can wait without limit behind a gate hold. Fix before the train goes native.
- P2-4: `HZ_RELEASE_DIR` is ignored on the native path (the setting `release_dir` decides); refuse when both differ.
- P2-7: the ship line (tag-on-green-main only) is run by no test and `sm head ship` does not exist yet; keep the
  flag off.
- P2-8: no long-path transcript. Enough for attended use by the checks above; record one sandbox run of the whole
  path (real runner, stub executables) as a test before the train calls this command unattended. Watch the first
  live cut at the push through `sm gate run` and at the tag wait.
- This verdict assumes the core runner keeps answering a timeout with rc 124 and "timed out after" and that
  `sm gate run` keeps returning the command's exit code and output (both outside this digest).

## Re-review 2026-11-13: ci.yml re-pin (Core 53c4e88)

The only change in the review unit since the previous verdict (sha256=715a1eff...) is
`release_lib/approved_sources/ci.yml`, re-pinned to the exact bytes of Core `.github/workflows/ci.yml` at 53c4e88
(identical to the shipped head 02ae550c8; file sha256 edccd5b1...). Its diff is one line: the lint job's
`timeout-minutes: 5` -> `10`. No job removed or renamed, no condition or concurrency change. The unit digest at
the previous commit recomputes to 715a1eff..., at this commit to cefa96ac... (34 files).

Previous verdict: approved at sha256=cefa96ac1680a8ef2d7039457ca05adc48aefa1529d6979c64b2b51d0ce740bb.

## Re-review 2026-11-13: fix/batch-release at 61dcce0 (35 files)

Independent reviewer (wrote none of it). Unit changes since cefa96ac..., all on this branch: `runner.py`,
`release.py`, `candidate_policy.py`, `queue_lib/main_checkpoint.py`, new `queue_lib/job_proof.py`. The merge, tag,
publish and verdict paths are unchanged; every gate before them (main green or candidate gate, previous release
accepted, merge train, first-attempt candidate) is untouched. `release pr` is reviewed in `release-pr.md`.
Line numbers: `native/release_lib/runner.py` unless named.

- 3zye, plugin PyPI poll (2412-2427): a success run without the version on PyPI keeps polling for at most
  `PYPI_PROPAGATION` = 300 s (86) from the first time it is seen, then stops; a failed run stops at once; the step
  timeout bounds it too. Done is still `pypi_has` only. Bounded. OK.
- 98hi, compose replacement (1195-1209, `compose_fix_landed` 1233-1259): only a failed `compose` (not finalize) run
  of this receipt, only when plugins main is a different SHA, compare status `ahead` from the failed run's head, and a
  plugin pyproject whose version changed is on PyPI; once per failed run (1239); `compose_busy()` (any active
  compose run) defers the dispatch (1201), so no fresh compose starts while one is active. The fresh dispatch uses the
  runner's own inputs (`dispatch_compose`) and records a new `compose_dispatch` that resets the adopted ID, so the
  failed run cannot be re-adopted. Done stays `released()` = the final `app-vX` release: a replaced or mis-adopted
  run can never pass the step. Residual: a failed replacement can be replaced again if main moves again with another
  published plugin change (each needs new evidence; fine).
- F7, run binding (`dispatch` 1110-1129, `ours` 465-467, `bound_run` 485-525): an unmarked dispatch records repo,
  workflow, ref resolved to an exact SHA (unresolvable: no dispatch), event and inputs; only a `workflow_dispatch` run
  of that workflow at that SHA created at or after dispatch-5 s is adopted; two candidates stop; an adopted run is
  re-read by ID with path, SHA and event checked. A run of another commit or event cannot be adopted. Marked
  (acceptance) and pre-binding receipts keep their old route. Residual F7-a (not blocking): inputs are not visible in
  the run list, so a foreign hand dispatch of the same workflow at the same SHA in the seconds before ours is listed
  would be adopted alone; stated in the commit, needs a run-name marker in the workflows. It cannot publish, tag or
  merge anything: every adopting step is done only by its own proof (`pypi_has`, final `app-vX`). Residual F7-b: if
  main moves between the SHA read and the dispatch, no run matches and the step times out (fail-closed).
- 5de092f, empty-steps proof (`queue_lib/job_proof.py`, `candidate_policy.py:58,73-78`,
  `queue_lib/main_checkpoint.py:302`): the pinned reader is unchanged; only a completed success job of the same run,
  attempt 1, with a runner ID above 0 and `steps == []` gets one synthetic step, and only when that exact job's log is
  non-empty; an unreadable log raises `CandidateError` (a `ValueError`) and leaves the job for the reader to refuse.
  Every other reader check (conclusion, head SHA, attempt, inventory) still applies. A narrow, evidenced relaxation
  of one reader condition, not a bypass.
- The runbook (`docs/RUNBOOK-release-cut.md`) matches: step 9a, no `--release-dir`, the compose replacement, the
  PyPI window.

Tests and lint run through `sm gate run` on 61dcce0 (results in the verdict commit message). NOT verified: live
dispatches, a live compose replacement, a live PyPI window.

REVIEWED: sha256=9af0b9aac389ab91f3273ae70b19c338f072811cf351cb6d596a6e550221d2fd
## Independent review (feat/repair-main 7fc817a, base main 5c87f14)

Reviewer: independent; wrote none of this code. Unit recomputed by `sm self reviews`. Stale only because it shares
queue_lib/queue_daemon.py: diffed every file of the unit against 5c87f14; the only change is the `sm queue main-rerun`
repaired-plan path (queue_daemon.py:501-507, :514-519, :579-583, :650-669, :679-682; see reviews/queue-main-rerun.md).
No queue add/drop, daemon tick, ship, PR, release or train code path changed; the restored-by-rerun result keeps its
shape (one optional `repaired` key). Related suites and `sm self lint` pass through `sm gate run`.

REVIEWED: sha256=5cf634bca48beb8e76c53e487465b4ae875a248d0a2bb69605939701b6bb3340

VERDICT: APPROVE

## Independent re-review, campaign group 5 (integration/sm-complete at 479a8e8, 2026-11-13)

Reviewer wrote none of this code. Own worktree on review/campaign-g5; digest recomputed with `uv run sm self reviews`; every file of the unit diffed 1d69c43..479a8e8.
- Core files in the digest (engine, native, config, models, protocols, helpdoc, defaults, hookspecs, plugins, runlock,
  window): typing, the `Environment` model (only reader of os.environ), review-stop file/sha256 in the report, a
  `running` state with pid before a step acts, `_supersede` of an older run of the same argument (marked, never
  deleted), `native.call`/`gate_problem` (the same review gate for pipeline steps), a promote switch makes write
  commands exit 75 with nothing run, transitive import digests, `gate run --exclusive` (queues as priority 2, bounded
  by a 30 min default wait, starts only with no heavy slot busy). Nothing here weakens a gate of this unit.
- runner.py `bound_run`/`dispatch`: an unmarked dispatch is bound to repo, workflow, event workflow_dispatch and the
  head SHA resolved before the dispatch; two candidates stop (never guessed); rejects and the adoption are recorded;
  an adopted run is re-read by ID and its identity re-checked. Receipts from before the change (no `_dispatch`) keep
  the old path. N1 (minor): if the ref moves between the SHA read and `gh workflow run`, the run is rejected and the
  step waits to its timeout; a resume then dispatches again (the compose queue wait keeps it serial). Visible, bounded.
- compose replacement (`compose_fix_landed`): only when plugins main moved past the failed run's head (compare
  status ahead) AND a plugin whose pyproject version changed is on PyPI; once per failed run (compose_replaced),
  the failed run kept in the receipt; waits for the compose queue. Fail-closed on every read error (returns None,
  the old Stop).
- `release plugin`: stamps go to their own receipt (`hz-release-plugins.json`); refuses before any write while the
  core receipt still holds stamps; a success run without the version on PyPI is polled for at most 5 minutes.
- hzlib.gh / merge_train.gh: every gh call bounded (120 s reads, 300 s writes, exit 124 on timeout); a failed
  `gh pr merge` is read back (merged / not merged / unknown), unknown keeps the row queued with an alarm and stops
  the batch; the check-settle wait is bounded (80 x 45 s). Release merge path unchanged otherwise (its candidate
  guard stays the pre-merge guard).
- queue_daemon: flock around every load-change-save, per-process tmp file, an unreadable state file refuses (never
  read as empty), the main-plan check before a core merge and at `queue add`, needs-work re-check only at the
  unchanged pinned head; `__main__` blocks removed (no second way in). job_proof: a success job with empty steps is
  accepted only with run id, attempt 1, a runner id and a non-empty log of that exact job.
- N2 (test, not code; owner: the queue unit): `tests/moved/test_native_queue_recheck.py::test_a_green_needs_work_row_
  at_its_pinned_head_is_requeued_and_merges` FAILS at 479a8e8: it expects the train kwargs without the
  `pre_merge_guard` that the merged main-plan check (fix/stale-main-plan) now adds for core. The code is right; the
  test's expectation must take the guard. The integration head is not green until it does.
- `release plugin` PyPI propagation: the 5 minute window starts when the success run is first seen; the step's own
  timeout still bounds the whole wait. Plugin receipt keys stay out of the core receipt, so handoff, verify and the
  next cut never read them (mini.core_receipt treats a stamps-only global receipt as absent).
- Tests through `sm gate run -- uv run pytest` at 479a8e8: tests/test_native_release_*, test_pipeline_*, test_native_rehearse*,
  test_native_lock*, test_native_queue_*, test_native_go*, tests/moved/*release*|*queue*|*rehearse*, core test_core_engine,
  _native, _runlock, _window: 1 failed, 2330 passed, 24 skipped, 1 xfailed. The one failure is
  moved/test_native_queue_recheck.py::test_a_green_needs_work_row_at_its_pinned_head_is_requeued_and_merges (stale kwargs
  expectation, see N2 in release-resume.md); no test of this unit's own paths fails. NOT verified: any live GitHub write.

COVERS: release resume
COVERS: release plugin
REVIEWED: sha256=9c5f2884b1eda8cf0398f4c18853f569a111eb978e6ad6bcb30fe08089384f48
VERDICT: APPROVE

## Independent re-review, re-verdict campaign g5, release plugin fix (integration/sm-complete at 539b7f7, 2026-11-13)

Reviewer wrote none of this code. Own branch review/reverdict-g5-ovvg from 539b7f7 (contains fix/release-plugin-runid 234dc8d, bead
heinzel-management-ovvg; no code change between 60a83a5 and 539b7f7); digest recomputed with `uv run sm self reviews`; code diffed 2d0877b..60a83a5 (release.py,
release_lib/runner.py only). The 479a8e8..2d0877b delta of this shared unit is reviewed in release-pr.md (previous section).
- The change: `release plugin` no longer goes through the generic `dispatch`/`ours` pair. `plugin_dispatch` resolves
  plugins main to an exact SHA, records `{key}_dispatch` (repo, workflow, ref, sha, event, inputs) with `{key}_run_id`
  None BEFORE the POST, then POSTs `actions/workflows/release.yml/dispatches` with `-F return_run_details=true`, and
  records the `workflow_run_id` of the reply plus the head SHA the run really took (re-read by ID).
  GitHub API checked today (github/rest-api-description): version 2022-11-28 (gh's default; gh sends no version
  header) has the boolean body parameter `return_run_details` and answers 200 with `workflow_run_id`, `run_url` and
  `html_url` (204 without it); version 2026-03-10 always answers 200 with the ID and no longer lists the parameter.
  A reply without an int ID > 0 (204, empty, non-JSON) stops with UNBOUND, and a gh failure stops too; both leave the
  recorded `_dispatch` with run_id None, so every rerun stops with UNBOUND and adopts nothing (test).
- Resume follows only that ID: `plugin_run` raises UNBOUND when no ID is recorded and otherwise calls `bound_run`
  on its ID path only (re-read by ID; path, event and head SHA must match, else Stop). The run-list adoption path
  of `bound_run` can no longer be reached for a plugin. A receipt from an older sm (dispatch time only) stops with
  "only a dispatch time ... no other run is adopted" and names `--redispatch`; nothing is dispatched or listed.
- Success: the step's done check is PyPI, and after it the recorded run's RELEASE_TAG must equal
  `heinzel-NAME/vVERSION`. The tag is read from the job log of the successful `publish`/`github-publish` jobs. Checked
  against a real log (plugins run 43490248415, publish job): the regex yields exactly {heinzel-google/v0.9.4}. The job
  names match release.yml. Zero or two tags: None, so Stop.
- `--redispatch` is explicit (argparse: only with --plugin, else exit 2). Without it a recorded dispatch is only
  followed. A failed run stops with "after the fix: hz-release --plugin NAME --redispatch". With it the recorded run
  must read as completed (an unreadable run refuses, fail-closed), and the old run is appended to `{key}_replaced`
  (run, conclusion, at) before the new dispatch. The native `sm release plugin --redispatch` passes only that flag.
- Today's incident is covered: `test_a_resume_follows_only_the_recorded_run_never_the_newest_of_another_plugin` lists
  a newer successful tailscale run while google's recorded run fails. Both the first run and the rerun stop on google's
  run; there is no `run list` call, no call naming the tailscale run, and no second dispatch.
  `test_success_needs_the_runs_release_tag_...` stops when the run released heinzel-tailscale/v0.9.7.
- Tests: 10 new in test_native_release_plugin_runid.py (dispatch records ID and head; follows only own run; time-only
  stamp adopts nothing; tag mismatch; no readable tag; redispatch after failure keeps _replaced; redispatch refused while
  active; time-only + redispatch; --redispatch needs --plugin; reply without ID). Three older tests adapted to the new
  seam (plugin_dispatch/plugin_run instead of dispatch/ours); no assertion weakened.
- Open, not blocking: N4: with no recorded ID (UNBOUND, or an older sm's time-only stamp), `--redispatch` cannot check
  whether that unbound run is still active; the message tells the operator to check the release runs first. N5: when the
  version is on PyPI but the recorded run released no matching tag, the command stops for good (`--redispatch` cannot
  help because PyPI already answers done); that needs an owner's call, never a guess. N6: if gh ever sends API version
  2026-03-10, the extra `return_run_details` field may be refused; that ends in a Stop plus UNBOUND (fail-closed).
- Tests through `sm gate run -- uv run pytest` at 60a83a5 (code-identical to 539b7f7; the same 85 files as before): 2506 tests, 0 failed, 0 errors, 25 skipped; the 10 tests of test_native_release_plugin_runid.py all pass.
  NOT verified: a live dispatch or any live GitHub write.

COVERS: release resume
COVERS: release plugin
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

COVERS: release resume
COVERS: release plugin
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
- Absorb hint (be8a0d1): runner.py:339 now says `sm release resume OTHER --absorb --reason ..., or sm release train
  clear`; both match the CLI (resume takes VERSION then --absorb; `train clear` exists). The other new hints
  (`--refresh`, `sm ci watch RUN -R ...`, `sm head ship`, `sm release plugin NAME --redispatch`) name real commands.
- Plugin release binding (ovvg): plugin_dispatch, plugin_run, plugin_run_tag and release_plugin are unchanged in logic
  since the approved baseline: bound to GitHub's returned workflow_run_id, no guessing, --redispatch only after the
  recorded run completed, RELEASE_TAG checked after PyPI. Only hint text changed. Plugin stamps go to
  state/release-plugins.json.
- release_flow: the flag and the flow directory are settings now (queue_release_tag_on_main, queue_release_flow_dir),
  defaulting into the state directory; the sidecar pinning is unchanged.
- FINDING: the same wrong hint as in release-drop is printed by `sm release resume` itself:
  plugins/heinzel/src/steuermaschine_heinzel/native/release_lib/runner.py:2775 (`sm release resume VERSION
  --drop-candidate`, an option resume does not have; should be `sm release drop VERSION --reason ...`), pinned by
  plugins/heinzel/tests/test_native_release_writes.py:290. A brief that asks for correct release hints must not
  leave the one recovery path for an unfinished drop pointing at a refused command.

COVERS: release resume
COVERS: release plugin
REVIEWED: sha256=0d5c8f6f59530f3584e321ddb59738692cb3bdbff3728fd54ecd8b76fbb49d6e
VERDICT: CHANGES

## Re-verdict at abc64a4, group B (2026-11-14)

Independent reviewer; wrote none of this code. Findings, baselines and tests: ~/Code/packets/sm-reverdict-abc64a4/B.md (recorded verbatim by the integrator).

COVERS: release resume
COVERS: release plugin
REVIEWED: sha256=62dace46b549abcec99680095be4a302347cc24c548253bb3610a67ad0b9e549
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Own verdict (recut A8). release.py changed only in the drop section; runner.py's release_pr change reaches resume: steps "release PR open"/"merged" now ignore a CLOSED or fork PR, so resuming a dropped-then-recut version opens a fresh PR (prepare_release_pr still refuses to touch an existing branch), and merge_release_pr guards `release_pr(...) or {}`. Two open PRs on the branch now stop instead of picking gh's first. `release plugin` paths unchanged. Approve.
COVERS: release resume
COVERS: release plugin
REVIEWED: sha256=b1d5d5f5226d59fc09a60cad53a998ed18f01c71c852e1329d6401c1d6a0464a
VERDICT: APPROVE
