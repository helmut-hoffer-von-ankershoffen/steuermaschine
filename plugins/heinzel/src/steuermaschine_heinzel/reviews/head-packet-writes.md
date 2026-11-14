# Review: the packet builders

COVERS: head manifest
COVERS: head packet preopen
COVERS: head packet predict
COVERS: head packet bind
COVERS: head cases collect
COVERS: head cases compare

This command writes on this machine and was registered `writes=False`; it is now a write command under the
review gate (Fable review 2026-11-13, F9). Unit: the module, its `_lib` package and every first-party file they
import (`sm self reviews` lists them and the digest).

What it writes: the packet files under the review root (`head manifest --output`, `head packet preopen|predict|bind`), predicted commit objects and scratch refs in a scratch clone (`predict`), the JUnit file of `head cases collect`, and the id list of `head cases compare --output` (since g3 follow-up 2026-11-13: it was `writes=False`; review finding 3 below).

What the reviewer checks: nothing outside this machine is written, nothing is fetched or pushed, the output directory must be new or empty and inside the review root, and the shared repository is never written by `predict`.

State: open. Waiting for an independent review.

## Independent review (campaign g3, 2026-11-13, Claude Opus 5.5; wrote none of this code)

Unit: `packet.py`, `packet_lib/*` and the imported core (25 files). Diff 1d69c43..479a8e8: `packet.py` only (five
commands `writes=True` with this review file; collection runs get `child_environment()` instead of `os.environ`);
`packet_lib` unchanged; core as in head-apply.md plus `runlock.py` (exclusive lane). New unit, so `packet_lib` was
read for the write paths.

Checked: nothing is fetched or pushed (`exact_commit`/`commit_of` refuse a missing object: "this tool never
fetches"); no registry or GitHub write; preopen, predict and bind write only into `fresh_dir` (resolved, strictly
inside `--review-root`, new or empty); predict refuses when `--core-repo` is the configured shared Core
(`same_repo`, `live_core=_core(None)`) and writes commit objects and `refs/...predicted` only in the scratch clone;
collection extracts trees into `mkdtemp` and removes it, the private index borrows objects through alternates
(read only); collection runs go through the run lock and have timeouts (1800 s, 300 s); a refused build removes
packet.json and REVIEW.md (`discard_on_refusal`); `print` of the wave tool is captured, so `--format json` stays one
document. Tests through `sm gate run`: `test_native_packet*.py`, `test_native_lock*.py`: green.

Findings (none blocking):
1. (S3) The stated rule "the output directory must be new or empty and inside the review root" holds for
   preopen/predict/bind only: `head manifest --output` and `head cases collect --output` overwrite any path
   (`packet_lib/ops.py:93`, `collect_cases.py:189`). Downstream binding makes it fail-safe (prepare and apply hash
   the packet bytes), but a reviewed packet.json can be overwritten silently. Refuse an existing output, or say so.
2. (S3) A refused preopen leaves `sel.json`, `collect.err`, `manifest.err` in the output directory; the directory
   is then not empty and the same `--out` refuses. Matters for `sm head admit` (see head-admit.md, finding 2).
3. (S3) `head cases compare` writes `--output` but stays `writes=False` ("nothing, or the id list ..."). Local only;
   list it among the exempt local writers or gate it with this unit.
4. (S3) `admit_wave.LIVE_CORE` hard-codes `~/Code/heinzel` as fallback; the native path passes the setting, so only
   the old argparse entry depends on it.

COVERS: head manifest
COVERS: head packet preopen
COVERS: head packet predict
COVERS: head packet bind
COVERS: head cases collect
REVIEWED: sha256=a976256da8e672d908b15587dfa9140287e5af3e4acdd7ed498d1f27f38cd9a3
VERDICT: APPROVE

## Re-verdict (campaign g3, 2d0877b, 2026-11-13, Claude Opus 5.5; independent, wrote none of this code)

Diff 479a8e8..2d0877b of the 25 digest files: `packet_lib/*`, `lock.py`, `lock_lib/refuse.py`, `runlock.py`,
`github/*` unchanged; core `engine.py` and `native.py` as in head-apply.md (supersede lock, `EXEMPT_PATHS`, promote
pid-reuse check, `NativeRuntime.commands`). `packet.py`: `head cases compare` is now `writes=True,
review=PACKET_REVIEW` (my earlier finding 3, resolved) and listed by `COVERS` in this file.

Full review of `head cases compare` (`packet.py:304-324`, `check_ops.compare`, `collect_cases.compare/read_junit`):
- Reads three local JUnit files with `xml.etree` (no external entities resolved; expat guards entity expansion);
  `OSError`, `ValueError`, `ET.ParseError` -> `PacketRefused` -> refusal, nothing written.
- Rule: hosted vs head-tree collection must be equal as sorted lists, else exit 1 (not reproduced, refuse); then
  head tree vs resulting tree, else exit 3 (tree-scanning, ids listed); else 0. Matches the help and `exit_code`.
- Its one write: `--output` gets the differing ids (empty file on identical), written after all three inputs are
  read, so an `--output` equal to an input is safe. Without `--output` nothing is written. No GitHub, no git, no
  run lock needed (pure, cheap). As a writer it now also waits while a promote switch is pending.
- Tests: `test_native_packet_cases.py` (text/json, exit codes, registration, COVERS line): green.
Findings (S4, not blocking):
1. `--output` overwrites any existing path non-atomically and follows symlinks (same as finding 1 above for
   `manifest`/`collect`); the file is an advisory id list that nothing binds, so harmless.
2. Equality is by multiset but the listed ids are set differences: a JUnit with a duplicated testcase gives exit 1
   or 3 with an empty id list. Fails closed (refuses), only the message is empty.
3. (core, not this unit) `review_state` accepts a `COVERS` line anywhere in the file, so the header line 8 counts;
   safe only because `REVIEWED` must also name the current digest.
Tests through `sm gate run` at 2d0877b: `test_native_packet{,_cases,_manifest}.py`, `test_native_lock.py`,
`tests/test_core_{native,gate_units,runlock}.py`: green.

COVERS: head manifest
COVERS: head packet preopen
COVERS: head packet predict
COVERS: head packet bind
COVERS: head cases collect
COVERS: head cases compare
REVIEWED: sha256=3f9c317a836bbd8b3dbf39d3c335dbf8f9b5ba06285fa7c08ce79f47c7708eaa
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

COVERS: head manifest
COVERS: head packet preopen
COVERS: head packet predict
COVERS: head packet bind
COVERS: head cases collect
COVERS: head cases compare
REVIEWED: sha256=6f76ec294b5b8ccdfeb870383575da9410453d0cef51c0c1dd3517bef9e220a6
VERDICT: APPROVE

## Campaign re-verdict g3 (2026-11-14, independent; reviewer wrote none of this code)

Target integration/sm-dod frozen at c36bbe1; baseline 903a1b7 (every unit APPROVED there). Digests recomputed with `uv run sm self reviews` in a fresh worktree at c36bbe1; they equal the campaign list. Delta read with `git diff 903a1b7 c36bbe1 --` over the unit files and every imported core file.

Unit delta (`packet_lib/focused_manifest.py`, `admit_wave.py`, `floor_check.py`): merge tier b'/b''/b''' (Core ADR 0149,
branch ci/desktop-focused-0942 at 64a7b0d2f, checked against ~/Code/packets/desktop-focused-0942/sm-changes.md).
- b''' trust boundary, checked line by line: `base_protected_blobs` (focused_manifest.py:402) decides on the BASE blob
  of scripts/focused_plan.py (`def protected_blobs(` present or not), copies only the base's four BASE_SCRIPTS blobs
  into a fresh 0700 temp dir, and runs `sys.executable -I -c PROTECTED_DRIVER SCRATCH TREE`. The candidate tree is
  passed only as the merge-tree SHA (a validated 40-hex) and read as Git objects by the base's code (`git ls-tree`
  in cwd=--core-repo, GIT_* dropped). Verified empirically with the sm interpreter (3.14.8): under `-I`, neither cwd
  nor PYTHONPATH is on sys.path and no `scripts` package exists in site-packages, so `from scripts import` resolves
  only to the scratch copy; a sibling the base does not ship fails the import and the command refuses. Ad-hoc test
  (not committed): a candidate that rewrites its own scripts/focused_plan.py (`protected_blobs` writing a marker and
  returning {}) and scripts/ci_workload.py: the marker is never written, `execution` is the base function's set, and
  "protected execution changed: scripts/focused_plan.py" demands a reviewed protected producer. A nonzero exit, a
  timeout or a non-{path: 40-hex} result refuses (fail closed). `protected_changed` also lists a pinned file the
  candidate deletes. The candidate cannot pick its protected set or run code there.
- Older base (no protected_blobs): `execution` falls back to PROTECTED_FILES plus PROTECTED_PATTERNS literals read
  with ast from the base. Gating of the manifest's merge-tier rules is per base (`WHOLE_SUITES` in the base's plan),
  never the `merge_tier_mode` setting, so a setting cannot relax the manifest. `check_scope` returns a note instead
  of refusing only for a merge-tier base, where Core's selector itself runs the full set (fail open = more tests).
  Whole-suite tokens stand alone in their own lane; advisory lanes accept failures but not skips; Node cases own by file.
- admit_wave: SIM_ENV identity and PREDICTED_REFS renamed (hz -> sm). This changes predicted commit SHAs, so a predict
  dir recorded before the switch no longer binds; bind refuses (fail closed), re-predict.
- floor_check: temp prefix only.
Shared core delta 903a1b7..c36bbe1 (in every g3 digest): `config.child_environment` gains `drop`/`drop_prefixes`
(callers that dropped `GIT_*` by hand now use it; same result), `search_path`, `variable`; `native._command` runs each
write under `writers_lock` (shared flock in the project state directory; `sm self migrate-state` takes it exclusively),
the switch-pending check moved inside it and still fails 75 for a single run and skips a loop tick; the review gate
(`gate_problem`) is unchanged and still runs first. `statemove.require_migrated` refuses while an old `~/.cache/hz-*`
path still holds state or both paths hold state, so the state-path move fails closed. helpdoc adds lint letters only.
`models` adds StateMigration only. No force push, stash, auto-merge or new write path without the digest gate.

Findings (none blocking):
1. (hardening) The trust boundary rests on `base` being trusted. It is operator input (default origin/main), but
   `verify_packet` (packet_lib/ops.py:110) and `--verify` take it from the packet file, and now also execute that
   commit's focused_plan.py and siblings (affected.py ran from it already at the baseline). A packet whose base is not
   main would run that commit's scripts. Suggest refusing, before any base script runs, a base that is not origin/main
   or its ancestor (floor_check.py already applies this rule).
2. (robustness) The driver imports `scripts` as a namespace package (focused_manifest.py:410 creates no
   `__init__.py`). A regular `scripts` package in the interpreter's site-packages would win over the scratch copy.
   None exists today and it is not candidate-controlled; an empty `scripts/__init__.py` in the scratch dir closes it.
3. (dead path) `Policy.protected_paths` (focused_manifest.py:282) has Round-2 semantics (tests/ only, one level); a
   Round-3 bare pattern such as "conftest.py" would match differently. No Core base has PROTECTED_PATTERNS without
   protected_blobs (both came in a992d9ada), and Core's judge compares the dict exactly, so a mismatch refuses in CI.
4. (tests) Add the candidate-rewrites-its-own-plan case above as a committed regression test.

Tests through `sm gate run` at c36bbe1, by explicit file (39 files: `test_native_admission_*` (14), `test_native_combine{,_cov_b_plan}.py`, `test_native_coord_merges.py`, `test_native_dispatch{,_cov_a_focused,_focused}.py`, `test_native_lock{,_refuse_matrix}.py`, `test_native_merge_tier_{rules,validate}.py`, `test_native_packet{,_cases,_dirs,_floor,_manifest,_manifest_merge_tier,_parity,_size,_wave}.py`, `test_pipeline_head_admit_merge_tier.py`, `tests/test_core_{native,runlock,statemove,window,gate_units}.py`): exit 0, about 1006 passed, 26 skipped, 0 failed. Serial: this repository has no pytest-xdist, so `-n 2` is refused by pytest.

COVERS: head manifest
COVERS: head packet preopen
COVERS: head packet predict
COVERS: head packet bind
COVERS: head cases collect
COVERS: head cases compare
REVIEWED: sha256=fc5e0ba5a52bed0491f426efbd6f07d0a8d5154eff7f19dfcc95dd498902ab62
VERDICT: APPROVE

## Re-verdict at abc64a4, group A (2026-11-14)

Independent reviewer; wrote none of this code. Baselines, shared core and tests: ~/Code/packets/sm-reverdict-abc64a4/A.md (recorded verbatim by the integrator).

`trusted_base` (packet_lib/ops.py): build and verify refuse a base that is neither origin/main nor its ancestor
unless its scripts/ tree equals that of its fork point on main, before any base script runs (stronger). The scratch
`scripts/__init__.py` makes the base's scripts a regular package so no installed `scripts` shadows them (stronger).

COVERS: head manifest
COVERS: head packet preopen
COVERS: head packet predict
COVERS: head packet bind
COVERS: head cases collect
COVERS: head cases compare
REVIEWED: sha256=a811a46c96ff0dd96fbd7addc14f06b7d63c522890f014b78347a409e0e1402a
VERDICT: APPROVE

## Re-verdict at feat/sm-next-admission 5fe179a (delta review 4, 2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-next-admission/REVIEW.md, delta review 4 (recorded verbatim by the integrator; COVERS from `self reviews`).

COVERS: head manifest
COVERS: head packet preopen
COVERS: head packet predict
COVERS: head packet bind
COVERS: head cases collect
COVERS: head cases compare
REVIEWED: sha256=c304199fabecf841ddea0e49777c362b36f4cbc1d12694f97d0ca8b84c1337c2
VERDICT: APPROVE

## Delta review: feat/sm-gate-lanes d94de45 (independent reviewer)

Changed in this unit since the last verdict: src/steuermaschine/runlock.py (light lane, rerun refusal); plugin native/lock.py (passes git_tree_state, setting text). Reviewed in full in two rounds (e6db9d9, d94de45),
probes now tests; see ~/Code/packets/sm-gate-lanes/REVIEW-2.md. Nothing in this command got weaker.

REVIEWED: sha256=fafd50aa20246156c893debc6290dd957f05a342d13736fd42df93e69cfee334
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

next-admission 0rxo (focused_manifest: whole-file coverage of selector files) plus gate lanes (collection runs under the policy with the rerun refusal; the collector argv carries --tree SHA, so only a true unchanged rerun is refused). Both verdicts at their bases; merged files equal each side. X1 applies in the rare unchanged-rerun case. Approve.
COVERS: head manifest
COVERS: head packet preopen
COVERS: head packet predict
COVERS: head packet bind
COVERS: head cases collect
COVERS: head cases compare
REVIEWED: sha256=55fe842deddf9ed5b54e75efc0d3b8141c53706d8155ebf5d55980fb9dc016de
VERDICT: APPROVE
