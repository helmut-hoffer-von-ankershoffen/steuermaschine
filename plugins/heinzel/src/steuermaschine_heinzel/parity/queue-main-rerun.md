# Parity record: `sm queue main-rerun`

Scenarios `main-rerun-dry-nothing`, `main-rerun-refused-unknown-main`, `main-rerun-nothing` of `port/parity.py`: old
`hz-queue main-rerun [--dry-run]` against `sm queue main-rerun [--dry-run]`, the same exit code, streams, calls and
state file. Method and declared differences: [queue-add.md](queue-add.md). Not compared: a plan that reruns (no
scenario builds a red main with KNOWN failures), and `sm queue checkpoint` (no scenario; its refusals are in
tests/test_native_queue_main_commands.py).
