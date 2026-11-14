# Port review: `sm daemon governor` (one tick of the agent load governor)

Old: `tools/<governor>` (6 lines) and `tools/hz_governor.py` (170 lines). New: `native/guards_lib/governor.py` (127 lines),
`native/guards.py: daemon_governor`, `mini_governor`, `GovernorTick`, `GovernorStatus`, `governor_state`, `governor_digest`.

## What it does

Above the high load (45) it stops (SIGSTOP) up to 6 builder processes of this user per tick, highest CPU first; below the low load
(25) it continues 6 per 20 s. Eligible: args under builder worktrees, or a build tool whose cwd is under one. Never: the NEVER list
(live service, coordinator commands, Claude.app, Chrome, the governor), its own pid and parent. While the kill-switch file exists it
continues everything. It was never scheduled (no plist); this port makes it a tick so it can be.

## What moved unchanged

`eligible`, `pick`, `step`, `resume`, `alive`, `BUILDER`, `CHILD` and the constants: 60 of 100 non-blank lines identical (difflib).
All changes are listed:

| Old | New | Risk |
|---|---|---|
| `NEVER` | plus `sm daemon\|queue\|release\|train\|go\|gate\|mini`, and every sm process by its real shape (`guards_lib/smprocs.py`: the venv's python running the `steuermaschine`/`sm` entry, or anything under the install root), whatever it runs | only protects more; the shapes are taken from the live table of a real `sm daemon ... --loop 1` and `sm gate run` in `test_native_guards_smprocs.py` |
| (none) | no install root of sm (setting `guards_sm_install_roots`, else the running sm's) = nothing stopped, exit 1; resuming goes on | deliberate: fail closed |
| `BUILDER` `pytest-of-<user>` | `pytest-of-[^/\s]+` | matches other users' directories too; their processes fail the uid test |
| `HIGH`/`LOW` from environment | `bind(high, low)` from settings | settings may be set above the old ones: the owner's call |
| `ps_table`, `cwd_of` ran `ps`/`lsof` | parse text; the edge runs the commands | none |
| `safe_kill` (`os.kill`) | `kill -STOP/-CONT` through the Commands provider | failures ignored as before |
| state file, log file, `.off` file, loop, `stop`, `resume-all` | store state, stdout lines, setting for the off file, `--loop`, not ported | `stop`/`resume-all` are replaced by creating the off file and one tick |
| unreadable `ps` | signals nothing, exit 1 | deliberate |

## Risks

1. It stops processes. A wrongly eligible process is stopped until load falls under 25 or the off file exists.
2. It cannot tell a builder from a person's own pytest under `/Code/hz-*`; as before.
3. Not scheduled today: switching it on is a behaviour change of the Mini, not a port.

## What the independent reviewer must check

1. `NEVER` still covers the live service, the coordinator's own commands and the agent app; run `eligible` on a real `ps` listing read-only.
2. The off file path (`guards_governor_off_file`) and that one tick with it continues every recorded pid.
3. That `--dry-run` records and signals nothing (a test asserts it).
4. Decide whether the loop should be scheduled at all.

No verdict yet.

## Independent review (main f9134cc)

Reviewer: independent; wrote none of this code. Tests test_native_guards_governor.py pass; `--dry-run` records and
signals nothing (guards.py:452-453, governor.py:108).

Findings:
1. Touches non-agent processes: `BUILDER` (governor.py:36) matches `/Code/heinzel-management` (`heinzel-[^\s/]*`), the
   coordinator's checkout. Live on the Mini now: `/bin/sh /Users/<user>/Code/heinzel-management/tools/hz-popup-guard` is
   eligible by args, and every git/uv/python the coordinator runs there is eligible by cwd (governor.py:72-75). Only
   `.worktrees` under it is a builder. Exclude the management checkout outside `.worktrees`.
2. Not single instance: load-modify-save of the state without `store.lock` (guards.py:442-453); two ticks can lose a
   stopped pid, which is then never continued.
3. Stopped pids are dropped only when dead; a recorded pid reused by another process gets SIGCONT (governor.py:99,119-127):
   record and compare the args as the load guard does.
4. Never scheduled before: enabling it is an owner decision (behaviour change), and no alarm reports pids held stopped
   for long; add one.

Verdict at f9134cc: CHANGES (superseded below).

## Independent review (integration/sm-complete 479a8e8, campaign g6)

Reviewer: independent; wrote none of this code. Unit recomputed with `sm self reviews` (17 files, shared with
daemon-loadguard), digest as in the campaign list. Read in full: `guards.py`, `guards_lib/governor.py`, `smprocs.py`,
`legacy.py`, and the tick loop with the promote-switch skip in `steuermaschine/native.py`.

The four findings of the f9134cc review are fixed:
1. Allowlist: only test runners (`TEST`: pytest, vitest, playwright, jest, coverage run) under a builder worktree
   (`BUILDER`, which now excludes `~/Code/heinzel-management` outside `.worktrees/`) and their descendants are eligible
   (governor.py:46-47, 89-111); git/uv/make/node by cwd alone never are. Every candidate, descendants included, still
   passes `protected` (own pid and parent, other uid, NEVER with 8737 / the uv tool install / Claude.app / Chrome /
   coordinator commands, any sm process by its real shape). Read-only run of `pick` on the live Mini `ps`/`lsof`
   table: 3 eligible rows, all one builder's vitest in a heinzel-TOPIC worktree; nothing of the coordinator, the
   live service or the owner's apps.
2. Single instance: the tick runs under `store.lock(GOVERNOR_KIND)`; a second tick gets exit 75 and touches nothing
   (guards.py:423-429; test `test_a_second_governor_tick_waits_while_one_holds_the_store_lock`).
3. Reused pids: `same_process` keeps a record only while the pid is alive with the recorded first 120 characters of
   its args (governor.py:131-133, 141); a reused pid is dropped, never continued. The former state file stored the
   same shape, so adopted records are compared the same way.
4. Alarm, not auto-fix: a pid held > 30 min logs `ALARM` on every tick and in the digest line (governor.py:136-139,
   142-143, 184-185); nothing is continued because of the alarm.
Also: fails closed without sm shapes or a readable `ps` (exit 1, nothing signalled); `--dry-run` signals and saves
nothing; signals are SIGSTOP/SIGCONT only, never kill. `--loop` repeats bounded ticks (every `ps`/`lsof`/`kill` call
has a timeout); a write tick is skipped while ~/.cache/sm-promote-switch-pending names a live pid, and the loop ends
when the release pointer moves (native.py:446-470).

Not blocking:
- `~/Code/heinzel-plugins` (the shared plugins base checkout, not a worktree) matches `BUILDER`; a test run there is
  eligible. Builders use `heinzel-plugins-TOPIC` worktrees; tighten when convenient.
- No test pins "alive pid with other args is dropped and gets no SIGCONT" (only the dead-pid case); the code is right.
- The governor and the load guard keep separate records: a load guard resume between load 25 and 40 can continue a
  pid the governor holds (safe direction; the governor drops the record when the process exits).
- Inside `--loop` a busy lock (exit 75 through `fail`) ends the loop; launchd restarts it. Acceptable.
- Scheduling it at all stays the owner's call (a behaviour change of the Mini; no plist exists).

Tests: `sm gate run -- uv run pytest` test_native_guards_{governor,loadguard,smprocs,floor,hooks,lim,uv}.py: all pass.

COVERS: daemon governor
REVIEWED: sha256=25992363a4b5449ea8992a91189ae3be41621b8968c3d9384ddec474154dc56c
VERDICT: APPROVE

## Re-verdict (integration/sm-complete 2d0877b, frozen; campaign re-verdict g6)

Reviewer: independent; wrote none of this code. Digest recomputed with the worktree's sm (`self reviews`, 17 files):
59e29e5c14f1bf551d5f853ca262cf63ac7e9dd5408e1b2263372a46a5adcb07, as in the campaign list. Delta read: 479a8e8..2d0877b of every unit file.

- `guards_lib/governor.py` (bead ltui): a recorded pid that is alive with other args is now continued before it is
  dropped when its uid is the recorded one (new records store `uid`; an old record without one counts as the
  governor's own uid, the only one it stops). Another uid: no signal, an ALARM line. SIGCONT refused: ALARM line.
  `--dry-run` signals nothing. Pinned by the three new tests (continued once then dropped; other uid; refused CONT).
  Follow-up done.
- `guards.py`: help text of `lim` only.
- Core files in the unit, 479a8e8..2d0877b: `engine.py` `_supersede` now takes the older run's own lock
before marking it superseded (Busy: exit 75, nothing half-written; the new state is only in memory then);
`native.py` `switch_pending` ignores a pending file whose pid started after the file's UTC stamp (`ps -o etime=`
through the process runner; unreadable start: the file holds, fail closed; bin/promote writes "PID UTC-TIME"),
`EXEMPT_PATHS` limits `exempt=` to `gate run`, and `NativeRuntime.commands` is a lazy lookup. All safe.

Not blocking:
- Follow-up still open, and no bead found (`bd search`): `BUILDER` (governor.py:54,
  `/Code/heinzel-(?!management...)`) still matches the shared `~/Code/heinzel-plugins` base checkout, so a test
  run there is eligible for SIGSTOP. Safe direction (it is continued below LOW), but it should become a bead.
- A same-uid reused pid gets one SIGCONT; that continues it if something else (the load guard, a shell's ^Z) had
  stopped it. Rare and harmless compared to leaving the governor's own stop in place.

Tests through `sm gate run -- uv run pytest`: test_native_guards_{floor,floor_cov_rules,governor,hooks,lim,
loadguard,smprocs,uv}.py, test_native_leakwatch{,_moved}.py, test_native_train_{commands,cov_b_state,digest_known}.py,
test_native_queue_cov_a_merge_train.py, moved/test_native_{queue_merge_train,train_main_gate,train_next_start,
train_pipelined,train_plan,train_self_test}.py: 487 passed.

COVERS: daemon governor
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

COVERS: daemon governor
REVIEWED: sha256=526c2a471bb46783a0b9b8b9df1cbccb51845adad7ad40e1fe1fc71fb293a621
VERDICT: APPROVE

## Campaign re-verdict g6 (2026-11-14, independent; reviewer wrote none of this code)

Target integration/sm-dod at c36bbe1, baseline 903a1b7. Digest recomputed with the worktree's sm (`uv run sm self reviews`); it equals the campaign list.
Shared core delta read (`git diff 903a1b7 c36bbe1`): native.py (every write run, loop ticks included, holds `writers.lock` shared in the project's state directory; `self migrate-state` takes it exclusively; switch-pending handling unchanged in effect), statemove.py (new: hard link plus symlink-over-rename per file, rename plus link per directory, the whole pass refused on any problem, `require_migrated` refuses unmigrated state), config.py (`child_environment` drop lists, `search_path`, `variable`), models.py (StateMigration), helpdoc.py (lint letters O and X), smprocs.py (moved from guards_lib to the core, logic byte-identical, one message reworded).
Tests through `sm gate run -- uv run pytest` (xdist is not installed in this repo, so without -n 2): test_native_popup_guard, test_native_guards_{governor,loadguard}, test_native_leakwatch{,_moved}, test_native_train_{commands,cov_b_state,digest_known}, test_pipeline_{admit_cut,cov_head_admit,cov_release_cut,head_admit_merge_tier}, tests/test_core_{statemove,native}: 268 passed, exit 0. No launchd job was loaded or unloaded.

Unit delta (guards.py, guards_lib/{governor,hooks,legacy,loadguard}.py, state.py): the output limiter left this module for the core (`sm lim`, its own unit); smprocs is imported from the core, unchanged; `guard uv` reads its environment through `child_environment()` (same content). The governor's former-state read moved from the setting legacy_state_dir (unset: fresh state) to `state.dashboard_dir` (unset: the state directory), and the file name from the old prefixed name to governor.json, the name `self migrate-state` gives it (state.NAMES). `state.root()` refuses with MigrationError while any plugin state is still at its old name, so a tick never reads a half-migrated directory (fail closed). The off file default stays guards/governor.off in the state directory. The NEVER pattern is unchanged: port 8737, `hz serve`, `hz daemon`, the heinzel launchd label, Claude.app and Chrome are never stopped; the governor still stops only builder trees of this user and only above the high load.
State paths after migrate-state: no governor plist is installed; the live loadguard, leakwatch, queue and train plists carry only ~/Library/Logs log paths and the sm launcher, no state path. The user config's guards_loadguard_legacy_dir and leakwatch_state_dir name old paths; bin/migrate_config.py rewrites both (inside a move's old path) to loadguard-legacy and leakwatch in the state directory, consistent with state.ELSEWHERE.
Note, not blocking: the loop now takes the writers' lock (shared) per tick, so `self migrate-state` waits for at most one tick.

COVERS: daemon governor
REVIEWED: sha256=06ac74440bd9c504e5d13eb117192740cd3ecbbe8ab06d6ea6d8f5648079a3a8
VERDICT: APPROVE

## Re-verdict at abc64a4, group B (2026-11-14)

Independent reviewer; wrote none of this code. Findings, baselines and tests: ~/Code/packets/sm-reverdict-abc64a4/B.md (recorded verbatim by the integrator).

COVERS: daemon governor
REVIEWED: sha256=ea8eb3a0e6fa9da69a2d0526cc1c6d658dec3cf7cd7ff94575015450799f9088
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does.
COVERS: daemon governor
REVIEWED: sha256=fd47f3a4a3c4370fa231d89214e14ddc989eaa2f9b2900f7b7e78c62c5abf72a
VERDICT: APPROVE

## Re-verdict at feat/sm-ship d05ef01 (2026-11-14)

Independent reviewer; wrote none of this code. Unit code unchanged. The digest moved only through guard_floor.py:
`sm ship` is now a one-word write step that the switch refusal blocks while a promote switches the live sm. The
existing two-word steps are unchanged. This command does nothing differently.
COVERS: daemon governor
REVIEWED: sha256=063a7f69232b372dcb4631a69cc9fb27c6643fed58cba1127fb40c9bb3451862
VERDICT: APPROVE
