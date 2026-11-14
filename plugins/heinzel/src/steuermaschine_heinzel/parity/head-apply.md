# Parity: `sm head apply` against the former focused admission tool

No real apply was run. The comparison is a replay of recorded transactions against an in-memory registry.

## What was compared

| Check | Inputs | Result | Where |
|---|---|---|---|
| Replayed apply through `ops.apply` with real Git and the real trusted-main slot codec; the receipt it writes against the real receipt | the 7 recorded single packets that were applied | 7 of 7: every field equal except `admission_id` (random per admission) | `test_native_admission_ops.py::test_replayed_apply_writes_the_recorded_receipt` (needs `SM_ADMISSION_PARITY_OBJECTS`) |
| The one write | same | exactly one `set_variable(slot, value)`, slot and value equal to the recorded `slot` and `desired[slot]` | asserted in `tests/admission_parity.py` |
| Hermetic replay of `apply_prepared` (slot codec answered with the recorded proposal) against the real receipt | same 7 | fields equal except `admission_id`; file in `<home>/.cache/hz-focused-admission-receipts/head-<head>-<id>.json`; bytes = canonical JSON + newline; the five variables read back equal `desired` | `test_apply_of_a_recorded_transaction_reproduces_the_real_receipt` (always runs) |
| A registry value that changed since prepare | 1 | refused before the write, queue restored, no receipt | `test_a_changed_registry_value_refuses_before_the_write` |

## Side effects of apply, in order (unchanged: `focused_slots.apply_prepared` and `focused_admit.apply_slots` are the moved code)

1. Reads only: stored arguments, stored transaction, digest check, worktree HEAD, then the complete preparation again
   (GitHub reads, disposable clones under `~/.cache/hz-focused-admission-work`, one cancel probe per stuck-queued
   receipt).
2. Queue pause: `launchctl bootout` of the queue agent, wait for absence; locks `hz-queue.lock`, `hz-train/train.lock`,
   `hz-release.lock`, every version lock; the registry writer lock `hz-focused-pr-registry.lock`.
3. Guard (main, PR or pre-open proof, local head, freeze, holds, queue state, active runs, budget); five-variable read
   equal to `expected`; trusted-main proposal equal to the approved candidate digest; guard again; five-variable read
   again.
4. THE write: `gh variable set <slot>` with the candidate on stdin. One variable, once.
5. Five-variable readback equal to `desired`; guard again (no cancel probe after the write).
6. Receipt file, written once (`O_EXCL`, hard link).
7. Queue restore (`launchctl bootstrap`), verified; a failed restore fails the command even if the receipt exists.

## What differs on purpose

- Arguments: the former apply repeated every prepare argument plus `--prepared FILE --approved-sha256 SHA`. The native
  one takes `PACKET --approved SHA`; the rest is read from `prepare.args.json` and verified against the reviewed
  transaction (see reviews/head-apply.md, section 3).
- Refusals added in front of the unchanged path: approved digest is not the digest of the stored `prepare.out`; stored
  arguments disagree with the transaction; a batch transaction; the worktree is not at the reviewed head (with the fix).
- stdout: a result model; the text form is the receipt path, as before.

## Not compared

- The queue pause and restore against real launchd, the real variable write, and the locks under contention: replaced
  in every test. The code is the moved code.
- Apply with `--pr`, with a PR update, resulting-main-only, and batch: moved tests only.
