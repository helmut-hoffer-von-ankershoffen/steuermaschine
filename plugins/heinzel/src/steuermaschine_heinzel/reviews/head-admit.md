# Review: `sm head admit` (engine pipeline)

Unit: `pipelines/head_admit.py` and every first-party file it imports, including core (`sm self reviews` lists
them and the digest). The steps it runs are other units: each native command keeps its own review and its gate is
checked again when the pipeline calls it (`native.call`).

What it writes: the run state of this head (state store); through the steps: preflight summary, packet files,
prepare.out (local); with --live: the plan slot (head apply), one push and the PR (head pr), one queue entry
(queue add).

What the reviewer checks: every step calls the native command and does not redo its logic; the two review stops
name the file and the canonical sha256 the next write consumes (the apply passes exactly the digest the stop
checked); main or head moving before the apply voids packet, reviews and prepare; nothing outside this machine is
written without --live; a receipt or open PR found is adopted, never written twice.

State: open. Waiting for an independent review.

## Independent review (campaign g3, 2026-11-13, Claude Opus 5.5; wrote none of this code)

Read at integration/sm-complete 479a8e8 against live main 1d69c43: `pipelines/__init__.py`, `pipelines/common.py`,
`pipelines/head_admit.py` (new, whole files), the engine changes (review stops with file and canonical sha256,
`--approved` consumed once per stop, `running` marker, `_supersede`), `native.call`/`gate_problem`/`unit`, the
import-following digest, `runlock.py` (exclusive lane), `config.Environment`. Tests through `sm gate run`:
`test_pipeline_admit_cut.py`, `test_pipeline_cov_head_admit.py`, `tests/test_core_engine.py`,
`tests/test_core_native.py`, `tests/test_core_runlock.py`, `tests/test_core_window.py`: 83 passed.

Good: every step goes through `native.call` (the step's own gate holds, also for test overrides); external steps
(apply, pr, queue) run only with `--live`; probes adopt a receipt, an open PR at this head and a queue entry instead
of writing twice; main or head moving before the first external step voids packet, both reviews and prepare; the
apply passes exactly the digest its stop checked and `head apply` recomputes byte equality; an approval digest spent
on one stop cannot open the next; the verdict's reviewer must differ from the driver; prepare's wait is bounded
(45 min, exit 75); no hz-* call, no force push, no stash, no auto-merge; `--validated` is required for hosted lanes.

Findings:
1. (S2, bug) `head_admit.py:118-120`: `core = Path(str(... or "")).expanduser(); if not str(core)` never refuses:
   `str(Path(""))` is `"."`. With no `core_repo` input and no `core_repo_dir` setting, `head packet preopen` runs
   against the current directory instead of refusing. Fix as `release_cut.py:62-63` does (`not core or core == "."`),
   or test the raw string before building the Path; add a test with the setting unset.
2. (S2, resume trap) The packet directory is deterministic (`REVIEW_ROOT/BRANCH/m-MAIN8-HEAD8`) and
   `head packet preopen` requires it new or empty (`admit_wave.fresh_dir`). A refused preopen (collection failure,
   case diff) leaves `sel.json`, `collect.err`, `manifest.err` there (`discard_on_refusal` removes only packet.json
   and REVIEW.md). The next `sm head admit` refuses "exists and is not empty" for the same head and main forever,
   and NEXT does not say so. Fix: build into a fresh attempt directory (or clear a directory with no `wave.json`
   that this pipeline created) and name the recovery in the refusal.
3. (S2, binding) The packet review digest is not tied mechanically to what is prepared: `packet_review` takes
   `packet_sha256` from `wave.json`/state, never recomputes it from `packet.json` on disk, and `apply_review` does
   not check that `prepare.out`'s `packet_sha256` equals the approved packet-review digest. The apply reviewer is
   told to check it by hand. Fix: recompute the file hash in `packet_review`, and refuse in `apply_review` when the
   prepared document names another packet digest.
4. (S3) `_supersede` writes another subject's state while only this subject's lock is held; an older run still
   executing a step will overwrite "superseded" on its next save. Take the old subject's lock (skip on Busy).
5. (S3) `prepared_path`/`prepare_probe` fall back to `Path("")` when `packet_dir` is missing (same `"."` trap; harmless
   today because packet always precedes them, but fragile).
6. (S3, core, shared with gate run) runlock exclusive lane: a normal request that probed `exclusive_held()` false
   just before the exclusive request takes the exclusive file and slot 0 can still lock slot 1 and run beside it.
   Narrow window, bounded (30 min default wait); re-check `exclusive_held()` after locking a normal slot.
7. (S3) `--set validated=RUN` is free text: nothing checks the run exists, is green, or ran on a throwaway child
   commit rather than the PR head SHA.

COVERS: head admit
REVIEWED: sha256=85c38ff6533fa4b5b9f135db0b859fe2e470c4ba67143cea0868b6158a7d079f
Changes needed before approval: 1, 2, 3 (4-7 may follow as beads).
VERDICT: CHANGES

## Independent review of fix/pipelines-p1-p3 at fcad53c (base integration/sm-complete 479a8e8)

Reviewer: independent; wrote none of this code. Reviewed 161b9dd, then the deltas 803d811 and fcad53c. Unit and
digest recomputed with `uv run sm self reviews` at fcad53c. Paths under `pipelines/` unless named.

- P1, hosted lanes proven by runs (`head_admit.py` `_hosted`): `validated=` takes digit run ids only (free text:
  USAGE refuse). For every lane preflight named (loop over `lanes`, each refuses on its own; one run cannot cover two
  lanes because `lane_branch` gives each lane its own ref), one given run must
  - have this machine's dispatch receipt in focused-runs.jsonl: kind `dispatch`, branch = the lane ref, and `what`
    exactly the label `focused.cmd_dispatch` writes (`_label` rebuilds it from `rules.selection`; the receipt path is
    the same as `dispatch.bound`'s `dispatch_state_file` default). This binds the run to `sm head validate`'s own
    dispatch: kind, selector (the lane) and OS are no longer taken on trust;
  - be read from the configured repo only (`_repo` refuses without OWNER/NAME; `gh api repos/R/actions/runs/ID`,
    `repos/R/git/commits/SHA`, `pr list -R R`), so a run id of another repository is not found there;
  - be `.github/workflows/focused-job.yml`, event `workflow_dispatch`, `head_branch` = the lane ref, completed with
    conclusion success (the API returns the latest attempt);
  - name an exact 40-hex `head_sha` that is not the head SHA, whose tree (GitHub's commit object) equals the
    local `rev-parse HEAD^{tree}`. Both are git object ids, so the comparison can only be fooled by a SHA-1
    collision; `window.make_commits` builds the throwaway commit with `commit-tree HEAD^{tree}`, so a real
    validation always matches.
- macOS and web lanes: all four hosted lanes (`rules.LANES`: cli, supervised, web-1, web-2) are e2e lanes in
  `rules.selection`, so web lanes get the e2e label and are not refused. The label pins `on ubuntu-24.04`, so
  a run of `sm head validate --os macos-latest` is refused. That is right for admission: on a pull request, CI's
  e2e matrix is ubuntu-24.04 unless `macos_full` (core `ci.yml` e2e job), so the Linux run is the evidence every PR
  needs; a macOS-only validation does not prove the Linux lane. A user who validated on both OSes passes both ids;
  ids without a matching receipt are ignored. Not Required (see Should-fix 1).
- g3 (1): `common.core_repo` refuses empty, None, "." and "./"; used by `packet` here and by release cut.
- g3 (2): `_clear_own_attempt` moves a non-empty packet dir aside to OUT.failed-N only when this run's state
  recorded it as its own attempt (`packet_attempt`, saved before preopen; the engine saves state on Refuse);
  a foreign non-empty directory is refused and not touched; nothing is deleted.
- g3 (3): `packet_review` recomputes `packet_sha` (sha256 of packet.json's bytes, the same as `focused_slots.digest`
  and `admit_wave.file_sha`) and refuses when it differs from the built digest; `apply_review` and `apply` refuse
  unless prepare.out's `packet_sha256`, the bytes on disk and the reviewed digest are all equal.
- Tests: the new tests fail on 479a8e8's sources and pass here (P1 cases incl. no receipt, another workflow,
  edge.yml, empty head_sha, a second lane unproven; g3 cases). The sibling cov tests were updated, not weakened
  (free text now refuses; the packet digest is the real bytes').
- Ran through `sm gate run`: test_pipeline_admit_cut.py, test_pipeline_cov_head_admit.py,
  test_pipeline_cov_release_cut.py, test_heinzel_plugin.py, tests/test_gate_structure.py: see the commit message.

Should-fix (not blocking):
1. The validation OS is fixed to ubuntu-24.04. A head whose PR CI also runs the macOS e2e matrix (`macos_full`)
   is admitted on Linux evidence only; preflight names no OS. Let preflight name the OSes and bind them here.
2. focused-runs.jsonl drops finished receipts after 24 h (`focused.rows`): an older validation is refused as
   "no dispatch receipt". Fail-closed, but the message should say "older than 24 h: validate again".
3. With two given ids for one lane, only the first one listed is checked (a red first id refuses even when a later
   one is green). Conservative; say so in the help.

COVERS: head admit
REVIEWED: sha256=1c4721e22ef3bac5e49ad169a67647a4c36194282826353f0bf23b7e6025fbdd

VERDICT: APPROVE

## Re-verdict (integration/sm-complete 2d0877b, frozen; campaign re-verdict pipelines)

Reviewer: independent; wrote none of this code. Digest recomputed with the worktree's sm (`self reviews`, 40 files):
543908021fe445f618239692dc262c9f6222f3e4eea71891e66e70cda56d2294, as in the campaign list.

Delta fcad53c..2d0877b of every unit file: `pipelines/*.py` (head_admit, release_cut, common) are unchanged.
Changed: `dispatch_lib/focused.py` (head admit only): a dispatch that GitHub answers with a definite 4xx (not 408)
removes the pending marker and refuses "nothing started"; timeout, 5xx or no reply keep the marker (the start may
have happened). `queue_lib/hzlib.py`: `gh_api_method` (timeout bound only). `admission_lib/wait.py`: docstring.
Core: `engine.py` `_supersede` takes the older run's own lock before marking it; `native.py` `switch_pending` ignores
a pending file whose pid started after its UTC stamp (unreadable: holds), `EXEMPT_PATHS` = {gate run};
`github/cli.py` `ci cancel` digit-only id. Nothing regressed: every pipeline guard from the fcad53c review is
byte-identical.

Should-fixes of the fcad53c review are beads (open, label sm): validation OS (heinzel-management-a528), `_approved`
author rule (heinzel-management-554n), receipt older than 24 h message (heinzel-management-xjcj), two ids per lane
(heinzel-management-h8n5).

Not blocking: `_supersede` now locks every other key of the pipeline before it checks arg and status, so a new subject
waits (exit 75) while any other subject of the same pipeline is advancing, even one for another worktree. Liveness
only (a retry passes; supersede is idempotent); checking arg and status before taking the lock (and again under it)
would avoid it.

Tests through `sm gate run -- uv run pytest`: test_pipeline_{admit_cut,cov_head_admit,cov_release_cut}.py,
test_native_admission_cov_a_focused_admit{,_run}.py, test_native_dispatch_{cov_a_focused,focused}.py,
test_native_preflight.py, tests/test_core_{engine,native,runlock,window,preflight}.py: 364 passed.

COVERS: head admit
REVIEWED: sha256=543908021fe445f618239692dc262c9f6222f3e4eea71891e66e70cda56d2294
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

COVERS: head admit
REVIEWED: sha256=bf4491ca80fccb41178254b005e6649d2df05d8a0d6e62ba4df913d3efc1fc15
VERDICT: APPROVE

## Campaign re-verdict g6 (2026-11-14, independent; reviewer wrote none of this code)

Target integration/sm-dod at c36bbe1, baseline 903a1b7. Digest recomputed with the worktree's sm (`uv run sm self reviews`, 43 files); it equals the campaign list. Read: pipelines/head_admit.py and common.py in full delta, the merge-tier tests, the engine's handling of an empty review digest (engine.py:325, "nothing to review"), native/merge_tier.py, and the shared core and state delta (common.receipt_heads now reads the state directory through state.dashboard_dir). Tests through `sm gate run`: test_pipeline_{admit_cut,cov_head_admit,head_admit_merge_tier}.py within the g6 run (268 passed, exit 0).

Known gap, not judged here: `sm head admit` has no nonconsumers input (bead lftt).

New behaviour: at a merge-tier main (detected from main's scripts/focused_plan.py) the packet, both independent reviews, prepare and apply are adopted as "not needed" unless a protected file changed or `widen=yes`. Today Core main has no WHOLE_SUITES, so every step still runs; the change goes live the moment Core's ADR 0149 lands. Whether the pipeline stays fail-closed: not fully. Two inputs open the skip instead of keeping the manifest path:

CHANGES:
1. pipelines/head_admit.py:217-224 `_literal` returns `()` when PROTECTED_FILES or PROTECTED_PATTERNS is missing or not a plain literal (`ast.literal_eval` ValueError). Then `protected_changes` finds nothing and `_decide` skips the manifest and both reviews even for a change to ci.yml, focused_plan.py or uv.lock. A Core that writes `PROTECTED_FILES = BASE + (...)`, a frozenset, or renames the constant silently turns the protected check off. tests/test_pipeline_head_admit_merge_tier.py:64 asserts this fail-open result. Fix: an unreadable or absent literal must keep the manifest path (return None and treat it like an older Core), and the test should assert that.
2. pipelines/head_admit.py:255 detects the tier with the substring `"WHOLE_SUITES" not in plan` and ignores the setting merge_tier_mode. native/merge_tier.py has the canonical test (`capable`: `^WHOLE_SUITES\s*=`) and `active(mode, ...)`, which every other caller uses. Here a comment or string that mentions WHOLE_SUITES enables the skip, and `merge_tier_mode = "off"` (the owner's switch) does not bring the reviews back for admission. Fix: decide through `merge_tier.active(setting("merge_tier_mode"), ...)` on the plan text already read.
Everything else holds: an unreadable main or diff keeps every step; a skipped review records digest "" ("nothing to review") without a verdict that could be reused; the plan is decided once per (worktree, main, head, widen); preflight, PR and queue steps still run through their own gates.

COVERS: head admit
REVIEWED: sha256=673fd1634c02ef1e0a9a24bc8d0d289da4aa31fe996c035c70f6dfd7ac265942
VERDICT: CHANGES

## Re-verdict at abc64a4, group A (2026-11-14)

Independent reviewer; wrote none of this code. Baselines, shared core and tests: ~/Code/packets/sm-reverdict-abc64a4/A.md (recorded verbatim by the integrator).

- g6 finding 1 closed: `_literal` (pipelines/head_admit.py:229-250) returns None when the name is missing, assigned
  twice, annotated without value, augmented, computed, or not strings; `protected_changes` returns None and `_decide`
  keeps the manifest ("not a plain literal"). Unparsable plan text -> None too.
- g6 finding 2 closed: `_decide` uses `merge_tier.active(merge_tier_mode, plan)` (anchored `^WHOLE_SUITES\s*=`);
  `off` keeps every step; `on` with an unreadable main gives an empty plan -> protected None -> manifest kept.
- New (401c0e1): a stored READY preflight that skipped checks is adopted only with `skip_reason` AND a packet reviewer
  (no merge-tier skip); the packet review digest becomes the sha256 of preflight-skips.json, which names the packet
  sha, so the verdict binds packet + skip + reason. Before, such a summary was adopted silently: stronger.
- Preflight delta (d2948b1, b47f9b0): deleted files leave scoped coverage (correct). Core dependency changes now pass
  when PLUGIN_API (plain literal, consumer ref at/after plugins ADR 0003) shows every contract library inside its
  major; otherwise the old refusal. Hosted consumer-check still runs.
  NOTE (non-blocking): `outside_contract` (native/preflight_lib/head.py ~305) probes only 5 versions outside the major;
  `rich>=15,!=16.0,<16.5` is judged inside rich 15 (verified: returns []), though it admits 16.1. Probe a dense grid
  of the neighbouring majors (as `inside` does with range(100)), or require an upper bound `<major+1`.
- focused_slots R3/R6 delta: see head-apply.

COVERS: head admit
REVIEWED: sha256=3ad1dc7a35ba5d193fe89c880658d8b328306433c5bccb45db551fac33813016
VERDICT: APPROVE

## Re-verdict at feat/sm-next-admission 5fe179a (delta review 4, 2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-next-admission/REVIEW.md, delta review 4 (recorded verbatim by the integrator; COVERS from `self reviews`).

COVERS: head admit
REVIEWED: sha256=d661b92daa3c560a6976aa41e87965cb667af45a092ab168ed0f8c0e15a9cbb1
VERDICT: APPROVE

## Delta review: feat/sm-gate-lanes d94de45 (independent reviewer)

Changed in this unit since the last verdict: src/steuermaschine/runlock.py (light lane, rerun refusal); plugin native/lock.py (passes git_tree_state, setting text). Reviewed in full in two rounds (e6db9d9, d94de45),
probes now tests; see ~/Code/packets/sm-gate-lanes/REVIEW-2.md. Nothing in this command got weaker.

REVIEWED: sha256=679c201b17a7af91fd697daaa4db17c1bf7910a5b20c28616dfc823b58f138de
VERDICT: APPROVE

## Re-verdict at a28f1a7 (preflight parity, bead 98bw)

Independent reviewer; wrote none of this code. Delta: native/preflight.py and preflight_lib/head.py gained the steps
`types`, `standards` and `related` (head.py, ~230 lines); ship.py, ship_lib, queue_lib, queue.py are unchanged.
Read in full, three rounds of findings (~/Code/packets/sm-preflight-parity/REVIEW.md): the commands run as argument
lists through the run lock; the type checks are the hook's own (TYPES of its script, whole project, also for config
changes); the unit selection is Core's own related_tests.py, read from standard output only, ALL runs the unit suite or
fails, any other line fails, no fall-open (round 2 R5 reproduced and closed); an empty or unmatched standards set
fails; every fallback (no related_tests.py, unparsable TYPES, unmatched default glob, web head) warns by name.
Run for real against origin/main of Core: standards 9 files, related 18 hook tests plus 4 extras, mypy and pyright
clean, pyproject.toml answered ALL. Tests (parity, preflight, preflight_head) green through sm gate.
Waived by the coordinator (R6): a head touching web/ or desktop/ is warned, not failed.

COVERS: head admit
REVIEWED: sha256=8717b2bf40797ce07ff2f946cdd8dd674f91684c8c1a50696350800645c481a2
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Three branches: next-admission k7fj (three-hash selection check, rebuild after reject), preflight parity (admit's preflight now also runs types, standards and related: heavier, by design) and gate lanes (lock.py, window). Checked that parity reads no live selection (head.py: sel_path only for output/display; `selection_sha256` hashes sel_bytes), so the k7fj guarantee holds in the merged tree. Digest cap 45 (coordinator decision). Approve.
COVERS: head admit
REVIEWED: sha256=664039ff722f5aac00dd8d8ea98c5036e05b5aae2e27084a98f47cf9c40db906
VERDICT: APPROVE

## Change: apply waits for running validations (2026-11-14)

Builder note for the re-verdict (beads heinzel-management-qji9, heinzel-management-5zb9); no verdict is written here.

The pipeline's `apply` step now calls `head apply` with `wait_=True` (pipelines/head_admit.py): it waits for the validation runs going (exit 75 after
admission_wait_max_minutes: the same command again) instead of refusing 'unknown active Actions run'. The `prepare` step already waited. The hold itself is
taken inside `head prepare` and `head apply` (reviews/head-apply.md, head-prepare.md), so `sm head admit` holds the validation window exactly while those two
steps run. Nothing else in the pipeline changed.

Awaiting an independent re-verdict: COVERS, REVIEWED and VERDICT lines follow from the reviewer.

## Re-verdict at 5b48447: window admission merged onto integration/sm-batch2 (2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-window-admission/REVIEW.md, round 3 (delta 8927d34..5b48447: admission hold, release-window order, drop of gone waiters, release-hold; withdraw and PrunedWindow removed). Shared core changed by integration/sm-batch2 (runlock, usage log, gate) is covered by its own reviews, not by this one.

COVERS: head admit
REVIEWED: sha256=29271e654734310dab5e811df6594c0275a328857ca2b0568685fb10876511e4
VERDICT: APPROVE

## Re-verdict at f346492 (bead heinzel-management-04ro + integration/sm-batch3, 2026-11-14)

Independent reviewer; wrote neither 04ro nor window admission. Review: ~/Code/packets/sm-04ro/REVIEW.md.
Stale through window admission: the apply step passes `wait_=True` (verdict still checked once, before the hold);
new trap text; ops.apply wraps the write in `wait.tracked` (a refusal after the write becomes WrittenWithoutReceipt,
never retried); native/window.py and core window.py add holds. Pipeline tests green.
COVERS: head admit
REVIEWED: sha256=0990f8b71b9451839293ab198991adb871517ba23d1f333c95700aaf678cc5f0
VERDICT: APPROVE
