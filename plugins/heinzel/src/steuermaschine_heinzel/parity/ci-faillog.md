# ci faillog (read-only)

Compared on 2026-11-12 23:08 for run 42398276348 with the recorded failed-step log (`tests/fixtures/ci`): the
condensed lines are the old `condense(clean(log), 6, 12)` line for line (`clean`, `condense`, the error and noise
patterns moved verbatim), except for the first point below.

Differs on purpose:

- `gh` prints an escape character into a pipe in caret notation; the old pattern left a stray caret and bracket on
  every coloured line. The escape pattern has one more alternative for that form.
- No log file is written (the old tool kept the full log under the cache directory). `--raw` prints the failed-step
  log of a run to stdout instead.
- The header names `sm ci faillog RUN --raw` instead of a file path; the last line names `sm ci one rerun ...`.
- A failing check without an Actions run is not read as a run.
- The advisory class line is gone from this command: a read never sends anything outside this machine. The outside
  classifier is `sm ci classify RUN`, a reviewed write command, off by default (`parity/ci-classify.md`).
