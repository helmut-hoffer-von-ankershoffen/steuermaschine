# Port review: the release train (`sm daemon train`, `sm release train clear | next | resume-ci`; reads: `release train status | plan`)

Slice heinzel-native-queue-release (branch slice/heinzel-native-train). Written by the porting agent. The `REVIEWED:`
line and the verdict line are the independent reviewer's and are NOT there yet. One verdict covers the four write
commands below; `sm self reviews` prints the digest the verdict must name (all four share one unit).

COVERS: daemon train
COVERS: release train clear
COVERS: release train next
COVERS: release train resume-ci

## 1. The review unit

`native/train.py` plus `files=("train_lib", "queue_lib", "release_lib", <the reviewed candidate sources>)`: everything
the four commands execute in this process, and the two reviewed sources the release child runs (`release_lib/
approved_sources/`), so a re-pin voids this verdict as it voids the release's. `queue_lib` is read by the moved train for the main gate, the freeze and the flow flag (`hzlib`,
`release_flow`); `release_lib` gives the edge (`sm()`, the notes, the records) and the wording of old names
(`release_lib/names.py`). Neither library is changed by this slice (the release unit stays at its approved digest
715a1eff...). The release itself is NOT executed in process: the train starts `sm release resume X ...` as a child
process, which is gated by its own approved review. Not in any unit: `toolmap.py` (data; `release_lib/names.py`
reads its rows to word old names) and the reads' own files.

## 2. What moved, verbatim

`python3 plugins/heinzel/port/move.py verify OLD_TOOLS_DIR` recomputes every moved file from its source, prints every
changed line and fails on any other difference or when a count moves (train_lib: 44 edge lines, 57 lines removed or
replaced; 20 changed lines in the moved tests). Source: the management checkout's tools at git 170e6f3.

| Old file | Module in `native/train_lib/` | Lines | Changed lines |
|---|---|---|---|
| hz-train | train.py | 1156 | 29 |
| hz_next.py | batch_next.py | 145 | 8 |
| hz_plan.py | plan.py | 149 | 7 |
| hz_digest.py, 10 definitions (roots REAL_BAD, is_focused, classify_pr, load_known, known_bead) | digest.py | extract | 0 |

The digest extract is keyed `./hz_digest.py` in `train_lib/moved_map.py`: the same old file is already extracted by
queue_lib under the key `hz_digest.py` (other roots), and move.py keys its tables by source name.

Moved tests (assertions unchanged except where named in TEST_PATCHES): test_train.py, test_pipelined_train.py,
test_train_main_gate.py, test_next_start.py, test_plan.py -> `tests/moved/test_native_train_*.py`.

## 3. The edges (every changed line is one of these; `moved_map.py` gives each patch its reason)

- Line rules of port/move.py: sibling imports relative (`from . import batch_next as hz_next`, `plan as hz_plan`),
  `subprocess.run(` -> `edge.run(` (the core's runner inside a native command), `from . import edge` added.
- hz-train: the detached runner is `sm daemon train --run X` (own session, as before); one release is
  `sm release resume X --intro ... --snapshot-acceptance --candidate-gate` as a child whose lines are read; the version
  probe in the release checkout is `sm gate run --priority 2 --hold-exempt -- uv run --frozen bump-my-version ...`;
  the child's environment carries the train's checkout as `SM_HEINZEL__RELEASE_DIR` (and `HZ_RELEASE_DIR`, same
  value) and no longer `HZ_GATE_PRIORITY`/`HZ_GATE_HOLD_EXEMPT` (the run lock reads no environment); freeing runner
  slots is `sm ci defer --cancel --force-release`; the deferred runs are listed with `sm ci deferred` (a failed call
  is noted as an unreadable list, never as "nothing deferred") and resumed with `sm release train resume-ci`, or with
  `sm ci rerun --all` under HZ_TRAIN_RESUME_ALL=1 (only options the native `ci defer|deferred|rerun` of the ops
  slice have); the block note names the version to clear (`clear X.Y.Z`, it has no default); the Mini proof is
  `sm release verify --mini --minimum X`; the note is `edge.note`; the owner notification is the notifier provider;
  the `__main__` block is gone.
- hz_next.py: no `sys.path`; the shell command line is split into an argument list (`edge.argv`, a leading `sm` is
  this program) and run by the core's runner; `hz-queue list --active` -> `sm queue list --active`,
  `hz-tick --digest` -> `sm go --view tick` through `edge.digest`, which turns an empty or failed digest into one
  `UNKNOWN: ...` blocker line (the view never says `blockers: none` without having read them); `hz-refill` ->
  `sm beads refill`; `hz-queue add` -> `sm queue add`; the GO message tool is retired (tool map), so `--start`
  announces nothing.
- hz_plan.py: no `sys.path`; the candidate list and the KNOWN registry are read from the records checkout (setting
  `queue_records_dir`) when used; the `__main__` block is gone.

## 4. New code, read in full

`native/train.py` (commands, settings binding, line capture), `native/train_status.py` (status as a model),
`native/train_lib/edge.py`, `native/train_lib/names.py` (wording: the train's state files are named, `the train's
v0.9.36.log`, not printed as paths; any other old name inside a path stays as on disk), `native/train_lib/resume_ci.py`
(port of the shell tool, failing closed on a GitHub read), `native/train_lib/legacy.py` (the former deferred list,
read-only), `native/train_lib/moved_map.py`.

## 5. Should-fixes of the release review, honoured here

- P2-5 (priority and hold exemption by environment): the train's own gated command passes them as options
  (`edge.gate()` of train_lib); the environment variables are no longer set. NOT fixable inside this unit: the gated
  commands of the release child (`release.sh`, the push) use `release_lib/edge.gate()`, which passes no option, and
  release_lib is frozen. Hunk needed there (owner of the release unit, re-review of 715a1eff):

  ```diff
  --- a/plugins/heinzel/src/steuermaschine_heinzel/native/release_lib/edge.py
  +++ b/plugins/heinzel/src/steuermaschine_heinzel/native/release_lib/edge.py
   def gate() -> list[str]:
  -    """The run lock in front of a heavy local command or a push: `sm gate run -- COMMAND...`."""
  -    return [*sm(), "gate", "run", "--"]
  +    """The run lock in front of a heavy local command or a push of a release: ahead of builders and past a hold,
  +    `sm gate run --priority 2 --hold-exempt -- COMMAND...` (the old train set both for every release)."""
  +    return [*sm(), "gate", "run", "--priority", "2", "--hold-exempt", "--"]
  ```

  Until then a release started by the native train waits like a manual one: behind the gate queue, and behind a
  hold. The train's own hold is off by default (HZ_TRAIN_GATE_HOLD unset), so the train does not block itself, but
  a hold somebody else set, or HZ_TRAIN_GATE_HOLD=1, stops the release at release.sh until the hold goes.
- P2-4 (HZ_RELEASE_DIR silently ignored): for the train the setting wins and the tick says so. `daemon train` (tick
  and `--run`) warns on stderr when HZ_RELEASE_DIR is set and differs from the train's checkout, and when
  HZ_TRAIN_CHECKOUT / HZ_TRAIN_STATE_DIR / HZ_TRAIN_EVERY is set and differs from its setting
  (release_train_checkout, release_train_state_dir, release_train_every_minutes). A variable without a setting
  applies as before. The same warning for a manual `sm release resume` belongs in `release_lib/binding.py`
  (frozen; not done here).

## 6. State adopted, unchanged

`~/.cache/hz-train/` (setting `release_train_state_dir`; default the old path or HZ_TRAIN_STATE_DIR):
`vX.Y.Z.json` (atomic, indent 1, sorted keys), `vX.Y.Z.lock`, `vX.Y.Z.log`, `train.json` (cadence), `train.lock`,
`mini.lock`, `state.json` -> `state.json.migrated`, and the batch records `next-vX.json`. Read as before:
`~/.cache/hz-release.json`, `~/.cache/hz-release/vX.json`, `~/.cache/hz-queue.json` (plan). The deferred runs are no
longer the old file: resume-ci reads and replaces the store document `sm ci defer` records (kind
`heinzel.ci_ops.deferred`, key `runs`) under the same store lock; until that document exists it reads the former
file named by `ci_ops_legacy_deferred_file` read-only (`train_lib/legacy.py`), as `sm ci deferred` does. It never
writes an old path. Tests run on byte-identical copies of the live train files
(`tests/fixtures/release/train/`, compared with the live ones on 2026-11-13): status reads them, clear rewrites one in
the same format and leaves the others byte for byte, `next --start` writes `next-vX.json` in the old format.

## 7. Side effects, in order (unchanged from the old train)

Tick: train lock -> incident pause -> legacy migration (not with --dry-run) -> start a detached runner per idle active
train -> cut decision (release PR open, freeze, foreign receipt, tail, releasable commits, cadence, main gate) ->
state file -> note -> detached runner. Runner: version lock -> prepare checkout (git, version probe under the run
lock) -> optional gate hold -> `sm release resume` child, its lines into vX.log -> free runner slots once (`sm ci
defer --cancel --force-release`, cancel an unstarted main run) -> after the tag: resume deferred CI -> Mini proof ->
owner notification once -> finish. `next --start`: `sm queue add` for the green PRs, then the record. `clear X.Y.Z`
(version required): refused, nothing written, when X has no active train, when the release receipt names X
unfinished, when a release/vX or bump/vX PR is open, when a run of either branch is not completed, when any of these
reads fails, and while the version lock is held; otherwise X's state file and a note. `resume-ci`: refused with the
list unchanged when the runs or the open PRs cannot be read; else `gh run rerun` per queued or combine PR's run,
then the deferred list replaced (not with --dry-run).

## 8. Known gaps the reviewer should weigh

- `sm ci defer` is not native yet (ops slice): freeing runner slots and listing/resuming deferred CI fail softly
  (best effort, as the moved code treats them); `sm go --view tick` and `sm beads refill` are not native yet: `next`
  then lists one UNKNOWN blocker and no builders to nudge.
- `release train next` is one write command (its view alone writes nothing; `--start` writes), as asked by the
  coordinator, instead of a split into a read and a write command (INTEGRATION 8.1).
- `train_lib/train.py: import subprocess` is listed in tests/gate_known.json: the detached runner and the streamed
  release child need Popen, which the core's runner cannot express.

## 9. Fixes after the independent review of 79686d1

- B1 (`release train clear`): the version is required (no default; the old default cleared the newest active train,
  in the pipelined case the waiting N+1 instead of the blocked N). Refused, with nothing written, while that version's
  candidate lives: the release receipt names it unfinished, its release/vX or bump/vX PR is open, or a run of either
  branch is not completed; a failed read refuses too. Only the named version's file is ever written. The old tool's
  uses stay: a train blocked by NOT ACCEPTED or by three failed attempts is cleared once its cause is fixed, and a
  dropped candidate (`sm release drop X --reason ... --close-pr`) is cleared after the drop. Tests on copies of the
  real train files in the pipelined state (N 0.9.39 blocked, N+1 0.9.40 waiting with the real pre-tag receipt): no
  version, receipt unfinished, PR open, run active, unreadable GitHub or receipt, the allowed clear of N (N+1, the
  receipt and GitHub untouched) and the clear after a drop.
- S2: the train calls only options the native `ci defer` family has (`sm ci deferred`, `sm ci rerun --all`,
  `sm ci defer --cancel --force-release`); resume-ci works on the store list `sm ci defer` records. Should the ops
  slice change the document's shape, `release train resume-ci` must follow (it reads `{"runs": [{"id", "branch",
  "workflow"}]}`, as `ci_ops_lib/deferred.py` on slice/heinzel-native-ops2 writes it).
- S3 (`release train resume-ci`): a failed `gh run list` or `gh pr list` refuses (exit 1); nothing is re-run and the
  list stays as it was.
- Not changed here: S1 (needs the release unit's hunk), S4 (setting fallback; contract 8.4).

## Independent review of 39fb0a0 (one verdict for the four commands above)

Checked against the code (paths under `native/`):
- `release train clear` (`train.py:273-285`, `clear_refusal` 231-262): the version is a required argument (no
  argument: exit 2, nothing read); no active train of that version refuses before any GitHub read (277-278, tried
  ad hoc with 0.9.41: exit 1, files and GitHub untouched); an unfinished receipt of that version (247), an open
  release/vX or bump/vX PR (249-254), a not completed run of either branch (255-261) and any failed or malformed
  read (243-246, 250, 257) refuse with exit 1 before `run(module, ["clear", ...])`, so nothing is written. The
  moved `clear` (`train_lib/train.py:1116-1126`) still refuses under the version lock and runs under train.lock.
  The block notes name the version (`train_lib/train.py:619, 772, 849`). Tests 131-210 on copies of the pipelined
  state: the old trap (clearing the waiting N+1) is refused.
- `resume-ci`: a failed or non-list `gh run list`/`gh pr list` returns before `save` (`train_lib/resume_ci.py:43-51`,
  save only at 76-77); test 302. It reads and replaces the store document kind `heinzel.ci_ops.deferred` key `runs`
  under the store lock of the same name (`train.py:50, 398-414`), identical to `ci_ops_lib/deferred.py` KIND/LOCK and
  row shape on slice/heinzel-native-ops2 (1d0a65b).
- The train calls only `sm ci defer --cancel --force-release`, `sm ci deferred`, `sm ci rerun --all` and
  `sm release train resume-ci` (`train_lib/train.py:394, 503, 512, 514`), all present on ops2; no `--list` or
  `--resume` call remains (only old wording in strings, translated by `names.py`). An unreadable `sm ci deferred`
  is noted as unreadable, never as "nothing deferred" (505-508), so without ops2 the train re-runs nothing.
- General: no force push, no git stash, no push at all in this unit, no auto-merge, no PR head SHA validated, no
  hz-* script started (Popen only for `sm release resume` and the detached `sm daemon train --run`); writes are
  serialised by train.lock (tick, clear), vX.lock (run, clear) and the store lock (resume-ci); the version probe runs
  under `sm gate run --priority 2 --hold-exempt`. `--format json` emits one document (tests parse stdout whole).
- Ran: train tests, moved train tests, release writes, `tests/test_gate_structure.py` (all pass); `sm self lint`
  passes. No parity scenarios exist for the train.

Open, not blocking:
- The ops2 dependency is explicit only in this file (8, 9 S2), not in code: before ops2 merges, a manual
  `sm release train resume-ci` finds no setting `ci_ops_legacy_deferred_file` and answers "nothing deferred"
  (exit 0, writes nothing). Safe, but misleading; refuse when the list source is unknown.
- `resume-ci`: a failed `sm queue list --active` is read as "nothing queued" (`train.py:393-397`); fail safe (the
  runs stay as `later`), but silent.
- `clear`: the GitHub checks run before train.lock is taken (small window, as the old lock check had).
- `release train next --start` takes no lock of its own (as before; `sm queue add` serialises itself).
- S1 (release_lib gate options) stays open as described in 5.

## Independent re-review on main a870f08 (digest 3cd50284... at 5cc25de -> c4be5d5e... on main)

- The unit's 44 files (`sm self reviews --format json`) differ between 5cc25de and main in one file only:
  `release_lib/approved_sources/ci.yml`, one line, the structural lint job `timeout-minutes: 5` -> `10` (the merged
  re-pin, independently reviewed with the release unit). No train behaviour depends on it; it only voids the digest.
  No file of the unit is missing or added on main.
- Merge with ops2 (1d0a65b): `release train resume-ci` (`train.py:398-414`) reads and replaces the store document
  kind `heinzel.ci_ops.deferred` key `runs` under `store.lock("heinzel.ci_ops.deferred")`, identical to
  `ci_ops_lib/deferred.py` KIND/KEY/LOCK, which `sm ci defer --cancel` and `sm ci rerun` take (`ci_ops.py:212, 329`);
  the saved row shape `{"id": str, "branch", "workflow"}` is the one `deferred.save` writes; the legacy fallback
  parses the old file exactly as `ci_ops_lib/legacy.deferred_runs` does. The setting `ci_ops_legacy_deferred_file`
  now exists (ops2, `ci_ops.py:41`), so the earlier note is resolved: without a store list resume-ci reads the
  same source as `sm ci deferred`, and says "nothing deferred" exactly when `sm ci deferred` lists nothing.
  Not blocking: the train duplicates KIND, the lock and `save` instead of importing `ci_ops_lib/deferred.py`; a
  future shape change must touch both.
- Ran: train commands + moved train tests + merge-train + ci_ops + tick tests: 393 passed, 4 failed. The 4 failures
  are ops2 tests outside this unit (`test_*_off_until_*review_is_approved` in test_native_ci_ops_{defer,quarantine,
  rerun,sonar}.py): `reviewed=False` sets only `allow_unreviewed`, and with the ci-* verdicts merged in 3c27d1b the
  gate is open, so those tests no longer reach the gate. Fixture issue of the ops2 area, not of the train.
  `tests/test_gate_structure.py` passes; `sm self lint` passes.

## Independent review (feat/repair-main 7fc817a, base main 5c87f14)

Reviewer: independent; wrote none of this code. Unit recomputed by `sm self reviews`. Stale only because it shares
queue_lib/queue_daemon.py: diffed every file of the unit against 5c87f14; the only change is the `sm queue main-rerun`
repaired-plan path (queue_daemon.py:501-507, :514-519, :579-583, :650-669, :679-682; see reviews/queue-main-rerun.md).
No queue add/drop, daemon tick, ship, PR, release or train code path changed; the restored-by-rerun result keeps its
shape (one optional `repaired` key). Related suites and `sm self lint` pass through `sm gate run`.

REVIEWED: sha256=5b542d4028c18d457083158e190bef61ce9510715b2f2f7616ed6570fd7e3392

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
- Train unit only: `release_lib/candidate_policy.py` (the `job_proof` call above) and `release_lib/runner.py`
  changed too. The train reaches the runner only through `sm release resume ... --candidate-gate`
  (`train_lib/train.py:442`); no train_lib file or `train.py` changed. Runner changes read for the train path:
  unmarked `dispatch` (`runner.py:1110-1123`) now resolves the ref to an exact SHA first and `bound_run`
  (`:485-525`) adopts only a workflow_dispatch run of that workflow at that SHA created after the dispatch,
  stops on two candidates and keeps marked (acceptance) dispatch unchanged (`:1111`, `:465`); a main move between
  resolve and dispatch leaves the run unadopted until the step times out (fail closed, no misbinding). Compose
  refresh (`:1200-1209`, `compose_fix_landed` `:1233-1259`) re-dispatches once per failed run only when plugins
  main moved ahead and a changed plugin version is on PyPI (bounded per failed run, not in total; not blocking).
  PyPI propagation window (`:86`, `:2418-2426`) only delays the stop by 5 minutes after a success run.
  `--open-pr` (`:2499-2504`, `:2590`) is a separate mode never taken by the train. The release units' own verdict
  on these runner bytes is the other reviewer's.
- Read along (not in this unit's digest): beads ka69/iy04. `go.report` (`native/go.py:127-136`) passes NEXT, THEN and
  ALSO through `go_lib/live.py` `checker`, which shows any `sm` command whose leaf path or `--option` is not in the
  running tree as `missing: ...` (`live.py:21-50`); the bare `sm` of this worktree (read-only) printed only live
  commands. ASIDE lines are not checked (they named `sm focus add/done`, which are live); not blocking.

COVERS: daemon train
COVERS: release train clear
COVERS: release train next
COVERS: release train resume-ci
REVIEWED: sha256=7bb8e3772a8888092da816b790f1c955726e930f24a9392812a5c066ce4366fd

## Independent re-review (integration/sm-complete 479a8e8, campaign g6)

Reviewer: independent; wrote none of this code. Unit recomputed with `sm self reviews` (66 files; the digest now also
covers the imported core files and admission_lib). Diffed every unit file against 61dcce0 (the last APPROVE above).

What the train executes changed in three places:
- `train_lib/train.py`: every `gh` call of `Real` is bounded (`hzlib.gh_timeout`: reads 120 s, writes 300 s); a timed-out
  `_gh` raises (the callers already treat a raise as a failed read: fail closed), a timed-out `run cancel` is noted as
  unknown, `has_tag` raises instead of reporting "no tag".
- `queue_lib/hzlib.py`: `gh()` bounded the same way, a timeout is exit 124 with an empty answer; `main_release_gate`
  then refuses ("main CI evidence is unreadable"), so a stalled forge never opens a cut.
- `queue_lib/known_failures.guard_cleared` needs the same repo and the exact cleared head (stricter; not on the train path).
The rest of the unit's diff (`queue_daemon.py`, `main_plan.py`, `merge_train.py`, `release_lib/runner.py`,
`admission_lib/*`, core) is not imported by the train's code (train.py and train_lib import only queue_lib `edge`,
`hzlib`, `release_flow`, `known_failures`, and release_lib `approved`, `edge`, `names`); the release runner runs in
the `sm release resume` child under its own review. Removed `__main__` blocks in queue_lib are dead code for the train.

Daemon checks: single instance by non-blocking flock (train.lock for tick and clear, vX.lock per runner; no pid files,
so no reused-pid hazard); the tick is bounded, the detached runner lives as long as its release child; no signal, no
kill, no install or restart of the live hz (the Mini proof is `sm release verify --mini`, a read); every write
command (tick, `--run`, clear, next --start, resume-ci) waits or skips its tick while ~/.cache/sm-promote-switch-pending
names a live pid, and a `--loop` ends when the release pointer moves (native.py:446-470). Blocks and stalls are notes
and owner notifications, not auto-fixes.

Not blocking:
- `has_tag` now raises on a timeout; inside the `keep_hold` watcher thread (train_lib/train.py:459-472) that ends the thread, so
  the gate hold is no longer refreshed and expires (900 s) and runner slots may not be freed: safe direction. Catching
  it there would keep the early hold release.
- `proc.wait()` on the release child has no ceiling of its own (as before; the release steps carry their timeouts).
- `--loop` is accepted together with `--run X.Y.Z` (tick=True); vX.lock makes a repeat harmless.

Tests: `sm gate run -- uv run pytest` test_native_train_{commands,cov_b_state,digest_known}.py,
test_native_queue_cov_a_merge_train.py and moved/test_native_{queue_merge_train,train_main_gate,train_next_start,
train_pipelined,train_plan,train_self_test}.py: all pass.

COVERS: daemon train
COVERS: release train clear
COVERS: release train next
COVERS: release train resume-ci
REVIEWED: sha256=7e46f72231ab707abba8bdc511f3361c994cfa4511c0f4cbe6c718782a2d6467
VERDICT: APPROVE

## Re-verdict (integration/sm-complete 2d0877b, frozen; campaign re-verdict g6)

Reviewer: independent; wrote none of this code. Digest recomputed with the worktree's sm (`self reviews`, 66 files):
719d3d04a5c41d8383305095674e0030276dea1dacdc0ad628236f843e22758d, as in the campaign list. Delta read: 479a8e8..2d0877b of every unit file.

- `train.py`, `train_lib/*`, `train_status.py`: unchanged.
- `queue_lib/hzlib.py`: `gh_api_method` also reads `--method=X`, `-XPUT`, and a `gh api` with -f/-F/--input
  as POST; only the timeout bound (write 300 s) depends on it.
- `queue_lib/flakes_read.py`, `merge_gate.py`: gh calls bounded; a timeout reads as "" or a failed write, never
  as success. `merge_train.py`: the freeze wait gives up after 60 polls with "NOT merged" (the queue lock is no
  longer held forever). `queue_daemon.py`: a main rerun is refused when Core's focused jobs would run again on
  attempt 2 (they refuse it), and `main_plan.gap` is checked against the pinned head (`guarded_head`), not
  a live read. `release_lib/runner.py`: `has_tag` answers only on 200 or a 404 body and stops otherwise; an
  `--open-pr` of a rejected release is refused first. `admission_lib/wait.py`: docstring. `github/cli.py`
  `ci cancel`: digit-only run id. All fail closed.
- Core files in the unit, 479a8e8..2d0877b: `engine.py` `_supersede` now takes the older run's own lock
before marking it superseded (Busy: exit 75, nothing half-written; the new state is only in memory then);
`native.py` `switch_pending` ignores a pending file whose pid started after the file's UTC stamp (`ps -o etime=`
through the process runner; unreadable start: the file holds, fail closed; bin/promote writes "PID UTC-TIME"),
`EXEMPT_PATHS` limits `exempt=` to `gate run`, and `NativeRuntime.commands` is a lazy lookup. All safe.

Not blocking:
- Follow-up still open, and no bead found: `keep_hold` (train_lib/train.py:457-472) calls `self.has_tag`, which
  raises RuntimeError on a gh timeout, outside any try. The watcher thread dies, so the gate hold stops being
  refreshed (it expires after 900 s while the release still runs) and `on_candidate` (freeing runner slots) never
  fires if it had not yet. The `finally` still releases the hold and the release itself goes on, so correctness
  holds; catch the error there (log it, keep looping). This should be a bead.
- As before: `proc.wait()` has no own ceiling; `--loop` with `--run` is harmless.

Tests through `sm gate run -- uv run pytest`: test_native_guards_{floor,floor_cov_rules,governor,hooks,lim,
loadguard,smprocs,uv}.py, test_native_leakwatch{,_moved}.py, test_native_train_{commands,cov_b_state,digest_known}.py,
test_native_queue_cov_a_merge_train.py, moved/test_native_{queue_merge_train,train_main_gate,train_next_start,
train_pipelined,train_plan,train_self_test}.py: 487 passed.

COVERS: daemon train
COVERS: release train clear
COVERS: release train next
COVERS: release train resume-ci
REVIEWED: sha256=719d3d04a5c41d8383305095674e0030276dea1dacdc0ad628236f843e22758d
VERDICT: APPROVE

## Independent re-review, re-verdict campaign g5, release plugin fix (integration/sm-complete at 539b7f7, 2026-11-13)

Reviewer wrote none of this code. Own branch review/reverdict-g5-ovvg from 539b7f7 (contains fix/release-plugin-runid 234dc8d, bead
heinzel-management-ovvg; no code change between 60a83a5 and 539b7f7); digest recomputed with `uv run sm self reviews`; code diffed 2d0877b..60a83a5 (release.py,
release_lib/runner.py only). Everything before the fix is covered by this file's previous verdict at 539b7f7.
- Moved only because release_lib/runner.py is in this unit. The train starts a release as `sm daemon train --run X.Y.Z`
  (a core version, never `--plugin`), so it never reaches `release_plugin`, `plugin_dispatch`, `plugin_run` or
  `plugin_run_tag`. main()'s new check (`--redispatch` only with `--plugin`) cannot fire on a train command line. The
  generic `dispatch`/`ours`/`bound_run` that the core pypi, compose and acceptance steps use are unchanged. Only the runner
  reads the plugin receipt (hz-release-plugins.json) with its new `_dispatch`/`_run_id`/`_replaced` keys; train_lib does not.
  Train, merge-train and train-plan suites plus the plugin and cut tests through `sm gate run`: 270 passed, 0 failed.
- Tests through `sm gate run -- uv run pytest` at 60a83a5 (code-identical to 539b7f7; the same 85 files as before): 2506 tests, 0 failed, 0 errors, 25 skipped; the 10 tests of test_native_release_plugin_runid.py all pass.
  NOT verified: a live dispatch or any live GitHub write.

COVERS: daemon train
COVERS: release train clear
COVERS: release train next
COVERS: release train resume-ci
REVIEWED: sha256=13a4dbfcc67600545aaed0d8eb060e587523b8e461fd1b7f342af11cb37ca5f7
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

COVERS: daemon train
COVERS: release train clear
COVERS: release train next
COVERS: release train resume-ci
REVIEWED: sha256=49d06bf4a4d3ea4102f9a469cc7971074348692a02a7a991e6bca92ad15ca54f
VERDICT: APPROVE

## Campaign re-verdict g6 (2026-11-14, independent; reviewer wrote none of this code)

Target integration/sm-dod at c36bbe1, baseline 903a1b7. Digest recomputed with the worktree's sm (`uv run sm self reviews`, 226 files); it equals the campaign list. Read: the delta of train.py, train_lib/{train,plan,batch_next,edge}.py, queue_lib/ and release_lib/ (the unit's own libraries), state.py, environment.py's use here, and the shared core (native.py writers' lock, statemove.py, config.py, models.py, helpdoc.py, smprocs.py). The other changed imports are other groups' units. Tests through `sm gate run`: test_native_train_{commands,cov_b_state,digest_known}.py within the g6 run (268 passed, exit 0). No launchd job was loaded or unloaded.

State paths: STATE_DIR, the gate hold, the receipt, the tail receipts, the queue state, the freeze and the holds now come from `state.path(...)` (the state directory, refusing while any of it is unmigrated) or from a setting; the old variables HZ_TRAIN_STATE_DIR, HZ_TRAIN_CHECKOUT, HZ_TRAIN_EVERY, HZ_TRAIN_GATE_HOLD, HZ_TRAIN_RESUME_ALL, HZ_TRAIN_NOTIFY reach only their settings (`old_variable`), a file setting wins and a differing variable is named on stderr. The repo plist (not installed by this branch) and the live hz-train plist name no state path, only ~/Library/Logs/hz-train.log, which is outside the moves on purpose (docs/SWITCH-state-paths.md). The detached run gets `child_environment()`, the release child gets HZ_RELEASE_DIR and SM_HEINZEL__RELEASE_DIR as before. New: a failing tag check keeps the gate hold and asks again (fail closed).
Queue library: main_state's merge-tier rules (a cancelled run replaced by a newer push, a green rerun repairing the pinned red run) apply only when `hzlib.merge_tier()` is true (setting merge_tier_mode, else main's focused_plan.py; unreadable: older rules). The release gate keeps "a release needs a first-attempt full green main run": main_release_gate still sees the attempt of the effective run.
Notes, not blocking:
- queue_lib/main_batch.py:112 `valid_run` now accepts `run_attempt >= 1` for the delegated final run regardless of the merge tier (the docstring ties it to ADR 0149); the delegating run still must be attempt 1 (hzlib.py:365), and main_release_gate still distinguishes attempt 1. Gate it on the tier or say why not.
- train_lib/train.py:726 dry-run text names `HZ_TRAIN_RESUME_ALL=1 = sm ci defer --resume`; `sm ci defer` has no --resume and the variable is now the setting release_train_resume_all.

COVERS: daemon train
COVERS: release train clear
COVERS: release train next
COVERS: release train resume-ci
REVIEWED: sha256=2b523f509b9d160966d35f842ac1dffa8a5a4cff2e9883ff3eb96751643ee7cb
VERDICT: APPROVE

## Re-verdict at abc64a4, group B (2026-11-14)

Independent reviewer; wrote none of this code. Findings, baselines and tests: ~/Code/packets/sm-reverdict-abc64a4/B.md (recorded verbatim by the integrator).

COVERS: daemon train
COVERS: release train clear
COVERS: release train next
COVERS: release train resume-ci
REVIEWED: sha256=a8082e815ceed440aa95e6ed8979e3a978a4a4c41543267ff5b6592c27d7d436
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Delta: release_lib/runner.py (release recut): release_pr ignores fork and CLOSED PRs, prefers OPEN over MERGED, two open PRs stop; merge_release_pr no longer indexes None. It reaches the train only through the resume steps ("release PR open/merged"): after a drop closed the PR, a resume of that version opens a new PR instead of skipping the step, which is the intended recut path. toolmap row, defaults, config/protocols. runner.py is byte-equal to the recut tip aaae054.
COVERS: daemon train
COVERS: release train clear
COVERS: release train next
COVERS: release train resume-ci
REVIEWED: sha256=cca4b95f372b8d3cd9e33a7c30f43dc431ff2e6035ab19a9cf4e10dd728b5894
VERDICT: APPROVE
