# Port review: the merge queue (`sm queue add`, `sm queue drop`, `sm daemon queue`; reads: `queue list`, `queue why`)

Slice heinzel-native-queue-release. Written by the porting agent. `queue-drop.md` and `daemon-queue.md` are this same
text; each needs its own verdict line. The verdict line is the independent reviewer's and is NOT there yet.

## 1. What moved, verbatim

`python3 plugins/heinzel/port/move.py verify OLD_TOOLS_DIR` recomputes every moved file from its source, prints every
changed line and fails on any other difference or when the number of changed lines is not the pinned one
(51 in modules, 15 import lines in tests; `queue_lib/moved_map.py` EDGE_LINES). Sources: the working tree of the management
checkout on 2026-11-12 (git 170e6f3 plus the uncommitted edits live that evening).

| Old file | Module in `native/queue_lib/` | Lines | Changed lines |
|---|---|---|---|
| hz-queue | queue_daemon.py | 1601 | 17 |
| hzlib.py | hzlib.py | 439 | 8 |
| hz-merge-train | merge_train.py | 242 | 4 |
| hz-merge-gate | merge_gate.py | 256 | 3 |
| hz-diff-guard | diff_guard.py | 168 | 2 |
| main_checkpoint.py | main_checkpoint.py | 745 | 5 |
| main_batch.py, release_candidate.py | same names | 980 | 2 each |
| main_checkpoint_reader.py (STANDALONE: also started as a program of its own) | same name | 192 | 0 |
| known_main_infra.py, release_flow.py, hz_combine_notes.py (combine_notes.py) | same names | 213 | 0 |
| hz_alarm.py, 29 of its definitions | stall.py | extract | 2 |
| hz_digest.py, 8 definitions | known_failures.py | extract | 0 |
| hz-flakes, 6 definitions | flakes_read.py | extract | 3 |
| hz-stamp, 14 definitions | bead_stamp.py | extract | 3 |

An extract is the named root definitions plus everything they refer to (computed, printed by `verify`), in source
order, under the source's own import lines; each definition is the source's text. Roots: stall = REQUIRED,
green_since, wait_reason, stall_alarm, pending_reason, process, clear; known_failures = load_known, known_bead,
guard_cleared; flakes_read = read_attempt; bead_stamp = do_stamp.

Line rules (all 51 lines are one of these or a patch of section 2): sibling imports become relative imports; the
`sys.path` line is removed; `Path(__file__).resolve().parent.parent` becomes `edge.records_dir()`;
`subprocess.run(` becomes `edge.run(` (same arguments); `from . import edge` is added.

New code, not moved, to be read in full: `native/queue.py` (commands and the tick),
`native/queue_lib/edge.py` (edges), `native/queue_lib/moved_map.py` (tables).

The review digest of all three commands covers `native/queue.py` and every `.py` in `native/queue_lib/`
(`sm self reviews` prints it). `daemon queue` is registered from `native/queue.py` for that reason: the tick is
queue code. Other tools of this slice (push gate, ship, pr, combine) go to `native/ship.py` + `ship_lib/`, so they do
not void this verdict.

## 2. Every semantic change, with its test

Tests are in `plugins/heinzel/tests/`: C = test_native_queue_commands.py, E = test_native_queue_edge.py,
M = moved/ (the old tests, assertions unchanged).

1. **No script is executed.** `hzlib.load(name)` returns a fresh copy of the moved module and raises for a tool that
   is not in this package (old: executed the script file). E `test_a_cross_tool_load_...`,
   E `test_no_module_of_the_package_reaches_a_script_directory_...`.
2. **Note** (`hzlib.note`): the note script is not started; `edge.note` appends the same line
   (`- MM-DD HH:MM text`, whitespace collapsed) to the same document under the same lock. A note that cannot be
   written returns False; the old call ignored the script's result too. E `test_note_appends_...`,
   E `test_a_note_that_cannot_be_written_...`. To be replaced by the journal of the coordinator-tools slice.
3. **Bead stamp at merge**: the stamp script is not started; `edge.stamp_merged` calls the moved `do_stamp` with
   output swallowed and every failure ignored, as the unread subprocess was. Its `gh`/`bd` calls go through the
   runner. E `test_stamp_merged_...`, C `test_tick_merges_a_ready_pr_...`.
4. **Alarm**: the stall diagnosis and the alarm state functions are the moved text (stall.py), so the state file
   format (`~/.cache/hz-alarm.json` or HZ_ALARM_STATE), its lock and the owner push are unchanged.
   `edge.raise_alarm(key, level, text)` / `edge.clear_alarm(key)` are the two calls for the alarm owner to take over.
   E `test_raise_alarm_writes_the_alarm_state_format_...`, C `test_tick_records_a_pr_the_forge_blocks_...`.
5. **Known failures and failing tests of a run**: moved text (known_failures.py, flakes_read.py); the registry file
   comes from `queue_records_dir/research/`. M test_native_queue_main_rerun.py, test_native_queue_fix_forward.py.
6. **Processes** (`edge.run`): inside a native command a text-capturing call goes through `providers.commands`; a
   runner timeout (rc 124) is raised as `TimeoutExpired`, a missing program as `FileNotFoundError`, `check=True`
   raises, so the moved code's handling is alive (review finding S1). Calls with stdin input, output into a file or
   bytes stay plain calls (list in section 4). A call that asks for lenient decoding (`errors=`, only the
   `gh pr merge` call) is the plain call when the runner is the core's strict one, so a stray byte in gh's output
   cannot kill the tick after the merge (finding D1); a scripted runner still receives it.
   E `test_a_merge_call_whose_output_is_not_utf8_...`, E `test_a_strict_call_still_goes_through_the_core_runner`. E `test_run_goes_through_the_runner_...`,
   E `test_the_moved_admission_read_retries_once_after_a_timeout_...`, E `test_run_outside_a_native_command_...`.
6a. **The checkpoint reader is untouched** (`main_checkpoint_reader.py`, listed as STANDALONE in moved_map.py): the
   queue starts it isolated (`uv run --no-project ... python -I FILE`), where a relative import fails; no rule
   applies to it and it equals the old file byte for byte (finding B3).
   E `test_the_checkpoint_reader_starts_as_a_program_of_its_own_...`, E `test_the_checkpoint_reader_starts_exactly_as_the_queue_starts_it`.
7. **Self-restart of the loop**: watches the files of `queue_lib/`, the native modules above it and the core
   (old: ten script files). M `test_shared_validator_updates_invalidate_the_running_queue` (unchanged).
8. **State file**: without the setting `queue_state_file` the moved rule holds (HZ_QUEUE_STATE, else
   `~/.cache/hz-queue.json`); with the setting, that file, and a different HZ_QUEUE_STATE is refused (finding S3).
   Lock and log stay `~/.cache/hz-queue.lock` / `.log`. C `test_two_named_state_files_are_refused_...`,
   C `test_list_reads_the_real_state_file_...`, C `test_drop_removes_only_the_named_entry_...`.
9. **Options of `add`**: every option of the old parser, typed; an option given with an empty value is passed on and
   refused by the moved code as before (finding B2). C `test_add_refuses_what_the_old_parser_refused_...` (14 cases).
   Two kinds at once is exit 2, as the old mutually exclusive group. C `test_add_refuses_two_kinds_...`.
10. **Output**: stdout and stderr lines are the moved code's, each on its stream, with the old command name replaced
    in hints; exit codes are the moved ones. json adds typed fields: entries, `red_main` (pinned run and head, fixing
    commits, red batch, restoring rerun), `stalled_with_green_checks`, `blocked`. `drop` now prints `dropped KEY` /
    `not in the queue: KEY` (old: silent). C `test_list_json_...`, `test_list_shows_a_pinned_red_main_...`,
    `test_why_reports_a_stall_with_green_checks_...`, `test_drop_...`.
11. **The loop** (`sm daemon queue --loop 1`): one call is one decision whether a tick is due (first call, interval
    passed, queue file changed, merge hold released), then the moved `tick()` with the moved log lines. Differences to
    the old `run`: (a) a second loop exits 75 instead of blocking on the lock (the service manager restarts it);
    (b) the first log line is `queue run started` (old: `hz-queue run started`); (c) a tick's lines reach stdout at
    the tick's end, the log file as before; (d) the lock is taken at the first call and held until the process ends.
    C `test_tick_merges_...`, `test_tick_does_not_merge_while_a_merge_freeze_holds_and_wakes_...`,
    `test_tick_wakes_when_the_queue_file_changed_...`, `test_a_second_loop_waits_...`.
12. **Write gate**: `add`, `drop` and `daemon queue` exit 70 until their review file ends with the verdict line.
    C `test_a_write_command_without_an_approved_review_refuses`.

## 3. Parity

`parity/queue-add.md`: 23 scenarios, old tool against native command on the same state copy and scripted `gh`,
all the same after six declared differences: exit code, both streams, the call transcript, state file, notes, alarm
state, log, lock. Includes a merge tick (`gh pr merge 801 --squash --match-head-commit HEAD` after the same 12 reads
on both sides), a merge the forge refuses, a freeze, a moved reviewed head, BLOCKED with green checks, and the B2
cases. Reproduce: `python3 plugins/heinzel/port/parity.py OLD_TOOLS_DIR`.

## 4. Deviations from docs/INTEGRATION.md section 2 (accepted for this pass; for the refactoring pass)

- The moved code keeps `time.time` / `time.sleep`, `os.environ` (`HZ_*`: HZ_QUEUE_STATE, HZ_MAIN_BATCH_PUBLISH,
  HZ_KNOWN_FAILURES, HZ_ALARM_STATE, HZ_ALARM_NOTIFY, HZ_ALARM_RENOTIFY_MIN, HZ_INTERVENTIONS_LOG, HZ_RELEASE_FLOW_DIR),
  `fcntl` locks and `~/.cache` paths (hz-queue.json/.lock/.log/.tmp, hz-alarm.json, hz-note.lock, hz-merge-freeze,
  hz-release-holds, hz-flakes/). This is what makes the switch lose nothing.
- GitHub calls are `gh` argv built in the moved modules, run by the one runner; not yet the `Gh` client.
- Plain process calls the runner cannot express: `merge_gate.py` (ruleset PUT with stdin), `main_checkpoint.py`
  (artifact download into a file; `uv run` of the checkpoint reader), `main_checkpoint_reader.py`, `main_batch.py`
  and `release_candidate.py` (fixed git/gh with `check=True`, bytes), `stall.push` (the owner's notify command).
- The owner push of a stall alarm is still the configured notify command (HZ_ALARM_NOTIFY or the bridge script),
  not `providers.notifier`: switching it without knowing the plugin's notifier would silence the stall alarm.
- Notes and the interventions log still carry the old tool name in their text (`hz-queue: batch: ...`).
- State is not in `providers.store`; the queue's files are the old ones on purpose.
- `bead_stamp.ENV` puts `~/.local/bin:/opt/homebrew/bin` first on PATH for `gh` and `bd` (moved text).

## 5. Not verified

- Nothing ran against GitHub, the live queue file, its lock or log, or launchd. The plist
  `plugins/heinzel/launchd/io.github.helmut-hoffer-von-ankershoffen.sm-queue.plist` is linted, not loaded.
- After `bin/promote` the running loop keeps executing the previous install (its files do not change), so the
  self-restart does not fire: the switch needs a `launchctl kickstart` of the job. Not tested.
- `--loop` over real time; a tick longer than its interval; SIGTERM during a tick (the moved idle wait is no longer
  used; the core loop sleeps through `providers.clock`).
- `queue checkpoint` and `queue main-rerun` are moved but have no native command yet; `--fix-forward` runs through
  the moved code and its moved tests but has no command-tree test and no parity scenario.
- 6 moved tests skip: 4 load the release runner (now another slice), 2 the whole flakes tool.
  `test_main_checkpoint.py` needs the candidate policy module and is not moved yet.
- A batch of several core PRs with the main batch proof (HZ_MAIN_BATCH_PUBLISH=1) has only its moved unit tests.
- Coverage of the area is not measured (`bin/check` is not on main).

## 6. Independent review and verdict: `sm daemon queue`

Reviewer: independent agent, wrote none of this code. Working notes: `REVIEW-INDEPENDENT-queue.md` (worktree root, untracked).

## Independent review (feat/repair-main 7fc817a, base main 5c87f14)

Reviewer: independent; wrote none of this code. Unit recomputed by `sm self reviews`. Stale only because it shares
queue_lib/queue_daemon.py: diffed every file of the unit against 5c87f14; the only change is the `sm queue main-rerun`
repaired-plan path (queue_daemon.py:501-507, :514-519, :579-583, :650-669, :679-682; see reviews/queue-main-rerun.md).
No queue add/drop, daemon tick, ship, PR, release or train code path changed; the restored-by-rerun result keeps its
shape (one optional `repaired` key). Related suites and `sm self lint` pass through `sm gate run`.

COVERS: daemon queue
REVIEWED: sha256=151e6a41e7adfa99b119249b56bb565176819143f0e21414e4285af19e20f95b

Reviewed code: commit 4e44e9c (on main ed4a1df). `native/queue.py` blob 6760281f56301a6af59f929b6e7f4d955d862f50,
`native/queue_lib/` tree c40aef0417ab01d41253fe76eb9cd0bf57535757. The digest is the one `sm self reviews` prints for
these 20 files. An earlier verdict of mine named f8d157b9 (commit 5441640); since then only three files of the unit
changed, all read: `queue_lib/main_checkpoint_reader.py` (back to the old file, byte for byte), `queue_lib/edge.py`
(`run`: a call with `errors=` or `encoding=` is the plain call when the runner is the core's), `queue_lib/moved_map.py`
(STANDALONE table, 51 edge lines). Every check below was run again on 4e44e9c.

Scope of this verdict: `daemon queue`, one tick and the loop around it. `queue add` and `queue drop` have their own
files. It does not cover `queue checkpoint` and `queue main-rerun` as commands (there are none yet), nor the launchd
plist, which nothing installs.

Review unit: `native/queue.py` imports only the core and `queue_lib/edge`; `queue_lib/` imports only itself, the
standard library and the core. Everything of the plugin that the tick executes is in the unit. Outside it: the core
(command tree, runner, loop option, this gate), the registration lines in `native/__init__.py` and `plugin.py`,
the programs `gh`, `bd`, `git` and `uv` (the checkpoint reader, which by design runs `scripts/` of the core
repository at an authenticated main commit), the owner's notify command (stall alarm), and the records checkout as
data (known failures, queue order, notes document, bead database).

What I checked myself, not taken from the builder:
- Verbatim move: `port/move.py verify` exit 0 (51 edge lines); my own `diff` of the 12 whole files (43 changed lines,
  every one read: imports, three record paths, `subprocess.run` -> `edge.run`, load, note, stamp, restart check) and
  an AST comparison of the four extracts (57 definitions, source-identical except 4 edge lines).
- `tick()`, `assess`, `gate`, `main_state`, `core_batch` and `merge_train.train` are the old text. The new code is
  the function `daemon_queue` in `native/queue.py` and `queue_lib/edge.py`; both read in full.
- Old `run --once` against native `daemon queue` on throwaway homes with a scripted `gh`: 47 scenarios (23 of the
  builder, 24 of mine) the same in exit code, both streams, call transcript in order, state file, notes, alarm
  state, log and lock, after the declared differences.
  - Main: no merge on a red main, none when main's runs cannot be read; merge when the newest run is in progress
    and the last completed one is green; on a red main the `--fixes-main` PR goes alone and the other stays.
  - Batch: 7 ready core PRs, exactly 5 `gh pr merge` calls; a second batch within five minutes waits.
  - Holds: `~/.cache/hz-merge-freeze`, and a file at `~/.cache/hz-release-holds` (the moved rule: anything there but
    an empty directory is a hold), each stop every core merge.
  - Required contexts: a failing required check marks needs work; BLOCKED with a required context missing waits
    and records the reason; a failing gate check that main requires, or rules that cannot be read, means no
    merge; a failing gate check that main does not require merges (the old rule).
  - Head: every merge is `gh pr merge N --squash --match-head-commit HEAD` with the head whose checks were just
    read; a pinned PR whose head moved before the tick or inside the train is not merged and marked needs work.
  - Draft, closed, merged elsewhere, conflicts, unmet `--after`, pending check, forge refusing the merge: as before.
- Real time, old and native runner sharing one throwaway home: an old loop holding `~/.cache/hz-queue.lock` makes
  the native tick exit 75 without ticking; a native loop holding it makes the old `run --once` wait and tick only
  after the lock is free; `--loop 1 --interval 4` ticks at 0, 4 and 8 s; a queue edit wakes it within a second;
  touching a code file re-executes the same process, which takes the lock again; SIGTERM ends it at once; nothing
  is printed between ticks.
- B3, closed: `queue_lib/main_checkpoint_reader.py` equals the old file byte for byte; started the way
  `main_checkpoint.isolated_reader` starts it (`uv run --no-project --no-build --with coverage==7.17.0 python -I
  FILE`) it gets past its imports and stops at the same line as the old file on the same request. At 5441640 it
  died at a relative import, so the checkpoint gate could never pass.
- D1, closed: with a byte that is not UTF-8 in the output of `gh pr merge`, for a merge that succeeded and for one
  the forge refused, old and native now do the same (merged with batch and notes; needs work with the reason).
  At 5441640 the native tick died after the merge call.
- No script of the old tool set is started: note, bead stamp, alarm state, known failures and the failing-test
  read are moved text or `edge` functions; `edge.note` writes the line the note tool wrote, compared with it.
- Gate: without a verdict the command exits 70 and leaves the state untouched. `SM_ALLOW_UNREVIEWED_WRITES=1` and
  `allow_unreviewed_writes = true` in a settings file do not open it (core 2913b2c; both tried: exit 70).
  This verdict assumes that core fix stays.
- The 81 queue tests of the old `test_hz_watch_queue.py` (head pins, cap of five, freeze, red main and its
  exceptions), which the slice had not moved, pass on the moved modules with the same verdicts as on the old tools
  (run at 19b23fb, whose moved modules differ from this code only in the three files named above).

Declared differences to the old loop, each read and accepted: the command name in hints and log lines (nothing in
the old tools reads the `run started` line); times; the home path; `drop` output; one empty stdout line; usage
text; a second loop exits 75 instead of blocking (launchd starts it again); the tick's lines reach stdout when the
tick ends, the log file as before; the lock is held from the first call to the end of the process.

Not verified by me: a tick against GitHub or the live state; launchd; the main batch proof with
HZ_MAIN_BATCH_PUBLISH=1, a rerun of main's KNOWN failures and the fix-forward lane beyond their moved unit tests;
a checkpoint proof end to end (only that the reader starts).

Open, not blocking the tick:
- The stall alarm still starts the owner's notify command as a plain process instead of `providers.notifier`.
  Behaviour as before; switching it blind could silence the alarm. The owner has to accept it or name the notifier.
- `edge.run` keeps lenient decoding only when the runner is exactly the core's `Subprocess`. If the core ever wraps
  its runner, the merge call decodes strictly again; the two tests added for D1 would show it.
- `bead_stamp.ENV` puts `~/.local/bin:/opt/homebrew/bin` first on PATH (moved text), so a black-box run with
  `--bead` reaches the real `gh` and `bd`; no parity scenario may carry a bead until that is scripted.
- `tools_changed_at` stats the unit, `native/` and the core once a second; a file that vanishes between listing and
  stat ends the process with a traceback and launchd starts it again.
- The old queue suite I ran is still not in `tests/moved/` at this commit.

For the switch, outside this verdict: boot out the old job first (otherwise the new one exits 75 every 30 s);
keep `HZ_MAIN_BATCH_PUBLISH=1` in the plist; after `bin/promote` a running loop keeps the previous install until
it is kickstarted, and the gate is read only when the loop starts.

VERDICT: APPROVE

## Independent re-review of fix/batch-release at 61dcce0 (against f9134cc)

Reviewer did not write any of the change. Files of this unit that changed since f9134cc (`sm self reviews --format json`
files x `git diff --name-only f9134cc..61dcce0`): `queue_lib/job_proof.py` (new) and `queue_lib/main_checkpoint.py`
(+3 lines). Nothing else in the unit moved, so queue/ship behaviour is otherwise unchanged.

- `job_proof.needs_log` (`queue_lib/job_proof.py:24-37`) adds a step only for a dict job with int `id` > 0, the same
  `run_id`, `run_attempt == 1`, `status == "completed"`, `conclusion == "success"` (so cancelled, skipped, failure,
  neutral are left alone), int `runner_id` > 0 (0, None, "7" are left alone) and `steps == []` exactly.
  `log_proven` (`:40-50`) then requires a non-empty log after `strip()`; a `ValueError`/`OSError` (the gate's
  `CandidateError` is a ValueError, `release_candidate.py:44`; `main_checkpoint.require` raises it, `:51-53`) leaves
  the job unchanged. Any other exception (e.g. a subprocess timeout) propagates and refuses the whole gate: fail closed.
- A job left unchanged still meets the pinned check `job["runner_id"] > 0 and job.get("steps")`
  (`release_candidate.py:434-437`, file not changed); a proven job still passes all other checks of `validate`
  (inventory names, run, attempt, head SHA, conclusion) unchanged.
- Used in exactly two places: `main_checkpoint.py:302` (merged candidate anchor of the queue's main checkpoint,
  reads `actions/jobs/{id}/logs` through `download` with the 128 MiB bound `:48`) and
  `release_lib/candidate_policy.py:58` (release gate, `job_log` `:73-78`). The isolated focused reader
  (`main_checkpoint_reader.py`) does not import it.
- Not blocking: a non-empty log is a weak proof on its own (GitHub writes runner setup lines before any step);
  the weight is carried by runner_id > 0 + success + attempt 1, as bead 7d8e specifies. `candidate_policy.job_log`
  reads the log with `capture_output` and no size bound (memory only, not a gate weakening).
- Tests run (`sm gate run -- uv run pytest -q ...`): go live-next/views/workload-stale, queue commands/edge/main
  commands, ship commands, train commands, candidate empty-steps, compose refresh, plugin PyPI, release pr open,
  run binding, plus 18 moved queue/train/checkpoint/ship/candidate/go tests: exit 0, no failures. `sm self lint`: pass.

COVERS: daemon queue
REVIEWED: sha256=4aee60bf72fcf5bb67ee3cd3433eae76998e0a8f142ca64bdd9e7b5e80972275

VERDICT: APPROVE

## Independent review of integration/sm-complete at 479a8e8 (against live 1d69c43, campaign 2026-11-13)

COVERS: daemon queue
REVIEWED: sha256=2a9a2a0fc57c6e5f7a32fa76e4997a0272bb7bb25636b93194712d00fdb8dd40

Reviewer: independent (campaign group 4, branch review/campaign-g4); wrote none of this code. Digest recomputed with
`uv run sm self reviews` at 479a8e8. Diffed every file of the unit 1d69c43..479a8e8 (live sm = 1d69c43). Changed in
the unit: queue_lib/queue_daemon.py, main_plan.py (new), merge_train.py, hzlib.py, known_failures.py, job_proof.py
(new), main_checkpoint.py, moved_map.py, the `__main__` blocks dropped from diff_guard/main_batch/merge_gate/
merge_train/queue_daemon, native/queue.py; core config/defaults/engine/helpdoc/models/native/protocols/runlock (shared;
read for their effect on this unit only: the review gate, the promote switch-pending skip, typing).

Checked, in the order the code runs:
- State lock and corrupt file (F1): `load()` returns defaults only on FileNotFoundError; any other OSError, non-JSON or a
  non-object raises StateUnreadable (alarm best effort). `main()` turns it into exit 1 `REFUSED`, native queue
  list/why/add/drop and the daemon's result into Refuse(code 1); ship refuses with exit 1. Every save is inside
  `state_lock()` (flock on `<state>.state.lock`, reentrant per thread): update, add, add_fix_forward, drop, the
  checkpoint path; temp file is per pid. A corrupt file raises before any rerun or merge (update() of main_reruns
  precedes `gh run rerun`; tick's `data = load()` precedes the merge loop). Critical sections hold no network call.
- Guarded head (F2): `queue add` reads the head before and after the diff guard and refuses when it is unreadable or
  moved (nothing queued for that PR); the row stores `guarded_head`. The tick pins `reviewed_head or guarded_head`
  as `expected_head`; a row with neither is marked needs work and never merged. `guard_cleared` matches repo and the
  exact head.
- Stale main plan (q9kh): for a core row the tick's pre_merge_guard is plan_guard (earlier progress guard first, then
  `main_plan.gap`). In `merge_train.train` the guard runs after the settle loop, the head re-check and the freeze
  wait, immediately before `gh pr merge --match-head-commit HEAD`; a False or an exception returns before the merge
  call. The queue replaces the generic detail with the named refusal (row -> needs work, `queue why` names it with the
  re-prepare command). `gap` fails closed on: setting `queue_core_repo` unset, unreadable head/main, main or head moved
  during the read, a merge conflict, registry reader failure, a plan whose base is not current main. merge-tree runs
  in a disposable `--shared` clone (configured checkout never changed), git calls 120 s, reader 180 s. `queue add`
  refuses a core PR with a gap before anything is written.
- Merge read-back (F4): a failed `gh pr merge` reads `state,mergeCommit`: MERGED -> recorded as merged; OPEN/CLOSED ->
  not merged as before; unreadable -> "merge state unknown": the row stays queued with merge_unknown, alarm + note,
  and the tick `break`s (nothing more merged on an uncertain main). Next tick a MERGED merge_unknown row is
  re-recorded via train's `skip: MERGED` only after its head equals the pin; the alarm is cleared.
- gh timeouts (F5): hzlib.gh 120 s reads / 300 s writes, exit 124 on timeout; merge_train.gh the same (the merge
  call goes through plain subprocess with the timeout and is caught); settle loop bounded at 80 polls (60 min).
- recheck_needs_work: only rows with a reviewed_head, detail "checks ...", open, not draft, head unchanged, all
  blocking checks settled and green; requeued rows pass every guard of the tick again (plan guard included).
- No force push, stash, auto-merge or hz-* call added; the merge stays `--squash --match-head-commit`.

Findings (none blocks a merge that should not happen; all fail closed):
1. plan_guard passes `item.get("reviewed_head", "")`, not the pin (`reviewed_head or guarded_head`); for a guarded
   row `gap` re-reads the head from GitHub. Still safe (train re-checks head == pin and merges with
   --match-head-commit), but the plan should be computed for the pinned head: pass `pinned`. Same in `add`: pass
   `guarded_head`, not `expected_head`.
2. Residual race: main can move between `gap` and `gh pr merge` (seconds; another merger such as the release train or
   a person). GitHub offers no expected-base merge; document it as accepted.
3. Still unbounded on the tick path (pre-existing): train's `while merge_frozen: sleep(60)` holds the queue lock for
   the whole freeze; `merge_gate.write_ruleset` (`gh api -X PUT`, tick's flip) and `flakes_read.gh` have no timeout.
4. `gh_timeout` reads `gh api -f/-F/--input` without `-X` as a read (gh posts then): 120 s instead of 300 s; bounded.
5. Operational: rows queued by the live sm (1d69c43) carry no guarded_head; on the first tick of this code they turn
   "needs work: no guarded head recorded; ship it again". Re-ship the open rows at the switch.
6. main_plan._git inherits the caller's GIT_* and global git config; the protected reader strips them. Minor.

Tests (through `sm gate run`): test_native_queue_state_safety, moved/test_native_queue_main_plan, queue_commands,
queue_cov_b_daemon, queue_cov_a_merge_train, queue_cov_b_hzlib, ship_commands, queue_main_commands,
queue_repaired_main, rejudge, queue_edge: all pass. Moved queue suites (combine_notes, fix_forward,
guard_and_neighbours, main_batch, main_cancelled_timeout, main_rerun, merge_train, recheck, wake, watch_queue,
ship_push_gate) and the checkpoint suites: all pass except ONE: moved/test_native_queue_recheck.py::
test_a_green_needs_work_row_at_its_pinned_head_is_requeued_and_merges. It asserts the train kwargs
`{queue_core, expected_head}`; since the merge of fix/stale-main-plan (479a8e8) a core row also passes
`pre_merge_guard` (plan_guard). The behaviour is right (the row is requeued and merged through the guard); the test is
stale: e94fc35 (recheck) and bb2ead7 (stale main plan) were never run together. Tests are not in the digest.

Changes required:
1. Fix moved/test_native_queue_recheck.py: assert expected_head == PIN and that a core row's pre_merge_guard is the
   plan guard (the branch's own suite is red). The code digest does not change; a reviewer may then approve it.
2. Recommended in the same round (changes the digest): finding 1, pass the pinned/guarded head to main_plan.gap.

VERDICT: CHANGES

## Independent re-verdict of integration/sm-complete at 2d0877b (against 479a8e8, campaign 2026-11-13)

COVERS: daemon queue
REVIEWED: sha256=b3edd03f866a889f7f8209121d9b6b4556adf35723710f97f67824afe8083a84

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
Both changes required by the 479a8e8 review are done (test fixed; pin passed to gap). CHANGES lifted.

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

COVERS: daemon queue
REVIEWED: sha256=2a0087b4a28cd338c1ec54670de7304b44f74122510d79bd4d7754eb8347aa4c
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

COVERS: daemon queue
REVIEWED: sha256=bdd50bfde2e1fbc97c9a4c47bba5375001be8e1822a6a3449d526521804f8b46
VERDICT: CHANGES (finding 1: main_batch.valid_run accepts reruns without the merge-tier gate)

## Re-verdict at abc64a4, group B (2026-11-14)

Independent reviewer; wrote none of this code. Findings, baselines and tests: ~/Code/packets/sm-reverdict-abc64a4/B.md (recorded verbatim by the integrator).

COVERS: daemon queue
REVIEWED: sha256=b35f4cce4470c9ebd05175e0386d7877fc10612886dafc5e56804bfaf335b668
VERDICT: APPROVE

## Re-verdict at feat/sm-next-admission 5fe179a (delta review 4, 2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-next-admission/REVIEW.md, delta review 4 (recorded verbatim by the integrator; COVERS from `self reviews`).

COVERS: daemon queue
REVIEWED: sha256=e2f997ac837e2a3271876f4cf5bd0a0d82a59c78f971de08899a2245bd4695ca
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Delta: queue.py and queue_hold.py (next-admission gtco: the hold stops the tick; --except owner/repo#N), defaults.py (FileStore.delete), config/protocols. No other branch touches these files; the merged queue.py equals 5fe179a's. Matches the review file.
COVERS: daemon queue
REVIEWED: sha256=6ace5e4381653826f9172d023e9448a32a12ed0e9241b7e9e3922bd80388b4bd
VERDICT: APPROVE
