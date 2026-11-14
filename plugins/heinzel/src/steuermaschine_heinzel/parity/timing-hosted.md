# Parity: `sm timing hosted` against `hz-wait hosted`

See [timing-gate.md](timing-gate.md) for the verdict on every part of the old tool.

Moved unchanged: `elapsed_seconds`, `valid_outcome`, `optional_strings`, `valid_job_fields`, `hosted_job`,
`hosted_read`, `hosted_run`, `hosted` (of the 246 moved lines of the wait tool, 210 are identical, 226 when the type
annotations on `def` lines are ignored). All twelve tests of `hz_wait_hosted_test.py` pass with their assertions
(32 cases); changed: the refusal line starts `timing hosted:`, and the test of invalid ids goes through the command
tree (exit 2, no read).

Read-only run against the real repository on 2026-11-12, runs 43305436883 and 43304958269 (focused jobs, first
attempts of a manual dispatch): `dispatch to start 4s | job wall 197s` and `4s | 286s`, two reads per run.

| Behaviour | Result |
|---|---|
| two reads per run: the run, then the jobs of its current attempt (paginated, slurped) | identical argv |
| every identity and inventory check; no partial report | identical |
| dispatch wait only for attempt 1 of a manual dispatch; wall time from job start to completion | identical |
| text lines, control characters escaped with `ascii()` | identical |
| 1 to 8 distinct positive run ids, else usage (exit 2) before any read | identical |

Differs on purpose:
- json: one `HostedTiming` document; the two durations are `dispatch_to_start_seconds` and `job_wall_seconds`
  (were `..._s`);
- a refusal is one line `timing hosted: run N: reason` on stderr (text) or the error object (json), exit 1;
- each read has a 90 s timeout and a transient failure is tried once more (the one client).
