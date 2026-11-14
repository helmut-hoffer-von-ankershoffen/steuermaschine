# ci why (read-only)

One command for the question the scripted era spread over four tools: the cause line of the cause tool, the failing
log of the log tool, the one-PR view (`why.explain`, owned by the read-only slice) and the known-failure registry of
the digest.

Compared on 2026-11-12 23:08 with recorded real answers (`tests/fixtures/ci`): run 43186264208 (PR 784, a hosted-only
scoped-coverage failure) and run 42398276348 (PR 585, one failing end-to-end test that is in the registry).

| Input | Old cause tool | `sm ci why` |
|---|---|---|
| run 43186264208 | `not all download, no rerun -> Focused tests (python)=UNKNOWN, CI OK=UNKNOWN` | UNKNOWN for the job, with the HINT line and the CAUSE `scoped coverage under the floor (a hosted-only gate)`; CI OK is AGGREGATE |
| run 42398276348 | `... End-to-end web-2 (ubuntu-24.04)=UNKNOWN, CI OK=UNKNOWN` | TEST, the failing test name, `[KNOWN 9kk]` with the registry entry |
| the old tool's own test cases (downloads, Electron checksum gateway, browser setup timeout, a timeout in another step) | as asserted there | identical (`classify`, `verdict` moved verbatim; the tests moved with them) |

Differs on purpose:

- `gh run view --log-failed` no longer names the step (its step column says `UNKNOWN STEP`). The old tool looked the
  log up by step name, found nothing and called every job UNKNOWN. Native: lines of the job with that unnamed step
  are judged against each failed step; a log that names its steps is still read per step.
- `CI OK`, the aggregate job, is AGGREGATE when another job failed too. With the old tool it was UNKNOWN, so "every
  failure is a download" could never be true for a CI run.
- The rerun of an all-download run is `sm ci one rerun TARGET --wait` (the old line printed a `gh run rerun`).
- A failing check that is not a job of an Actions run (its link is not an `actions/runs` link: `coverage-floor`,
  `merge-gate`, posted by another check suite) is a GATE line. The old log tool read its check-run id as a run id
  and printed an empty log for it.
- New: `[KNOWN bead]` from the registry (setting `ci_known_failures_file`), NEXT and TRAP lines, PENDING checks,
  cancelled jobs counted as deferrals.
