# Port review: `sm beads agents add`

For the independent reviewer. The command is off until this file names the code (`REVIEWED: sha256=<digest>` from
`sm self reviews`) and ends with the verdict line; the author of the port writes neither.
Review unit: `native/agents.py`, `native/agents_lib/`, `native/coord_lib/`.

## Exactly what it changes
One file: the routing table (setting `agents_table`, default `research/agent-ids.md` in the tracker's repository).
One row `| BEAD | PR #N ROLE | AGENT_ID | MODEL |` is added at the end; a missing final newline is added first. Under an
exclusive lock (setting `agents_lock_file`, default `<table>.lock`, created if missing) from the read to the write, the
file is read as UTF-8, checked, and replaced by the core's `journal.replace_text` (temporary `.NAME.PID.tmp` next to it,
fsync, mode copied, `os.replace`): never truncated in place. An agent id that is already a whole cell of a row:
nothing is written. A file that is not UTF-8, has no table row, or has a row not closed by `|`, and an agent id that is
empty or holds whitespace or `|`: refused (exit 1), nothing written. No service. Not committed, not pushed.

## Differences from the old tool
| # | Difference | Why | Test |
|---|---|---|---|
| 1 | `add` is the old function with the file as a required keyword. | No path in code. | `test_agent_add` (the old test) |
| 2 | A missing table file is a usage refusal (exit 2); the old tool raised a traceback. | Exit-code table. | `test_agents_commands_add_once_list_and_need_the_table` |
| 3 | Lock from read to write and atomic replace (review 2, B4: 8 processes x 40 adds left 6 of 532 rows). | Agents are started several at once. | `test_parallel_adds_from_real_processes_keep_every_row` (8 real processes x 40 adds on a 212-row table: all 532 rows) |
| 4 | An unusable table or agent id is refused, the file untouched. | Review 2, B4. | `test_add_refuses_an_unusable_table_and_leaves_it_untouched`, `test_agents_add_command_refuses_an_unusable_table_with_exit_1` |
| 5 | Duplicate check is a whole-cell match (was: substring anywhere); `|` in a cell becomes `/`, whitespace and newlines are squeezed. | Review 2, should-fix of B4. | `test_add_matches_whole_cells_and_cleans_the_cells_it_writes` |
| 6 | New setting `agents_lock_file`. NOTE: the default lock file lies next to the table and is not ignored by git in the tracker's repository. | One file all writers agree on. | - |

## What the reviewer should check
1. `python3 plugins/heinzel/tests/coord_verbatim.py` (add: one meant difference, now the whole body).
2. One run on a COPY of the real table.

## Independent review 3 (2026-11-13, reviewer wrote none of the code)
Reviewed an export of `6230986` (fix of B4 on `6e4042e`). Full findings: `REVIEW.md` in ~/Code/tmp-sm-reviews/coord2/.
- B4 fixed. `agents_lib/table.py:95-104`: `flock(LOCK_EX)` on a separate lock file is taken before the read and held
  through the duplicate check and `journal.replace_text`; locking a separate file (not the table, whose inode the
  rename replaces) is correct. The temporary file is `doc.with_name(".NAME.PID.tmp")`, the same directory, so
  `os.replace` is atomic. Row encoded and agent id validated before the lock; unusable file refused untouched.
- Shown on a COPY of the real `research/agent-ids.md` (213 table lines) with the default lock path: 8 processes x 40
  adds give 533 lines (all 320 new ids), the original content is an unchanged prefix, a concurrent reader made 85762
  reads and never saw fewer than 213 rows, no temporary file left. The real file's sha256 is unchanged.
- No real-path default: the table comes from `agents_table`, else `coord_tracker_dir`, else `coord_document`'s
  directory; with none configured the command refuses (usage).
- NOTE (default lock path): `research/agent-ids.md.lock` is created in the management repository and is not ignored
  there (`git check-ignore` finds no rule). `sm doc handoff` stages only the snapshot and `handoff_files` by explicit
  path, so it commits the lock only if `handoff_files` names `research` or the lock itself; an agent's
  `git add -A` / `git add research` would commit it (an empty file: noise, not harm). Add `*.lock` to that repository's
  `.gitignore` or point `agents_lock_file` outside the repository. `git clean` removing it during a running add would
  split the lock.
- NOTE: the old `hz-agent` takes no lock; while it still runs, its truncating write can still lose rows. A
  `FileNotFoundError`/`PermissionError` between the existence check and the read is a traceback, not a refusal (nothing
  written). The row goes at the end of the file, not the end of the table (as before).

## Re-issued verdict (2026-11-13, branch chore/sm-clean, reviewer wrote none of the code)

Help text only since the previous verdict (sha256=cd70ce1bb63779ce7617020b1045ad4b188361f0ac0342948bd1f292aab05dd2, approved on main at f9134cc).
The whole unit diff from the merge base f9134cc to 6acd0b4 is native/agents.py: `help=` added to the BEAD, AGENT_ID and MODEL arguments of `beads_agents_add`.
No default, type, metavar, option name or body line changed; the digest at f9134cc recomputes to the old
REVIEWED value, so nothing else in the unit moved.

COVERS: beads agents add
REVIEWED: sha256=8aba0e9ec2724e018edc43adc2c7dae5e05510680bcb63e7327fd4008124a7a8
VERDICT: APPROVE

## Campaign re-review g1 (2026-11-13, independent; reviewer wrote none of this code)

Target: integration/sm-complete 479a8e8 against live main 1d69c43. Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/campaign-g1); it equals the unit list.

Unit change outside core: `agents.py` adds `help=` to BEAD, AGENT_ID, MODEL; no default, type or body line changed. Write path (one row in the agents table, refusal on an unreadable table) unchanged.

Core part of the unit (shared by every unit; read as diffs 1d69c43..479a8e8): `native.py` (digest now follows first-party imports via ast, files labelled by package path; `gate_problem`/`call` share one gate for CLI, pipelines and core writers; `exempt`; a write command exits 75 and runs nothing while a promote switch is pending, a loop skips ticks then), `engine.py` (running/pid in state, `_supersede`, review stop names the file, a spent `--approved` digest is not reused), `config.py` (one typed `Environment`, the only os.environ reader; same semantics as before for every variable, incl. the PYTEST_CURRENT_TEST push refusal in `defaults.py`), `helpdoc.py` (strict lint, declared writes vs Changes line), `models.py` (typed result models, `Raw` gone), `protocols.py` (types only). No force push, stash, auto-merge or old-tool call was added; `--format json` stays one document (emit now always validates against a model; `None` prints `{}` as `Nothing`).
- NOTE (core, not blocking here): the digest does not include a parent package `__init__.py` that Python runs for `from pkg.sub import X` (tests/test_core_gate_units.py:65 asserts this); code placed there changes behaviour without voiding a verdict. Suggest including the `__init__.py` of every package on the import path.
- NOTE (core): `Engine._supersede` loads and saves other subjects' state without their store lock; a run of the older subject that is still advancing can overwrite the mark or be overwritten. Take `store.lock` per old key (skip on Busy).
- NOTE (core): `switch_pending()` trusts `os.kill(pid, 0)`; a stale pending file whose pid was reused blocks every write command (exit 75) until the file is removed. Checking the file age or the process command would close it.
Tests: `sm gate run -- uv run pytest` on 25 files (core native, gate units, cli, engine, runlock, settings, json contract; plugin coord tasks/briefs/asks/handoff/skills/velocity/journal/docs/merges, guards lim/governor/loadguard/floor, lock, lock refuse matrix, plugin): 476 passed, 1 xfailed.

COVERS: beads agents add
REVIEWED: sha256=e366590198e74d37ee41a35179ec4e6220f5aaf702d09234a0c661b4498f6b93
VERDICT: APPROVE

## Campaign re-verdict g1 (2026-11-13, independent; reviewer wrote none of this code)

Target: integration/sm-complete 2d0877b (frozen), delta read against the g1 review at 479a8e8 (`git diff 479a8e8 2d0877b -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/reverdict-g1); it equals the unit list.

Unit change outside core: none (`agents.py`, `agents_lib`, `coord_lib` unchanged). Digest moved only through core.

Core part of the unit (shared; diff 479a8e8..2d0877b): `native.py`: `exempt=` now counts only for paths on the core allowlist `EXEMPT_PATHS = {"gate run"}` (`is_exempt`, used by `gate_problem` and `self reviews`; test_core_gate_units: another exempt writer stays gated); `switch_pending(commands, now)` reads "PID UTC-TIME" (bin/promote_lib.sh writes it) and ignores a pid whose `ps -o etime=` start is later than the file time + 2 s; old one-word files and an unreadable start fail closed (pid holds); `NativeRuntime.commands` gives the plugin registry from the runtime. `engine.py`: `_supersede` now loads, checks and saves each older run under that run's own `store.lock` (Busy -> the advance exits 75). `app.py`: `focus add|done` and `alarm ack` read-modify-write under `store.lock` (`locked`, Busy -> exit 75); `self reviews` uses `is_exempt`. `github/cli.py`: `ci cancel` refuses a non-digit run id before the gate. No force push, stash, auto-merge or old-tool call added; json stays one document.
- NOTE (core): a Busy on the second or later older run leaves the earlier ones already marked superseded; the retry finishes the job, nothing is lost.
- NOTE (core, carried): a parent package `__init__.py` on the import path is still outside the digest.
Tests: `sm gate run -- uv run pytest` on 25 files (core native, gate units, cli, engine, github cli, runlock, settings, json contract, promote review gate; plugin coord tasks/briefs/asks/handoff/skills/velocity/journal/docs/merges, guards lim/governor/loadguard/floor, lock, lock refuse matrix, plugin): 446 collected, 445 passed, 1 xfailed.

COVERS: beads agents add
REVIEWED: sha256=66b9354f9ae68818480dfc440e4a58f9ee8083081a76ada1254586af90039bec
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

COVERS: beads agents add
REVIEWED: sha256=5d63865111050c3c2fc3eed5d75dbebf10c3f79e875fe9e194c6ba1adf339216
VERDICT: APPROVE

## Campaign re-verdict g1 (2026-11-14, independent; reviewer wrote none of this code)

Target: integration/sm-dod c36bbe1 (frozen), delta read against the live baseline 903a1b7, where this unit was approved (`git diff 903a1b7 c36bbe1 -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/campaign-g1-sm-dod); it equals the unit list of the sm-dod campaign.

Unit change outside core: `coord_lib/base.py` `child_env` now builds on `config.child_environment` and `search_path`: the same environment and PATH as before.

Core part of the unit (read as diffs 903a1b7..c36bbe1): `config.py` (`child_environment` gains `drop`/`drop_prefixes`, `search_path`, `variable` for a plugin setting's old environment name; defaults keep the old behaviour), `plugins.py` (`old_variable` metadata: the old variable sets a field only when no file or SM_ variable does, a bool only on its exact `on` value; `state_moves` hook), `hookspecs.py` (`sm_state_moves`), `helpdoc.py` (lint letters O and X; `sm self lint` passes), `models.py` (`StateMigration`), `native.py` (every gated write run, and every tick of a loop, holds the shared `writers.lock` in the state directory; readers, exempt writers and runs without a project do not; the switch-pending behaviour is unchanged: a single run exits 75, a loop skips the tick). No force push, stash, auto-merge, --admin or old hz-* call was added; `--format json` stays one document.
- NOTE: no test proves that a native write command takes the shared `writers.lock` (only the migrate side is tested, with a hand-held lock); a test around `native._command` would pin it.
Tests (one gated command each, by file; this repo has no xdist, so without `-n 2`): `sm gate run -- uv run pytest tests/test_core_statemove.py tests/test_core_migrate.py tests/test_core_gate_units.py tests/test_core_commands.py tests/test_core_native.py tests/test_gate_structure.py`: all passed; `sm gate run -- uv run pytest` on plugin coord asks/briefs/docs/handoff/journal/skills/tasks/velocity/velocity_moved, guards_lim, lock, lock_refuse_matrix: exit 0 (all passed, 1 xfailed).

COVERS: beads agents add
REVIEWED: sha256=64449d87d55a71a92c3ea9eb9c9a36ccd44b86283e52c67fe5a096ad03af3976
VERDICT: APPROVE

## Re-verdict at abc64a4, group C (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-abc64a4/C.md (recorded verbatim by the integrator).

agents.py, agents_lib/* unchanged.
COVERS: beads agents add
REVIEWED: sha256=b0df49e12d856ddeaa211c3a3b2546b7b3c6cffe3cdc8162ab9f15381c62e3ea
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does.
COVERS: beads agents add
REVIEWED: sha256=cd9acb0e6b772683e56241006e3b3e4471b82cb4afa138ba63c08a12e78c3dad
VERDICT: APPROVE
