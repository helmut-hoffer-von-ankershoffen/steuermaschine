# Port review: `sm beads stamp-released`

For the independent reviewer. The command is off until this file names the code (`REVIEWED: sha256=<digest>` from
`sm self reviews`) and ends with the verdict line; the author of the port writes neither.
Review unit: `native/coord.py`, `native/coord_lib/`.

## Exactly what it changes
One service: the issue tracker (bd): one appended note and one added label on EACH qualifying open bead. No file of
this tool, no GitHub write (read: `gh pr view`, `gh release view`; local read: `git tag --contains`).

## Side effects, in order
(1) read `bd list --status open --json --limit 0`. For each bead labelled `merged-N` and not `released-*`:
(2) read `gh pr view N` (the LAST merged label), skip when not merged; (3) read `git tag --contains <merge commit>`
in `core_repo_dir`; the earliest `vX.Y.Z` tag; skip when none or when `--version` names another;
(4) the released stamp of `sm beads stamp BEAD --released TAG` (see reviews/beads-stamp.md: release must be
published, label check, note, label).

## Differences from the old tool (`stamp sweep`)
| # | Difference | Why | Test |
|---|---|---|---|
| 1 | A subcommand `sweep` became the command `beads stamp-released`. | `beads sweep` is the evidence table; one word per command. | `test_beads_stamp_released_command_in_text_and_json` |
| 2 | The count is the number of beads whose stamp did not fail (as before); each bead's lines are printed above it. | Same rule, visible rows. | `test_sweep_stamps_only_merged_unreleased` |

## What the reviewer should check
1. `--dry-run` writes nothing (`test_sweep_skips_other_versions_unmerged_prs_and_unreadable_lists`).
2. One `--dry-run` on the real tracker after the next publish; compare with the old tool's `sweep --dry-run`.

## Independent review (2026-11-12, reviewer wrote none of the code)
Reviewed at head `09c9ce1` (unit unchanged at `1210f33`). Full findings: `REVIEW-INDEPENDENT-coord.md` in the slice's worktree.
- Writes only the released stamp of `beads stamp` (one note, one label) per qualifying bead. Selection read against the
  old `sweep` line by line, identical: status `open` only; a `merged-N` label and no `released-*` label; pull request
  merged; earliest `vX.Y.Z` tag that contains the merge commit; `--version` compared exactly (`0.9.4` is not `0.9.40`);
  the release must be published and not a draft, else that bead is refused and nothing is written to it. Ids are the
  tracker's full ids. `--dry-run` writes nothing.
- Can it label the wrong bead? Not by version, state or prefix. One case, as in the old tool (S7): `merged-N` is looked
  up in the core repository only, so a bead whose `merged-N` came from another repository is matched to core #N.
  Use `--version` after a publish; fix by carrying the repository in the label or skipping non-core stamps.
- Exit 70 through the installed command tree without this verdict (run, with `--dry-run`).
- Should fix, none blocking: S7, S6 (gh missing or signed out gives "0 bead(s) stamped" without a reason), S1 (gate
  test), N4 (a dry run still says "stamped").
- Not verified: a dry run against the real tracker after a publish.

COVERS: beads stamp-released
REVIEWED: sha256=114a4e055bec3c9160b12ec7db64abdd366eef65eb051bc8065331431022affe
VERDICT: APPROVE

## Campaign re-review g1 (2026-11-13, independent; reviewer wrote none of this code)

Target: integration/sm-complete 479a8e8 against live main 1d69c43. Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/campaign-g1); it equals the unit list.

Unit change outside core: none in `coord.py`/`coord_lib`; only the shared core files moved. Same writes and refusals as approved.

Core part of the unit (shared by every unit; read as diffs 1d69c43..479a8e8): `native.py` (digest now follows first-party imports via ast, files labelled by package path; `gate_problem`/`call` share one gate for CLI, pipelines and core writers; `exempt`; a write command exits 75 and runs nothing while a promote switch is pending, a loop skips ticks then), `engine.py` (running/pid in state, `_supersede`, review stop names the file, a spent `--approved` digest is not reused), `config.py` (one typed `Environment`, the only os.environ reader; same semantics as before for every variable, incl. the PYTEST_CURRENT_TEST push refusal in `defaults.py`), `helpdoc.py` (strict lint, declared writes vs Changes line), `models.py` (typed result models, `Raw` gone), `protocols.py` (types only). No force push, stash, auto-merge or old-tool call was added; `--format json` stays one document (emit now always validates against a model; `None` prints `{}` as `Nothing`).
- NOTE (core, not blocking here): the digest does not include a parent package `__init__.py` that Python runs for `from pkg.sub import X` (tests/test_core_gate_units.py:65 asserts this); code placed there changes behaviour without voiding a verdict. Suggest including the `__init__.py` of every package on the import path.
- NOTE (core): `Engine._supersede` loads and saves other subjects' state without their store lock; a run of the older subject that is still advancing can overwrite the mark or be overwritten. Take `store.lock` per old key (skip on Busy).
- NOTE (core): `switch_pending()` trusts `os.kill(pid, 0)`; a stale pending file whose pid was reused blocks every write command (exit 75) until the file is removed. Checking the file age or the process command would close it.
Tests: `sm gate run -- uv run pytest` on 25 files (core native, gate units, cli, engine, runlock, settings, json contract; plugin coord tasks/briefs/asks/handoff/skills/velocity/journal/docs/merges, guards lim/governor/loadguard/floor, lock, lock refuse matrix, plugin): 476 passed, 1 xfailed.

COVERS: beads stamp-released
REVIEWED: sha256=51fbd61a04246f167ddb70d65c7cfd58fda81f5a01b6cb679098eb6d87992b3f
VERDICT: APPROVE

## Campaign re-verdict g1 (2026-11-13, independent; reviewer wrote none of this code)

Target: integration/sm-complete 2d0877b (frozen), delta read against the g1 review at 479a8e8 (`git diff 479a8e8 2d0877b -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/reverdict-g1); it equals the unit list.

Unit change outside core: none (`coord.py`, `coord_lib` unchanged). Digest moved only through core.

Core part of the unit (shared; diff 479a8e8..2d0877b): `native.py`: `exempt=` now counts only for paths on the core allowlist `EXEMPT_PATHS = {"gate run"}` (`is_exempt`, used by `gate_problem` and `self reviews`; test_core_gate_units: another exempt writer stays gated); `switch_pending(commands, now)` reads "PID UTC-TIME" (bin/promote_lib.sh writes it) and ignores a pid whose `ps -o etime=` start is later than the file time + 2 s; old one-word files and an unreadable start fail closed (pid holds); `NativeRuntime.commands` gives the plugin registry from the runtime. `engine.py`: `_supersede` now loads, checks and saves each older run under that run's own `store.lock` (Busy -> the advance exits 75). `app.py`: `focus add|done` and `alarm ack` read-modify-write under `store.lock` (`locked`, Busy -> exit 75); `self reviews` uses `is_exempt`. `github/cli.py`: `ci cancel` refuses a non-digit run id before the gate. No force push, stash, auto-merge or old-tool call added; json stays one document.
- NOTE (core): a Busy on the second or later older run leaves the earlier ones already marked superseded; the retry finishes the job, nothing is lost.
- NOTE (core, carried): a parent package `__init__.py` on the import path is still outside the digest.
Tests: `sm gate run -- uv run pytest` on 25 files (core native, gate units, cli, engine, github cli, runlock, settings, json contract, promote review gate; plugin coord tasks/briefs/asks/handoff/skills/velocity/journal/docs/merges, guards lim/governor/loadguard/floor, lock, lock refuse matrix, plugin): 446 collected, 445 passed, 1 xfailed.

COVERS: beads stamp-released
REVIEWED: sha256=8e79764e80fae91459769ba13192020bc704a4fd132ad769eb49d282c7a63515
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

COVERS: beads stamp-released
REVIEWED: sha256=7e5a59cd3fdf5f424ea4993fc194e26a1f1f9ddcfb9ca26cffd67a8db240f764
VERDICT: APPROVE

## Campaign re-verdict g1 (2026-11-14, independent; reviewer wrote none of this code)

Target: integration/sm-dod c36bbe1 (frozen), delta read against the live baseline 903a1b7, where this unit was approved (`git diff 903a1b7 c36bbe1 -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/campaign-g1-sm-dod); it equals the unit list of the sm-dod campaign.

Unit change outside core: `coord_lib/base.py` `child_env` now builds on `config.child_environment` and `search_path`: the same environment and PATH as before.

Core part of the unit (read as diffs 903a1b7..c36bbe1): `config.py` (`child_environment` gains `drop`/`drop_prefixes`, `search_path`, `variable` for a plugin setting's old environment name; defaults keep the old behaviour), `plugins.py` (`old_variable` metadata: the old variable sets a field only when no file or SM_ variable does, a bool only on its exact `on` value; `state_moves` hook), `hookspecs.py` (`sm_state_moves`), `helpdoc.py` (lint letters O and X; `sm self lint` passes), `models.py` (`StateMigration`), `native.py` (every gated write run, and every tick of a loop, holds the shared `writers.lock` in the state directory; readers, exempt writers and runs without a project do not; the switch-pending behaviour is unchanged: a single run exits 75, a loop skips the tick). No force push, stash, auto-merge, --admin or old hz-* call was added; `--format json` stays one document.
- NOTE: no test proves that a native write command takes the shared `writers.lock` (only the migrate side is tested, with a hand-held lock); a test around `native._command` would pin it.
Tests (one gated command each, by file; this repo has no xdist, so without `-n 2`): `sm gate run -- uv run pytest tests/test_core_statemove.py tests/test_core_migrate.py tests/test_core_gate_units.py tests/test_core_commands.py tests/test_core_native.py tests/test_gate_structure.py`: all passed; `sm gate run -- uv run pytest` on plugin coord asks/briefs/docs/handoff/journal/skills/tasks/velocity/velocity_moved, guards_lim, lock, lock_refuse_matrix: exit 0 (all passed, 1 xfailed).

COVERS: beads stamp-released
REVIEWED: sha256=2a377fc5edf68e4e5feaece358bde4ac9082f742260ab6f1e5e5e5977283cd8e
VERDICT: APPROVE

## Re-verdict at abc64a4, group C (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-abc64a4/C.md (recorded verbatim by the integrator).

coord.py -> journal.stamp_released unchanged; coord_journal tests.
COVERS: beads stamp-released
REVIEWED: sha256=474049f747d7b73732da992ca8d4541316ad8201db0739f6b85ca6e637108409
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does.
COVERS: beads stamp-released
REVIEWED: sha256=8cc2e25fda19739c1877d182174be9408965b013daa556d37d01eb0a523e99ea
VERDICT: APPROVE
