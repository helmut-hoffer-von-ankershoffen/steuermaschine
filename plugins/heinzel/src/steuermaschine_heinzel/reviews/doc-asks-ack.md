# Port review: `sm doc asks-ack`

For the independent reviewer. The command is off until this file names the code (`REVIEWED: sha256=<digest>` from
`sm self reviews`) and ends with the verdict line; the author of the port writes neither.
Review unit: `native/asks.py`, `native/asks_lib/`, `native/coord_lib/`. Core code it runs: `src/steuermaschine/journal.py`.

## Exactly what it changes
One state entry of this tool: store kind `heinzel.coord.asks`, key `watermark`, value `{"ts": THROUGH}` (a file under
the project's state home; `sm self config` shows `home`). No other file, no service. Never moved backwards.

## Behaviour
The effective watermark is the later of this entry and the former tool's file `hz-ask-watermark.json` in
`legacy_state_dir` (read-only, `asks_lib/legacy.py`). `THROUGH` must parse as ISO 8601; otherwise exit 2.

## Differences from the old tool (`--ack`)
| # | Difference | Why | Test |
|---|---|---|---|
| 1 | The timestamp is an argument, printed by `sm doc asks`. The old audit wrote a `pending` mark and `--ack` moved to it. | The listing stays read-only; a message that arrives after the listing still cannot be acknowledged unseen. | `test_message_arriving_after_audit_is_not_acked` |
| 2 | The former watermark file is never written. | Old state is adopted read-only. Consequence: an acknowledgement made here is not seen by the former audit while it still runs (the former tick and hourly tools call it). | `test_the_former_watermark_is_adopted_read_only_and_never_moves_backwards` |

## What the reviewer should check
1. `ack` cannot move the mark backwards and cannot write the former file.
2. Whether consequence 2 is acceptable until the former tick and hourly tools are gone (the coordinator decides).

## Independent review (2026-11-12, reviewer wrote none of the code)
Reviewed at head `09c9ce1` (unit unchanged at `1210f33`). Full findings: `REVIEW-INDEPENDENT-coord.md` in the slice's worktree.
- Writes one store entry (kind `heinzel.coord.asks`, key `watermark`), atomically, only when THROUGH is later than the
  effective mark; never the former watermark file (read in `asks_lib/legacy.py` only). Nothing else.
- Exit 70 through the installed command tree without this verdict (run).
- This verdict is for `doc asks-ack` only. `doc ask` shares the unit and is NOT approved (blockers B1, B2); a fix
  inside `asks.py` or `asks_lib/` voids this verdict too.
- Should fix, not blocking: S8 (any timestamp is accepted, also a future one, compared as text, and the mark cannot be
  moved back: a wrong THROUGH hides owner messages nobody saw; refuse a THROUGH that is not a message's timestamp),
  S1 (gate test). Consequence 2 above (the former audit does not see an acknowledgement made here) is the
  coordinator's call; it errs on the side of showing a message twice.

COVERS: doc asks-ack
REVIEWED: sha256=58cc86a8fb0e2b0a06eec81abf43aa30cd801c0f0063a7c5682b5b91e403c462
VERDICT: APPROVE

## Campaign re-review g1 (2026-11-13, independent; reviewer wrote none of this code)

Target: integration/sm-complete 479a8e8 against live main 1d69c43. Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/campaign-g1); it equals the unit list.

Unit change outside core: none in `asks.py`/`asks_lib`/`coord_lib`; only the shared core files moved.

Core part of the unit (shared by every unit; read as diffs 1d69c43..479a8e8): `native.py` (digest now follows first-party imports via ast, files labelled by package path; `gate_problem`/`call` share one gate for CLI, pipelines and core writers; `exempt`; a write command exits 75 and runs nothing while a promote switch is pending, a loop skips ticks then), `engine.py` (running/pid in state, `_supersede`, review stop names the file, a spent `--approved` digest is not reused), `config.py` (one typed `Environment`, the only os.environ reader; same semantics as before for every variable, incl. the PYTEST_CURRENT_TEST push refusal in `defaults.py`), `helpdoc.py` (strict lint, declared writes vs Changes line), `models.py` (typed result models, `Raw` gone), `protocols.py` (types only). No force push, stash, auto-merge or old-tool call was added; `--format json` stays one document (emit now always validates against a model; `None` prints `{}` as `Nothing`).
- NOTE (core, not blocking here): the digest does not include a parent package `__init__.py` that Python runs for `from pkg.sub import X` (tests/test_core_gate_units.py:65 asserts this); code placed there changes behaviour without voiding a verdict. Suggest including the `__init__.py` of every package on the import path.
- NOTE (core): `Engine._supersede` loads and saves other subjects' state without their store lock; a run of the older subject that is still advancing can overwrite the mark or be overwritten. Take `store.lock` per old key (skip on Busy).
- NOTE (core): `switch_pending()` trusts `os.kill(pid, 0)`; a stale pending file whose pid was reused blocks every write command (exit 75) until the file is removed. Checking the file age or the process command would close it.
Tests: `sm gate run -- uv run pytest` on 25 files (core native, gate units, cli, engine, runlock, settings, json contract; plugin coord tasks/briefs/asks/handoff/skills/velocity/journal/docs/merges, guards lim/governor/loadguard/floor, lock, lock refuse matrix, plugin): 476 passed, 1 xfailed.

COVERS: doc asks-ack
REVIEWED: sha256=83e5dd85cf45f298675d83c85c28a7ccaf773bc8346556207a12df2d3158d286
VERDICT: APPROVE

## Campaign re-verdict g1 (2026-11-13, independent; reviewer wrote none of this code)

Target: integration/sm-complete 2d0877b (frozen), delta read against the g1 review at 479a8e8 (`git diff 479a8e8 2d0877b -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/reverdict-g1); it equals the unit list.

Unit change outside core: none (`asks.py`, `asks_lib`, `coord_lib` unchanged). Digest moved only through core.

Core part of the unit (shared; diff 479a8e8..2d0877b): `native.py`: `exempt=` now counts only for paths on the core allowlist `EXEMPT_PATHS = {"gate run"}` (`is_exempt`, used by `gate_problem` and `self reviews`; test_core_gate_units: another exempt writer stays gated); `switch_pending(commands, now)` reads "PID UTC-TIME" (bin/promote_lib.sh writes it) and ignores a pid whose `ps -o etime=` start is later than the file time + 2 s; old one-word files and an unreadable start fail closed (pid holds); `NativeRuntime.commands` gives the plugin registry from the runtime. `engine.py`: `_supersede` now loads, checks and saves each older run under that run's own `store.lock` (Busy -> the advance exits 75). `app.py`: `focus add|done` and `alarm ack` read-modify-write under `store.lock` (`locked`, Busy -> exit 75); `self reviews` uses `is_exempt`. `github/cli.py`: `ci cancel` refuses a non-digit run id before the gate. No force push, stash, auto-merge or old-tool call added; json stays one document.
- NOTE (core): a Busy on the second or later older run leaves the earlier ones already marked superseded; the retry finishes the job, nothing is lost.
- NOTE (core, carried): a parent package `__init__.py` on the import path is still outside the digest.
Tests: `sm gate run -- uv run pytest` on 25 files (core native, gate units, cli, engine, github cli, runlock, settings, json contract, promote review gate; plugin coord tasks/briefs/asks/handoff/skills/velocity/journal/docs/merges, guards lim/governor/loadguard/floor, lock, lock refuse matrix, plugin): 446 collected, 445 passed, 1 xfailed.

COVERS: doc asks-ack
REVIEWED: sha256=f0f6c893828dd76a65f4e3b0e35a70cbd15b32920e2d50d778483d1b7307a396
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

COVERS: doc asks-ack
REVIEWED: sha256=6822912afc3ab6fdce418817209b6edde346f8ee6873f01cbae5940f0eae239c
VERDICT: APPROVE

## Campaign re-verdict g1 (2026-11-14, independent; reviewer wrote none of this code)

Target: integration/sm-dod c36bbe1 (frozen), delta read against the live baseline 903a1b7, where this unit was approved (`git diff 903a1b7 c36bbe1 -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/campaign-g1-sm-dod); it equals the unit list of the sm-dod campaign.

Unit change outside core: `coord_lib/base.py` `child_env` now builds on `config.child_environment` and `search_path`: the same environment and PATH as before.
`asks_lib`: the former audit's watermark is read from `state.dashboard_dir` (setting `legacy_state_dir`, default now the project's state directory) as `ask-watermark.json`, the name `sm self migrate-state` gives `~/.cache/hz-ask-watermark.json`; read-only, and an unmigrated state refuses (exit 70, names the command) instead of reading a stale watermark. New `state.py` and `statemove.py` enter the digest by import; nothing in this unit moves a file.
- NOTE: a site that sets `legacy_state_dir` to the old `~/.cache` would now look for `ask-watermark.json` there and see no watermark; the live config uses the default, and `bin/migrate_config.py` rewrites old state paths.

Core part of the unit (read as diffs 903a1b7..c36bbe1): `config.py` (`child_environment` gains `drop`/`drop_prefixes`, `search_path`, `variable` for a plugin setting's old environment name; defaults keep the old behaviour), `plugins.py` (`old_variable` metadata: the old variable sets a field only when no file or SM_ variable does, a bool only on its exact `on` value; `state_moves` hook), `hookspecs.py` (`sm_state_moves`), `helpdoc.py` (lint letters O and X; `sm self lint` passes), `models.py` (`StateMigration`), `native.py` (every gated write run, and every tick of a loop, holds the shared `writers.lock` in the state directory; readers, exempt writers and runs without a project do not; the switch-pending behaviour is unchanged: a single run exits 75, a loop skips the tick). No force push, stash, auto-merge, --admin or old hz-* call was added; `--format json` stays one document.
- NOTE: no test proves that a native write command takes the shared `writers.lock` (only the migrate side is tested, with a hand-held lock); a test around `native._command` would pin it.
Tests (one gated command each, by file; this repo has no xdist, so without `-n 2`): `sm gate run -- uv run pytest tests/test_core_statemove.py tests/test_core_migrate.py tests/test_core_gate_units.py tests/test_core_commands.py tests/test_core_native.py tests/test_gate_structure.py`: all passed; `sm gate run -- uv run pytest` on plugin coord asks/briefs/docs/handoff/journal/skills/tasks/velocity/velocity_moved, guards_lim, lock, lock_refuse_matrix: exit 0 (all passed, 1 xfailed).

COVERS: doc asks-ack
REVIEWED: sha256=e5208c5994dbce825e9dcbe7a3c37cf9b12a6178609dbb0cf6a6b10931197d4a
VERDICT: APPROVE

## Re-verdict at abc64a4, group C (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-abc64a4/C.md (recorded verbatim by the integrator).

Same files as doc-ask, unchanged; coord_asks tests.
COVERS: doc asks-ack
REVIEWED: sha256=583ed2cb05fb4e76cddbad9c446e7737997cfc6e8c2e52ff2fab6e07e66c7bc5
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does.
COVERS: doc asks-ack
REVIEWED: sha256=989bda8d69cb5ab1fe111ae2c5270701c53e247927cc7e46973a150dbf77bcac
VERDICT: APPROVE
