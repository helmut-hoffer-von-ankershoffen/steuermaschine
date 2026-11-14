# Parity: `sm ci main` against the former main-evidence tool

Same reads and the same verdict: the run list, `resolve_main_runs`, `attempt_jobs` and `main_state` are the merge
queue's moved code (queue_lib, imported unchanged, loaded as `sm queue` loads it); the note lines are the moved
`explain` (tests/views_verbatim.py: only the queue module became a parameter). Compared in
tests/test_native_ci_evidence.py on scripted runs (red with failing jobs, cancelled and superseded, none).

Differs on purpose:
- The old tool let `main_state` write the queue's pin (`main_failure`, `red_batch`) as a side effect, so a "read" could
  change what the queue does. Here that write is caught and shown (`queue_would_record`); the queue's own tick writes it.
- The verdict line reads `merge queue main_state:` instead of the old tool name.
