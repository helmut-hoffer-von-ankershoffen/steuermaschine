# Port review: `sm beads brief`

For the independent reviewer. The command is off until this file names the code (`REVIEWED: sha256=<digest>` from
`sm self reviews`) and ends with the verdict line; the author of the port writes neither.
Review unit: `native/briefs.py`, `native/briefs_lib/`, `native/coord_lib/`. Core code it runs: `src/steuermaschine/journal.py`.

## Exactly what it changes, in order
1. Reads: `ps -axo pid=,comm=`, `lsof -a -d cwd -Fpn -p PIDS`, the 1-minute load. Machine full without `--force`:
   refusal, nothing below happens.
2. Machine full with `--force REASON`: one journal line in the tracking document (`coord_lib.journal.note`).
3. Only when `<brief_code_dir>/<clone>-TOPIC` does not exist (an existing one is only read with `git worktree list`):
   `git -C <clone> fetch -q origin main` (network read),
   then `git -C <clone> worktree add -q <tree> -b BRANCH origin/main` (a new directory, a new local branch, one entry
   in the clone's worktree list).
No push, no GitHub write, no tracker write.

## Changes after the first review (finding B3, S11; `briefs_lib/builder.py`, `briefs.py`)
- TOPIC must match `[A-Za-z0-9][A-Za-z0-9._-]*`; the resolved target must be a direct child of the code directory, must
  not be a main clone of `brief_clones` and not the tracker's repository. Checked before the machine check and before
  any process starts; exit 2 (`test_a_topic_that_is_not_a_plain_name_is_refused_before_anything_runs`,
  `test_a_topic_that_names_a_clone_or_the_tracker_is_refused`, both red before).
- An existing directory is reused only when `git worktree list --porcelain` of the clone lists it on BRANCH; otherwise
  exit 1 naming what is there (`test_an_existing_directory_is_reused_only_when_it_is_this_clones_worktree_on_the_branch`).
- A failed `git fetch origin main` is a refusal; no worktree is added from a stale origin/main (`test_a_failed_fetch_...`).

## Differences from the old tool
| # | Difference | Why | Test |
|---|---|---|---|
| 1 | Limits, agent process names, code directory and the clone table are settings. | No fixed paths or names. | `test_brief_counts_agent_processes_by_cwd` |
| 2 | The prompt names `sm beads stamp BEAD --merged <PR>` and the tracking document from the settings. | No old tool name in output. | `test_brief_names_the_bead_and_the_other_repository` |
| 3 | The forced-brief journal line starts `builder brief TOPIC forced past the limit`. | Same. | `test_brief_refuses_when_full_and_force_goes_ahead_and_logs` |
| 4 | Unknown `--repo`: exit 2. A git refusal: exit 1 with git's text. | Exit-code table. | `test_brief_reports_a_git_refusal_and_is_off_until_reviewed` |
| 5 | The worktree of the default repository is `<clone>-TOPIC` like the others (was special-cased to `heinzel-TOPIC`, the same name). | One rule. | `test_brief_makes_the_worktree_and_prints_the_prompt` |

## What the reviewer should check
1. A refusal reaches no `git` call (asserted in the tests).
2. `test_brief_adds_a_real_worktree_on_a_new_branch_from_origin_main` runs a real `git worktree add` in a scratch
   repository (as the old test did), including reuse and git's refusal of an existing branch.

## Independent review 2 (2026-11-13, reviewer wrote none of the code)
Reviewed an export of `6e4042e` (rebased on `d98aee1`). Full findings: `REVIEW.md` next to this file in the review hand-off (~/Code/tmp-sm-reviews/coord/).
- B3 fixed: TOPIC is a plain name; the target must be a direct child of the code directory, not a configured clone
  and not the tracker's repository; an existing directory is reused only when this clone's `git worktree list` shows
  it on BRANCH; a failed fetch refuses before `worktree add`. All before any git write.
- Should fix, not blocking: the new branch still tracks origin/main (add `--no-track`); `document(s)` is first needed
  after the worktree exists when only `coord_tracker_dir` is configured; S7 (prompt without `--repo`).

COVERS: beads brief
REVIEWED: sha256=f3ce4739e648e0b9dcb73b5f3ed7237e4511f0700cb4ebb87065428e3e96e630
VERDICT: APPROVE

## Campaign re-review g1 (2026-11-13, independent; reviewer wrote none of this code)

Target: integration/sm-complete 479a8e8 against live main 1d69c43. Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/campaign-g1); it equals the unit list.

Unit change outside core: none in `briefs.py`/`briefs_lib`; the imported `runlock.py` gained the exclusive request (an extra `Status.exclusive` field and the exclusive lock file); the brief reads the lock status only and writes the same files as before.

Core part of the unit (shared by every unit; read as diffs 1d69c43..479a8e8): `native.py` (digest now follows first-party imports via ast, files labelled by package path; `gate_problem`/`call` share one gate for CLI, pipelines and core writers; `exempt`; a write command exits 75 and runs nothing while a promote switch is pending, a loop skips ticks then), `engine.py` (running/pid in state, `_supersede`, review stop names the file, a spent `--approved` digest is not reused), `config.py` (one typed `Environment`, the only os.environ reader; same semantics as before for every variable, incl. the PYTEST_CURRENT_TEST push refusal in `defaults.py`), `helpdoc.py` (strict lint, declared writes vs Changes line), `models.py` (typed result models, `Raw` gone), `protocols.py` (types only). No force push, stash, auto-merge or old-tool call was added; `--format json` stays one document (emit now always validates against a model; `None` prints `{}` as `Nothing`).
- NOTE (core, not blocking here): the digest does not include a parent package `__init__.py` that Python runs for `from pkg.sub import X` (tests/test_core_gate_units.py:65 asserts this); code placed there changes behaviour without voiding a verdict. Suggest including the `__init__.py` of every package on the import path.
- NOTE (core): `Engine._supersede` loads and saves other subjects' state without their store lock; a run of the older subject that is still advancing can overwrite the mark or be overwritten. Take `store.lock` per old key (skip on Busy).
- NOTE (core): `switch_pending()` trusts `os.kill(pid, 0)`; a stale pending file whose pid was reused blocks every write command (exit 75) until the file is removed. Checking the file age or the process command would close it.
Tests: `sm gate run -- uv run pytest` on 25 files (core native, gate units, cli, engine, runlock, settings, json contract; plugin coord tasks/briefs/asks/handoff/skills/velocity/journal/docs/merges, guards lim/governor/loadguard/floor, lock, lock refuse matrix, plugin): 476 passed, 1 xfailed.

COVERS: beads brief
REVIEWED: sha256=b9cb0e2873af9c13a0236990f128b4432d8cc91454229988e400099f83c9e72f
VERDICT: APPROVE

## Campaign re-verdict g1 (2026-11-13, independent; reviewer wrote none of this code)

Target: integration/sm-complete 2d0877b (frozen), delta read against the g1 review at 479a8e8 (`git diff 479a8e8 2d0877b -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/reverdict-g1); it equals the unit list.

Unit change outside core: none (`briefs.py`, `briefs_lib`, `coord_lib`, `runlock.py` unchanged). Digest moved only through core.

Core part of the unit (shared; diff 479a8e8..2d0877b): `native.py`: `exempt=` now counts only for paths on the core allowlist `EXEMPT_PATHS = {"gate run"}` (`is_exempt`, used by `gate_problem` and `self reviews`; test_core_gate_units: another exempt writer stays gated); `switch_pending(commands, now)` reads "PID UTC-TIME" (bin/promote_lib.sh writes it) and ignores a pid whose `ps -o etime=` start is later than the file time + 2 s; old one-word files and an unreadable start fail closed (pid holds); `NativeRuntime.commands` gives the plugin registry from the runtime. `engine.py`: `_supersede` now loads, checks and saves each older run under that run's own `store.lock` (Busy -> the advance exits 75). `app.py`: `focus add|done` and `alarm ack` read-modify-write under `store.lock` (`locked`, Busy -> exit 75); `self reviews` uses `is_exempt`. `github/cli.py`: `ci cancel` refuses a non-digit run id before the gate. No force push, stash, auto-merge or old-tool call added; json stays one document.
- NOTE (core): a Busy on the second or later older run leaves the earlier ones already marked superseded; the retry finishes the job, nothing is lost.
- NOTE (core, carried): a parent package `__init__.py` on the import path is still outside the digest.
Tests: `sm gate run -- uv run pytest` on 25 files (core native, gate units, cli, engine, github cli, runlock, settings, json contract, promote review gate; plugin coord tasks/briefs/asks/handoff/skills/velocity/journal/docs/merges, guards lim/governor/loadguard/floor, lock, lock refuse matrix, plugin): 446 collected, 445 passed, 1 xfailed.

COVERS: beads brief
REVIEWED: sha256=c67de343cfd3512a8b8a4d529d5aeaf90e0e8c67dee52a9c6ad02f53fe49c671
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

COVERS: beads brief
REVIEWED: sha256=96d3d0c06f182f3ab4183e9b9d1535ebcee875432594f78a49525c037828806f
VERDICT: APPROVE

## Campaign re-verdict g1 (2026-11-14, independent; reviewer wrote none of this code)

Target: integration/sm-dod c36bbe1 (frozen), delta read against the live baseline 903a1b7, where this unit was approved (`git diff 903a1b7 c36bbe1 -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/campaign-g1-sm-dod); it equals the unit list of the sm-dod campaign.

Unit change outside core: `coord_lib/base.py` `child_env` now builds on `config.child_environment` and `search_path`: the same environment and PATH as before.
`runlock.py` unchanged.

Core part of the unit (read as diffs 903a1b7..c36bbe1): `config.py` (`child_environment` gains `drop`/`drop_prefixes`, `search_path`, `variable` for a plugin setting's old environment name; defaults keep the old behaviour), `plugins.py` (`old_variable` metadata: the old variable sets a field only when no file or SM_ variable does, a bool only on its exact `on` value; `state_moves` hook), `hookspecs.py` (`sm_state_moves`), `helpdoc.py` (lint letters O and X; `sm self lint` passes), `models.py` (`StateMigration`), `native.py` (every gated write run, and every tick of a loop, holds the shared `writers.lock` in the state directory; readers, exempt writers and runs without a project do not; the switch-pending behaviour is unchanged: a single run exits 75, a loop skips the tick). No force push, stash, auto-merge, --admin or old hz-* call was added; `--format json` stays one document.
- NOTE: no test proves that a native write command takes the shared `writers.lock` (only the migrate side is tested, with a hand-held lock); a test around `native._command` would pin it.
Tests (one gated command each, by file; this repo has no xdist, so without `-n 2`): `sm gate run -- uv run pytest tests/test_core_statemove.py tests/test_core_migrate.py tests/test_core_gate_units.py tests/test_core_commands.py tests/test_core_native.py tests/test_gate_structure.py`: all passed; `sm gate run -- uv run pytest` on plugin coord asks/briefs/docs/handoff/journal/skills/tasks/velocity/velocity_moved, guards_lim, lock, lock_refuse_matrix: exit 0 (all passed, 1 xfailed).

COVERS: beads brief
REVIEWED: sha256=0ac08dadb7721dcbf7237c8ecf6bbdc0ff406da64a5fef65dacbc2104b189399
VERDICT: APPROVE

## Re-verdict at abc64a4, group C (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-abc64a4/C.md (recorded verbatim by the integrator).

briefs.py, briefs_lib/{builder,reviewer}.py, runlock.py unchanged; coord_briefs tests.
COVERS: beads brief
REVIEWED: sha256=304d256251b360f33a956b37c3f1f6ef212ebbaad3b9923dc2ef6c1a45593fee
VERDICT: APPROVE

## Delta review: feat/sm-gate-lanes d94de45 (independent reviewer)

Changed in this unit since the last verdict: src/steuermaschine/runlock.py (light lane, rerun refusal). Reviewed in full in two rounds (e6db9d9, d94de45),
probes now tests; see ~/Code/packets/sm-gate-lanes/REVIEW-2.md. Nothing in this command got weaker.

REVIEWED: sha256=62de82b3d0a15f26ace7c756b9205edf7331faae61013267a79dec51ad192813
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does. runlock.py (gate lanes) is in the digest through `sandbox_load`, which the delta does not touch; the brief runs no gated command.
COVERS: beads brief
REVIEWED: sha256=e90a96c1e20c09aa174eb56fd6b7329318fa9949dc8fbd678065cb71bd4059c4
VERDICT: APPROVE
