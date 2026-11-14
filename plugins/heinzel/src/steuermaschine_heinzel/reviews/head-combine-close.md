# Port review: `sm head combine-close`

For the independent reviewer. The command is off until this file names the code (`REVIEWED: sha256=<digest>` from
`sm self reviews`) and ends with the verdict line; the author of the port writes neither.
Review unit: `native/merges.py`, `native/merges_lib/`, `native/coord_lib/`.

## Exactly what it changes (only with `--apply`)
1. GitHub: `gh pr close MEMBER -R REPO --comment "merged via #X at <9 hex>"` for each member that is OPEN. Not retried.
2. The issue tracker (bd, run in the tracker directory): `bd note BEAD "merged via #X at <sha>; merged via, not yet
   released"` for at most five beads per member whose id appears in `bd search "#MEMBER"`.
No file of this tool. No bead is closed. Without `--apply`: reads only (`gh pr view`, `bd search`).

## Order
read the combine (`gh pr view X --json body,state,mergeCommit`); not MERGED or unreadable: refuse, nothing written.
Then per member, in the order of the `Combines #N` lines: read its state, close it if open, search its beads, note them.

## Differences from the old tool
| # | Difference | Why | Test |
|---|---|---|---|
| 1 | A failed close is shown (`CLOSE FAILED: reason`) and the exit code is 1; the old tool ignored it. | Honest output. | `test_a_failed_close_is_reported_and_exits_1` |
| 2 | An unreadable combine is a refusal with gh's reason, exit 1; was gh's stderr and exit 2. | Exit-code table. | `test_an_unmerged_or_unreadable_combine_is_refused_and_nothing_is_written` |
| 3 | A combine without `Combines #N` lines says so. | The old tool printed only the dry-run line. | `test_a_combine_without_members_and_the_review_gate` |
| 4 | The bead id pattern `management-xxx` is unchanged (it is the old tool's; it matches the tracker's ids without the first word). | Parity. | `test_apply_closes_open_members_with_the_comment_and_notes_the_beads` |

## What the reviewer should check
1. `members` is the old function (`python3 plugins/heinzel/tests/coord_verbatim.py`).
2. No write is reachable without `--apply` (`test_a_dry_run_lists_and_writes_nothing` asserts the call list).
3. Whether `bd note` still exists in the installed tracker (the old tool called it; not run during the port).
4. One dry run against a real merged combine (read-only).

## Change after the review (author, 2026-11-13): finding S4 fixed, the unit's digest changed
`merges_lib/combine_close.py` resolves the tracker directory once, before the first read and write. Without a configured
tracker `--apply` now refuses with exit 2 before anything is closed (`test_apply_without_a_configured_tracker_refuses_before_the_first_write`,
red before the change). Nothing else in the unit changed. The verdict below names the digest before this change and
is therefore stale until the reviewer names the new one.

## Independent review (2026-11-12, reviewer wrote none of the code)
Reviewed at head `09c9ce1` (unit unchanged at `1210f33`). Full findings: `REVIEW-INDEPENDENT-coord.md` in the slice's worktree.
- Without `--apply` only reads run (`gh pr view`, `bd search`); the tests assert the call list. With `--apply`:
  `gh pr close MEMBER -R REPO --comment` (not retried) for members whose state is OPEN, and `bd note` on at most five
  beads per member. No bead is closed. Read against the old tool (44 lines): same reads, same comment, same note text.
- Which pull requests can be closed: only numbers on `Combines #N` lines of the body of a pull request that GitHub
  reports as MERGED, in the same repository, and only while they are OPEN. An unmerged or unreadable combine refuses
  before any write. The combine itself cannot be closed (checked with a body that lists itself: it is MERGED, skipped).
  Membership is the body text; it is not checked against the merged commits (S12).
- `bd note` exists in the installed tracker (help read; not run).
- Exit 70 through the installed command tree without this verdict (run, with and without `--apply`).
- Condition of use (S4): configure `coord_document` or `coord_tracker_dir` first and do a dry run first. Without the
  setting, `--apply` closes the first open member and then exits 2 "the tracking document is not configured" (the
  tracker directory is resolved inside the loop, after the close; shown on fakes). Resolve it before the loop.
- Should fix, none blocking: S4, S12 (notes are written again on every `--apply`; `bd search` matches titles only,
  includes closed beads, by substring), S1 (gate test).
- Not verified: a dry run against a real merged combine.

## Independent review 2 (2026-11-13, reviewer wrote none of the code)
Reviewed an export of `6e4042e` (rebased on `d98aee1`). Full findings: `REVIEW.md` next to this file in the review hand-off (~/Code/tmp-sm-reviews/coord/).
- S4 fixed: the tracker directory is resolved before the first read or write (`combine_close.py:65`); the diff since
  the approved digest is that line and its two uses. The earlier findings and conditions (S12; dry run first) stand.
  The verdict of review 1 above named the digest before this change and is replaced by the lines below.

COVERS: head combine-close
REVIEWED: sha256=1c6ffc1f2db0deb05a0e144796b4ab0c7b8884d32e27735f01c4099523573440
VERDICT: APPROVE

## Re-review (campaign g3, 2026-11-13, Claude Opus 5.5; independent, wrote none of this code)

Why stale: the digest now follows imports into core (19 files). Diff 1d69c43..479a8e8: `merges.py`,
`merges_lib/*`, `coord_lib/*`, `steuermaschine/journal.py`, `steuermaschine/github/{base,client,forge,models}.py`
unchanged. Changed core in the digest: `config.py`, `engine.py`, `native.py`, `models.py`, `helpdoc.py`,
`protocols.py` (typing, `Environment`, import-following digest, `gate_problem`/`call`, the promote-switch wait,
read_only from `writes=`). None reaches the command's writes: `gh pr close` per open member and `bd note` only with
`--apply`, nothing on a dry run, refusal before any write when the combine is not MERGED or unreadable.
Tests through `sm gate run`: `test_native_combine*.py`, `test_native_coord_merges.py`: green.

Findings: none new. Earlier open point stands: `bd note` against the installed tracker is not exercised by tests.

COVERS: head combine-close
REVIEWED: sha256=3b707f62679b0e1736f717bcd83a765f9393e94c1b4d2f3ae7182e9f248a7260
VERDICT: APPROVE

## Re-verdict (campaign g3, 2d0877b, 2026-11-13, Claude Opus 5.5; independent, wrote none of this code)

Diff 479a8e8..2d0877b of the 19 digest files: `merges.py`, `merges_lib/*`, `coord_lib/*`, `journal.py`, `github/*`
unchanged. Changed: core `engine.py` (supersede takes the older run's lock) and `native.py` (`EXEMPT_PATHS`
allowlist, `NativeRuntime.commands`, promote pid-reuse check that fails closed); details in head-apply.md.
`head combine-close` is not exempt and runs no pipeline, so neither reaches its writes (`gh pr close` per open
member and `bd note`, only with `--apply`; refusal before any write when the combine is not MERGED).
Tests through `sm gate run` at 2d0877b: `test_native_combine.py`, `tests/test_core_native.py`,
`tests/test_core_gate_units.py`: green.

Findings: none new. Earlier open point stands (`bd note` against the installed tracker not exercised by tests).

COVERS: head combine-close
REVIEWED: sha256=7b65ac0ff62d96e0f732f53a32eade077fdb9e7d27ea90db3404a121d65ff9d8
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

COVERS: head combine-close
REVIEWED: sha256=277924f06d21d0c9f41c7ff4fc67e630c0beaffab6304089e9a58a030725947a
VERDICT: APPROVE

## Campaign re-verdict g3 (2026-11-14, independent; reviewer wrote none of this code)

Target integration/sm-dod frozen at c36bbe1; baseline 903a1b7 (every unit APPROVED there). Digests recomputed with
`uv run sm self reviews` in a fresh worktree at c36bbe1; they equal the campaign list. Delta read with
`git diff 903a1b7 c36bbe1 --` over the unit files and every imported core file.

Unit delta: `merges.py` and `merges_lib/` are unchanged. `coord_lib/base.py` `child_env` now builds the same
environment through `child_environment(PATH=...)` and `search_path()` (equal result: this process's environment with
the configured tool directories in front of PATH). The rest is the shared core below.

Findings: none.

Shared core delta 903a1b7..c36bbe1 (in every g3 digest): `config.child_environment` gains `drop`/`drop_prefixes`
(callers that dropped `GIT_*` by hand now use it; same result), `search_path`, `variable`; `native._command` runs each
write under `writers_lock` (shared flock in the project state directory; `sm self migrate-state` takes it exclusively),
the switch-pending check moved inside it and still fails 75 for a single run and skips a loop tick; the review gate
(`gate_problem`) is unchanged and still runs first. `statemove.require_migrated` refuses while an old `~/.cache/hz-*`
path still holds state or both paths hold state, so the state-path move fails closed. helpdoc adds lint letters only.
`models` adds StateMigration only. No force push, stash, auto-merge or new write path without the digest gate.

Tests through `sm gate run` at c36bbe1, by explicit file (39 files: `test_native_admission_*` (14),
`test_native_combine{,_cov_b_plan}.py`, `test_native_coord_merges.py`, `test_native_dispatch{,_cov_a_focused,_focused}.py`,
`test_native_lock{,_refuse_matrix}.py`, `test_native_merge_tier_{rules,validate}.py`, `test_native_packet*` (9),
`test_pipeline_head_admit_merge_tier.py`, `tests/test_core_{native,runlock,statemove,window,gate_units}.py`): exit 0,
about 1006 passed, 26 skipped, 0 failed. Serial run: this repository has no pytest-xdist, so pytest refuses `-n 2`.

COVERS: head combine-close
REVIEWED: sha256=8b9f5edd5f6a9d3b0c972518baae6c4221a5633150244d422976e4f9957eb8bd
VERDICT: APPROVE

## Re-verdict at abc64a4, group A (2026-11-14)

Independent reviewer; wrote none of this code. Baselines, shared core and tests: ~/Code/packets/sm-reverdict-abc64a4/A.md (recorded verbatim by the integrator).

Delta: `coord_tool_path` defaults to [] (R5): children bd/gh/git get PATH as is; a missing tool fails the command,
it does not widen anything. Core as above.

COVERS: head combine-close
REVIEWED: sha256=2128a673cbe0b9c1834c9e26fefa1f4796a41651fe38b21577991ef14f1deb9c
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does.
COVERS: head combine-close
REVIEWED: sha256=3d105ad88cffc579660e931a4e3e2a4f8a4b5f7f960f3bfb6e7f3b6fe4aeaf9c
VERDICT: APPROVE
