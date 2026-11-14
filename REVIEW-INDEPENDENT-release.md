# Independent review: release runner port (slice/heinzel-native-release)

Reviewer: independent (wrote none of the code). Untracked working file; verdicts go into
`plugins/heinzel/src/steuermaschine_heinzel/reviews/` only with zero blockers.
Reviewed commits: 532b5f1, 4546f02, c22120a, 66b2541 (on ed4a1df). Unit digest the builder froze:
393a906a57f2857f404206273cc68662bc5154a65aa9ddad168a8b2e2bb39924 (32 files).

Status: **NO VERDICT** for `release resume`, `release plugin`, `release drop` (open blockers below).

## Blockers

### B1 BLOCKER: the approved-source files are outside the review digest (re-pin without review)
- `src/steuermaschine/native.py:74`: a directory in `files=` contributes `entry.rglob("*.py")` only.
- `native/release.py`: `files=("release_lib", "queue_lib")`; `release_lib/approved.py:32` computes the pins from
  `approved_sources/ci.yml` and `approved_sources/release_candidate.py.txt` at import
  (`release_lib/candidate_policy.py:11`).
- Proof: `NativeCommand.code_files()` of `release resume` lists 32 files, all `*.py`; neither approved file is in it.
- Scenario: Core main's ci.yml changes; the candidate gate refuses the source; someone copies the unreviewed ci.yml over
  `approved_sources/ci.yml` (the docstring of approved.py says "replace the file, nothing else"). APPROVED_SOURCES
  follows, the unit digest is unchanged, the verdict stays valid, `sm release resume` accepts a candidate built by
  unreviewed CI and tags it. In the old tool the pins were literals in the reviewed policy file.
- Fix: name both files in `files=` of the three commands; a test that every file under `approved_sources/` is in
  `code_files()`; re-freeze. Sent to the builder 23:5x.

## Phase 1

### 1. Verbatim move: CONFIRMED (edges only)
- `python3 plugins/heinzel/port/move.py verify ~/Code/heinzel-management/tools`: rc 0, "VERBATIM except the edge lines
  above"; release_lib 48, release_read_lib 10, tests 53 (35 + 3 + 15 queue) as declared.
- Independent check (plain `diff`, my own normalisation = only `subprocess.run(` -> `edge.run(`): the old runner with the
  reviewed patch applied by me (`git apply --include=tools/hz-release --include=tools/tests/test_release_drop_candidate.py`,
  patch sha256 658d19ea...7743 as named in moved_map.py) differs from `release_lib/runner.py` in exactly: the 8 import
  lines, `work = edge.release_dir()`, the lock hint, `gate = edge.gate()` twice with `[*gate, ...]` twice,
  `edge.ship(pr)`, and the removed `__main__` block. Nothing else; same statements, same order. The runner is NOT split:
  hz-release moved as one file to runner.py; the other five modules were separate files before.
- notes_audit (2 lines), rejection (2), candidate_policy (6, incl. the pin line), candidate_capacity (5), snapshot (1),
  chores, verify, assets: diffed by hand, edges only. `status.py` is an extract of four pure functions.
- The originals are the WORKING TREE of heinzel-management at 170e6f3 (hz-release has 445 uncommitted added lines
  there; that is the live tool). sha256 of what I compared against:
  hz-release 4a6f03db01070eeb7eeefdcb2bae3acbb02f80986240a28dc101d726c2706a2a,
  release_rejection.py 279bd456..., release_candidate_policy.py 2a2b67b0..., release_candidate_capacity.py 97444a77...,
  release_snapshot.py 3dd316cf..., release_notes_audit.py ec6c029e..., hz_chores.py d5faf5ad..., hz-verify bb5dcb2e...,
  hz_assets.py ec649441..., hz_release_status.py bae68581..., release_candidate.py 596afe5f..., release_flow.py 3cf9c6cf...,
  hzlib.py 1b8fc1cb....
- Can the verifier hide a difference?
  - N1 NOTE `port/move.py:299,312`: EDGE_LINES counts only added lines. A PATCHES row that only deletes (like the
    `__main__` removal) does not move the count; it is printed, not gated. The table is in the digest, so a reviewer
    sees it, but "a new patch cannot slip in unseen" is not true for a pure deletion. Count `-` lines too.
  - N2 NOTE `port/move.py:267`: `splitlines()` on both sides: a CRLF, form feed or missing final newline is invisible.
    My byte diff shows none today.
  - N3 NOTE: the proof needs the old tools directory, which is being deleted (hz-go, hz-why are gone). Record the
    sha256 of the originals next to the review (as admission-originals.sha256 does); the list is above.
  - The hand-written files are not covered by the verbatim proof and are reviewed line by line in Phase 2:
    release.py, release_lib/{edge,approved,binding,names}.py.

### 2. Pins: CORRECT today (but see B1)
- sha256(approved_sources/ci.yml) = 4ba621c12257aca5ea01cb63c6b96b741e911f761df6a68e92fb3c55b79cdc2c,
  sha256(approved_sources/release_candidate.py.txt) = 596afe5f2c5aff2fe384d795af0bf9d1872ab53f41e17080bce43b7e389e0b78:
  equal to the literals in the old `release_candidate_policy.py:10`, and byte-equal (`cmp`) to
  `.github/workflows/ci.yml` and `scripts/release_candidate.py` at Core main f19a3782, which `git ls-remote` confirms is
  the remote head of main now.
- `moved_support.unmoved()` is a real reconstruction (drops the edge import, restores `subprocess.run(`), not a
  tautology: queue_lib/release_candidate.py differs from the pinned recorder by exactly those two lines.

### 3. The 57 formerly failing tests: no assertion weakened
- I ran the 21 original test files on a private copy (patch applied, throwaway HOME) and the moved ones, and compared
  by test id: 691 = 691, none missing, none added. 632 pass/pass, 57 fail/pass (exactly the 57: 48 capacity, 2 policy,
  7 pr_gate), 2 pass/SKIP.
- The 50: only the source of the "reviewed" bytes changed (stale fixture c1e5d8a6... -> the approved files); assertions
  identical. Consequence to know: "pin == hash of the reviewed file" is now true by construction; the protection is
  the review digest (B1).
- The 7 in pr_gate: the fixture gets `scripts/release_notes.py` (needed by 7) and a stub of the notes audit.
  - S1 SHOULD-FIX `tests/moved/test_native_release_pr_gate.py:14` (TEST_PATCHES in release_lib/moved_map.py): the audit
    stub is not needed. I verified on the originals: with `scripts/release_notes.py` AND an empty-section
    `CHANGELOG.md` in the fake checkout all 34 pass with the real audit running. Stubbing removes the only place where
    the cut's call of the audit is exercised. Write the changelog file instead of the stub.
  - S2 SHOULD-FIX `tests/moved/moved_support.py:14`: two tests that passed before now skip
    (`test_ask_adds_a_row_and_check_finds_it_until_a_pr_is_named`, `test_brief_makes_the_worktree_and_prints_the_prompt`:
    the ask and brief tools did not move). Not release logic; name the owner slice so they are not lost.
- Not moved, by design, checked: test_pipelined_train, test_train_main_gate (train, later), test_verify_mini,
  test_main_checkpoint (other slices). Every other original test that loads a moved release module is moved.

### 4. The seven read commands: READ-ONLY confirmed
- status: train state files read; `gh pr list`, `gh run list`, `gh run view` (GET). candidate: core provider
  `runs/run/jobs/log_tail` (GET). verify: PyPI GET, `gh release list`, site GET. pins: `git show/log/diff` on the core
  checkout. after: prints. notes-audit: `git describe/log` on the release checkout.
- assets: GET with Range to the asset URLs; writes its own cache file (declared in its help). The cache is read only
  for a digest line, no process acts on it. Acceptable as `writes=False`.
- N4 NOTE `release_read_lib/assets.py:46`, `verify.py:26`: the User-Agent sent outside still carries the old tool
  names (moved verbatim).

## Phase 2

(in progress)
