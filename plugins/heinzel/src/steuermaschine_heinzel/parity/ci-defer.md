# Parity: `sm ci defer` against the former deferral script

No run was cancelled for this record (forbidden for the port).

| What | How compared | Result |
|---|---|---|
| The allowlist | the old jq filter and `defer.eligible` over the same real `gh run list` output, 2026-11-12: 67 queued, 0 in progress, 100 completed rows | identical selection (67, 0, 25) |
| The macOS rule | the old jq expression and `defer.macos_started` over 6 real jobs documents (0 and 40 jobs) | identical counts |
| Every decision of the old test file | its 11 tests with the same canned runs against the new command | pass; 2 changed on purpose (below) |
| A whole dry run | `ci_ops.defer(cancel=False)` against GitHub behind a runner that refuses writes | 67 `would cancel`, 137 reads, 0 writes |

Side effects of a live deferral, in order: `pr view` per kept PR -> (with --if-main-starved) `run list --status queued
--branch main` -> `pr list` -> `run list --status queued` -> `run list --status in_progress` -> per run: jobs read ->
audit line `attempt` (with the caller, read once with `ps`) -> run read -> `POST .../cancel` -> the run appended to
the deferred list -> audit line `cancelled`. Asserted call by call in `test_a_live_deferral_makes_exactly_these_calls_in_this_order`.

Differs on purpose:
- without `--cancel` the command is a dry run (the old script refused without a mode); `--if-main-starved` needs `--cancel` to act;
- `--if-main-starved MIN` is `--if-main-starved --minutes MIN`;
- a listing that cannot be read refuses (exit 1) instead of cancelling nothing silently;
- the cancel is `gh api -X POST .../cancel` after one read of the run, not `gh run cancel`;
- the deferred list and the audit log are in the store, not in two cache files; `FAILED cancel` is a line of the result, with its reason;
- a dry run ends with one line that says so.

Not compared: a real cancel.
