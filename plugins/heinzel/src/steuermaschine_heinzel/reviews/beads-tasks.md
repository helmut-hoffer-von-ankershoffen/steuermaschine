# Port review: `sm beads tasks add | note | claim | due-set | pr | done`

For the independent reviewer. The command is off until this file names the code (`REVIEWED: sha256=<digest>` from
`sm self reviews`) and ends with the verdict line; the author of the port writes neither.
One file for the six write commands of `native/tasks.py` (`review="beads-tasks.md"`); name each with a `COVERS:` line.
Review unit: `native/tasks.py`, `native/tasks_lib/`, `native/asks_lib/`, `native/coord_lib/`.

## Exactly what each changes (the issue tracker bd, run in the tracker directory; `add --ask` also one file)
| Command | Tracker calls, in order | Other |
|---|---|---|
| `add TITLE` | `bd create --title TITLE -p N -t TYPE --silent [-a AGENT] [-l LABELS] [-d TEXT]` | with `--ask`: one ask row in the tracking document (`asks_lib.asks.add`, the code of `sm doc ask`), only after bd exited 0 and printed an id of the project's form |
| `note ID TEXT` | `bd update ID --append-notes "HH:MM TEXT"` | - |
| `claim ID AGENT [--due]` | `bd update ID --status in_progress --assignee AGENT`; with `--due` the calls of `due-set` | - |
| `due-set ID WHEN` | `bd list --json --limit 0` (read), `bd label remove ID due-...` per old label, `bd label add ID due-YYYYMMDD-HHMM` | - |
| `pr ID N [STATE]` | `bd update ID --append-notes "HH:MM PR #N STATE"` | - |
| `done ID EVIDENCE` | `bd close ID --force --reason EVIDENCE` (CLOSES the bead) | - |
Every bd call is checked: a non-zero exit refuses (exit 1) with bd's last lines on stderr and nothing after that call runs. Every ID is checked before the first call: the prefix plus `[a-z0-9]{3,12}` with optional `.N` (all 440 ids of the real tracker have this form), else exit 2. No GitHub call. `due`, `ls`, `show` are read-only commands of the same module and need no review.

## Differences from the old tool (77 lines, read in full)
| # | Difference | Why | Test |
|---|---|---|---|
| 1 | `add --ask` calls the ask code in process; when the document has no asks table the bead already exists and the refusal says so. | No command starts a script; honest partial result. | `test_add_with_ask_writes_the_ask_row_too_and_says_so_when_the_table_is_missing` |
| 2 | Empty title or empty evidence: exit 2. | The old tool passed them to the tracker. | `test_add_creates_...`, `test_note_pr_claim_due_set_and_done_...` |
| 3 | The tracker's exit code is checked (review 2, B5): non-zero is a refusal with bd's message; `add` refuses unless bd printed a bead id of the project's form, and then writes no ask row; `due-set` refuses when the bead list is not JSON (the old due labels could not be removed). The old tool reported success. | A failed close or note must not exit 0. | `test_every_write_refuses_with_the_trackers_message_when_bd_fails_and_writes_nothing_after_it`, `test_add_refuses_when_bd_prints_no_bead_id_and_writes_no_ask_row` |
| 5 | An ID that is empty or not of the project's form is refused before bd runs (an empty ID was the bare prefix, which bd resolves by prefix match). | Review 2, should-fix of B5. | `test_ids_that_are_not_of_the_projects_form_are_refused_before_bd_runs` |
| 6 | The title goes after `--title`: a title starting with `-` is not parsed as a bd flag. | Review 2, should-fix of B5. | `test_add_refuses_when_bd_prints_no_bead_id_and_writes_no_ask_row` |
| 4 | Times come from the clock provider; tracker directory and id prefix from the settings. | No fixed paths. | every test |

## What the reviewer should check
1. The argument lists against the old tool's lines 60 to 68.
2. `done` is reachable only with an explicit evidence text; nothing else closes a bead.
3. No write was made to the real tracker during the port; one `sm beads tasks ls` on the real tracker (read-only) is enough to see the wiring.

## Independent review 3 (2026-11-13, reviewer wrote none of the code)
Reviewed an export of `6230986` (fix of B5 on `6e4042e`). Full findings: `REVIEW.md` in ~/Code/tmp-sm-reviews/coord2/.
- B5 fixed. `Tracker.run` (`tasks_lib/beads.py:95-102`) is the only path to bd and refuses (exit 1) on any non-zero
  exit with bd's last three lines; every write goes through it, so nothing after a failed call runs. `add` writes the
  ask row only after bd exited 0 and printed `PREFIX[a-z0-9]{3,12}(.N)*` as its last line. `setdue` refuses when the
  bead list is not JSON, before any label call. Tests fail bd at every call of every write (12 cases) and assert the
  failing call is the last one and no ask row is written.
- bd 1.3.4 on a scratch tracker: a missing id, bad priority, bad type, bad status all exit 1 with an `Error:` line;
  `create --silent` prints only the id on stdout. So checking the exit code is sufficient.
- IDs: `bid` refuses (exit 2, before any call) anything but the prefix plus the pattern; empty, bare prefix, spaces,
  `;`, upper case, leading `-` are refused (tested). `done` needs ID and non-empty EVIDENCE, passes one id to
  `bd close ID --force --reason EVIDENCE`; no other command closes; it cannot close in bulk. Argument lists match the
  old tool's lines 58-66, plus `--title`.
- SHOULD-FIX: bd resolves a partial id (scratch: `tst-gn` resolved to `tst-gnj` for show, update and close), so a
  truncated 3+ character suffix acts on the one bead it prefixes. Confirm the exact id (`bd show ID --json`) before
  `done`. WHEN is not validated: `due-set ID tomorrow` removes the old due labels and adds `due-tomorrow`, prints `ok`,
  and the bead drops out of `sm beads tasks due` (as the old tool). With `--ask`, a failure of the ask step after the
  bead was created (no asks table, or `coord_document` unset while `coord_tracker_dir` is set) does not print the new
  bead id. The id is read from stdout plus stderr, so a bd warning on stderr makes `add` refuse after creating the bead
  (fails closed, message says to look first).
- NOTE: `claim --due` that fails at the label step leaves the claim done (reported). `--force` on `close` also closes
  a closed bead again and passes gates.

## Re-issued verdict (2026-11-13, branch chore/sm-clean, reviewer wrote none of the code)

Help text only since the previous verdict (sha256=ca416f174766eef6fd14c8ad7adfba3290f2e778c6d671c081187c648e08092d, approved on main at f9134cc).
The whole unit diff from the merge base f9134cc to 6acd0b4 is native/tasks.py: `help=` added to TITLE (add), TEXT (note), AGENT (claim), PR and STATE (pr).
No default, type, metavar, option name or body line changed; the digest at f9134cc recomputes to the old
REVIEWED value, so nothing else in the unit moved.

COVERS: beads tasks add
COVERS: beads tasks note
COVERS: beads tasks claim
COVERS: beads tasks due-set
COVERS: beads tasks pr
COVERS: beads tasks done
REVIEWED: sha256=f1fa62d407b63a3963d3d168872531b49a558ad7fdd2c7135e3c5e81d23721e0
VERDICT: APPROVE

## Campaign re-review g1 (2026-11-13, independent; reviewer wrote none of this code)

Target: integration/sm-complete 479a8e8 against live main 1d69c43. Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/campaign-g1); it equals the unit list.

Unit change outside core: `tasks.py` adds `help=` to TITLE, TEXT, AGENT, PR, STATE; no default, type or body line changed. `tasks_lib`/`asks_lib` unchanged: every bd call still checked, ids validated before the first call, `done` needs evidence. The earlier SHOULD-FIX (bd prefix match of a partial id before `done`) is still open.

Core part of the unit (shared by every unit; read as diffs 1d69c43..479a8e8): `native.py` (digest now follows first-party imports via ast, files labelled by package path; `gate_problem`/`call` share one gate for CLI, pipelines and core writers; `exempt`; a write command exits 75 and runs nothing while a promote switch is pending, a loop skips ticks then), `engine.py` (running/pid in state, `_supersede`, review stop names the file, a spent `--approved` digest is not reused), `config.py` (one typed `Environment`, the only os.environ reader; same semantics as before for every variable, incl. the PYTEST_CURRENT_TEST push refusal in `defaults.py`), `helpdoc.py` (strict lint, declared writes vs Changes line), `models.py` (typed result models, `Raw` gone), `protocols.py` (types only). No force push, stash, auto-merge or old-tool call was added; `--format json` stays one document (emit now always validates against a model; `None` prints `{}` as `Nothing`).
- NOTE (core, not blocking here): the digest does not include a parent package `__init__.py` that Python runs for `from pkg.sub import X` (tests/test_core_gate_units.py:65 asserts this); code placed there changes behaviour without voiding a verdict. Suggest including the `__init__.py` of every package on the import path.
- NOTE (core): `Engine._supersede` loads and saves other subjects' state without their store lock; a run of the older subject that is still advancing can overwrite the mark or be overwritten. Take `store.lock` per old key (skip on Busy).
- NOTE (core): `switch_pending()` trusts `os.kill(pid, 0)`; a stale pending file whose pid was reused blocks every write command (exit 75) until the file is removed. Checking the file age or the process command would close it.
Tests: `sm gate run -- uv run pytest` on 25 files (core native, gate units, cli, engine, runlock, settings, json contract; plugin coord tasks/briefs/asks/handoff/skills/velocity/journal/docs/merges, guards lim/governor/loadguard/floor, lock, lock refuse matrix, plugin): 476 passed, 1 xfailed.

COVERS: beads tasks add
COVERS: beads tasks note
COVERS: beads tasks claim
COVERS: beads tasks due-set
COVERS: beads tasks pr
COVERS: beads tasks done
REVIEWED: sha256=a7d3892e6e9aabb72a0174eb403c17dcdebfa055bb331ed498a176dc01ff6d55
VERDICT: APPROVE

## Campaign re-verdict g1 (2026-11-13, independent; reviewer wrote none of this code)

Target: integration/sm-complete 2d0877b (frozen), delta read against the g1 review at 479a8e8 (`git diff 479a8e8 2d0877b -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/reverdict-g1); it equals the unit list.

Unit change outside core: the earlier SHOULD-FIX (bd matches a partial id) is closed. `Tracker.done` now calls `exact()`: `bd show FULL --json` must return exactly one row whose id equals the full id; anything else (another id, several, no JSON) refuses before `bd close` (exit 2 for a partial id). `tasks.py` adds the help paragraph only. Test: test_native_coord_tasks `test_done_refuses_a_partial_id_...` (closes nothing).
- NOTE: `note`, `claim`, `due-set` and `pr` still pass a partial id to bd unchecked (an append or a claim, not a close); same `exact()` would cover them.

Core part of the unit (shared; diff 479a8e8..2d0877b): `native.py`: `exempt=` now counts only for paths on the core allowlist `EXEMPT_PATHS = {"gate run"}` (`is_exempt`, used by `gate_problem` and `self reviews`; test_core_gate_units: another exempt writer stays gated); `switch_pending(commands, now)` reads "PID UTC-TIME" (bin/promote_lib.sh writes it) and ignores a pid whose `ps -o etime=` start is later than the file time + 2 s; old one-word files and an unreadable start fail closed (pid holds); `NativeRuntime.commands` gives the plugin registry from the runtime. `engine.py`: `_supersede` now loads, checks and saves each older run under that run's own `store.lock` (Busy -> the advance exits 75). `app.py`: `focus add|done` and `alarm ack` read-modify-write under `store.lock` (`locked`, Busy -> exit 75); `self reviews` uses `is_exempt`. `github/cli.py`: `ci cancel` refuses a non-digit run id before the gate. No force push, stash, auto-merge or old-tool call added; json stays one document.
- NOTE (core): a Busy on the second or later older run leaves the earlier ones already marked superseded; the retry finishes the job, nothing is lost.
- NOTE (core, carried): a parent package `__init__.py` on the import path is still outside the digest.
Tests: `sm gate run -- uv run pytest` on 25 files (core native, gate units, cli, engine, github cli, runlock, settings, json contract, promote review gate; plugin coord tasks/briefs/asks/handoff/skills/velocity/journal/docs/merges, guards lim/governor/loadguard/floor, lock, lock refuse matrix, plugin): 446 collected, 445 passed, 1 xfailed.

COVERS: beads tasks add
COVERS: beads tasks note
COVERS: beads tasks claim
COVERS: beads tasks due-set
COVERS: beads tasks pr
COVERS: beads tasks done
REVIEWED: sha256=46654a7620db3cdb377e8ce737a748b192ba76a8d40f6d024bfc7ba600b44837
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

COVERS: beads tasks add
COVERS: beads tasks note
COVERS: beads tasks claim
COVERS: beads tasks due-set
COVERS: beads tasks pr
COVERS: beads tasks done
REVIEWED: sha256=6c53222bc70da7544ba0f1d991d884f4f765e41353fde1bd4b69778964abf746
VERDICT: APPROVE

## Campaign re-verdict g1 (2026-11-14, independent; reviewer wrote none of this code)

Target: integration/sm-dod c36bbe1 (frozen), delta read against the live baseline 903a1b7, where this unit was approved (`git diff 903a1b7 c36bbe1 -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/campaign-g1-sm-dod); it equals the unit list of the sm-dod campaign.

Unit change outside core: `coord_lib/base.py` `child_env` now builds on `config.child_environment` and `search_path`: the same environment and PATH as before.
`asks_lib`: the former audit's watermark is read from `state.dashboard_dir` (setting `legacy_state_dir`, default now the project's state directory) as `ask-watermark.json`, the name `sm self migrate-state` gives `~/.cache/hz-ask-watermark.json`; read-only, and an unmigrated state refuses (exit 70, names the command) instead of reading a stale watermark. New `state.py` and `statemove.py` enter the digest by import; nothing in this unit moves a file.
- NOTE: a site that sets `legacy_state_dir` to the old `~/.cache` would now look for `ask-watermark.json` there and see no watermark; the live config uses the default, and `bin/migrate_config.py` rewrites old state paths.

Core part of the unit (read as diffs 903a1b7..c36bbe1): `config.py` (`child_environment` gains `drop`/`drop_prefixes`, `search_path`, `variable` for a plugin setting's old environment name; defaults keep the old behaviour), `plugins.py` (`old_variable` metadata: the old variable sets a field only when no file or SM_ variable does, a bool only on its exact `on` value; `state_moves` hook), `hookspecs.py` (`sm_state_moves`), `helpdoc.py` (lint letters O and X; `sm self lint` passes), `models.py` (`StateMigration`), `native.py` (every gated write run, and every tick of a loop, holds the shared `writers.lock` in the state directory; readers, exempt writers and runs without a project do not; the switch-pending behaviour is unchanged: a single run exits 75, a loop skips the tick). No force push, stash, auto-merge, --admin or old hz-* call was added; `--format json` stays one document.
- NOTE: no test proves that a native write command takes the shared `writers.lock` (only the migrate side is tested, with a hand-held lock); a test around `native._command` would pin it.
Tests (one gated command each, by file; this repo has no xdist, so without `-n 2`): `sm gate run -- uv run pytest tests/test_core_statemove.py tests/test_core_migrate.py tests/test_core_gate_units.py tests/test_core_commands.py tests/test_core_native.py tests/test_gate_structure.py`: all passed; `sm gate run -- uv run pytest` on plugin coord asks/briefs/docs/handoff/journal/skills/tasks/velocity/velocity_moved, guards_lim, lock, lock_refuse_matrix: exit 0 (all passed, 1 xfailed).

COVERS: beads tasks add
COVERS: beads tasks note
COVERS: beads tasks claim
COVERS: beads tasks due-set
COVERS: beads tasks pr
COVERS: beads tasks done
REVIEWED: sha256=1efafc9d2b4352a1d376eaa53f3ea133a38c3d6f0c0db8acff5978e813903d2a
VERDICT: APPROVE

## Re-verdict at abc64a4, group C (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-abc64a4/C.md (recorded verbatim by the integrator).

tasks.py, tasks_lib/beads.py, asks_lib/*, statemove.py, heinzel state.py unchanged; coord_tasks tests.
COVERS: beads tasks add, beads tasks note, beads tasks claim, beads tasks due-set, beads tasks pr, beads tasks done
REVIEWED: sha256=75e0d49093649e7cf17923e82bd75bf09b4a55ca749b892828aea443230f023d
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does.
COVERS: beads tasks add
COVERS: beads tasks note
COVERS: beads tasks claim
COVERS: beads tasks due-set
COVERS: beads tasks pr
COVERS: beads tasks done
REVIEWED: sha256=d13ecb8e2b93cd616e594ec96a2791e98640c17a5bfb696f9cf3f6e2557df846
VERDICT: APPROVE
