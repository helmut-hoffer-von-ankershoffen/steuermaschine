# Port review: `sm head apply` (and the read commands it depends on: `head prepare`, `head hash`, `head blockers`)

For the independent reviewer. `head apply` is the one command of this piece that writes: one GitHub repository
variable (a focused plan slot of the registry that decides which tests protect main). It is unavailable until this
file ends with the verdict line; the author of the port does not write that line.

Scope of this review: the single-packet path, including `prepare --wait`. Not in scope yet: batch as a native
command, the release-main route, the `head admit` pipeline.

Registration: `head hash`, `head blockers`, `head prepare` are `writes=False`; `head apply` is `writes=True` with this
file. `head prepare` writes two local files next to the packet and sends nothing to GitHub but reads (change 2.5);
that it counts as a read command although `head apply` consumes its files is the coordinator's decision of
2026-11-12, and apply trusts neither file (section 3, rows 3.2 and 3.3).

## 1. What moved verbatim, and how to check it

```
python3 plugins/heinzel/tests/admission_verbatim.py diff      # every moved file against its original
python3 plugins/heinzel/tests/admission_verbatim.py sums      # sha256 of the originals; compare with reviews/admission-originals.sha256
```

The script applies its `RULES` (import lines and one renamed file reference, all listed in the script) to the original
and prints a unified diff against the moved file. Result at this commit:

| Moved file (`native/admission_lib/`) | Original | Result |
|---|---|---|
| `focused_slots.py` | installed `hz_focused_slots.py` | 3 hunks: changes 2.4 and 2.5 |
| `focused_resulting.py` | installed `hz_focused_resulting.py` | VERBATIM |
| `focused_update.py` | installed `hz_focused_update.py` | VERBATIM |
| `focused_replay.py` | installed `hz_focused_replay.py` | VERBATIM |
| `focused_admit.py` | installed `hz_focused_admit.py` | 2 hunks: changes 2.1 and 2.4 |
| `tests/test_native_admission_batch.py`, `_resulting.py`, `_stuck_queued.py` | installed `tests/test_focused_*.py` | VERBATIM |
| `tests/test_native_admission_slots.py` | installed `tests/test_focused_slots.py` | 4 hunks: changes 2.2, 2.3, 2.4 |

"Installed" is the working tree of the shared tools directory (untracked there); its bytes at move time are pinned by
the sha256 list. All moved tests pass in one run and each file alone.

## 2. Changes to moved code

| # | Change | Why | Test |
|---|---|---|---|
| 2.1 | `focused_admit._management_core()` returns `identity.CORE` instead of loading `hzlib.py` from the tool's directory with `importlib`. Same shape check (`OWNER/NAME`), same refusal text. | The plugin has no tools directory. `identity.py` carries the repository identity and the PR key shape (`split_key`), copied from `hzlib.py` (lines `CORE`, `SHORT`, `split_key`); the moved code still calls it `hzlib`. | every moved test that builds a `Runtime` or calls `_canonical_source`; mutation "repository identity must be OWNER/NAME" |
| 2.2 | Three moved tests started the old script files as processes (`hz_focused_admit.py --apply`, the symlinked entry with `--help`, `hz-focused-admit prepare missing ...`). No script files exist; see 2.4 for what they do now. | | themselves |
| 2.3 | The `env` fixture returns deep copies of the module-level `PLANS`. | The fixture handed out the shared dicts; a test that edited its packet changed them for later tests. This is the known order dependence (2 stuck-queued tests failed with "main changed" when the files ran together, before any port). | the combined run is green |

| 2.4 | The `__main__` blocks at the end of `focused_slots.py` (`raise SystemExit(main())`) and `focused_admit.py` (`raise SystemExit("Component only: ...")`) are removed. `focused_replay.py` keeps its block: `focused_resulting.replay_original` copies that file and runs the copy as a script. | `python -m ...focused_slots apply ...` was a working write entry outside the review gate (independent review, B2). `main(argv)` stays a function for the moved tests and the parity driver; no file, console script or module entry reaches it. Moved tests adjusted: `test_component_has_no_generic_write_entry` (was: refuses with 'Component only'; now: running the module does nothing and the file has no `__main__`), `test_symlink_installed_entry_supports_help` and `test_update_cli_requires_full_sha_and_positive_pr` (call `main` in process, same assertions). | those three, `test_module_entry_is_not_a_second_door_to_the_write`, `test_the_replay_driver_runs_as_a_script` |
| 2.5 | `focused_slots.prepare(..., *, probe=True)` passes `probe` to its `fresh_guard` (2 lines). Default unchanged. | So that the command form of prepare can run without the cancel probe (a POST). The probe is one of the checks of a stuck-queued receipt; with `probe=False` every read check of the run still holds (queued, no conclusion, no jobs, old enough, head in no registry), exactly what the moved guard does after the write. The transaction bytes do not depend on it. `apply` re-prepares with the default (`probe=True`) and probes again in both guards before the write. | `test_moved_prepare_passes_probe_to_its_guard`, `test_the_command_form_of_prepare_sends_no_cancel_probe`, `test_apply_reprepares_with_the_probe_and_the_stored_arguments`; parity driver: native prepare 0 probes and byte-equal transaction, native apply 3 x receipts (= former apply), former prepare 1 x receipts |

Nothing else in the moved modules was touched. In particular `apply_prepared`, `fresh_guard`, `check_consumers`,
`registry_io`, `apply_slots`, `_write_receipt`, `Runtime` are byte-identical to the originals, and `prepare` differs
by the one keyword of 2.5.

`identity.py` is new and not covered by the diff script: `CORE`, `SHORT`, `split_key` copied from the former `hzlib.py`.
It is a reviewed constant, not a setting; `test_the_repository_identity_is_the_reviewed_constant` pins the values.

## 2b. Change 3 (after the first verdict): two changes for the release-main route

Made after the verdict on digest 1ad47247; they change the digest, so this file's verdict lines are stale until an
independent reviewer re-issues them. The exact hunks are `reviews/release-admit-frozen.patch` (7 hunks, applied in one
commit of their own); the route itself is reviewed in `reviews/release-admit-apply.md`.

| # | File | Hunks | What | Why | Test |
|---|---|---|---|---|---|
| 2.6 | `focused_admit.py`, `apply_slots` | 5 | New keyword `release_main` next to `pairs`. `singleton = resulting_main if release_main is None else release_main`; both given: "conflicting singleton admission proof types". `mode` is the resulting-main mode unless `release_main` is given. The pair-count check reads `singleton is not None and pairs != 1` (was `resulting_main is not None and pairs != 1`); the scope check `singleton is None and len(plans) != 2 * pairs`; the non-singleton branch (exact counts of PR and push-tree keys per pair) is unchanged text; the singleton branch and the two later tests read `singleton` where they read `resulting_main`; the return is `result_receipt if release_main is not None else _write_receipt(result_receipt, home)`. | Port plan 3.1, the operator copy's `release_main` merged with the installed batch `pairs`. | `test_native_release_admit_ops.py`: pair count other than one refuses, both singletons refuse, mode and manifest must be exact; moved batch tests; moved release tests |
| 2.7 | `focused_resulting.py` | 2 | The trusted-decoder request carries a `required` map instead of `original_key` and `original_sha256`; `registry_proof` is rewritten on `_registry_witness`; new `release_registry_proof`. The operator copy's change, a three-way merge without conflict. | The release route proves that its one-entry append keeps every old entry. | moved `test_native_admission_resulting.py` (unchanged), moved release tests |

| 2.8 | `focused_slots.py`, `prepare_batch` | 1 (2 lines) | `prepare_batch(..., *, probe=True)` passes `probe` to its `batch_guard`. Default unchanged; the same change as 2.5, for the batch. | So that the command form of the batch prepare sends no cancel probe either. `apply_batch` is unchanged and its guard probes before the write. | `test_moved_prepare_batch_passes_probe_to_its_guard`, `test_prepare_batch_stores_transaction_and_arguments_and_sends_no_probe`, `test_apply_batch_recomputes_compares_and_applies_once` |

New code in the same second review (`ops.py`, `admission.py`; `tests/test_native_admission_batch_ops.py`, 28 tests):

| # | Addition | Test |
|---|---|---|
| 3.10 | Batch as commands. `sm head prepare --packets A --packets B --core-repos WT_A --core-repos WT_B [--prs N --prs 0] --review-root ROOT --out DIR` calls the moved `prepare_batch` with the entries of the former `--packets` branch (same checks: one checkout and one PR number per packet, 0 = pre-open; single-packet options excluded) and stores `prepare.out` and `prepare.args.json` in DIR. `sm head apply DIR --approved SHA` (a directory instead of a packet) verifies the stored arguments against the transaction before anything runs (mode, digest, member count, each member's PR and packet sha256 in order, the receipts), checks every member's worktree head (3.4), recomputes with the moved `prepare_batch`, requires equality, and calls the moved `apply_batch`. `--wait` works as for one packet. | the tests named above and `test_apply_batch_refuses_*`, `test_command_*` |
| 3.11 | The batch rule of 2026-11-12, stated and checkable. A batch admits each member's pair for the main it was prepared on; the first merge moves main, and then the resulting tree of every other member has no plan: its merge turns main red. (a) The result of a batch prepare prints the rule (`FIRST_MERGE_ONLY`). (b) `ops.first_merge_only(prepared, current_main)` refuses once main moved and names the way on (bind each remaining head to the new main, admit it on its own). **Open, for the coordinator and the reviewer:** nothing in this slice merges, so (b) is a library check that the queue has to call before it merges a member of a batch; and "chained plans" in the sense of admitting, in the same transaction, plans for the later members on the predicted mains is NOT built: the moved `prepare_batch` requires every member's base to be current main, and a PR plan binds a base commit that does not exist before the earlier member merged. | `test_first_merge_only`, `test_first_merge_only_holds_for_the_recorded_batch` |

With `release_main` absent (every `head apply`): `singleton` is `resulting_main`, `mode` is `"resulting-main-only/v1"`,
and each rewritten condition is the former condition with the name substituted, so the same branches are taken and
`_write_receipt` is called as before. Evidence that ordinary heads behave byte-identically: with the hunks applied,
the prepare parity (8 of 8) and the replayed applies against the real receipts (7 of 7, and the 7 hermetic ones) pass
unchanged, as do all moved PR admission tests. `focused_slots.py` is touched by change 3 only in `prepare_batch` (2.8).

Verbatim diff after change 3, as `admission_verbatim.py diff` counts hunks (one line of context): `focused_admit` 8 (2.1, 2.4, six for 2.6),
`focused_slots` 5 (2.4, 2.5 twice, 2.8 twice), `focused_resulting` 5 (2.7) against the installed original; against the operator copy's `hz_focused_resulting.py` it is identical after the
import rules (`diff <(sed rules) ...`, or `admission_verbatim.py diff --installed <operator tools>` for that one file).

## 2c. Change C of 2026-11-13: stuck-queued receipts from the list rows, one full proof before the write

Design and its stated weakening: `heinzel-management/research/fast-fix-recut-2026-11-13.md`, section C. Voids the
verdicts of this file (195adb07) and of `release-admit-apply.md` (53b56f03); one independent review covers both.
New module `admission_lib/receipt_guard.py` (`row_failure`, `vanished`, `parallel`, `pre_write`, about 60 lines).

| # | File, function | What |
|---|---|---|
| C.1 | `focused_slots.py`, import | `from . import receipt_guard` |
| C.2 | `active_runs(rt, rows=None)` | `inventory(keep)` also stores the second scan's full list row per run into `rows`; membership and drift check unchanged |
| C.3 | `check_consumers(..., probe=True, full=True)` | Default (`full`) = today's per-run proof, its loop body moved verbatim into `authenticate_run` (`continue` -> `return`), run on 8 threads, first refusal in run order raised after all finish; registries read once up front. `full=False`: a stuck-queued receipt is accepted only from its list row: exact pinned identity, queued, no conclusion, repository, allowlist, age, `updated_at == created_at == run_started_at`, head in no live or desired plan; a receipt run absent from the inventory refuses (new); no per-run call, no cancel POST. Other receipt kinds keep the per-run proof |
| C.4 | `fresh_guard`, `global_guard`, `batch_guard` | pass `full` (default `False`, list-only) |
| C.5 | `apply_prepared`, `apply_batch` | guard passes `full=receipt_guard.pre_write(written)()`: True only on `apply_slots`' second guard while nothing is written, the one directly before the write |
| C.6 | `release_slots.py`: `release_guard(..., full=True)`; `prepare_release` passes `full=False`; `apply_release`'s `guard(full=False)`, `check_fresh=lambda: guard(full=pre_write())` | the release route: four list-only guards, one full proof before the write, no cancel POST after it |
| C.7 | `release_admit.py` | help and docstring: prepare sends no cancel probe |

Tests: `test_native_admission_receipt_guard.py` (22, FakeGh with 67 zombies plus the moved fakes); fixture rows of
the moved stuck tests and the parity driver gain `updated_at` (the real zombies have it); the parity driver expects
67 probes per apply instead of 201 and none in prepare; two fake guard signatures accept `full`.

## 3. New code: `native/admission_lib/ops.py` (about 220 lines) and `native/admission.py` (the four registrations)

`ops.prepare` and `ops.apply` call the moved `focused_slots.prepare` and `focused_slots.apply_prepared`. They replace
the single-packet branch of `focused_slots.main` (which is still there, unchanged, as the argv form used by the moved
tests and by the parity driver). Line by line against that branch:

| Former `main` | `ops` | Test |
|---|---|---|
| `require(args.pr is None or args.pr > 0, 'PR must be positive')` | `_checks`, same text | `test_prepare_refuses_bad_options_before_any_read` |
| resulting-main-only requires a PR and excludes protected producer | `_checks`, same text | same |
| `update.validate_option(args.pr, args.expected_remote_head)` | `_checks` | same |
| argparse exclusivity of `--expected-remote-head` and `--resulting-main-only` | `_checks`, explicit refusal | `test_exclusive_modes_refuse` |
| receipts from `--nonconsumers` via `_stable_file` + `_json_bytes` | `_receipts`, same calls | `test_prepare_stores_...` |
| `prepare(rt, packet, review_root, protected=, pr=, nonconsumers=, expected_remote_head=, resulting_main_only=)` | same call, same keywords | parity on 8 recorded packets |
| `print(a.canonical(prepared).decode())` | `prepare.out` = that text + newline, written atomically next to the packet | parity: byte-equal to the recorded files |
| apply: `approved = _json_bytes(_stable_file(args.prepared))`; `require(approved == prepared, 'prepared transaction changed; reprepare and review')` | same check, same text, same order (after the recomputed prepare) | `test_apply_refuses_when_the_recomputed_transaction_differs_and_never_reaches_the_write` |
| `except (AdmissionError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError)` -> refused, exit 1 | `native/admission.py: ERRORS`, same six, -> `Refuse` (exit 1) | `test_a_timeout_of_a_process_is_a_refusal_not_a_traceback` |
| `apply_prepared(rt, prepared, packet, source, proofs, args.approved_sha256, snapshot=workspace / 'repo')` | same call | replayed apply: receipt equal to the real one |
| `finally`: remove `source` and `workspace` | `_cleanup` | not tested directly |

Added (semantic changes, each with its test in `tests/test_native_admission_ops.py`):

| # | Addition | Test |
|---|---|---|
| 3.1 | `prepare` writes `prepare.args.json` next to `prepare.out`: its own arguments with absolute paths and the digest of the transaction. | `test_prepare_stores_the_transaction_and_its_arguments_next_to_the_packet` |
| 3.2 | `apply PACKET --approved SHA` reads those arguments. **They are not trusted** (`check_stored_args`): before any process starts, each one is compared with a field the approved digest covers: `sha256` = approved digest; `pr` = `prepared.pr`; `protected` = `prepared.protected_producer`; `resulting_main_only` = presence of `prepared.resulting_main`; `expected_remote_head` = `prepared.pr_update.old`; sha256 of the packet file = `prepared.packet_sha256`; receipts of the nonconsumers file = `prepared.nonconsumers`. What has no plain field is covered by the unchanged recompute-and-compare: `review_root` (the packet pins the review's sha256, every nonconsumer receipt pins its review's sha256, both inside the transaction) and `core_repo` (repository from its origin = `prepared.repository`; its HEAD = `prepared.head`, checked by 3.4 and again by every guard). A changed arguments file therefore either leads to the identical transaction or refuses. | `test_apply_refuses_changed_stored_arguments_before_anything_runs` (8 cases), `test_apply_refuses_a_changed_packet_or_another_receipts_file` |
| 3.3 | `apply` refuses, before anything else runs, when `--approved` is not 64 lowercase hex or is not the digest of the stored `prepare.out`. (Before: found later by `apply_prepared`; that check is still there.) | `test_apply_needs_the_exact_approved_digest_and_stored_arguments` |
| 3.4 | One worktree per head: `apply` refuses when `git rev-parse HEAD` of the worktree is not `prepared.head`, and the message says how to fix it: a new worktree on its own branch at that SHA (`git worktree add -b admit/<head12> ../admit-<head12> <head>`; a detached one would fail the pre-open proof), not moving this one back. The moved guard "local reviewed head changed" stays behind it. Skipped for resulting-main-only, where the worktree is at the original head by design. | `test_apply_refuses_a_worktree_that_left_the_reviewed_head_and_names_the_fix` |
| 3.5 | The single-packet apply refuses a batch transaction (`mode` present). | `test_apply_refuses_a_batch_transaction` |
| 3.6 | The digest in one place: `ops.canonical_sha256(value)` = `focused_slots.digest(focused_admit.canonical(value))`, the two moved functions the write path itself uses. `sm head hash` prints it. Equal to `steuermaschine.hashing.canonical_sha256` on all recorded files. | `test_native_admission_recorded.py` |
| 3.7 | Not offered natively: `blockers --cancel-blocking-pr-runs` (it ran another script). `report_blockers(..., cancel=True)` is still in the moved module and unreachable from a command. | none |
| 3.8 | `prepare --wait` (`wait.py`, 80 lines): polls the read-only run inventory (`report_blockers`, two scans, no POST) until no uncovered run is active, naming the runs on stderr; then runs the identical prepare. Retried: "truncated inventory" and "active run membership drift" (in the poll or in prepare) and a run that appeared between poll and prepare. Every other refusal is raised unchanged. After `admission_wait_max_minutes` (45) it exits 75 (`Wait`) naming the run. Time comes from `providers.clock`. Prepare writes its files only on success, so a retry leaves nothing behind. | `test_native_admission_wait.py` (10 tests), `test_head_prepare_wait_exits_75_and_names_the_run` |
| 3.9 | The hint `sm head blockers ...` is attached only to refusals about active runs. | `test_head_prepare_without_wait_refuses_at_once_with_the_blockers_hint` |

## 4. Deviations from docs/INTEGRATION.md section 2, accepted for this pass, to be removed in the next

The moved code keeps its own edges, exactly as in the former tool:

| Rule | Where it is broken | Note |
|---|---|---|
| processes only through `providers.commands` | `focused_admit._run`, `_isolated_resolve`, `_isolated_slot_append`, `_gh_*`; `focused_slots.Runtime.run`; `focused_update`; `focused_resulting` use `subprocess.run` | They pass bytes on stdin and read bytes (the slot value, `git diff-tree -z`, the isolated resolver). `Commands.run` has no `input` and returns text. |
| GitHub only through the `Gh` client | `Runtime.api/pages/variables/set_variable/cancel_probe` build `gh` argv; `focused_admit._read_main` | `Runtime` is the whole IO boundary and every test replaces it; mapping its five methods to `steuermaschine.github` is the next step. The variable is written with `gh variable set NAME -R REPO` and the value on stdin. |
| time through `providers.clock` | `time.time`, `time.monotonic`, `time.sleep` in `Runtime` (queue pause and restore loops) and `focused_admit._lock` | |
| no `os.environ` | `focused_slots.prepare` refuses when `HZ_QUEUE_STATE`, `HZ_TRAIN_STATE_DIR` or `HZ_RELEASE_DIR` is set; `_run` strips `GIT_*` for children | the first is a guard, the second is the allowed pass-through |
| no paths under `~/.cache`, no old names in state paths | receipts `~/.cache/hz-focused-admission-receipts`, locks `hz-queue.lock`, `hz-train/train.lock`, `hz-release.lock`, `hz-focused-pr-registry.lock`, flags `hz-merge-freeze`, `hz-release-holds`, work directories `hz-focused-validator`, `hz-focused-admission-work`, the PR actions file under `~/Code/heinzel-management/tools`, the queue agent label and plist | Kept on purpose: plans applied by the former tool stay valid. **Cross-slice rule:** apply excludes the queue, the train and a release only through these lock files and this LaunchAgent label. A native queue, train or release that takes a store lock instead runs concurrently with an apply without any refusal; a queue under another label makes apply refuse (closed). These paths and the label must stay shared until admission moves with them in one reviewed step. |
| interpreter of the isolated resolver and slot codec | `sys.executable`: formerly the `python3` on PATH (3.15), now the interpreter of the `sm` install (3.14 here) | The slot value is gzip output. Parity on this machine: the native port under 3.14 reproduces, byte for byte, the 8 transactions the former tool produced under the system interpreter. Another install must repeat the parity run. |
| settings typed | limits (`BUDGET`, `MAX_VAR`, slot names, `MAX_BATCH`) are module constants | not settings yet |
| digest from `steuermaschine.hashing` | `ops.canonical_sha256` uses the moved functions | proven equal on the recorded files |
| locks through `providers.store.lock`, no `fcntl` | `focused_admit._lock` (moved) | the lock files are shared with the queue, train and release (see the cross-slice rule above) |
| receipts through `providers.store.put_receipt` | `focused_admit._write_receipt` (moved) | same directory and file format as the former tool, on purpose |
| state through `providers.store` | NEW code: `ops.prepare` writes `prepare.out` and `prepare.args.json` as files next to the packet | the reviewer reads and hashes exactly that file; apply trusts neither (3.2, 3.3) |
| processes through `providers.commands` | NEW code: `ops.check_worktree_head` starts git through `focused_admit._run` | the same call the moved guard makes; `Commands` cannot replace `_run` yet (bytes) |
| old names never printed | refusal texts of the moved code are unchanged; `report_blockers(cancel=True)` names a script (unreachable) | |

## 5. Evidence

- `uv run ruff check plugins/heinzel`: no finding in the admission area. The moved `focused_*.py` and their four moved
  test files are exempt (`pyproject.toml`, per-file ignores and a mypy override), so that they stay diffable against
  the originals. `mypy --strict`: `ops.py`, `wait.py`, `identity.py` clean (ops disables `no-untyped-call` for the
  calls into the moved modules); `admission.py` has only the core's "untyped decorator" for `@native`.
- Moved tests: slots, batch, resulting, stuck-queued: all green in one run.
- New tests: `test_native_admission_ops.py`, `test_native_admission_recorded.py`.
- Parity: `parity/head-prepare.md`, `parity/head-apply.md`. Prepared transactions byte-equal on 8 of 8 recorded
  single packets (former tool, native, argv form); replayed apply reproduces 7 of 7 real receipts.

## 6. Mutation checks on the guards of this piece

`uv run python plugins/heinzel/tests/admission_mutants.py` (each mutant weakens one guard; the listed tests must fail). Run 2026-11-12: **30 killed, 0 survived** (the module-entry mutant left the list with change 2.4: there is no entry to weaken; 29 remain in the script).

| Mutant | Result |
|---|---|
| approved digest must be the stored transaction digest | killed |
| approved must be 64 hex | killed |
| args sha256 bound to approval | killed |
| args pr verified | killed |
| args protected verified | killed |
| args resulting-main verified | killed |
| args expected remote head verified | killed |
| args packet bytes verified | killed |
| args nonconsumer receipts verified | killed |
| batch transaction refused by single apply | killed |
| worktree must be at the reviewed head | killed |
| worktree check skipped | killed |
| stored args belong to this packet | killed |
| missing args file refused | killed |
| recomputed transaction must equal the reviewed one | killed |
| PR must be positive | killed |
| digest newline excluded (canonical form) | killed |
| repository identity must be OWNER/NAME | killed |
| registry snapshot must equal expected (moved guard, sanity) | killed |
| command prepare sends no probe | killed |
| moved prepare passes probe to its guard | killed |
| module entry of focused_slots refuses | killed |
| exclusive modes refuse | killed |
| wait: only truncation and drift are transient | killed |
| wait: limit ends the wait | killed |
| wait: prepare does not run while a run blocks | killed |
| wait: other refusal in the poll is raised | killed |
| command: process timeout is a refusal | killed |
| command: StillWaiting becomes exit 75 | killed |
| command: head apply is a write command | killed |

First run (before the tests were tightened): 14 killed, 5 survived (64-hex check, resulting-main and expected-remote-head
argument checks hidden behind a later refusal with a similar text, the recompute comparison, the identity shape). Each got
its own test. "wait: limit ends the wait" loops forever by construction; it was killed by ending the hanging test run
(the script now counts a timeout as killed).

## 6a. Answers to the independent review of 8cd75dd

| Finding | Answer |
|---|---|
| B2 module entry writes outside the gate | closed: change 2.4 |
| S1 `head prepare` registered as read | prepare no longer sends anything but reads (2.5); registration explained at the top |
| S2 `SubprocessError` | added to `ERRORS`, tested |
| S3 detached worktree in the fix hint | the hint names the `-b` form (3.4) |
| S4 tampered nonconsumers path probed before refusal | closed: `check_stored_args` compares the receipts with the transaction before anything runs (3.2) |
| S5 identity docstrings, pin test | docstrings corrected; pin test added |
| S6 help names `--wait` | `--wait` exists (3.8) |
| N7 hint on every refusal | 3.9 |
| N3 shared lock paths | stated in section 4 |
| N4 interpreter | stated in section 4 |
| N1 leftover test name `test_symlink_installed_entry_supports_help` | kept: moved tests keep their names; it now tests `--help` of the argv form |
| B1, N2 (core gate) | closed in core (2913b2c): no settings switch, verdict bound to the code digest (section 8). |

## 7. Unverified

- No command of this piece was run against live GitHub, launchd or the real registry.
- `Runtime` (the real `gh`, `launchctl` and lock calls) is the moved code; no test executes it. It was not executed
  by its original tests either.
- Modes without a recorded transaction (`--pr`, PR update, resulting-main-only) rest on the moved tests.
- The review gate itself (core) is outside this slice.

## 8. What the verdict is bound to

The core binds a verdict to the code: `sm self reviews` prints the sha256 over `native/admission.py` and every file
under `native/admission_lib/`. An approval is the line `REVIEWED: sha256=<that digest>` plus the exact last line
`VERDICT: APPROVE`; any later change to one of those files voids it. The author of the port writes neither line.

## 9. Independent review (2026-11-12, reviewer wrote none of the code)

Reviewed: commit `99fab40` (on main `2913b2c`), exported with `git archive`; every run below used that export, never
the builder's working tree. No GitHub call, no registry write, no real apply. Full notes with file:line:
`REVIEW-INDEPENDENT-admission.md` in the root of the admission worktree (untracked).

COVERS: head apply
REVIEWED: sha256=1ad47247619aae87bf967571b94654c4f9f1f2684e93f2df3042ecc5ef34cb6d

That digest is over `native/admission.py` and the 9 files of `native/admission_lib/` (my own computation and
`sm self reviews` agree). A change to any of them, or a new `.py` under `admission_lib/`, voids this verdict.

What I checked myself:

- Verbatim: raw `diff` of the five originals against the moved modules, no normalisation. Only import lines, the one
  renamed file reference, and changes 2.1, 2.4, 2.5 differ. `identity.py` equals the former module in `OWNER`, `CORE`,
  `PLUGINS`, `SHORT` (executed) and `split_key` (AST). Originals' sums equal the pinned list. Moved test counts equal
  the originals (167, 30, 51, 61); no changed test weakens an assertion.
- Digest: 10 recorded `prepare.out`: native digest = the former tool's = sha256 of the bytes without the final
  newline = core hashing = `transaction_sha256` of a real receipt; 3 also equal the sha256 in their `APPLY-REVIEW.md`.
- Gate: through the real entry point `sm head apply` exits 70 with an open review, plain and json; no environment
  variable or settings key opens it. `python -m` on `focused_slots` and `focused_admit` does nothing.
- 5a: former tool 8 of 8 and native prepare 8 of 8 byte-equal to the original recordings and to each other.
- 5b: replayed `ops.apply` on the 7 applied cases: receipt equal to the real one except `admission_id`; one write.
- `probe` (2.5) cannot make apply skip a guard: it reaches only the cancel POST in `stuck_queued_failure`; apply
  re-prepares with the default and `apply_prepared` is unchanged. Ordered log of the replay: re-prepare with 67 probes,
  pause, guard with 67 probes, read, guard with 67 probes, read, the write, readback, guard without probe, restore.
- Stored arguments (3.2): every argument that is a field of the transaction is checked before any process starts.
  `core_repo` and `review_root` are not fields; they cannot change what is written and equal what the operator typed
  into the former CLI.
- Mutants: 28 killed, 0 survived, 1 pattern stale; that one run by hand: killed.

Deviations from docs/INTEGRATION.md section 2 that section 4 does not name; they are accepted for this pass on the
same terms and belong in that table:

| Rule | Where it is broken |
|---|---|
| locks through `providers.store.lock`, no `fcntl` | `focused_admit._lock` (moved) |
| receipts through `providers.store.put_receipt` | `focused_admit._write_receipt` (moved) |
| state through `providers.store` | NEW code: `ops.prepare` writes `prepare.out` and `prepare.args.json` as files next to the packet |
| processes through `providers.commands` | NEW code: `ops.check_worktree_head` starts git through `focused_admit._run` |

Open, none blocks this command:

- `tests/admission_mutants.py`: the pattern of "command prepare sends no probe" no longer matches `ops.py`; the
  script stops there. Section 6 reports a count from an earlier state.
- The fix hint of 3.4 does not fit a PR update (that mode needs the PR's own branch).
- A failure after the write (readback mismatch, queue restore unverified) is printed as "refused", as before.
- `--pr`, PR update and resulting-main-only have no recorded transaction. The real `gh`, `launchctl` and lock calls
  are byte-identical to the former tool and executed by no test: watch the first live apply, keep the former tool
  installed until then.
- Not reviewed: batch as a command, the release-main route, packet building, `head admit`.

VERDICT: APPROVE

## 10. Independent re-review after the release route and batch (2026-11-13, reviewer wrote none of the code)

Reviewed: commit `d83f477` (branch `slice/heinzel-native-admission`, on top of `44e4152` and `216c11b`), exported with
`git archive`; every run used that export. No GitHub call, no registry write, no real apply. The 44e4152 hunks rest on
the second reviewer (`REVIEW-INDEPENDENT-admission-route.md`), which blocked only on B1 (head apply sent a directory to
`ops.apply_batch`).

- B1 fixed: `head_apply` raises `Refuse(..., code=USAGE)` when PACKET is a directory, before the `try` and before any
  process or `gh` call; it then calls only `ops.apply`. Grep of the unit: `ops.apply_batch` has no caller left in
  `native/`; `focused_slots.apply_batch` is reached only from `ops.apply_batch` and the legacy argv `main` (not wired to
  any command). `ops.apply` itself refuses a transaction with `mode` ("this is a batch transaction"), so a batch
  `prepare.out` next to a packet file cannot reach the batch write either.
- New test `test_native_admission_apply_refuses_batch.py`: exit 2, refusal text in text and json form, no gh call,
  no process (subprocess patched to fail), batch dir bytes unchanged, also with the review state approved; a packet
  file still reaches `ops.apply`. Passed.
- Tests 216c11b..d83f477: only `test_command_apply_takes_the_batch_directory` removed (it asserted the blocked
  behaviour) and the module docstring changed; no other assertion touched. The remaining batch-ops tests pass (32 with
  the new file).
- 44e4152 spot check, `focused_admit.apply_slots`: with `release_main=None`, `singleton = resulting_main` and
  `mode = "resulting-main-only/v1"`, every condition reads as before, and the receipt is still `_write_receipt(...)`.
  Exactly the old path.
- Parity and replayed apply in the export (`SM_ADMISSION_PARITY_OBJECTS=~/Code/heinzel`, through `sm gate run`): 15 passed.
- Digests from `sm self reviews` on the export: head apply `195adb07…030f`; release admit prepare/apply unchanged at
  `53b56f03…a209`.

Open, none blocks this command:

- Section 3.10 above still describes `sm head apply DIR` as working; the code refuses it. Update 3.10 when batch apply
  is made ready (first-merge-only, chained plans) and review it then as its own change.
- Everything listed open in section 9 still stands.

COVERS: head apply
REVIEWED: sha256=195adb07fe963dfa42a2171526d43f5bf2441c7502fb2ade781c2d52156d030f

VERDICT: APPROVE

## 11. Independent review of change C, 7522853 (2026-11-13, reviewer wrote none of the code)

COVERS: head apply
REVIEWED: sha256=26ccebd6193f3fe1f3b1d98d8edce4a9877620e8d8deadcef1f1ad687937029f

Findings (paths under `native/admission_lib/`):

- Full proof before every write: `apply_slots` writes only at `focused_admit.py:703`, right after the second
  `check_fresh()` (`:699`, re-read `:701`). `pre_write` (`receipt_guard.py:61-74`) is True exactly on call 2 with
  nothing written; `apply_prepared` (`focused_slots.py:626-629`) and `apply_batch` (`:748-749`) pass `full=full()`
  and hand that guard only to `apply_slots`. No write follows a list-only guard alone.
- `authenticate_run` (`focused_slots.py:416-429`) diffed against the 7522853^ loop body: identical except
  `continue`->`return` and the hoisted `desired`/registries check (`:398-401`, same refusal, once).
- Threading: `parallel` (`receipt_guard.py:51-58`) re-raises the first failure in inventory order after all finish;
  nothing swallowed. `Runtime.run` is a stateless `subprocess.run`. Difference: later runs are still read/probed after
  an earlier refusal; probes are only sent after every read check passes, so only 409-answering zombies are touched.
- Uncovered active run: `blocking_runs` refusal (`:391-394`) unchanged and before both modes.
- Touched receipt (left queued, timestamps differ, identity) refuses in `row_failure` (`receipt_guard.py:25-43`);
  jobs the timestamps hide are caught pre-write (test `pr_apply_pre_write_proof_catches_jobs...`). Vanished refuses
  only in list-only guards; the full guard keeps the old behaviour (absent run not checked).
- Tests: `admission_parity.py` keeps the exact-count assertion; native-apply now = one probe per receipt (67),
  proving the pre-write proof reaches every receipt. updated_at/run_started_at = created_at matches the real zombies;
  old tool ignores updated_at. The four one-line edits only add `full=` / `updated_at=` to fakes.
- Run: admission + release-admit suites with `SM_ADMISSION_PARITY_OBJECTS=~/Code/heinzel` (parity and replayed apply
  included): exit 0, no failure; `sm self lint`: pass.
- Open: "jobs => updated_at moves" is observed, not documented; only the non-write guards rely on it.

VERDICT: APPROVE

## 12. Independent review of repair-main, 7fc817a (2026-11-13, reviewer wrote none of the code)

COVERS: head apply
REVIEWED: sha256=5b343b183dd0ffa1a41dcc560399d27bfa8df4ee47868a3e8ee85044a10b5528

Findings (paths under `native/admission_lib/`):

- Only main's exact tree: repair prepare admits `plans=[main_manifest]` only (`focused_slots.py:575-576`, apply `:667-668`).
  `prove` requires a headless `push-tree:<plan.tree>` key (`repair_main.py:244`); `composition` requires plan base =
  main's single parent and plan tree = `main^{tree}` from the snapshot fetched at live main (`repair_main.py:183-188`;
  snapshot `focused_admit.py:301-316`); `validate_canonical` additionally proves tree = merge-tree(main^, head) and runs
  the trusted resolver as a push (`focused_slots.py:552-555`). `apply_slots` re-checks one plan, push-tree, headless,
  base/tree/digest = proof (`focused_admit.py:676-682`). No path writes another key.
- Live state: run, jobs, attempt, step and log (`repair_main.py:64-91`), main, parent, merged PR and its head
  (`:95-111`) come from GitHub; the admitted pair is decoded from the live registry by main's codec (`:220-231`);
  `guard` re-reads main and every live fact before and after the write (`:255-260`, via `packet_guard`
  `focused_slots.py:478-481`); apply re-runs prepare and demands byte equality (`ops.py` apply). The queue record and
  the original admission receipt are the coordinator's local files (`:114-145`), as the brief's condition 2 says;
  they are bound into the proof, not trusted for content (the pair itself comes from the registry).
- Refusals write nothing: every check runs before `apply_slots`; drift tests (main moved, rerun started, queue record,
  tampered args) assert no write and no receipt (`test_native_admission_repair.py:386-403`).
- Non-repair unchanged: `repair_main` defaults False/None everywhere; `packet_guard` reshape is equivalent (the
  main-changed branch raises); `check_stored_args` treats an absent key as False; `apply_slots` mode selection equals
  the old one when `repair_main is None`. Moved and recorded suites pass unchanged.
- No whitewash: the failed set must be exactly {Changed paths, CI OK}, each failing only its named step, every other
  job success/skipped/neutral, attempt 1 only, log contains the resolver's no-plan text (`repair_main.py:73-89`). A
  failed or cancelled test job refuses. Repair receipts never count as the original admission (`:141`), so no chaining.
- Run: all `test_native_admission*` + `test_native_release_admit*` with `SM_ADMISSION_PARITY_OBJECTS=~/Code/heinzel`
  through `sm gate`: exit 0 (1 skip); `sm self lint`: exit 0.
- Open, not blocking: (a) `validate_canonical` is stubbed in the repair tests; the real resolver with base main^ is
  unexercised until live. (b) Semantic interaction between the intervening merges and non-overlapping PR paths is
  not re-scoped (accepted by condition 4). (c) A rerun started between the write and the post-write guard refuses
  after the write without a receipt: fail-safe (main-rerun then finds nothing). (d) `queue_merge` accepts an empty
  `reviewed_head` and a missing `repo`.

VERDICT: APPROVE

## Re-review (campaign g3, 2026-11-13, Claude Opus 5.5; independent, wrote none of this code)

Why stale: the digest now follows imports into core and across `_lib` packages (19 files). Diff 1d69c43..479a8e8 of
the unit: `admission_lib/*` unchanged; `admission.py` one line (`head prepare` now `writes=True`, F9). Core files in
the digest: `config.py` (typed `Environment`, the only `os.environ` reader; `child_environment`, `secret`),
`engine.py` (review stops name file and canonical sha256; spent `--approved` digests; running marker; supersede),
`native.py` (digest by import graph; `gate_problem` with `exempt`; `call()` keeps the gate for pipeline steps;
writers refuse with exit 75 while a promote switch is pending), `models.py`, `helpdoc.py`, `protocols.py`: typing,
new result models, read_only derived from `writes=`. None changes what `head apply` writes or checks: recompute,
byte equality with the approved prepare.out, packet bytes against the prepared document, one slot write, receipt.
Tests through `sm gate run`: `test_native_admission*.py`, `tests/test_core_native.py`, `tests/test_core_engine.py`:
green (see the commit of this review).

Findings: none new for apply. Open from earlier reviews still stand (repair resolver untested live).

COVERS: head apply
REVIEWED: sha256=8a9ba37cd818b423c214917029bfac972422df1b6feac1061c5cadffcbb847e0
VERDICT: APPROVE

## Re-verdict (campaign g3, 2d0877b, 2026-11-13, Claude Opus 5.5; independent, wrote none of this code)

Diff 479a8e8..2d0877b of the 19 digest files: `admission.py` help text of `head prepare` only (a repair ends in a
fresh attempt-1 push run; merge one fix-forward PR; `queue main-rerun` refuses), consistent with `queue_main.py`;
`admission_lib/wait.py` docstring only (see head-prepare.md); `admission_lib/*` code otherwise unchanged. Core:
`engine.py` `_supersede` now takes each older run's own store lock before load/mark/save (non-blocking flock, Busy
-> exit 75; no deadlock, a partial supersede is idempotent on retry because the new state is saved only after it);
`native.py` adds `EXEMPT_PATHS={"gate run"}` so `exempt=` counts only on that allowlist (tightens the gate, apply
is not exempt), `NativeRuntime.commands` (lazy registry lookup, read-only), and pid-reuse detection for the promote
switch file ("PID UTC-TIME" from `bin/promote_lib.sh:25`; `ps -o etime=`; unreadable start fails closed, holds).
None changes what `head apply` writes or checks.
Tests through `sm gate run` at 2d0877b: `tests/test_core_{native,gate_units,engine,runlock,dispatch,window}.py`,
`test_native_admission_{wait,ops,repair,batch,slots,stuck_queued}.py`, `test_pipeline_admit_cut.py` and the packet,
dispatch, combine and lock suites: green.

Findings: none new. Earlier open points stand.

COVERS: head apply
REVIEWED: sha256=ff3d914c4ea8c449a3191cf5e1d641034e4cd68bc842a9982ef3954eaffb501b
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

COVERS: head apply
REVIEWED: sha256=6140790ceece5d9e86650b45c11eb3c6e18a59887462dd444ae903275212c8f2
VERDICT: APPROVE

## Campaign re-verdict g3 (2026-11-14, independent; reviewer wrote none of this code)

Target integration/sm-dod frozen at c36bbe1; baseline 903a1b7 (every unit APPROVED there). Digests recomputed with
`uv run sm self reviews` in a fresh worktree at c36bbe1; they equal the campaign list. Delta read with
`git diff 903a1b7 c36bbe1 --` over the unit files and every imported core file.

Unit delta (`admission.py`, `admission_lib/`, `environment.py`, `state.py`):
- `admission_nonconsumers_file` setting: used only when --nonconsumers is absent; default None (no real-path
  default). The receipts are still validated by the unchanged consumer checks; only the source of the path changed.
- State paths move from the scripted era's `~/.cache` names to the project state directory (`state.root()`): registry
  lock, receipts, validator cache, work root, queue/train/release locks, merge-freeze, release-holds. The symlink, owner
  and mode checks are kept on the new root. `root()` refuses while any old path still holds state, so the freeze and
  hold checks in `global_guard` cannot silently read an empty new directory while the state sits at the old name.
- The old state-override refusal (queue state, train state dir, release dir) is now
  `environment.state_overrides().any()`, the same three variables. GIT_* is still dropped for every git child
  (`child_environment(drop_prefixes=...)`).
- Disposable snapshot refs renamed to refs/sm-admission/* (scratch clones only). QUEUE_LABEL keeps the live launchd
  label on purpose. Message renames only otherwise.
- No change to the approval digest binding, the live-main identity check, the registry write or the locks' order.

Findings: none blocking. Note: `root()` caches a passed migration check per process; a long-lived process does not
see an old-name file that an older release writes later. Low risk, because the migration leaves a link at each old
name, so an old writer writes through it into the new location.

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

COVERS: head apply
REVIEWED: sha256=7f1db6950c00396e3d71d584614da80f347be13f10524edf1459f1415556c2e6
VERDICT: APPROVE

## Re-verdict at abc64a4, group A (2026-11-14)

Independent reviewer; wrote none of this code. Baselines, shared core and tests: ~/Code/packets/sm-reverdict-abc64a4/A.md (recorded verbatim by the integrator).

- R3: `QUEUE_LABEL` literal gone. `queue_label()` reads `queue_service_label` from the native runtime's
  `settings.of('heinzel')`; outside a native command (argv `main`) or unset -> None, `Runtime.service/plist` None,
  and `queue_state()` refuses first thing (focused_slots.py:162) naming the key. `exclusions()` calls `queue_state()`
  before the `try` with bootout, so nothing is paused or written when unset. Verified end to end: a user config
  `[heinzel] queue_service_label = "x"` resolves to "x" through `app.runtime()`; an empty config to None.
  migrate-values.toml on the Mini carries the live label.
- R6: `report_blockers` is read-only; the `--cancel-blocking-pr-runs` argv option and the `hz-ci-defer` exec path are
  gone. No caller passed cancel. Stronger.
- NOTE: `head prepare` (and `release admit prepare`) also refuse while the label is unset (`fresh_guard` ->
  `global_guard` -> `queue_state`, focused_slots.py:532); the setting's description and docs/SWITCH-queue-daemon.md
  name only the two applies. Fail-closed; fix the wording.

COVERS: head apply
REVIEWED: sha256=7dcae9f27a9291763e55f6403a7c14b093ecee06cf5203e2bd9f00dfd135fee6
VERDICT: APPROVE

## Re-verdict at feat/sm-next-admission 5fe179a (delta review 4, 2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-next-admission/REVIEW.md, delta review 4 (recorded verbatim by the integrator; COVERS from `self reviews`).

COVERS: head apply
REVIEWED: sha256=93f0b2c71b74c1c3f74eebd93689dba1d1f0620a318907d77667c54effe0fcab
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

next-admission only (lftt nonconsumers re-authenticated by prepare; m598 expected remote head; admission.py settings conflict kept all three fields). config/protocols. Matches the review file.
COVERS: head apply
REVIEWED: sha256=c6f2b129d16767c2d1c7ca6a9b0314eb6e8119b6c2b8dda6625990ac2192ca52
VERDICT: APPROVE

## Change: admission hold, waiting (2026-11-14)

Change (branch feat/sm-window-admission; beads heinzel-management-qji9, heinzel-management-5zb9). Builder note for the re-verdict; no verdict is written here.

Admission hold. `sm head apply` now runs inside `admission_hold` (native/admission_lib/hold.py): it registers a hold in the validation window
(`steuermaschine.window.Window.hold`, state key `holds` in the window's own store kind) and ends it in a `finally`. While a live hold exists,
`Window.enter` answers `wait` to every head that does not hold the window already: a validation that runs finishes, no new one starts.
A hold whose pid is gone, or whose lease ran out (window_lease_minutes), is ignored, so a killed admission never blocks the line for good.
Registering the hold is the first step; the admission code itself is unchanged except for what is listed below.
`--wait` (new on `sm head apply`): on a refusal that names an uncovered active run, or a truncated or drifting inventory, wait
admission_wait_poll_seconds and run the identical command again (`wait.retry_when_clear`, built on the existing `prepare_when_clear`), up to
admission_wait_max_minutes, then exit 75. Both refusals are raised by the guard BEFORE the registry write, so a retry never repeats a write;
the reviewer should confirm that for apply (focused_slots.prepare -> require(not blocking) precedes apply_prepared in ops.apply).

Check: the hold is released on every exit (tests/test_native_admission_hold.py: success, AdmissionError, the 75 wait); the hold is
registered before the first read of the run inventory; no write of the registry moved or changed.

Follow-up to the review (R1): `wait.tracked` wraps the apply of `sm head apply` and `sm release admit apply`; a refusal raised after the registry writer was called becomes `WrittenWithoutReceipt` (slot written, no receipt), which no wait retries: it stops at once with the recovery text. The earlier note that both retried refusals come before the write is superseded by this. The give-up message now says `apply`.

Awaiting an independent re-verdict: COVERS, REVIEWED and VERDICT lines follow from the reviewer.

## Re-verdict at 5b48447: window admission merged onto integration/sm-batch2 (2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-window-admission/REVIEW.md, round 3 (delta 8927d34..5b48447: admission hold, release-window order, drop of gone waiters, release-hold; withdraw and PrunedWindow removed). Shared core changed by integration/sm-batch2 (runlock, usage log, gate) is covered by its own reviews, not by this one.

COVERS: head apply
REVIEWED: sha256=5950d14cc4d5ef7f1df2b8ff7a1dd718529f1fcd71cf13df313619ab34f9d7e9
VERDICT: APPROVE

## Change: apply needs a recorded independent verdict (bead heinzel-management-04ro)

Before: `--approved SHA256` alone released the write; on 2026-11-14 it ran on a written approval before any receipt existed.
Now `head apply` calls `admission.require_verdict(approved)` after the batch-directory refusal and before `ops.apply`: it
refuses (exit 1, naming `sm review verdict DIGEST ...`) unless the store holds an `approve` receipt for the digest whose
reviewer is not one of the digest's authors. `head prepare` (single, waiting and batch) registers the digest with the
current agent as its author (`steuermaschine.verdicts.register`), so `sm review verdict` can record it when no run waits.
Tests: `test_native_admission_apply_needs_verdict.py`. The previous verdict line and digest are void; re-review.

After merging integration/sm-batch3 (window admission): the check runs BEFORE the release-window lock and the admission hold, so a missing verdict never holds either; `release admit prepare` registers inside its lock and hold, after `release_ops.prepare` (the one path of that command, with or without --nonconsumers).

## Re-verdict at f346492 (bead heinzel-management-04ro + integration/sm-batch3, 2026-11-14)

Independent reviewer; wrote neither 04ro nor window admission. Review: ~/Code/packets/sm-04ro/REVIEW.md.
Delta to f51cb78: merge of window admission. `require_verdict(approved)` runs before `admission_hold` and outside
`retry_when_clear` (checked once, a missing verdict never holds the window); hold and wait code as in sm-batch3.
Mutation (verdict moved inside the hold, release apply) killed by test_a_missing_verdict_refuses_before_any_lock_or_hold.
COVERS: head apply
REVIEWED: sha256=ea20838a13a7aa5f9cd43e106edc5ae5ee45ac1b20a1b63fecf9f264c003e24e
VERDICT: APPROVE
