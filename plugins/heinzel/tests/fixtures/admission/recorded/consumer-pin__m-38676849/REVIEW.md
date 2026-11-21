# preopen pair: ci/consumer-pin-0940 head 1f53d8f7e2ea onto 38676849f2a6 (builder-prepared by hz-admit-wave; NOT the independent review)

Members: none (one solo protected-producer PR ci/consumer-pin-0940: consumer plugin pin to the 0.9.40 relock)

- head `1f53d8f7e2ea5ba5929096cb8dcfc849976886c3`, base `38676849f2a6bee35c6663984a68afcc7b88892d` (given main), tree `e9bb3781a3b2542d2815f55a68bf769c26286f61` (git merge-tree)
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
head 1f53d8f7e2ea5ba5929096cb8dcfc849976886c3
base 38676849f2a6bee35c6663984a68afcc7b88892d
tree e9bb3781a3b2542d2815f55a68bf769c26286f61 (merge-tree)
changed (1):
  M .github/workflows/ci.yml
protected execution changed: .github/workflows/ci.yml -> needs --reviewed-protected-producer
pyproject.toml: unchanged
production (0): 
repo selector (base scripts/affected.py): repository selector selects the full set; .github/workflows/ci.yml: shared/sensitive path, selects the full set; advisory jobs commits, dependency-review, secrets, lint, setup, audit, test-linux, coverage, perf, e2e-cli, e2e-supervised, e2e-web-1, e2e-web-2, macos-smoke, macos-e2e, electron, template, core-wheel, consumers, build
selection: DECLARED, delta vs repository selector:
  + python tests/unit/acceptance_test.py
  + python tests/unit/affected_test.py
  + python tests/unit/browser_runtime_setup_test.py
  + python tests/unit/ci_macos_arm_test.py
  + python tests/unit/ci_node_test.py
  + python tests/unit/ci_supervised_test.py
  + python tests/unit/ci_test.py
  + python tests/unit/ci_uv_bootstrap_test.py
  + python tests/unit/ci_workload_test.py
  + python tests/unit/confidentiality_test.py
  + python tests/unit/focused_coverage_test.py
  + python tests/unit/focused_gate_test.py
  + python tests/unit/focused_job_test.py
  + python tests/unit/focused_plan_test.py
  + python tests/unit/main_batch_test.py
  + python tests/unit/merge_gate_test.py
  + python tests/unit/quarantine_test.py
  + python tests/unit/release_candidate_test.py
  + python tests/unit/release_channel_test.py
  + python tests/unit/review_tier_test.py
  + python tests/unit/scoped_checks_test.py
  + python tests/unit/ship_test.py
  + python tests/unit/standards_test.py
  + python tests/unit/web_build_test.py
  + python312 tests/unit/ci_test.py
  + python312 tests/unit/ci_workload_test.py
  + python312 tests/unit/focused_coverage_test.py
  + python312 tests/unit/focused_job_test.py
  + python312 tests/unit/focused_plan_test.py
  + python312 tests/unit/release_candidate_test.py
  (no bounded derivation)
case_sha256 python a2faaed9d595b395f482f96847fe846c72566803bebe524f3c3de18825fa2619 (junit sha256 4be18d5dd148605060a2af01aacd6736397dc4a0cb814fb08ae0fe880b96feac, 3371 cases, NOT bound to head by Git: confirm its run)
case_sha256 python312 d5b3eea2baf9b170c204eb2a3c9df8114a769370abd8eb5c43f8e02db9a06475 (junit sha256 4ff6c2ac812abb857485ea30dbd8159c6bff985060aa399e99af7457574cecc8, 493 cases, NOT bound to head by Git: confirm its run)
manifest_sha256 0e6b8ed0f2d565ba8a385fb30e5847434f552031bc8cb9d937b0cc8a12a49f19
main_manifest_sha256 2bcc201979c9d1d936b9377faa19f7bab6ac0fe22257173828f81c881dec4241
review: none yet (not admissible; review these digests, then rerun with --review)
wrote /Users/<user>/Code/tmp-pr-fill/admit-B/consumer-pin/m-38676849/packet.json sha256 0d9854e204cc404d793333befa34d1ed31b521925bf7309f7b1152ed2ae65bb5
```

## Notes for the independent reviewer
## Independent review (Fable, different model)

Fable reviewed and APPROVED this change (verdict /Users/<user>/Code/tmp-relock/c0940/REVIEW-VERDICT.md, relayed by the
coordinator on 2026-11-12). Head 1f53d8f7e is one commit over origin/main 38676849f. The only change is the two
`helmut-hoffer-von-ankershoffen/heinzel-plugins` checkout refs in `.github/workflows/ci.yml` (consumer jobs), moved
from c64ccd4d2 to the pushed plugins commit a1f583edc824cffe363f9d2570b539a396d67b92 on branch
candidate/consumers-relock-core-0940 (Core 0.9.40 candidate locks; relock caused by #776, pyjwt in the openai
extra). The matching APPROVED_SOURCES digest patch for heinzel-management (~/Code/tmp-relock/c0940/approved-sources.diff)
is applied separately by the coordinator. No src/ change, so no hz-scoped-cov.

Local evidence: `make lint` green (includes typos, actionlint, zizmor, confidential). Both lanes' JUnit were run
locally with the Core venv (Python 3.14.8); the hosted python312 lane runs the same ids on 3.12.
Follow-up: move the pin back to plugins main after SDK publication.
