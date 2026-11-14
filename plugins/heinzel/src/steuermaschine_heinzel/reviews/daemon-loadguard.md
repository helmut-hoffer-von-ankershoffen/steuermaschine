# Port review: `sm daemon loadguard` (one tick of the load guard)

Old: `tools/<load guard>` (165 lines, `tick` and `status`). New: `native/guards_lib/loadguard.py` (122 lines),
`native/guards.py: daemon_loadguard`, `LoadguardTick`, `loadguard_paused`, `killer`; library `guards_lib/legacy.py: loadguard_stopped`.

## What it does (and one correction of the brief)

Above load 100 it pauses (SIGSTOP) the newest pytest/coverage process tree of this user that runs from a worktree under `~/Code`
with no run-lock or release command above it, one tree per tick, root first; below load 40 it continues (SIGCONT) the earliest
paused tree, children first. It records what it paused. It **never kills and never touches the run lock's hold file**: the brief
described the write side as "the hold file of the run lock"; the source does not do that, so none of `runlock.Policy.hold` is used.

## What moved unchanged

`parse_clock`, `parse_ps`, `ancestors`, `descendants`, `ungated_roots`, `plan` and the body of `tick`: 68 of 98 non-blank lines are
identical to the old file (difflib); the rest is the new module header and docstring, `Edge`, and the edge below.

## Every changed edge

| Old | New | Risk |
|---|---|---|
| `ps` via `subprocess.run` | `commands.run(["ps","-axo",...], timeout=30)` | none; rc != 0 or empty output now means "no decision" (see next) |
| unreadable `ps` = no processes = empty paused list saved | nothing signalled, list kept, exit 1 | deliberate: the old behaviour leaked stopped processes |
| `os.kill(pid, sig)`, `OSError` ignored | `commands.run(["kill","-STOP"/"-CONT", pid], timeout=10)`, rc ignored | `/bin/kill` must exist (macOS: yes); a dead pid is a failed kill, ignored, as before |
| `stopped.json` | store `heinzel.guards.loadguard` / `stopped`; former file adopted read-only while the store has no record | after the first tick the old file is no longer read; two writers (old and new job) at once would not see each other |
| log file | text on stdout | launchd's log file already received stdout |
| `GATED` regex | plus every sm process (`guards_lib/smprocs.py`: the venv's python running the `steuermaschine`/`sm` entry, or anything under the install root) whose command path is `gate run`, `release`, `train` or `daemon train`; a gated process is never a root itself | the spelling `sm gate run` never occurs in `ps` (the launcher execs python); the shapes are taken from the live table of a real `sm gate run` in `test_native_guards_smprocs.py` |
| (none) | no install root of sm (setting `guards_sm_install_roots`, else the running sm's) = nothing paused, exit 1; resuming goes on | deliberate: fail closed |
| `time.time` | `clock.now` | none |

## Risks

1. It sends signals. A wrong candidate is a stopped test run, not a killed one; the resume path is the same tick below load 40.
2. A tree paused by this job stays paused if the job stops and load never drops: `sm daemon loadguard` must run again, or `kill -CONT`.
3. `sm` processes that start tests under a command the regex does not know are candidates above load 100 (as before for unknown tools).

## What the independent reviewer must check

1. `diff` the moved functions against the old file (`plan`, `ungated_roots`, `tick`).
2. Confirm no path signals a pid outside the tree of a root that `ungated_roots` returned, and none the guard did not pause on resume.
3. Confirm `GATED` cannot match an ordinary pytest (the test pins `sm gates run` as not gated).
4. Run `sm daemon loadguard` once on the Mini below load 40 with an empty paused list (it must signal nothing), watched.
5. Decide whether the switch-over of the launchd job (old to new, same label) can leave the old job's paused trees unresumed: the former file is read once.

No verdict yet.

## Independent review (main f9134cc)

Reviewer: independent; wrote none of this code. Tests test_native_guards_loadguard.py and _smprocs.py pass.
Correct: never kills (SIGSTOP/SIGCONT only, guards.py:122-129); only this user's pytest/coverage trees under ~/Code with
no gated or sm gate/release/train ancestor (loadguard.py:76-86); fails closed without sm shapes or `ps` (guards.py:325-339).

Findings:
1. Not single instance: state is load-modify-save with no `store.lock` (guards.py:331-335, loadguard.py:119-131). A
   manual tick beside the launchd loop, or the old job beside the new one, can overwrite a record; a paused tree is then
   forgotten and stays SIGSTOPped. Take a store lock around the tick (refuse/wait when held).
2. A paused tree whose root has exited is dropped (loadguard.py:119) while its paused children live on, reparented, and
   are never continued. Continue the recorded pids that still exist with the same uid, or raise an alarm.
3. No alarm when something stays paused (e.g. longer than 30 min): add one instead of relying on someone noticing.

Verdict at f9134cc: CHANGES (superseded below).

## Independent review (integration/sm-complete 479a8e8, campaign g6)

Reviewer: independent; wrote none of this code. Unit recomputed with `sm self reviews` (17 files, shared with
daemon-governor), digest as in the campaign list. Read in full: `guards.py`, `guards_lib/loadguard.py`, `smprocs.py`,
`legacy.py`, and the tick loop in `steuermaschine/native.py`.

The three findings of the f9134cc review are fixed:
1. Single instance: the whole tick (load, decide, save) runs under `store.lock(LOADGUARD_KIND)`; a second tick gets
   exit 75 and touches nothing (guards.py:300-306; test `test_a_second_tick_waits_while_one_holds_the_store_lock...`).
2. Orphans and reused pids: a recorded root that is gone or runs another command is dropped (loadguard.py:131); its
   recorded children that still exist, are this uid and are stopped (`T`) get SIGCONT, children first, with a line
   (loadguard.py:110-113, 132-137; test with a reparented stopped child). A running reused pid is left alone.
3. Alarm: a tree paused > 30 min logs `ALARM ... kill -CONT <pids>` on every tick (loadguard.py:138-142); no auto-kill.
Also: SIGSTOP/SIGCONT only, never kill; candidates are only this uid's pytest/coverage roots under `~/Code/<dir>/`
with no gated ancestor (run lock, release, train, any sm `gate run|release|train|daemon train` by real shape), one
tree per tick; the live service (uv tool install) never matches; fails closed without `ps` or sm shapes (exit 1,
list kept); every `ps`/`kill` call has a timeout. Read-only run of `ungated_roots` on the live Mini table: 0 roots.
Write ticks are skipped while ~/.cache/sm-promote-switch-pending names a live pid, and the loop ends on a release
switch (native.py:446-470).

Not blocking:
- The resume path continues the recorded child pids that are alive with this uid without comparing their commands
  (only the root is compared); a reused child pid gets a harmless SIGCONT unless something else stopped it.
- The governor and the load guard keep separate records (see daemon-governor.md); both directions are safe.
- The live launchd job `hz-loadguard` still runs the former tool; the switch-over adopts its stopped.json read-only
  (setting guards_loadguard_legacy_dir) until the store has a record: stop the old job before the new one starts.

Tests: `sm gate run -- uv run pytest` test_native_guards_{governor,loadguard,smprocs,floor,hooks,lim,uv}.py: all pass.

COVERS: daemon loadguard
REVIEWED: sha256=25992363a4b5449ea8992a91189ae3be41621b8968c3d9384ddec474154dc56c
VERDICT: APPROVE

## Re-verdict (integration/sm-complete 2d0877b, frozen; campaign re-verdict g6)

Reviewer: independent; wrote none of this code. Digest recomputed with the worktree's sm (`self reviews`, 17 files,
shared with daemon governor): 59e29e5c14f1bf551d5f853ca262cf63ac7e9dd5408e1b2263372a46a5adcb07, as in the campaign list. Delta 479a8e8..2d0877b: `loadguard.py`,
`smprocs.py`, `hooks.py`, `legacy.py`, `lim.py`, `uvguard.py` and `guard_floor.py` are unchanged; the governor
change (continue a changed pid of the recorded uid before dropping it) is reviewed in daemon-governor.md and does
not touch the load guard's records; `guards.py` changed help text only.
- Core files in the unit, 479a8e8..2d0877b: `engine.py` `_supersede` now takes the older run's own lock
before marking it superseded (Busy: exit 75, nothing half-written; the new state is only in memory then);
`native.py` `switch_pending` ignores a pending file whose pid started after the file's UTC stamp (`ps -o etime=`
through the process runner; unreadable start: the file holds, fail closed; bin/promote writes "PID UTC-TIME"),
`EXEMPT_PATHS` limits `exempt=` to `gate run`, and `NativeRuntime.commands` is a lazy lookup. All safe.

Not blocking: as before (child pids continued without a command check; separate records from the governor; retire
the old launchd job at switch-over).

Tests through `sm gate run -- uv run pytest`: test_native_guards_{floor,floor_cov_rules,governor,hooks,lim,
loadguard,smprocs,uv}.py, test_native_leakwatch{,_moved}.py, test_native_train_{commands,cov_b_state,digest_known}.py,
test_native_queue_cov_a_merge_train.py, moved/test_native_{queue_merge_train,train_main_gate,train_next_start,
train_pipelined,train_plan,train_self_test}.py: 487 passed.

COVERS: daemon loadguard
REVIEWED: sha256=59e29e5c14f1bf551d5f853ca262cf63ac7e9dd5408e1b2263372a46a5adcb07
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

COVERS: daemon loadguard
REVIEWED: sha256=526c2a471bb46783a0b9b8b9df1cbccb51845adad7ad40e1fe1fc71fb293a621
VERDICT: APPROVE

## Campaign re-verdict g6 (2026-11-14, independent; reviewer wrote none of this code)

Target integration/sm-dod at c36bbe1, baseline 903a1b7. Digest recomputed with the worktree's sm (`uv run sm self reviews`); it equals the campaign list. The shared core delta and the test run are those of the g6 section in reviews/daemon-governor.md (same digest, same files): native.py writers' lock, statemove.py, config.py, models.py, helpdoc.py, smprocs.py moved to the core unchanged. 268 passed, exit 0, through `sm gate run`. No launchd job was loaded or unloaded.

Load guard: smprocs from the core (identical), GATED, GATED_PATHS, the STOP/RESUME thresholds and the tick are unchanged. It still never pauses a tree with the run lock, a release or a train among its ancestors, another user's process or the live service; when no install root can be established nothing is paused. Its legacy directory is the setting guards_loadguard_legacy_dir (read, never written). The live config value is an old path that bin/migrate_config.py rewrites to loadguard-legacy in the state directory, which is where `self migrate-state` moves the old directory (state.ELSEWHERE). The live plist names no state path, only its log in ~/Library/Logs and the sm launcher.

COVERS: daemon loadguard
REVIEWED: sha256=06ac74440bd9c504e5d13eb117192740cd3ecbbe8ab06d6ea6d8f5648079a3a8
VERDICT: APPROVE

## Re-verdict at abc64a4, group B (2026-11-14)

Independent reviewer; wrote none of this code. Findings, baselines and tests: ~/Code/packets/sm-reverdict-abc64a4/B.md (recorded verbatim by the integrator).

COVERS: daemon loadguard
REVIEWED: sha256=ea8eb3a0e6fa9da69a2d0526cc1c6d658dec3cf7cd7ff94575015450799f9088
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does.
COVERS: daemon loadguard
REVIEWED: sha256=fd47f3a4a3c4370fa231d89214e14ddc989eaa2f9b2900f7b7e78c62c5abf72a
VERDICT: APPROVE

## Re-verdict at feat/sm-ship d05ef01 (2026-11-14)

Independent reviewer; wrote none of this code. Unit code unchanged. The digest moved only through guard_floor.py:
`sm ship` is now a one-word write step that the switch refusal blocks while a promote switches the live sm. The
existing two-word steps are unchanged. This command does nothing differently.
COVERS: daemon loadguard
REVIEWED: sha256=063a7f69232b372dcb4631a69cc9fb27c6643fed58cba1127fb40c9bb3451862
VERDICT: APPROVE
