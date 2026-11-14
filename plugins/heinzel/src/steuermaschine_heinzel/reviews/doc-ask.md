# Port review: `sm doc ask`

For the independent reviewer. The command is off until this file names the code (`REVIEWED: sha256=<digest>` from
`sm self reviews`) and ends with the verdict line; the author of the port writes neither.
Review unit: `native/asks.py`, `native/asks_lib/`, `native/coord_lib/`. Core code it runs: `src/steuermaschine/journal.py`.

## Exactly what it changes
One file: the tracking document (setting `coord_document`). One table row is inserted after the last row of the first
table below the first line that starts with the setting `asks_heading` (default `### Now`); the whole file is
rewritten (`read_text` / `write_text`, final newline ensured). No service. Not committed, not pushed.
Row: `| <ask> | ask YYYY-MM-DD HH:MM | Name an agent within the hour; small text or UI goes through the small-change lane | |`.
Locks held from the read to the write: `asks_lock_file` when the configuration names one (the former ask tool's),
then the journal's lock (`coord_journal_lock_file`, default `<document>.lock`).

## Code
`src/steuermaschine/journal.py` `add_table_row`, `table_end` (core, generic); `native/asks_lib/asks.py` `add`;
`native/asks.py` `doc_ask`. Check moved code: `python3 plugins/heinzel/tests/coord_verbatim.py`.

## Changes after the first review (findings B1, B2; both in core `src/steuermaschine/journal.py`, no file of the unit changed)
- B1: the document is never truncated in place. `add_table_row` encodes the row before it takes the locks, and
  `replace_text` builds all bytes (UTF-8) first, writes a temporary file next to the document with `open("xb")`,
  syncs it, gives it the document's mode and renames it over the document. An argument that cannot be encoded leaves
  the document byte-identical (`test_an_argument_that_cannot_be_encoded_leaves_the_document_byte_identical`, red
  before; `test_the_rewrite_is_atomic_keeps_the_mode_and_other_bytes`). The refusal text for such an argument is still
  the usage line of `asks.py` ("give the ask"): changing it would void the approved `doc asks-ack`; say if you want it.
- B2: `table_end` returns None when the next heading or the end of the file comes before a table row, so a heading
  without a table is refused and nothing is written (`test_a_heading_without_a_table_below_it_is_refused_and_nothing_is_written`,
  red before). The refusal names the heading (its text still reads "no line starts with", same reason as above).
- `append` and `line_of` (what `sm note` uses) are unchanged; a test now shows that `sm note` with such an argument
  leaves the document byte-identical as well.

## Differences from the old tool
| # | Difference | Why | Test |
|---|---|---|---|
| 1 | The journal's lock is held too. | The old tool rewrote the file under its own lock only: a journal line appended between its read and write was lost. | review item 1 |
| 2 | No table: refusal exit 1 naming the settings; empty text: exit 2. Was `SystemExit` text, exit 1. | Exit-code table. | `test_doc_ask_command_text_json_and_refusals` |
| 3 | `--check`, `--audit`, `--ack` are not options of this command: see `sm doc asks` (read-only) and `sm doc asks-ack`. | One command, one effect. | `test_native_coord_asks.py` |
| 4 | Heading and paths are settings. | The document was cut to a new layout; anchors must not be fixed in code. | sandbox tests |

## What the reviewer should check
1. Lock order and extent in `add_table_row`; an editor that writes the file without the locks can still lose the row
   (as before: agents edit this document by hand; not solved here).
2. On a COPY of the real tracking document: the row lands at the end of the `### Now` table, the rest is byte-identical
   (`diff`).

## Independent review 2 (2026-11-13, reviewer wrote none of the code)
Reviewed an export of `6e4042e` (rebased on `d98aee1`). Full findings: `REVIEW.md` next to this file in the review hand-off (~/Code/tmp-sm-reviews/coord/).
- B1 fixed in core `src/steuermaschine/journal.py`: the row is encoded before the locks; `replace_text` builds all bytes,
  writes a temporary file with `open("xb")`, fsyncs, keeps the mode and renames. On a copy of today's document a
  surrogate argument raises and leaves it byte-identical; a normal row lands at the end of the `### Now` table, nothing
  else differs.
- B2 fixed: `table_end` stops at the next heading or end of file; a heading without a table refuses (shown).
- Core code this approval relies on and no digest covers: `src/steuermaschine/journal.py` sha256
  `db7f7b7288e6fb45c26088747034011188cc41db0171be108b7e6ec911a81457`. A change there needs a new look.
- Should fix, not blocking: S13 an encoding error is still reported as the usage line (nothing is written). NOTE:
  the rename drops extended attributes and changes the inode; the section "Exactly what it changes" above still says
  read_text / write_text. Condition of use S2 (lock files during coexistence) unchanged.

COVERS: doc ask
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

COVERS: doc ask
REVIEWED: sha256=83e5dd85cf45f298675d83c85c28a7ccaf773bc8346556207a12df2d3158d286
VERDICT: APPROVE

## Campaign re-verdict g1 (2026-11-13, independent; reviewer wrote none of this code)

Target: integration/sm-complete 2d0877b (frozen), delta read against the g1 review at 479a8e8 (`git diff 479a8e8 2d0877b -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/reverdict-g1); it equals the unit list.

Unit change outside core: none (`asks.py`, `asks_lib`, `coord_lib` unchanged). Digest moved only through core.

Core part of the unit (shared; diff 479a8e8..2d0877b): `native.py`: `exempt=` now counts only for paths on the core allowlist `EXEMPT_PATHS = {"gate run"}` (`is_exempt`, used by `gate_problem` and `self reviews`; test_core_gate_units: another exempt writer stays gated); `switch_pending(commands, now)` reads "PID UTC-TIME" (bin/promote_lib.sh writes it) and ignores a pid whose `ps -o etime=` start is later than the file time + 2 s; old one-word files and an unreadable start fail closed (pid holds); `NativeRuntime.commands` gives the plugin registry from the runtime. `engine.py`: `_supersede` now loads, checks and saves each older run under that run's own `store.lock` (Busy -> the advance exits 75). `app.py`: `focus add|done` and `alarm ack` read-modify-write under `store.lock` (`locked`, Busy -> exit 75); `self reviews` uses `is_exempt`. `github/cli.py`: `ci cancel` refuses a non-digit run id before the gate. No force push, stash, auto-merge or old-tool call added; json stays one document.
- NOTE (core): a Busy on the second or later older run leaves the earlier ones already marked superseded; the retry finishes the job, nothing is lost.
- NOTE (core, carried): a parent package `__init__.py` on the import path is still outside the digest.
Tests: `sm gate run -- uv run pytest` on 25 files (core native, gate units, cli, engine, github cli, runlock, settings, json contract, promote review gate; plugin coord tasks/briefs/asks/handoff/skills/velocity/journal/docs/merges, guards lim/governor/loadguard/floor, lock, lock refuse matrix, plugin): 446 collected, 445 passed, 1 xfailed.

COVERS: doc ask
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

COVERS: doc ask
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

COVERS: doc ask
REVIEWED: sha256=e5208c5994dbce825e9dcbe7a3c37cf9b12a6178609dbb0cf6a6b10931197d4a
VERDICT: APPROVE

## Re-verdict at abc64a4, group C (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-abc64a4/C.md (recorded verbatim by the integrator).

asks.py, asks_lib/{asks,legacy}.py unchanged (document lock from coord_lib unchanged apart from the PATH default);
coord_asks tests.
COVERS: doc ask
REVIEWED: sha256=583ed2cb05fb4e76cddbad9c446e7737997cfc6e8c2e52ff2fab6e07e66c7bc5
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does.
COVERS: doc ask
REVIEWED: sha256=989bda8d69cb5ab1fe111ae2c5270701c53e247927cc7e46973a150dbf77bcac
VERDICT: APPROVE
