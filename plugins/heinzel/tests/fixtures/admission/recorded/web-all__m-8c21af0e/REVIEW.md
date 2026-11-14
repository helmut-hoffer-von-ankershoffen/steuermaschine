# preopen pair: feat/focused-web-all head 1c623ddd04ed onto 8c21af0ec3df (builder-prepared by hz-admit-wave; NOT the independent review)

Members: none (one solo protected-producer PR feat/focused-web-all: whole Vitest suite as a focused web selection)

- head `1c623ddd04eddcc0e198fb497a72e331ed0f2637`, base `8c21af0ec3df63363daa5c4a96f61fd33d79cbed` (given main), tree `185221bc6c04eb8c8f3d52517b1356c58c73c88d` (git merge-tree)
- cases COLLECTED on the resulting tree (no carried digest); the head-tree collection reproduces the local focused JUnit

## Overlap with main
```
none
```

## Case collection
```
python: collected head tree == local focused JUnit; resulting tree identical
python312: collected head tree == local focused JUnit; resulting tree identical
```

## Manifest summary
```
head 1c623ddd04eddcc0e198fb497a72e331ed0f2637
base 8c21af0ec3df63363daa5c4a96f61fd33d79cbed
tree 185221bc6c04eb8c8f3d52517b1356c58c73c88d (merge-tree)
changed (5):
  M scripts/focused_job.py
  M scripts/focused_plan.py
  M tests/unit/focused_coverage_test.py
  M tests/unit/focused_job_test.py
  M tests/unit/focused_plan_test.py
protected execution changed: scripts/focused_plan.py, scripts/focused_job.py -> needs --reviewed-protected-producer
pyproject.toml: unchanged
production (0): 
repo selector (base scripts/affected.py): repository selector selects the full set; scripts/focused_job.py: shared/sensitive path, selects the full set; scripts/focused_plan.py: shared/sensitive path, selects the full set; advisory jobs commits, dependency-review, secrets, lint, setup, audit, test-linux, coverage, perf, e2e-cli, e2e-supervised, e2e-web-1, e2e-web-2, macos-smoke, macos-e2e, electron, template, core-wheel, consumers, build
selection: DECLARED, delta vs repository selector:
  + python tests/unit/ci_test.py
  + python tests/unit/focused_coverage_test.py
  + python tests/unit/focused_gate_test.py
  + python tests/unit/focused_job_test.py
  + python tests/unit/focused_plan_test.py
  + python312 tests/unit/ci_test.py
  + python312 tests/unit/focused_coverage_test.py
  + python312 tests/unit/focused_job_test.py
  + python312 tests/unit/focused_plan_test.py
  (no bounded derivation)
case_sha256 python 8c86323ce8e127716284af61f028539f6a28e15c9f0850e88d3a190e03c53178 (junit sha256 405c57787db62366da070741b2d3d7811779e322c6983ac5bb9e3171f437c91e, 491 cases, NOT bound to head by Git: confirm its run)
case_sha256 python312 007a918b503f67d12df6c386b2eb355eef1e9970eed5c492a06808d59ec58c90 (junit sha256 e3b8534bd04ed0ecd79bfdc43c12e6688f8e87f971b5dcb66c7d4197b93a5fce, 383 cases, NOT bound to head by Git: confirm its run)
manifest_sha256 074c56eb2cfd8a0c8f0bc7c423bae0b41a903b6543b3bce48bfa521516bbb5f0
main_manifest_sha256 626f27ef89f6eb7e8fce72046c75e22101884c6d01ad0cc73d04927a5c3465a3
review: none yet (not admissible; review these digests, then rerun with --review)
wrote /Users/<user>/Code/tmp-pr-fill/admit-B/web-all/m-8c21af0e/packet.json sha256 0c61dfc65aa916692db0a0fd7184d2a6af71c3eacdc4d4b5dd50ffc2f2042183
```

## Notes for the independent reviewer
## Independent review (Fable, different model)

Fable reviewed the diff and APPROVED Core 6bfda930a (feat/focused-web-all) and management c255519
(tools/focused-web-all), relayed by the coordinator on 2026-11-12. Condition: the PersonalArtifacts duplicate-case
rename (web/src/pages/PersonalArtifacts.test.tsx:65, `(row %#)`) must land before any web/ALL packet is USED. This
lane head is independent of the rename. Head 1c623ddd0 = 6bfda930a plus a normal merge of origin/main 8c21af0ec
(#779, CLI help fix); the merge adds no change to scripts/ or tests/unit/focused_*.

## Scope

Protected producer change (scripts/focused_job.py, scripts/focused_plan.py): VITEST_ALL = "web/ALL"; the web lane
accepts exactly ["web/ALL"] and runs `vitest run` without a file filter. Refused when mixed, duplicated, spelled
`ALL` or in any other lane. No ci.yml, focused_gate.py, docs/ or cap change. No src/heinzel production.

Declared selection. python: tests/unit/focused_job_test.py, focused_plan_test.py, focused_coverage_test.py,
focused_gate_test.py, ci_test.py (local 3.14.8: 491 passed). python312: the same WITHOUT focused_gate_test.py
(local 3.12.15, uv --isolated: 383 passed). Reason: focused_gate_test.py:624 parametrizes with
gzip.compress(b"{}", mtime=0), whose header OS byte differs by Python/zlib build (3.12 local \x13, 3.14 \xff), so
that case id, and with it the python312 case digest, is not portable to the hosted runner (the first preopen
refused exactly this case). focused_gate.py is unchanged; the test stays in the python lane. Follow-up: give
that parametrize a fixed id.

Pre-check (full Vitest on fe594724 with the rename, 3 runs): 1525 cases / 167 files, 0 duplicates, ~347 KB < 2 MiB
MAX_FILE, identical cases_digest 826455d2...; without the rename main's cases_digest refuses (1 duplicate pair).
