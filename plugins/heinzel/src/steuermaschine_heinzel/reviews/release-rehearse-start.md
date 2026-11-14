# Port review: `sm release rehearse start` (also covers `release rehearse step`, `consumers`, `cancel`, `daemon rehearse`)

Old: `tools/hz-rehearse` (473 lines: 60 header, 353 functions, 60 `main`). New: `native/rehearse_lib/rehearsal.py`
(449 lines, written by a move script: copy, then replacements that must each match an exact count),
`native/rehearse_lib/others.py` (58), `native/rehearse_lib/legacy.py` (25), `native/rehearse.py` (420, the edge).

## What moved unchanged

299 of the 353 function lines are identical (difflib, line by line). Among them, untouched: `queue_waiting`,
`release_in_flight`, the order inside `refusal` and `preempted`, `stop_lanes` (except its one cancel call),
`lanes_for`, `throwaway_commit` (same tree, main as parent, refused when the new commit equals main or belongs to a
pull request), `start` (state saved before any branch exists), `refresh` (a run on another commit raises), `verdict`,
`suspects`, `delete_branches` (only a branch still on the throwaway commit), `finish`, `step` (refresh, give way,
180 minute bound, guard before every dispatch, consumers, verdict), `pinned_plugins`, `edge_wheel`, `line`, `due`,
`chosen`.

## What changed, edge by edge (the 54 lines)

| Old | New | Risk to check |
|---|---|---|
| `subprocess.run(["gh", ...], timeout=120)` in `gh()` | `Gh(commands, repo, timeout=120).run(args, retry=False)` | never retried, reads included, as before. Every `api()` write (POST commit, POST refs, DELETE refs) goes through it |
| `gh run cancel RUN --repo R`, result ignored | `GitHub(...).cancel_run(run, live=True)`: one read of the run, then `POST actions/runs/RUN/cancel` (`retry=False`); a finished run gets no POST | the lane counts as cancelled either way, as before; an answer other than cancelled / not cancellable is now said on stderr |
| a process of the single-lane tool per lane: `dispatch BRANCH --suite LANE --os OS`, run id read from its stdout | in process: `dispatch.bound(rt, ...)`, `focused.cmd_dispatch(window.lane_args(branch, lane, os, wait=False))`; `SystemExit` is the refusal; the run id is read from the same printed line with the same expression | every guard of that library applies (5 in flight, release run, SHA guard, pending marker). The reason kept in `waiting` is the last line of what it said, as before |
| the gate tool as a process: `make check NAME=.. CORE=..` in the plugins worktree, output to `<work>/<name>.log`, refusal detected by its text in the log | the run lock: `lock_of(allow_full).run(Request(argv=["make", "-C", TREE, "check", ...]))`; the lock's runner is replaced for this call by one that sends the output to the same log file (`sh -c 'exec "$@" >"$0" 2>&1' LOG make ...`); refusal = `Outcome.status != "ran"` | the full-suite refusal is checked by the lock on the plain `make -C TREE check ...` argv, before the `sh` wrapper exists. `-C TREE` replaces the working directory (the lock does not pass one to the child) |
| `HZ_ALLOW_FULL=1` in the launchd job's environment let the Consumer checks pass the gate | option `--allow-full` on `start`, `step`, `consumers` and `daemon rehearse`, passed to `lock_of(allow_full)`; logged by the lock | without it the checks are `unavailable` (verdict incomplete), as before without the variable |
| `pgrep -fl "…focused…admit"` and a word `prepare` or `apply` | `others.admission_running(commands)`: `ps -ww -axo pid=,command=` through the Commands provider; the same rule for the former tool, and in addition `sm head prepare`, `head apply`, `head admit`, `release admit`; a failing `ps` (or an OS error) refuses | wider than before on purpose: the native admission has no child process of the old name. Like before it matches the text anywhere in a command line: see Risks |
| `json.loads(queue_path().read_text()).get("items", [])`, path from `HZ_QUEUE_STATE` or `~/.cache` | `others.queue_items(queue_path())`, the path from `others.queue_file(settings)`: setting `queue_state_file` when the queue area has one, else the queue's file in `legacy_state_dir`; read-only in `legacy.py` | same file, same exceptions; in addition items that are not a list refuse instead of raising. The environment override is gone |
| state file `~/.cache/hz-rehearse.json` (`HZ_REHEARSE_STATE`), written atomically | `store.load/save("heinzel.rehearse.state", "rehearsal")`, the same document (`current`, `last`, `last_green`) | a NEW place: the old file is not read (it does not exist on this machine) |
| `fcntl.flock` on `<state>.lock`, exit 75 when held | `store.lock("heinzel.rehearse")`; `Busy` becomes `Wait` (exit 75) | not shared with the old tool's lock (see below) |
| `time.time()` (3 places) | `clock.now()` | none |
| the note tool as a process | `dispatch.journal(rt, text)`: one dated line in the store, kind `heinzel.journal` | the journal is not yet the page `sm note` writes |
| constants `R`, `PLUGINS`, `WORK`, the plugins repository inside `PIN` | settings `repo`, `rehearse_plugins_clone`, `rehearse_scratch_dir`, `rehearse_plugins_repo` (empty: `<repo>-plugins`), bound per call | the pin pattern is built from the setting; read live once (see parity) |
| `MAX_OPEN_MINUTES = 180` | the same default; setting `rehearse_max_open_minutes` may lower it, never raise it | none |
| messages that named a tool | `the merge queue holds ...`, `` cancelled by `sm release rehearse cancel` ``, `` (`sm release rehearse step` or `cancel`) ``, `the single-lane start gave no run`, `refusing: cannot read the merge queue state` (without the path, which named the queue tool) | text only |
| `main()`: argparse, the order start -> step, the exit codes | `rehearsal.command(cmd, ...)` (the same order and codes) and Typer options in `rehearse.py` | `start` on an open rehearsal still performs `step`; a refused start is `Wait` (exit 75); red is exit 1 |

## What is new (not a move)

- `daemon rehearse` is ONE tick (`tick=True`); the core adds `--loop`. A guard that refuses (75) or an error (1) is
  returned as the tick's result model instead of raised, so a loop goes on at its interval and never restarts fast.
- `last_verdict(store)` and `open_rehearsal(store)` in `rehearse.py` for the areas that read the outcome.
- `consumers` returns a model (text: four lines) instead of printing JSON.

## Risks

1. Old and new do not see each other: state and lock are not shared. A rehearsal opened by the old tool is invisible
   here and the reverse; both could hold lanes at once (the single-lane cap of 5 still bounds the runs). The old
   launchd job is not installed on this machine, so this needs a hand-typed old command. Retire the old tool in the
   same step this one is approved.
2. The admission guard reads the process table and matches text anywhere in a command line, as the old one did. On
   2026-11-12 a live read found one such row: a shell of another agent whose inline script contained the words.
   The effect is fail-closed (a start waits; a running rehearsal gives way and is `incomplete`, `preempted`), never
   an unguarded dispatch. The precise signal is the admission area's to give (see hand-over).
3. The merge queue guard reads the queue's state FILE. When the queue area moves its state, `others.queue_file` /
   `others.queue_items` must be repointed in the same step, or the guard reads a file nothing writes any more.
4. `cancel_run` reads the run before it cancels; a run that finished in between is left alone, the lane is still
   recorded as `cancelled`. Same record as before, one write fewer.
5. `gate.commands = Logged(...)` replaces the runner of one `RunLock` instance. If `lock_of` later keeps state on the
   runner, this line must follow.

## Tests

- 25 moved tests (`test_native_rehearse_lib.py`): 329 of 371 lines identical, 62 of 71 assert lines identical.
  Changed assert lines (7): the fixture's check of the two state paths; three `main(["status" ...])` exit codes, now
  `status_code()`; the gate fake's check of its argv (no gate program in it any more); the name of the old switch in
  the unavailable detail (`--allow-full`); one comment. The fixture's seams changed with the edges (`cancel_run`,
  `EDGE.lane`, `gated`); the recorders keep the tuple shapes the assertions name.
  Dropped (1): the test that only the old launchd template carries the full-suite variable; its rule is now
  `test_without_allow_full_the_run_lock_refuses_the_check_and_the_consumers_are_unavailable`.
- 33 new tests (`test_native_rehearse.py`) through the command tree on a scripted GitHub that keeps branches and
  runs, with the REAL single-lane library and the REAL run lock policy in between: text and json of every command,
  the review gate (exit 70) of the five write commands, and the exact writes in order:
  start = POST commit, POST ref x2, POST dispatch x2; verdict = DELETE ref x2; cancel = POST cancel x2, DELETE ref x2;
  give way = POST cancel of the one running lane, DELETE ref x2. Guards: queue, unreadable queue, process table,
  GitHub down, release in flight, the SHA guard of the single-lane library, the 5-run cap, the lock.
- Coverage of `rehearse.py` and `rehearse_lib/`: 97 % lines and branches.

## For the independent reviewer

1. Read the 54 changed lines: `diff <(sed -n 61,413p tools/hz-rehearse) native/rehearse_lib/rehearsal.py` (the move
   script with every replacement is in the hand-over).
2. Confirm no path reaches a POST or DELETE before `refusal()` in `start`, and before `refusal()` per `step` for
   pending lanes; confirm `gh()` cannot retry.
3. Confirm `stop_lanes` and `delete_branches` can only touch runs and branches recorded in this rehearsal's state,
   and that `refresh` (run on the throwaway commit) still precedes every cancel in `step`.
4. Decide on Risks 1 to 3 for the switch-over.
5. Nothing here has run against GitHub with a write: the first real `start` should be one lane
   (`--lanes cli --os ubuntu-24.04`), watched, followed by `cancel`.

## After the first independent read (2026-11-12)

- One review file and one unit for the five write commands (`start`, `step`, `consumers`, `cancel`, `daemon rehearse`),
  stated on the registrations: `files=("rehearse_lib", "dispatch.py", "dispatch_lib", "window.py", "lock.py", "lock_lib")`
  plus `rehearse.py` itself. Every lane is dispatched by `dispatch_lib/focused.cmd_dispatch` with the arguments of
  `window.lane_args` (`force=False`), journaled by `dispatch.journal`, and every Consumer check runs under the run lock
  (`lock.lock_of`), so a change in any of them voids this verdict too (the reviewer's R2).
- The read commands (`plan`, `status`) bind the library without a lane starter and without the gated runner
  (`bound_to_read`): they hold no way to dispatch.
- No path of this machine is a default any more: `rehearse_scratch_dir` defaults to the project's state home,
  `rehearse_plugins_clone` has no default (without it the Consumer checks are `unavailable`).
- Readers of the verdict (the reviewer's R1): `sm go` and the alarm watch read store kind `heinzel.rehearse.state`,
  key `rehearsal` (core-framework slice, commit 7decfe5).

COVERS: release rehearse start
COVERS: release rehearse step
COVERS: release rehearse consumers
COVERS: release rehearse cancel
COVERS: daemon rehearse

## Independent review (2026-11-13, main f9134cc)

REVIEWED: sha256=fa97f7290c5f4536311283d12f1857458a5641706841b3fb93179cfe609323c9

Verified: lanes only on rehearsal/* refs (`rehearsal.py:192`) on a throwaway commit refused when it is main or any PR's
head (`rehearsal.py:196-203`), dispatched through `focused.cmd_dispatch` with `force=False` (`window.py:166`), so
main and release/* are refused (`dispatch_lib/rules.py:30-37`), the SHA guard (`focused.py:567`), the 5-run cap and
a release/* run (`rules.py:113-116`) apply; `refusal()` precedes every write in `start` and every dispatch in `step`;
`gh()` never retries (`rehearsal.py:93`); cancel/finish touch only recorded runs and refs still on the throwaway
commit (`rehearsal.py:176-185, 269-275`); tick is one step, one open rehearsal, `--every` spacing; no hz-* calls;
`--format json` is one document; 58 tests pass, `sm self lint` passes.

Findings (blocking):
1. The Consumer check would NOT have caught yluq. It checks out core's PINNED plugins commit from ci.yml
   (`rehearsal.py:336-341, 366-369`), the same blind spot as the 0.9.40 candidate (pin on a relock branch, plugins
   main stale). It must check plugins MAIN, as compose does.
2. Not every consumer: names come from `make plan CONTRACT=... CORE=wheel` (`rehearsal.py:374-377`), which keeps only
   contract consumers whose declared core range admits the wheel (plugins `cli.py:124-127`). A plugin that does not
   allow the candidate is silently skipped, where compose's `validate_lock` raises (`cli.py:260-261`); an empty plan
   is `done` with nothing checked and can be green. Run `validate_lock(project, wheel)` for EVERY project on plugins
   main (bead yluq), failing on any refusal and on an empty set.
3. Consumers are opt-in (`--consumers`, plus `--allow-full`, else `unavailable` -> incomplete, not red). The yluq
   check is lock validation only, needs no full suite: make it default-on and not gated by `--allow-full`.
4. A running rehearsal does not yield to a candidate that starts mid-flight: `preempted()` checks admission and
   queue only (`rehearsal.py:155-165`); a release/* run blocks only new dispatches. Add `release_in_flight` there.
5. Minor: each pending lane may wait 30 min for a slot (`wait_for_slot=True`, `focused.py:261`) inside one tick under
   the lock; `--every 0` has no floor; the scratch dir (wheels) is never pruned.

VERDICT: CHANGES

## Fixes for the independent review (branch fix/rehearse-consumers, from main f9134cc; bead heinzel-management-yluq)

1. Plugins MAIN, not core's pin. `pinned_plugins` and the pin pattern are gone (and with them the setting
   `rehearse_plugins_repo`). `plugins_main()` runs `git -C CLONE fetch --quiet origin main` and
   `rev-parse --verify FETCH_HEAD^{commit}`; the detached worktree is that commit. The output says
   `plugins main <sha10>`; the record key is `plugins` (model field `plugins_main`).
2. Every consumer, compose's own check. `make plan` is gone. In the plugins worktree, `lock_check` runs the plugins
   repository's tooling (`uv run --quiet --frozen --no-build --group tooling python -c LOCK_CHECK WHEEL`):
   `cli.validate_lock(project, wheel)` for EVERY `cli.projects()` entry, as `scripts/compose.py` calls it. The only
   change to its input: the wheel's version is read as its release (`Version(v).base_version`), since an Edge wheel
   is X.Y.Z.devN and no plugin range admits a dev release; its dependencies are compared as built. Each refusal is
   `stale` (`name: why`) and red (`Consumer lock (...)`); no consumer at all (or none named `--only`) is red too.
   A script that does not run (no JSON on its last line) is `unavailable` (incomplete), never green.
   Every project, not only the `standard` profile compose installs: a superset, on purpose.
3. Default on. Every rehearsal records `consumers: {status: pending, full: --consumers}`; the lock check needs no run
   lock and no `--allow-full`. `--consumers` (and `consumers --full`) adds `make check` per plugin whose lock passed,
   under the run lock as before; a refused `make check` is `unavailable` but stale locks still make it red. Without
   a green Edge build yet the lock check stays pending and is retried each step while lanes run.
4. Yield to a candidate. `preempted()` also reads open PRs and active runs: a release/* PR or run that appears
   mid-flight cancels the rehearsal's own running lanes (`stop_lanes`, recorded runs only), drops pending ones,
   deletes only its own refs still on the throwaway commit, verdict incomplete with `preempted` and
   `yielded: gave way mid-flight (a release started (release/...))`. Unreadable GitHub cancels nothing.
5. Minor: a lane no longer waits for a slot inside the tick (`wait_for_slot=False` set in `start_lane`; it stays
   pending for the next tick); `due()` floors `--every`/the setting at 30 minutes; the downloaded wheel
   (`<work>/core`) is removed after every Consumer check, the logs stay.

Tests: through FakeGh (`test_native_rehearse.py`) the World now fakes plugins main on disk (one directory per plugin
with `pyproject.toml`, `plugin-lock.json`, `requirements.lock`) and runs the rehearsal's real `LOCK_CHECK` script
in process against `validate_lock` and its helpers copied from plugins main 3efe891 (`tests/rehearse_plugins_kit.py`).
New: the 0.9.40 case (core adds `pyjwt[crypto]` through an extra, one consumer not relocked: red with no option),
a consumer whose range does not admit the candidate (red, not skipped), an empty plugins main (red), a lock check
that cannot run (incomplete), no Edge build yet (retried, then green), a release run mid-flight (only its own lane
cancelled and its own refs deleted), the --every floor, plugins main read. 65 tests pass; coverage of
`rehearse.py` and `rehearse_lib/` 98 %; `sm self lint` clean.

## Independent re-review (2026-11-13, fix/rehearse-consumers 7b2f102)

COVERS: release rehearse start
COVERS: release rehearse step
COVERS: release rehearse consumers
COVERS: release rehearse cancel
COVERS: daemon rehearse
REVIEWED: sha256=42f296869ef8a9c23db127fb64955cf4ef1883093563bf2561577d47fdc9d51f

All four blocking and three minor findings of the first read are fixed:
1. Plugins MAIN, not the ci.yml pin: `plugins_main()` fetches origin main into the clone and the worktree is that
   commit (`rehearsal.py` `plugins_main`, `run_consumers`); the pin reader is gone.
2. Every consumer: `LOCK_CHECK` calls the plugins repository's own `validate_lock(project, wheel)` for every
   `cli.projects()` entry, each refusal is a `stale` row and red (`verdict`), an empty set is red, a check that
   cannot run is `unavailable` (incomplete, never green). Run for real by this reviewer: `LOCK_ARGV` in a detached
   worktree of plugins main 3efe891 against the 0.9.40 core wheel returned rc 0 and one verdict per plugin
   (12 projects; 4 accepted, 8 "Core dependencies changed"), so `uv run --frozen --no-build --group tooling` works.
3. Lock checks are part of every rehearsal, without `--consumers` or `--allow-full`; only `make check` needs them.
4. `preempted()` adds `release_in_flight`; a candidate starting mid-flight cancels only the rehearsal's own runs.
5. `wait_for_slot=False` per lane, `--every` floored at 30 minutes, the wheel dir removed after each check.

Regression test verified: `test_a_core_extra_that_adds_a_dependency_makes_a_stale_consumer_lock_red` (core adds
`pyjwt[crypto]` through an extra, one consumer lock not relocked) is red with no option, and `consumers` alone exits 1
with `STALE LOCK`. 65 tests pass, `sm self lint` passes (both via `sm gate run`).

Design choice (.dev wheel judged as its base release for ranges, dependencies compared as built): accepted. It
matches what compose will see for the release; it would mis-judge only if Edge's .dev base were not the next
release's version (for example a minor bump cut from a 0.9.x.dev Edge build).

Note, not blocking: checking EVERY project is stricter than compose (which validates "standard" profile plugins
only). On plugins main 3efe891 eight plugins hold locks that refuse the 0.9.40 wheel, so the first rehearsal on
current main will be red until they are relocked. That is the bead yluq scope; expect it.

VERDICT: APPROVE

## Independent re-review, campaign group 5 (integration/sm-complete at 479a8e8, 2026-11-13)

Reviewer wrote none of this code. Own worktree on review/campaign-g5; digest recomputed with `uv run sm self reviews`; every file of the unit diffed 1d69c43..479a8e8.
- rehearse.py, rehearse_lib/, dispatch.py, dispatch_lib/, lock_lib/ are byte-identical to 1d69c43. Changed imports:
  lock.py (`gate run` now writes=True with an explicit `exempt` reason, and `--exclusive`), core runlock (exclusive
  file, bounded default wait, an exclusive run is never light), window.py (typing), go/go_lib/status/mini (read-only
  views: pipeline run states, live-command check of NEXT), queue_lib (see release-resume.md). The rehearsal never
  asks for an exclusive run, so its lock behaviour is unchanged except that it waits while one holds the machine.
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

COVERS: release rehearse start
COVERS: release rehearse step
COVERS: release rehearse consumers
COVERS: release rehearse cancel
COVERS: daemon rehearse
REVIEWED: sha256=71e6b8227fca7b0ef3d6956aeb676a17ca83187ac0a0723bfd7fb728715c225c
VERDICT: APPROVE

## Independent re-review, re-verdict campaign g5 (integration/sm-complete at 2d0877b, 2026-11-13)

Reviewer wrote none of this code. Own worktree on review/reverdict-g5; digest recomputed with `uv run sm self reviews`
(equals the campaign list); every file of the unit diffed 479a8e8..2d0877b.
- Unit-own change: `dispatch_lib/focused.py` `cmd_dispatch` removes the pending marker only on a definite GitHub 4xx
  (not 408; `refused_by_github`), i.e. nothing started; a timeout/127 (message starts `refusing:`), a 5xx or no reply
  keeps the marker so the uncertain start must be reconciled. Tests in test_native_dispatch_focused.py. rehearse_lib,
  lock, window, go, status unchanged.
- Shared core files changed 479a8e8..2d0877b (engine.py, native.py): `_supersede` now takes the older run's own
  lock (`PIPELINE.KEY`, non-blocking flock; Busy is caught by the caller and reported as waiting, nothing half-marked
  matters since the marking is idempotent); `gate_problem` honours `exempt=` only for paths on the core allowlist
  `EXEMPT_PATHS` = {`gate run`} (narrows the gate, never widens it); `switch_pending` ignores a promote pid file only
  when `ps -o etime=` proves the pid started after the file's UTC time (+2 s), and holds (fails closed) when either
  time is unreadable; clock.now() is float per the Clock protocol, Commands.run returns Result(rc, out). Nothing
  here weakens a gate of this unit.
- Tests through `sm gate run -- uv run pytest` at 2d0877b (release, pipeline, rehearse, lock, queue, go, dispatch-focused,
  moved release/queue suites, core engine/native/runlock/window): 2496 collected, 0 failed, 0 errors, 25 skipped (N2 of the previous round is fixed at 2d0877b). NOT verified: any live GitHub write.

COVERS: release rehearse start
COVERS: release rehearse step
COVERS: release rehearse consumers
COVERS: release rehearse cancel
COVERS: daemon rehearse
REVIEWED: sha256=d86bda670e434353e30e3f907811cb7dd78e8a81910dfe139c47ceb541637ce1
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

COVERS: release rehearse start
COVERS: release rehearse step
COVERS: release rehearse consumers
COVERS: release rehearse cancel
COVERS: daemon rehearse
REVIEWED: sha256=14a55046fa511bf86634444bad89556be1dc0129f56ea2d7062d106445bc183b
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
- Delta: gated children get `child_environment()`; legacy.QUEUE_STATE is `queue.json`.
- FINDING (fail-open queue check): plugins/heinzel/src/steuermaschine_heinzel/native/rehearse_lib/others.py:33-34. The
  default of GoSettings.legacy_state_dir changed from Path("~/.cache") to None (native/go.py:29), but `queue_file`
  still does `settings.get("legacy_state_dir") or GoSettings.model_fields["legacy_state_dir"].default` and then
  `Path(str(root))`. With neither queue_state_file nor legacy_state_dir set this is `Path("None/queue.json")`,
  relative to the cwd (shown: `others.queue_file({})` == PosixPath('None/queue.json')). rehearsal.refusal() reads a
  missing file as "no queue was ever kept" (FileNotFoundError -> waiting = []), and preempted() likewise, so a
  rehearsal starts and keeps running while PRs wait in the merge queue. Baseline read ~/.cache/hz-queue.json. The
  machine values file sets queue_state_file, which masks it here, but the code default is wrong. Fix: fall back to
  `state.path("queue.json")` (or `state.root()`), as queue_daemon.state_file() does.
  plugins/heinzel/tests/test_native_rehearse.py:549 checks only `.name` and so misses it; assert the full path.

COVERS: release rehearse start
COVERS: release rehearse step
COVERS: release rehearse consumers
COVERS: release rehearse cancel
COVERS: daemon rehearse
REVIEWED: sha256=6472f8ef1b981a1fb832991038e921e7ec74d1ce4518a9ac33467f19df6effa7
VERDICT: CHANGES

## Re-verdict at abc64a4, group B (2026-11-14)

Independent reviewer; wrote none of this code. Findings, baselines and tests: ~/Code/packets/sm-reverdict-abc64a4/B.md (recorded verbatim by the integrator).

COVERS: release rehearse start
COVERS: release rehearse step
COVERS: release rehearse consumers
COVERS: release rehearse cancel
COVERS: daemon rehearse
REVIEWED: sha256=1814fb4fa5ee06f836437432444ab61f50b3198b1265305b5133d72d23f64f45
VERDICT: APPROVE

## Delta review: feat/sm-gate-lanes d94de45 (independent reviewer)

Changed in this unit since the last verdict: src/steuermaschine/runlock.py (light lane, rerun refusal); plugin native/lock.py (passes git_tree_state, setting text). Reviewed in full in two rounds (e6db9d9, d94de45),
probes now tests; see ~/Code/packets/sm-gate-lanes/REVIEW-2.md. Nothing in this command got weaker.

REVIEWED: sha256=650f415f91eb37907e4ff7874e598abdbe292568b4057afd8b68ecbb60583753
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Delta through shared files: lock.py (rerun refusal now applies to the consumer checks, `make -C DIR ...`; make exits 2 on a failed target, class usage, so they stay rerunnable), window/release_window/focused_slots/pr_owned (next-admission), mini.py, sonar.py, toolmap, defaults. Gate-lanes re-verdicted this unit on its own base; the composition adds nothing on the rehearse path. Approve.
COVERS: release rehearse start
COVERS: release rehearse step
COVERS: release rehearse consumers
COVERS: release rehearse cancel
COVERS: daemon rehearse
REVIEWED: sha256=70429ca6fa833ab9f5829d802470d23f29e17528335671c1060dfaf28ee6cbb6
VERDICT: APPROVE

## Re-verdict at f346492 (bead heinzel-management-04ro + integration/sm-batch3, 2026-11-14)

Independent reviewer; wrote neither 04ro nor window admission. Review: ~/Code/packets/sm-04ro/REVIEW.md.
Stale through core and native window.py; rehearse imports only `lane_args`, which is unchanged. Own code unchanged.
COVERS: release rehearse start
COVERS: release rehearse step
COVERS: release rehearse consumers
COVERS: release rehearse cancel
COVERS: daemon rehearse
REVIEWED: sha256=d807300f2c3c55ccd2e87bbbf2a8b376a21506d4cabb714871d18c489714261b
VERDICT: APPROVE
