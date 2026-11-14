# Port review: `sm mini leakwatch sample` (reads: `mini leakwatch report`, `mini leakwatch check`)

Slice heinzel-native-status. Written by the porting agent. The verdict line is the independent reviewer's and is NOT
there. Review unit (the digest of `sm self reviews`): `native/leakwatch.py` and every file in `native/leakwatch_lib/`.

## 1. What the write is

`sample` appends one JSON line to `samples.jsonl` in the directory of the setting `leakwatch_state_dir` (no default: the
command refuses until it is set; set to the directory the launchd sampler of the scripted era writes to, old and new
samplers extend the same file) and renames
the file to `samples.jsonl.1` once it reaches 20 MB. Nothing else is written. Toward the live hz it is read-only: `ps`,
`lsof`, `netstat`, `launchctl list LABEL` and one GET of `http://127.0.0.1:PORT/`; it never signals, restarts, updates
or opens the product or its data. Risk: low (a local append-only log that only `report` and `check` read).

## 2. What moved, verbatim

`python3 plugins/heinzel/tests/ci_verbatim.py` recomputes `leakwatch_lib/watch.py` from the old tool with the listed
rules and prints every differing line (none). The rules, all of them:

- the script header and docstring are replaced; the import block loses `subprocess` and `sys`;
- `EDGE`, `DEFAULTS` (the values of the old thresholds file, without the daemon label) and `configure` are added;
- `state_dir()` and `load_config()` read `EDGE` instead of two environment variables and the file beside the script;
- `run()` goes through the injected process runner;
- the old command-line `main` is not moved (its three branches are `native/leakwatch.py`).

Unchanged and to be read as moved code, with what the integration contract would forbid in new code: `urllib` for
the one local GET (`probe_web`), `time.monotonic` for the CPU interval, plain file appends for the samples.
The old tool's 15 test cases moved with it (`tests/test_native_leakwatch_moved.py`).

## 3. New code, to be read in full

`native/leakwatch.py` (about 150 lines): settings, the three commands, the result models.

Differs on purpose from the old tool:

- `check` no longer appends the live sample it takes when the sampler is stale: a read command writes nothing. The
  JSON document says `judged_on: live`.
- The daemon label is derived from the owner of the setting `repo` unless `leakwatch_daemon_label` is set.
- Thresholds are built in (`DEFAULTS`); `leakwatch_config_file` may override single values.

## 4. What the reviewer should check

- `sample` writes only below `leakwatch_state_dir`; rotation keeps one older file.
- No command of the three can signal or restart a process (the only process calls are the five read-only ones above).
- The launchd sampler still calls the old script; switching it to `sm mini leakwatch sample` is a promotion step,
  not part of this review.

## Independent review (main f9134cc)

Reviewer: independent; wrote none of this code. Unit recomputed by `sm self reviews` (3 files).

- Write: one JSON line appended to `<leakwatch_state_dir>/samples.jsonl` (watch.py:286-292); rotated once to
  `samples.jsonl.1` at 20 MB (watch.py:39,289-290): bounded at about 40 MB. Refuses without `leakwatch_state_dir`
  (leakwatch.py:100-101). Nothing else written; `check` no longer stores its live sample (leakwatch.py:144-146).
- Toward the live hz on 8737 read-only: `lsof`, `ps`, `launchctl list LABEL`, `netstat` (watch.py:232,262-278) and one
  local GET with a 5 s timeout (watch.py:240-250). No signal, kickstart, bootstrap, unlink or subprocess import.
- One shot (no loop; launchd schedules it); one model -> one JSON document; no hz-* call.
- Verbatim move confirmed (`ci_verbatim.py`: "leakwatch: moved verbatim"). Tests test_native_leakwatch.py and
  test_native_leakwatch_moved.py pass.
- Note: `sm daemon leakwatch` is a second port of the same sampler writing to the store, which these readers never
  read; keep this one as the sampler (see reviews/daemon-leakwatch.md).

COVERS: mini leakwatch sample
REVIEWED: sha256=77403edc5b1a35588c688c3d4f814e8e31be023c9a4d9d28988b8bec078e3c5b

## Independent re-review (integration/sm-complete 479a8e8, campaign g6)

Reviewer: independent; wrote none of this code. Unit recomputed with `sm self reviews` (10 files; the digest now also
covers the imported core files). Against live main 1d69c43 the unit's own files differ in one line only: the "Next"
hint of `mini leakwatch report` says `sm mini verify` instead of `sm mini status` (help text of a read command; no
behaviour). `leakwatch_lib/watch.py` is unchanged. The imported core changes (`native.py`, `engine.py`, `config.py`,
`models.py`, `helpdoc.py`, `protocols.py`) are the digest widening, typing, and the tick/promote-switch handling;
none changes what `sample` does.

Re-checked for a scheduled writer: one shot per call (no `--loop`; launchd schedules it), bounded (5 s GET, 20 MB
rotation, one older file kept); writes only below `leakwatch_state_dir` (refused when unset); toward the live hz on
8737 strictly read-only (`lsof`, `ps`, `netstat`, `launchctl list LABEL`, one local GET); no signal, kickstart,
unlink or install. As a write command it waits (exit 75) while ~/.cache/sm-promote-switch-pending names a live pid.

Not blocking: no lock around the append and rotation; while the old `hz-leakwatch` job and this one both write the
same file, a rotation race can lose a sample (append-only log, readers tolerate gaps). Retire the old job at switch-over.

Tests: `sm gate run -- uv run pytest` test_native_leakwatch.py, test_native_leakwatch_moved.py: all pass.

COVERS: mini leakwatch sample
REVIEWED: sha256=56ecc31f3393bfdf2607412863c9cae8a1b6c4f00949d9b32bcd0a6f832c0c14
VERDICT: APPROVE

## Re-verdict (integration/sm-complete 2d0877b, frozen; campaign re-verdict g6)

Reviewer: independent; wrote none of this code. Digest recomputed with the worktree's sm (`self reviews`, 10 files):
0274d40d28aeec92bfaf7e58525b2c8f829d26d8c85d9d54aadbd93dfff467b3, as in the campaign list. Delta 479a8e8..2d0877b: `leakwatch.py` and `leakwatch_lib/*` are unchanged; only
the shared core files moved.
- Core files in the unit, 479a8e8..2d0877b: `engine.py` `_supersede` now takes the older run's own lock
before marking it superseded (Busy: exit 75, nothing half-written; the new state is only in memory then);
`native.py` `switch_pending` ignores a pending file whose pid started after the file's UTC stamp (`ps -o etime=`
through the process runner; unreadable start: the file holds, fail closed; bin/promote writes "PID UTC-TIME"),
`EXEMPT_PATHS` limits `exempt=` to `gate run`, and `NativeRuntime.commands` is a lazy lookup. All safe.

Not blocking: as before (no lock around append and rotation while the old job also writes).

Tests through `sm gate run -- uv run pytest`: test_native_guards_{floor,floor_cov_rules,governor,hooks,lim,
loadguard,smprocs,uv}.py, test_native_leakwatch{,_moved}.py, test_native_train_{commands,cov_b_state,digest_known}.py,
test_native_queue_cov_a_merge_train.py, moved/test_native_{queue_merge_train,train_main_gate,train_next_start,
train_pipelined,train_plan,train_self_test}.py: 487 passed.

COVERS: mini leakwatch sample
REVIEWED: sha256=0274d40d28aeec92bfaf7e58525b2c8f829d26d8c85d9d54aadbd93dfff467b3
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

COVERS: mini leakwatch sample
REVIEWED: sha256=3eff919865a668ead0ee474af7c1a08e4d1985431baeeae4fab03d5dfff6c6c8
VERDICT: APPROVE

## Campaign re-verdict g6 (2026-11-14, independent; reviewer wrote none of this code)

Target integration/sm-dod at c36bbe1, baseline 903a1b7. Digest recomputed with the worktree's sm (`uv run sm self reviews`, 10 files); it equals the campaign list. No change in leakwatch.py or leakwatch_lib/; the delta is the shared core only: native.py (a write run holds `writers.lock` shared for one sample; `self migrate-state` takes it exclusively), config.py, models.py, helpdoc.py. Tests through `sm gate run`: test_native_leakwatch.py and test_native_leakwatch_moved.py within the g6 run (268 passed, exit 0). No launchd job was loaded or unloaded.

The sampler stays read-only toward hz (ps, lsof, netstat, launchctl list, one local GET) and appends to samples.jsonl in leakwatch_state_dir, which still refuses when unset. After the switch bin/migrate_config.py rewrites the configured old path to leakwatch in the state directory, where `self migrate-state` moves the old directory (rename plus a link at the old name, so a still-running older sampler keeps appending to the same files). The live plist carries only its ~/Library/Logs log path and the sm launcher.

COVERS: mini leakwatch sample
REVIEWED: sha256=cec205f5f83ece79c3ecb48ec1c76b8152cc2b9d479ab0e1f878f39e2d648047
VERDICT: APPROVE

## Re-verdict at abc64a4, group C (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-abc64a4/C.md (recorded verbatim by the integrator).

leakwatch.py, leakwatch_lib/watch.py unchanged; only native.py/models.py. leakwatch, leakwatch_moved tests.
COVERS: mini leakwatch sample
REVIEWED: sha256=08f614d747ac9c39197b2cf9dbe45a2c2364149ca3a281e8e21777313f20931a
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does.
COVERS: mini leakwatch sample
REVIEWED: sha256=f9e2ae665f1639199a15a41c2826c515805a486adc6043761f9f11bca8ef8900
VERDICT: APPROVE
