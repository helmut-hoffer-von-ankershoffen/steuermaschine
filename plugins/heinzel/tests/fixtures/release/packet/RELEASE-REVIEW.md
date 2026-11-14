# Independent release review: Core 0.9.42 on eb2c4529 (release-main-only/v1)

Reviewer: independent of the release author a7967758a91edfe03 (wrote none of it). Read-only review, 2026-11-14 CET.
No apply, push, tag, dispatch, rerun, PR, registry write or other GitHub write was made by this review. This file is the only
file written in the evidence root (my junit rerun and a scratch pair packet went to my scratchpad).
Reads: `git ls-remote` (Core main), `gh pr view` 814/815, `sm`, `sm head manifest` (--verify and a scratch rebuild), git on the
hooked clone. No `git fetch`. Live hz (8737) not touched.

## Verdict

**APPROVE.** The release commit is exactly the generated 0.9.42 version change, the notes and intro are true to #814 and #815, the
packet builder differs from 0.9.41 recut c only in the four version strings, and the packet binds the release commit, the pair
packet and the owning tests correctly. No findings that block; one advisory below.

## Identity reviewed

| Item | Value |
| --- | --- |
| Release head | `9ffb5d1b130c8b9be73a721be8bd485e39699cab`, message exactly `chore(release): v0.9.42` (no body) |
| Sole parent / base | `eb2c4529f5b48339733aea09bf2ea5e188b06412` (= remote `refs/heads/main` during this review; #815) |
| Tree | `60b0c412d30531268208474cc41dadff0b733faa` |
| CHANGELOG blob | `3af24991e93eca7ed1b77784fdae3483320bdf6e` (= packet `review.changelog_blob`) |
| `release-commit.diff` sha256 | `d6d2500cd1c5…` (= `git show 9ffb5d1b` byte for byte) |
| pair-packet.json sha256 | `e0d5d61e28a77248c8454b6bb4cb215df53039cafa9c6b1d2dc57f3f20889de4` |
| main_manifest_sha256 | `db733d06d9fbacbaebcc7218aea59b9272fd71066a0aa676ffa67e6c8b1b4f01` |
| Owning case digest | `d514b0cbaa331663d81928cb91afde5dde5183bde86d7d4582206cd3835b4f4c` (= 0.9.41; recomputed from my own rerun) |
| build-packet.py sha256 | `b1f166a79a7c7b51487ceb964393190ab2822842b1a0fc040a8e15bb38f54ede` |
| release-reservation-packet.json sha256 (reviewed, pre-rebuild) | `9f8b5e6f86533db65b021dad99631765ffd69569bbff4d1f49c7f43332454a3b` |

Remote: no `v0.9.42` tag and no `release/v0.9.42*` branch (`git ls-remote`). Local branch `release/v0.9.42` at 9ffb5d1b = clone HEAD;
clone clean before and after my test run. No `core.hooksPath`: the clone's own `.git/hooks` ran.

## Findings

### Advisory 1 (non-blocking): two cheap version-consistency tests are outside the owning selection

`tests/unit/standards_test.py::test_plugins_start_at_the_version_their_release_process_knows` (pyproject `version` =
`current_version`) and `tests/unit/repository_test.py::test_claude_plugin_marketplace_points_at_the_plugin` (reads the changed
agent-skills `plugin.json`) are not in `selection.json`. I ran both on 9ffb5d1b: **2 passed**. The selection is the same reviewed
set as 0.9.41 recut c and hosted CI runs the full set on the candidate (the repo selector already says "full set" for these
paths), so this does not block. Consider adding them to the release selection on the next cut, or in the sm release packet
builder (bead heinzel-management-asy6).

## 1. Release commit: verified

- `git cat-file -p 9ffb5d1b`: tree 60b0c412, one parent eb2c4529, message `chore(release): v0.9.42`.
- `commit-generated.log`: `Preparing v0.9.42 (was 0.9.41)` under `sm gate run`, every generator step ✓, every hook Passed,
  last line `v0.9.42`. That is the release.sh patch transcript in the hooked clone.
- `git diff --raw --no-abbrev eb2c4529 9ffb5d1b`: exactly 6 files, all `100644 -> 100644 M`: CHANGELOG.md (91cc9697 -> 3af24991),
  plugin.json (0539f131 -> c5084518), copier.yml (f4d7d004 -> e1d880b6), pyproject.toml (742a12cd -> 95d243b3),
  run_acceptance.py (5a410dec -> ed5fccf0; base changed by #814), uv.lock (33eb9cc4 -> 20295440). 15 insertions, 5 deletions.
  The base blobs of plugin.json, copier.yml, pyproject.toml and uv.lock equal the 0.9.41 release's new blobs: no other change to them since.
- Non-CHANGELOG changed lines are only `0.9.41` -> `0.9.42` (plugin.json version, copier default, pyproject version and
  `current_version`, uv.lock heinzel version) and the added `"0.9.42": ("ui", "serve"),` in FOREGROUND_UI (0.9.41 shape).
- CHANGELOG: added lines only, one `## [0.9.42] - 2026-11-14` section: the intro exactly as specified
  ("Chat works again with Claude Code 2.1.x: Heinzel checks that your Claude Code version is in the supported range and explains in
  plain language when it is not."), then Bug fixes #814 (cdf0b7e) and #815 (eb2c452). `git log --first-parent v0.9.41..eb2c4529`:
  exactly #815 and #814. No gap, nothing extra. Commit links match the merge commits from `gh pr view`.
- Truthfulness: #815's body says chat refused every Claude Code release except 2.1.312 and stopped on 2.1.321; it now admits
  2.1.312 up to before 2.2.0 and shows typed refusals in plain words with one action. The intro says exactly that. #814 is
  release tooling; naming it under Bug fixes without the intro is fine.
- `notes.generated.md` = the new section body; `notes.err` empty. `notes-audit.log`: ok. `pins.log`: both ok, and
  `git diff v0.9.41 eb2c4529 -- .github/workflows/ci.yml scripts/release_candidate.py` is empty. No company or colleague name.

## 2. Packet builder: verified

- `build-packet.0941.py` is byte-identical to `tmp-cut-0941/recut-c/build-packet.py` (sha256 ba34fc95…).
- My `diff build-packet.0941.py build-packet.py` = `build-packet.diff`: 4 hunks, docstring line 1 and line 5, the commit-log assert
  and `release['version']`, all `0.9.41` -> `0.9.42` (plus dropping "(recut b)"). No path, logic or hashing change.
- What it emits (read once): asserts one parent, main_manifest base/tree/event=push, commit log last line `v0.9.42`; hashes the
  clone's pre-commit, commit-msg, pre-push, the prek runner and the `current` stable sm's `release_scope.py`; writes
  `{schema:1, mode, manifest: main_manifest, release, review:{path, sha256 of the review file, release_sha256=h(release),
  manifest_sha256=h(manifest), changelog_blob}}`. No network, no push.

## 3. Packet: verified

- sha256 recomputed: `9f8b5e6f…454a3b` (= build-packet.out). Keys exactly manifest, mode, release, review, schema;
  mode `release-main-only/v1`, schema 1.
- `manifest` == `pair-packet.json`'s `main_manifest`; h(manifest) = `db733d06…` and h(release) = `30a64a71…` recomputed and equal.
- manifest: base eb2c4529, tree 60b0c412, event push, ref refs/heads/main, the same 6 changed files, config_blob = pyproject 95d243b3,
  every execution blob equals the blob at 9ffb5d1b (12 paths, uv.lock 20295440 included), selection = selection.json, case digest d514b0cb.
- release block: version 0.9.42, head/base/tree as above; hooks (884167…, f8f683…, 57c1fa…), prek runner (9f30f1…) recomputed from
  disk, all equal to the 0.9.41 recut c packet; commit_log sha256 0ce9652c… (= commit-generated.log); validator_sha256
  `bc4ab426…` = `release_scope.py` under `steuermaschine-stable/current` (now -> releases/b3083c2cdac3) and = 0.9.41 recut c.
- `sm head manifest --verify pair-packet.json --core-repo CLONE`: **BYTE-EQUAL sha256 e0d5d61e…**, exit 0.

## 4. Owning tests: verified

- `selection.json` byte-identical to 0.9.41 recut c (abe4678f…, 10 ids): release summary/version, changelog history/intros/combines,
  release notes, scripts, and the three acceptance FOREGROUND_UI tests. That covers every generated change of a version-only commit
  (CHANGELOG section, version strings, FOREGROUND_UI entry); see Advisory 1 for two extras.
- My rerun on 9ffb5d1b: `sm gate run -- bash -c "cd CLONE && uv run --frozen pytest -n 2 -p no:cacheprovider --junitxml=<scratch> <10 ids>"`:
  **219 passed**. Rebuilding the pair packet from my junit gives case digest `d514b0cb…4f4c` and the same bytes `e0d5d61e…`.

## Not verified

- Hosted CI, the candidate, consumers and rehearsal lanes; the admission transaction (separate APPLY-REVIEW.md).
- The promoted sm's `release_scope.py` (not yet known to me if it differs).

## What may change in the rebuild

The author rebuilds with the same script, inputs and clone. Allowed differences against the reviewed packet 9f8b5e6f, and only these:
1. `review.sha256`: the digest of this RELEASE-REVIEW.md (path stays `RELEASE-REVIEW.md`).
2. `release.validator_sha256`: the promoted stable sm's `release_scope.py` under `current`.
3. `review.release_sha256`: only as the derived h(release) of change 2.
Everything else must be byte-identical: manifest (h = db733d06…), head, base, tree, version, hooks, hook_runner, commit_log,
changelog_blob 3af24991, mode, schema. Any other difference voids this verdict.

VERDICT: APPROVE release=9ffb5d1b130c8b9be73a721be8bd485e39699cab tree=60b0c412d30531268208474cc41dadff0b733faa packet_sha256=9f8b5e6f86533db65b021dad99631765ffd69569bbff4d1f49c7f43332454a3b pair_packet_sha256=e0d5d61e28a77248c8454b6bb4cb215df53039cafa9c6b1d2dc57f3f20889de4 main_manifest_sha256=db733d06d9fbacbaebcc7218aea59b9272fd71066a0aa676ffa67e6c8b1b4f01 build_packet_sha256=b1f166a79a7c7b51487ceb964393190ab2822842b1a0fc040a8e15bb38f54ede
