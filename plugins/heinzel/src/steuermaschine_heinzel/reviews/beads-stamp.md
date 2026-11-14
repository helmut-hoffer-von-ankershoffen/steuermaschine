# Port review: `sm beads stamp` (and the function `coord_lib.journal.stamp`)

For the independent reviewer. The command is off until this file names the code (`REVIEWED: sha256=<digest>` from
`sm self reviews`) and ends with the verdict line; the author of the port writes neither.
Review unit: `native/coord.py`, `native/coord_lib/`.

## Exactly what it changes
One service: the issue tracker (bd, run in `coord_tracker_dir`): one appended note and one added label per stamp on
ONE bead. No file of this tool, no GitHub write (GitHub is read with `gh pr view` and `gh release view`).

## Side effects, in order
For `--merged PR`: (1) read `gh pr view PR -R REPO --json state,mergeCommit,mergedAt`; not MERGED: refuse, nothing
written. (2) read `bd show ID --json`; label `merged-PR` present: nothing written. (3) `bd update ID --append-notes
"HH:MM merged_in: PR #N sha=<8> at=<mergedAt>"`. (4) `bd label add ID merged-PR`.
For `--released V`: (1) read `gh release view vV -R CORE --json isDraft,publishedAt`; draft or unpublished: refuse.
(2) label check. (3) note `released_in: vV released_at=<publishedAt>`. (4) label `released-vV`.
With both options the merged stamp is written before the release is checked, as in the old tool.
bd runs in `coord_tracker_dir`; gh through the one client `steuermaschine.github.Gh`. No GitHub write.

## What moved
`merged_line`, `released_line`, `earliest_tag`, `merged_prs`, `is_released` are the old functions, byte for byte
(check: `python3 plugins/heinzel/tests/coord_verbatim.py`). `Stamper.stamp` is `do_stamp`, `Stamper.write` is
`stamp`, `Stamper.sweep` is `sweep`, with the process calls on the `Commands` provider and results as models.

## Differences from the old tool
| # | Difference | Why | Test |
|---|---|---|---|
| 1 | No JSON event lines (`stamp.start`, `stamp.outcome`) on stderr. | The result model carries the outcome (`status` per row). | `test_the_stamp_note_carries_the_time_the_line_and_the_label` |
| 2 | Nothing to stamp prints `BEAD: already stamped`; the old tool printed nothing. | A command says what it did. | `test_merged_stamp_is_idempotent` |
| 3 | A failed tracker write is shown as `(FAILED: the tracker refused)`, exit 1; the old tool printed the line as if written, exit 1. | Honest output. | `test_a_tracker_failure_is_reported_as_failed` |
| 4 | Neither `--merged` nor `--released`: exit 2 with the usage; as before. | - | `test_beads_stamp_command_refuses_without_a_stamp_and_for_an_open_pr` |
| 5 | The function `stamp()` returns the refusal in the model instead of exiting, so a caller (the merge queue) is not interrupted. | The queue ignored the old exit code. | `test_unmerged_pr_is_refused_and_nothing_written` |

## What the reviewer should check
1. The order above against `Stamper.stamp`.
2. `--dry-run` reaches no `bd update` and no `bd label add`.
3. One `--dry-run` against the real tracker for a bead with a merged pull request (read-only).

## Independent review (2026-11-12, reviewer wrote none of the code)
Reviewed at head `09c9ce1` (unit unchanged at `1210f33`). Full findings: `REVIEW-INDEPENDENT-coord.md` in the slice's worktree.
- Writes only `bd update ID --append-notes` and `bd label add ID LABEL`, on the one bead named, each stamp only after
  GitHub confirmed it (pull request MERGED; release published and not a draft) and only when the label is absent.
  `--dry-run` reaches neither write. GitHub is read only (`gh pr view`, `gh release view`). No file, no store entry.
- The five pure functions are the old ones (verifier: VERBATIM); `Stamper.stamp`, `write`, `has_label` read against
  `do_stamp`, `stamp`, `has_label` line by line: same order, same arguments.
- Exit 70 through the installed command tree without this verdict (run, with `--dry-run`).
- Should fix, none blocking: S5 (`--merged` with `--released` and an unpublished release: the merged stamp IS written,
  the output shows only the refusal and the help says nothing is written), S6 (gh runs without the tool path and the
  reason is dropped: "not merged" when gh is missing or signed out; fails closed), S7 (the prompt of `beads brief` for
  another repository omits `--repo`, so the core pull request of that number is stamped), S1 (gate test).
- Not verified: a dry run against the real tracker (no tracker call was allowed in this review).

COVERS: beads stamp
REVIEWED: sha256=114a4e055bec3c9160b12ec7db64abdd366eef65eb051bc8065331431022affe
VERDICT: APPROVE

## Campaign re-review g1 (2026-11-13, independent; reviewer wrote none of this code)

Target: integration/sm-complete 479a8e8 against live main 1d69c43. Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/campaign-g1); it equals the unit list.

Unit change outside core: none in `coord.py`/`coord_lib`; only the shared core files moved. The bd/journal writes and their refusals are byte-identical to the approved version.

Core part of the unit (shared by every unit; read as diffs 1d69c43..479a8e8): `native.py` (digest now follows first-party imports via ast, files labelled by package path; `gate_problem`/`call` share one gate for CLI, pipelines and core writers; `exempt`; a write command exits 75 and runs nothing while a promote switch is pending, a loop skips ticks then), `engine.py` (running/pid in state, `_supersede`, review stop names the file, a spent `--approved` digest is not reused), `config.py` (one typed `Environment`, the only os.environ reader; same semantics as before for every variable, incl. the PYTEST_CURRENT_TEST push refusal in `defaults.py`), `helpdoc.py` (strict lint, declared writes vs Changes line), `models.py` (typed result models, `Raw` gone), `protocols.py` (types only). No force push, stash, auto-merge or old-tool call was added; `--format json` stays one document (emit now always validates against a model; `None` prints `{}` as `Nothing`).
- NOTE (core, not blocking here): the digest does not include a parent package `__init__.py` that Python runs for `from pkg.sub import X` (tests/test_core_gate_units.py:65 asserts this); code placed there changes behaviour without voiding a verdict. Suggest including the `__init__.py` of every package on the import path.
- NOTE (core): `Engine._supersede` loads and saves other subjects' state without their store lock; a run of the older subject that is still advancing can overwrite the mark or be overwritten. Take `store.lock` per old key (skip on Busy).
- NOTE (core): `switch_pending()` trusts `os.kill(pid, 0)`; a stale pending file whose pid was reused blocks every write command (exit 75) until the file is removed. Checking the file age or the process command would close it.
Tests: `sm gate run -- uv run pytest` on 25 files (core native, gate units, cli, engine, runlock, settings, json contract; plugin coord tasks/briefs/asks/handoff/skills/velocity/journal/docs/merges, guards lim/governor/loadguard/floor, lock, lock refuse matrix, plugin): 476 passed, 1 xfailed.

COVERS: beads stamp
REVIEWED: sha256=51fbd61a04246f167ddb70d65c7cfd58fda81f5a01b6cb679098eb6d87992b3f
VERDICT: APPROVE

## Campaign re-verdict g1 (2026-11-13, independent; reviewer wrote none of this code)

Target: integration/sm-complete 2d0877b (frozen), delta read against the g1 review at 479a8e8 (`git diff 479a8e8 2d0877b -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/reverdict-g1); it equals the unit list.

Unit change outside core: none (`coord.py`, `coord_lib` unchanged). Digest moved only through core.

Core part of the unit (shared; diff 479a8e8..2d0877b): `native.py`: `exempt=` now counts only for paths on the core allowlist `EXEMPT_PATHS = {"gate run"}` (`is_exempt`, used by `gate_problem` and `self reviews`; test_core_gate_units: another exempt writer stays gated); `switch_pending(commands, now)` reads "PID UTC-TIME" (bin/promote_lib.sh writes it) and ignores a pid whose `ps -o etime=` start is later than the file time + 2 s; old one-word files and an unreadable start fail closed (pid holds); `NativeRuntime.commands` gives the plugin registry from the runtime. `engine.py`: `_supersede` now loads, checks and saves each older run under that run's own `store.lock` (Busy -> the advance exits 75). `app.py`: `focus add|done` and `alarm ack` read-modify-write under `store.lock` (`locked`, Busy -> exit 75); `self reviews` uses `is_exempt`. `github/cli.py`: `ci cancel` refuses a non-digit run id before the gate. No force push, stash, auto-merge or old-tool call added; json stays one document.
- NOTE (core): a Busy on the second or later older run leaves the earlier ones already marked superseded; the retry finishes the job, nothing is lost.
- NOTE (core, carried): a parent package `__init__.py` on the import path is still outside the digest.
Tests: `sm gate run -- uv run pytest` on 25 files (core native, gate units, cli, engine, github cli, runlock, settings, json contract, promote review gate; plugin coord tasks/briefs/asks/handoff/skills/velocity/journal/docs/merges, guards lim/governor/loadguard/floor, lock, lock refuse matrix, plugin): 446 collected, 445 passed, 1 xfailed.

COVERS: beads stamp
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

COVERS: beads stamp
REVIEWED: sha256=7e5a59cd3fdf5f424ea4993fc194e26a1f1f9ddcfb9ca26cffd67a8db240f764
VERDICT: APPROVE

## Campaign re-verdict g1 (2026-11-14, independent; reviewer wrote none of this code)

Target: integration/sm-dod c36bbe1 (frozen), delta read against the live baseline 903a1b7, where this unit was approved (`git diff 903a1b7 c36bbe1 -- <unit files>`). Digest recomputed with `uv run sm self reviews` in a fresh worktree (branch review/campaign-g1-sm-dod); it equals the unit list of the sm-dod campaign.

Unit change outside core: `coord_lib/base.py` `child_env` now builds on `config.child_environment` and `search_path`: the same environment and PATH as before.

Core part of the unit (read as diffs 903a1b7..c36bbe1): `config.py` (`child_environment` gains `drop`/`drop_prefixes`, `search_path`, `variable` for a plugin setting's old environment name; defaults keep the old behaviour), `plugins.py` (`old_variable` metadata: the old variable sets a field only when no file or SM_ variable does, a bool only on its exact `on` value; `state_moves` hook), `hookspecs.py` (`sm_state_moves`), `helpdoc.py` (lint letters O and X; `sm self lint` passes), `models.py` (`StateMigration`), `native.py` (every gated write run, and every tick of a loop, holds the shared `writers.lock` in the state directory; readers, exempt writers and runs without a project do not; the switch-pending behaviour is unchanged: a single run exits 75, a loop skips the tick). No force push, stash, auto-merge, --admin or old hz-* call was added; `--format json` stays one document.
- NOTE: no test proves that a native write command takes the shared `writers.lock` (only the migrate side is tested, with a hand-held lock); a test around `native._command` would pin it.
Tests (one gated command each, by file; this repo has no xdist, so without `-n 2`): `sm gate run -- uv run pytest tests/test_core_statemove.py tests/test_core_migrate.py tests/test_core_gate_units.py tests/test_core_commands.py tests/test_core_native.py tests/test_gate_structure.py`: all passed; `sm gate run -- uv run pytest` on plugin coord asks/briefs/docs/handoff/journal/skills/tasks/velocity/velocity_moved, guards_lim, lock, lock_refuse_matrix: exit 0 (all passed, 1 xfailed).

COVERS: beads stamp
REVIEWED: sha256=2a377fc5edf68e4e5feaece358bde4ac9082f742260ab6f1e5e5e5977283cd8e
VERDICT: APPROVE

## Re-verdict at abc64a4, group C (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-abc64a4/C.md (recorded verbatim by the integrator).

coord.py `beads stamp` -> journal.stamp (dry-run path intact), coord_lib/journal.py unchanged; coord_journal tests.
COVERS: beads stamp
REVIEWED: sha256=474049f747d7b73732da992ca8d4541316ad8201db0739f6b85ca6e637108409
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does.
COVERS: beads stamp
REVIEWED: sha256=8cc2e25fda19739c1877d182174be9408965b013daa556d37d01eb0a523e99ea
VERDICT: APPROVE
