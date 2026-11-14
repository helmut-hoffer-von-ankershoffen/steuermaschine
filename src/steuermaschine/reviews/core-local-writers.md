# Review: the core's local writers

COVERS: review verdict
COVERS: focus add
COVERS: focus done
COVERS: alarm ack

Unit: `src/steuermaschine/app.py` and every core file it imports (`sm self reviews` lists them and the digest).

What these commands write, all on this machine only:
- `review verdict`: one verdict receipt per digest (write-once); refuses the author of the run as reviewer.
- `focus add|done`: the focus list in the state store.
- `alarm ack`: the acknowledged alarm keys in the state store.

What the reviewer checks: nothing else is written; `review verdict` cannot overwrite a verdict and refuses a
reviewer who drives the run; each command refuses (exit 70) while this file does not approve the digest.

State: reviewed, see the campaign re-review below.

## Campaign re-review g1 (2026-11-13, independent; reviewer wrote none of this code)

Target: integration/sm-complete 479a8e8 against live main 1d69c43. Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/campaign-g1); it equals the unit list.

Unit (new): core `app.py` writers and every core file it imports. Read in full: `review verdict`, `focus add|done`, `alarm ack`, `gate()`, `core_units`, `write_units`, `writes_of`; plus the pipeline gate in `_pipeline_command` and `ci cancel`'s gate call.
- Each writer calls `gate(rt, path)` before its first write; unapproved -> exit 70, nothing written (the USAGE checks before it write nothing either). `review verdict` uses `put_receipt` (write-once; FileExistsError -> refusal naming the stored verdict), refuses a reviewer who authored any run with that digest, and refuses a digest no run waits on. `focus` and `alarm ack` write only their store keys; `alarm ack` refuses a key that does not hold now. json: one document, typed (Verdict, FocusList, AlarmAcks).
- NOTE: `focus add|done` and `alarm ack` are read-modify-write of one store key without a store lock; two concurrent calls can lose one entry. Local and rare; wrap in `store.lock(...)`.
- NOTE: `self config` now prints the core [github]/[sonar] values; only the token variable name (`token_env`) appears, never a token (`config.secret` is not printed).


Core part of the unit (shared by every unit; read as diffs 1d69c43..479a8e8): `native.py` (digest now follows first-party imports via ast, files labelled by package path; `gate_problem`/`call` share one gate for CLI, pipelines and core writers; `exempt`; a write command exits 75 and runs nothing while a promote switch is pending, a loop skips ticks then), `engine.py` (running/pid in state, `_supersede`, review stop names the file, a spent `--approved` digest is not reused), `config.py` (one typed `Environment`, the only os.environ reader; same semantics as before for every variable, incl. the PYTEST_CURRENT_TEST push refusal in `defaults.py`), `helpdoc.py` (strict lint, declared writes vs Changes line), `models.py` (typed result models, `Raw` gone), `protocols.py` (types only). No force push, stash, auto-merge or old-tool call was added; `--format json` stays one document (emit now always validates against a model; `None` prints `{}` as `Nothing`).
- NOTE (core, not blocking here): the digest does not include a parent package `__init__.py` that Python runs for `from pkg.sub import X` (tests/test_core_gate_units.py:65 asserts this); code placed there changes behaviour without voiding a verdict. Suggest including the `__init__.py` of every package on the import path.
- NOTE (core): `Engine._supersede` loads and saves other subjects' state without their store lock; a run of the older subject that is still advancing can overwrite the mark or be overwritten. Take `store.lock` per old key (skip on Busy).
- NOTE (core): `switch_pending()` trusts `os.kill(pid, 0)`; a stale pending file whose pid was reused blocks every write command (exit 75) until the file is removed. Checking the file age or the process command would close it.
Tests: `sm gate run -- uv run pytest` on 25 files (core native, gate units, cli, engine, runlock, settings, json contract; plugin coord tasks/briefs/asks/handoff/skills/velocity/journal/docs/merges, guards lim/governor/loadguard/floor, lock, lock refuse matrix, plugin): 476 passed, 1 xfailed.

COVERS: review verdict
COVERS: focus add
COVERS: focus done
COVERS: alarm ack
REVIEWED: sha256=b582fb00f5eaf08c90382951f8e2041b10a6c9b4f888073a7cff96aa34ec1269
VERDICT: APPROVE

## Campaign re-verdict g1 (2026-11-13, independent; reviewer wrote none of this code)

Target: integration/sm-complete 2d0877b (frozen), delta read against the g1 review at 479a8e8 (`git diff 479a8e8 2d0877b -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/reverdict-g1); it equals the unit list.

Unit (core only). The earlier NOTE is closed: `focus add`, `focus done` and `alarm ack` now do their read-modify-write under `store.lock` (`focus.list`, `alarms.acks`) and exit 75 when another process holds it; `focus done`'s USAGE refusal inside the lock writes nothing. `review verdict` unchanged (write-once receipt). Test: test_core_cli `test_focus_and_alarm_ack_write_under_the_store_lock_and_wait_while_another_holds_it`.

Core part of the unit (shared; diff 479a8e8..2d0877b): `native.py`: `exempt=` now counts only for paths on the core allowlist `EXEMPT_PATHS = {"gate run"}` (`is_exempt`, used by `gate_problem` and `self reviews`; test_core_gate_units: another exempt writer stays gated); `switch_pending(commands, now)` reads "PID UTC-TIME" (bin/promote_lib.sh writes it) and ignores a pid whose `ps -o etime=` start is later than the file time + 2 s; old one-word files and an unreadable start fail closed (pid holds); `NativeRuntime.commands` gives the plugin registry from the runtime. `engine.py`: `_supersede` now loads, checks and saves each older run under that run's own `store.lock` (Busy -> the advance exits 75). `app.py`: `focus add|done` and `alarm ack` read-modify-write under `store.lock` (`locked`, Busy -> exit 75); `self reviews` uses `is_exempt`. `github/cli.py`: `ci cancel` refuses a non-digit run id before the gate. No force push, stash, auto-merge or old-tool call added; json stays one document.
- NOTE (core): a Busy on the second or later older run leaves the earlier ones already marked superseded; the retry finishes the job, nothing is lost.
- NOTE (core, carried): a parent package `__init__.py` on the import path is still outside the digest.
Tests: `sm gate run -- uv run pytest` on 25 files (core native, gate units, cli, engine, github cli, runlock, settings, json contract, promote review gate; plugin coord tasks/briefs/asks/handoff/skills/velocity/journal/docs/merges, guards lim/governor/loadguard/floor, lock, lock refuse matrix, plugin): 446 collected, 445 passed, 1 xfailed.

COVERS: review verdict
COVERS: focus add
COVERS: focus done
COVERS: alarm ack
REVIEWED: sha256=411737dc2c2073584263b242318f9b691f2f6cf6fb1bb83e133677514ff4185f
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

COVERS: review verdict
COVERS: focus add
COVERS: focus done
COVERS: alarm ack
REVIEWED: sha256=b4467497eed820898958d1167e8a283c30d6f252d8193a208864bd7588ee770b
VERDICT: APPROVE

## Campaign re-verdict g1 (2026-11-14, independent; reviewer wrote none of this code)

Target: integration/sm-dod c36bbe1 (frozen), delta read against the live baseline 903a1b7, where this unit was approved (`git diff 903a1b7 c36bbe1 -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/campaign-g1-sm-dod); it equals the unit list of the sm-dod campaign.

Unit change: `app.py` adds `sm self migrate-state` (now in `CORE_WRITERS`, so under this file's verdict), `natives()` (core commands `commands/{doc,gate,lim,window}` as the innermost layer, a plugin command with the same path replaces one), `write_units`/`writes_of`/`command_tree` built on it, pipelines counted as ported in `tools gaps`. `commands/doc.py` and `commands/window.py` are read-only. New `statemove.py`. `review verdict`, `focus add|done`, `alarm ack` are unchanged.

`sm self migrate-state`, read in full:
- Lock: `--check`/`--list` change nothing and take no lock. A move first passes the review gate, then takes `writers.lock` exclusively, non-blocking, polling once a second up to `--wait` (default 600 s), then refuses exit 70 with nothing moved; every gated native writer holds it shared per run or tick, so none runs while files move.
- Split state: `problems()` checks every move before any rename; old and new both holding state, another device, or an old link to elsewhere refuses the whole pass (exit 70, nothing moved). At run time `require_migrated` refuses unmigrated and split state (unless both names are the same inode).
- Move: a file gets a hard link at the new name, then the old name becomes a symlink in one `os.replace` (never missing, a held flock still excludes); a directory is renamed and linked, entries an older process made meanwhile are taken over, and a clash refuses. Every rename is logged to `state-migrations.log`.
- Idempotence: a second pass finds every old name a link to the new one and moves nothing (tested: forward, back, forward again keeps the state identical).
- Rollback: `--back` reverses file and directory moves, also paths this release made first; `--keep` (the target release's `--list` JSON) leaves the moves the target also knows; `bin/promote_lib.sh` runs live `--back --keep` then target forward, both after `wait_idle` with the switch-pending file written, and any refusal stops the switch.
- NOTE: a pass stopped between `os.link` and the symlink replace leaves old and new as one inode; the next pass then refuses with "both hold state" instead of finishing. Fail-closed and readers still work (`_same_file`), but treating a same-inode pair as half-done in `problems()`/`forward()` would make it resumable.
- NOTE: pipelines (`_pipeline_command`, `native.call`) and the core's own writers do not take `writers.lock`; the docstring of `native.writers_lock` says "every run of a write command". Promote and rollback cover pipelines with `wait_idle` and the switch-pending hook, so this is not a defect here; wrapping `engine().advance` in the shared lock would make the rule hold on its own.
- NOTE: `--keep` help says "a JSON list"; the code reads an object with `moves` (what `--list --format json` prints). `_move_state` can return `new`, which the `StateMoveRow.state` description does not list.
- NOTE: no test proves that a native write command takes the shared `writers.lock` (only the migrate side is tested, with a hand-held lock); a test around `native._command` would pin it.

Core part of the unit (read as diffs 903a1b7..c36bbe1): `config.py` (`child_environment` gains `drop`/`drop_prefixes`, `search_path`, `variable` for a plugin setting's old environment name; defaults keep the old behaviour), `plugins.py` (`old_variable` metadata: the old variable sets a field only when no file or SM_ variable does, a bool only on its exact `on` value; `state_moves` hook), `hookspecs.py` (`sm_state_moves`), `helpdoc.py` (lint letters O and X; `sm self lint` passes), `models.py` (`StateMigration`), `native.py` (every gated write run, and every tick of a loop, holds the shared `writers.lock` in the state directory; readers, exempt writers and runs without a project do not; the switch-pending behaviour is unchanged: a single run exits 75, a loop skips the tick). No force push, stash, auto-merge, --admin or old hz-* call was added; `--format json` stays one document.
Tests (one gated command each, by file; this repo has no xdist, so without `-n 2`): `sm gate run -- uv run pytest tests/test_core_statemove.py tests/test_core_migrate.py tests/test_core_gate_units.py tests/test_core_commands.py tests/test_core_native.py tests/test_gate_structure.py`: all passed; `sm gate run -- uv run pytest` on plugin coord asks/briefs/docs/handoff/journal/skills/tasks/velocity/velocity_moved, guards_lim, lock, lock_refuse_matrix: exit 0 (all passed, 1 xfailed).

COVERS: review verdict
COVERS: focus add
COVERS: focus done
COVERS: alarm ack
COVERS: self migrate-state
REVIEWED: sha256=24e6e51e40f114bac61e3a86f08a7db0ff2163086c271e736b8ef74497b895f7
VERDICT: APPROVE

## Re-verdict at abc64a4, group A (2026-11-14)

Independent reviewer; wrote none of this code. Baselines, shared core and tests: ~/Code/packets/sm-reverdict-abc64a4/A.md (recorded verbatim by the integrator).

`review verdict`, `focus add|done`, `alarm ack`, `self migrate-state` unchanged since c36bbe1; app.py changes are the
pipeline lock (above), read-only `tools gaps`/`self reviews`, and the `sm_raw` removal. Nothing writes more than before.

COVERS: review verdict
COVERS: focus add
COVERS: focus done
COVERS: alarm ack
COVERS: self migrate-state
REVIEWED: sha256=69452b4c1fcec6a7c268fc2bcf67ac0e1e9a63b0b5d81c3bb824614ca8ef2405
VERDICT: APPROVE

## Delta review: feat/sm-gate-lanes d94de45 (independent reviewer)

Changed in this unit since the last verdict: src/steuermaschine/runlock.py, src/steuermaschine/commands/gate.py (light lane, rerun refusal); plugin native/__init__.py (one import). Reviewed in full in two rounds (e6db9d9, d94de45),
probes now tests; see ~/Code/packets/sm-gate-lanes/REVIEW-2.md. Nothing in this command got weaker.

REVIEWED: sha256=9f07ff9da3ed4c9d3d6976695dd282cf3310b34406bae9e8d2d17e6b70a837f2
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

app.py: `run_logged` wraps both `execute` and `main` once (no double record); `fail()` sets the reason code; `self stats` is read-only and refuses a bad --since (usage-stats review, usage-log.md). commands/gate.py and runlock.py: light lane, history key, rerun refusal (gate-lanes REVIEW-2). Store.delete in protocols/defaults (ReadOnlyStore refuses it). None of it changes review verdict, focus add/done, alarm ack or self migrate-state; the invocation log is one append in the state directory and never raises. Composition checked: statsreport reads the plugin's gate-history.jsonl (state dir == project home), whose rows now carry lane/tree/caller/failure; the report only reads at/exit/waited_s/ran_s, so the new fields do not break it (light-lane rows now count as gate runs). Advisory U1: for a project without a policy factory the core history is home/gate/history.jsonl, but `self stats` reads home/gate-history.jsonl, so its gate line is always zero there.
COVERS: review verdict
COVERS: focus add
COVERS: focus done
COVERS: alarm ack
COVERS: self migrate-state
REVIEWED: sha256=e9e2495582a58c25b353e9d077114c9fa206b0a9e6bfaef0b87e73e7a7e6223a
VERDICT: APPROVE

## Change: `review verdict` accepts a digest a command registered (bead heinzel-management-04ro)

New module `steuermaschine/verdicts.py`: `register` (a prepare records its digest and author in kind `review.digests`),
`authors_of`, `require_independent` (used by the applies). `review verdict` now takes the authors from the runs and from
registered digests, so a verdict can be recorded when no run is waiting; the reviewer still must not be an author.
Tests: `tests/test_core_verdicts.py`. The previous verdict line and digest are void; re-review.

## Re-verdict at f346492 (bead heinzel-management-04ro + integration/sm-batch3, 2026-11-14)

Independent reviewer; wrote neither 04ro nor window admission. Review: ~/Code/packets/sm-04ro/REVIEW.md.
`review verdict` and verdicts.py as approved at f51cb78 (advisories R1, R4, R5 stand); window.py holds do not reach
focus, alarm or migrate-state (holds are a key in the same window kind).
COVERS: review verdict
COVERS: focus add
COVERS: focus done
COVERS: alarm ack
COVERS: self migrate-state
REVIEWED: sha256=30c1e499e6a038726498aea8c3a0890c4d01fffb1971823764392f3481337e4a
VERDICT: APPROVE
