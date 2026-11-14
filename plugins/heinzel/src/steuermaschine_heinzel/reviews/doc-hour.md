# Port review: `sm doc hour`

For the independent reviewer. The command is off until this file names the code (`REVIEWED: sha256=<digest>` from
`sm self reviews`) and ends with the verdict line; the author of the port writes neither.
Review unit: `native/handoff.py`, `native/handoff_lib/`, `native/coord_lib/`. This file COVERS `sm doc hour`.
Core code it runs: `src/steuermaschine/digest.py`.

## Exactly what it changes
Without `--no-handoff`: exactly what `sm doc handoff` changes (reviews/doc-handoff.md: the tracker's repository, branch
main, remote origin; checks first; the snapshot file; a commit of only that file; a fast-forward push), after the facts
are gathered; `--include-local-commits` is passed through. With `--no-handoff`: no file is written and no git command
runs in the tracker's repository (`gh` reads and the facts' own read-only commands still run); the snapshot file, the
index, the working tree, the branch and the remote stay untouched. The old tool's help called itself a fact gatherer
and pushed by default; the help now says so in its Changes line.

## What it runs
The text of the read-only commands named in the setting `hour_facts`, through `digest.collect`: a command that is
registered `writes=True`, or needs an argument, is never called; a missing, refusing or failing one is shown with its
state and the update goes on. Plus `gh run list`, `gh release list`, the load, and the pull request table of the handoff.

## Differences from the old tool
| # | Difference | Why | Test |
|---|---|---|---|
| 1 | It no longer runs the merge gate's `flip` (the old tool changed repository rulesets from inside the hourly facts). | A ruleset change is its own reviewed write command, not a side effect of a status page. | `test_hour_without_handoff_gathers_facts_drafts_and_writes_nothing` (no write call) |
| 2 | Facts are the text of native commands by path; the default list names commands of other areas that may not exist yet (shown as missing). The old tool started eleven scripts in threads. | No command starts a script. | same |
| 3 | The owner-message audit is the fact `doc asks` (read-only); the old tool's audit wrote a pending mark. | reviews/doc-asks-ack.md. | same |
| 4 | Sonar is not a built-in fact (the old tool fetched the quality gate over HTTP itself). | The core's quality provider belongs to another area; add its command path to `hour_facts`. | - |
| 5 | A refused or failed handoff is exit 1 with the reason (for example local commits the remote lacks); the facts are then not printed: run it with `--no-handoff`. | reviews/doc-handoff.md. | `test_hour_runs_the_handoff_by_default_and_reports_its_failure` |

## What the reviewer should check
1. `digest.collect` cannot call a write command (`test_digest_calls_read_only_commands_with_their_defaults_and_survives_each_failure`).
2. With `--no-handoff` no git call reaches the tracker's repository (asserted).
3. Decision for the coordinator: `--no-handoff` is read-only but sits behind this command's review gate. If that is
   unwanted, the split is one registration: a read-only `doc hour` and the existing `doc handoff` after it.

## Independent review 2 (2026-11-13, reviewer wrote none of the code)
Reviewed an export of `6e4042e` (rebased on `d98aee1`). Full findings: `REVIEW.md` next to this file in the review hand-off (~/Code/tmp-sm-reviews/coord/).
- Same unit and publication as `doc handoff` (see its review). `digest.collect` (core, sha256
  `3093f459592b94ce7ed1a89232ec700cea334ca384514c5d31878767820ac679`) never calls a write command or one that needs an
  argument. With `--no-handoff` no git call reaches the tracker's repository.
- Should fix, not blocking: S15 on the real checkout (193 local commits) the handoff refuses and the facts and draft
  are lost with the refusal; print them and report the refusal. S14 as for `doc handoff`.

COVERS: doc hour
REVIEWED: sha256=bbf2c0afb2a60a1a358cc8d19b923c3dab178b421feb498f13553f596183b80b
VERDICT: APPROVE

## Campaign re-review g1 (2026-11-13, independent; reviewer wrote none of this code)

Target: integration/sm-complete 479a8e8 against live main 1d69c43. Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/campaign-g1); it equals the unit list.

Unit change outside core: none in `handoff.py`/`handoff_lib`. The unit grew to 187 files because `doc hour` does `from . import commands` (native/__init__.py imports every module); 59 of them changed. I checked the path into them: `hour.build` -> `steuermaschine.digest.collect`, which runs only commands with `writes=False` and no required arguments and skips every write command, so the write changes in those modules cannot run from here (they are gated by their own units). The commit/push to the tracker repo (plain push, no force) is unchanged.
- SHOULD-FIX (churn): any plugin change voids this verdict. Import the registry by name at run time (`importlib.import_module`) or move `commands()` to a small module so the unit stays handoff-sized.

Core part of the unit (shared by every unit; read as diffs 1d69c43..479a8e8): `native.py` (digest now follows first-party imports via ast, files labelled by package path; `gate_problem`/`call` share one gate for CLI, pipelines and core writers; `exempt`; a write command exits 75 and runs nothing while a promote switch is pending, a loop skips ticks then), `engine.py` (running/pid in state, `_supersede`, review stop names the file, a spent `--approved` digest is not reused), `config.py` (one typed `Environment`, the only os.environ reader; same semantics as before for every variable, incl. the PYTEST_CURRENT_TEST push refusal in `defaults.py`), `helpdoc.py` (strict lint, declared writes vs Changes line), `models.py` (typed result models, `Raw` gone), `protocols.py` (types only). No force push, stash, auto-merge or old-tool call was added; `--format json` stays one document (emit now always validates against a model; `None` prints `{}` as `Nothing`).
- NOTE (core, not blocking here): the digest does not include a parent package `__init__.py` that Python runs for `from pkg.sub import X` (tests/test_core_gate_units.py:65 asserts this); code placed there changes behaviour without voiding a verdict. Suggest including the `__init__.py` of every package on the import path.
- NOTE (core): `Engine._supersede` loads and saves other subjects' state without their store lock; a run of the older subject that is still advancing can overwrite the mark or be overwritten. Take `store.lock` per old key (skip on Busy).
- NOTE (core): `switch_pending()` trusts `os.kill(pid, 0)`; a stale pending file whose pid was reused blocks every write command (exit 75) until the file is removed. Checking the file age or the process command would close it.
Tests: `sm gate run -- uv run pytest` on 25 files (core native, gate units, cli, engine, runlock, settings, json contract; plugin coord tasks/briefs/asks/handoff/skills/velocity/journal/docs/merges, guards lim/governor/loadguard/floor, lock, lock refuse matrix, plugin): 476 passed, 1 xfailed.

COVERS: doc hour
REVIEWED: sha256=210cad4ac9e87043ddb180368c742a8d8a9b32aa93b77a180d41ab940e389503
VERDICT: APPROVE

## Campaign re-verdict g1 (2026-11-13, independent; reviewer wrote none of this code)

Target: integration/sm-complete 2d0877b (frozen), delta read against the g1 review at 479a8e8 (`git diff 479a8e8 2d0877b -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/reverdict-g1); it equals the unit list.

Unit change outside core: the earlier SHOULD-FIX (churn) is closed. `doc hour` no longer imports the plugin registry (`from . import commands`); it gets it from `rt.commands()` (core `registered(rt)` = `plugin.native(settings)`, the same list the hook returns). The unit is down from 187 to 21 files. `handoff_lib` unchanged; the tracker commit/push (plain push) unchanged. Test: test_native_coord_handoff.

Core part of the unit (shared; diff 479a8e8..2d0877b): `native.py`: `exempt=` now counts only for paths on the core allowlist `EXEMPT_PATHS = {"gate run"}` (`is_exempt`, used by `gate_problem` and `self reviews`; test_core_gate_units: another exempt writer stays gated); `switch_pending(commands, now)` reads "PID UTC-TIME" (bin/promote_lib.sh writes it) and ignores a pid whose `ps -o etime=` start is later than the file time + 2 s; old one-word files and an unreadable start fail closed (pid holds); `NativeRuntime.commands` gives the plugin registry from the runtime. `engine.py`: `_supersede` now loads, checks and saves each older run under that run's own `store.lock` (Busy -> the advance exits 75). `app.py`: `focus add|done` and `alarm ack` read-modify-write under `store.lock` (`locked`, Busy -> exit 75); `self reviews` uses `is_exempt`. `github/cli.py`: `ci cancel` refuses a non-digit run id before the gate. No force push, stash, auto-merge or old-tool call added; json stays one document.
- NOTE (core): a Busy on the second or later older run leaves the earlier ones already marked superseded; the retry finishes the job, nothing is lost.
- NOTE (core, carried): a parent package `__init__.py` on the import path is still outside the digest.
Tests: `sm gate run -- uv run pytest` on 25 files (core native, gate units, cli, engine, github cli, runlock, settings, json contract, promote review gate; plugin coord tasks/briefs/asks/handoff/skills/velocity/journal/docs/merges, guards lim/governor/loadguard/floor, lock, lock refuse matrix, plugin): 446 collected, 445 passed, 1 xfailed.

COVERS: doc hour
REVIEWED: sha256=a3545de453b96c60009ebbd240d431c3ffc4c3fb476ebe76de6088f04632e037
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

COVERS: doc hour
REVIEWED: sha256=29626c7d764fe2ca5f4c01c6a41d2905cf24840eedb7e8b46568abd00d2f6cfb
VERDICT: APPROVE

## Campaign re-verdict g1 (2026-11-14, independent; reviewer wrote none of this code)

Target: integration/sm-dod c36bbe1 (frozen), delta read against the live baseline 903a1b7, where this unit was approved (`git diff 903a1b7 c36bbe1 -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/campaign-g1-sm-dod); it equals the unit list of the sm-dod campaign.

Unit change outside core: `coord_lib/base.py` `child_env` now builds on `config.child_environment` and `search_path`: the same environment and PATH as before.
`handoff_lib`, `digest.py` unchanged.

Core part of the unit (read as diffs 903a1b7..c36bbe1): `config.py` (`child_environment` gains `drop`/`drop_prefixes`, `search_path`, `variable` for a plugin setting's old environment name; defaults keep the old behaviour), `plugins.py` (`old_variable` metadata: the old variable sets a field only when no file or SM_ variable does, a bool only on its exact `on` value; `state_moves` hook), `hookspecs.py` (`sm_state_moves`), `helpdoc.py` (lint letters O and X; `sm self lint` passes), `models.py` (`StateMigration`), `native.py` (every gated write run, and every tick of a loop, holds the shared `writers.lock` in the state directory; readers, exempt writers and runs without a project do not; the switch-pending behaviour is unchanged: a single run exits 75, a loop skips the tick). No force push, stash, auto-merge, --admin or old hz-* call was added; `--format json` stays one document.
- NOTE: no test proves that a native write command takes the shared `writers.lock` (only the migrate side is tested, with a hand-held lock); a test around `native._command` would pin it.
Tests (one gated command each, by file; this repo has no xdist, so without `-n 2`): `sm gate run -- uv run pytest tests/test_core_statemove.py tests/test_core_migrate.py tests/test_core_gate_units.py tests/test_core_commands.py tests/test_core_native.py tests/test_gate_structure.py`: all passed; `sm gate run -- uv run pytest` on plugin coord asks/briefs/docs/handoff/journal/skills/tasks/velocity/velocity_moved, guards_lim, lock, lock_refuse_matrix: exit 0 (all passed, 1 xfailed).

COVERS: doc hour
REVIEWED: sha256=dcb0900208314ab7f54d319e4243ed22685a9103233a12e7216dc7b4d00bc9ff
VERDICT: APPROVE

## Re-verdict at abc64a4, group C (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-abc64a4/C.md (recorded verbatim by the integrator).

Same files as doc-handoff, unchanged; coord_handoff tests.
COVERS: doc hour
REVIEWED: sha256=7a6d0340501caeaa256ce0ade88d5f718720e0c930a30db257b6dbb926e581ff
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does.
COVERS: doc hour
REVIEWED: sha256=5fecf27c41a579629dec28134087d946d754c6e149ca3f20d4332b83b6720009
VERDICT: APPROVE
