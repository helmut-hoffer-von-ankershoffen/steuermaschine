# Parity: `sm head prepare`, `sm head hash`, `sm head blockers` against the former focused admission tool

Recorded inputs: the real admissions of 2026-11-12, copied unstripped to `plugins/heinzel/tests/fixtures/admission/recorded/`
(10 prepared transactions; 8 single packets with `packet.json` and `REVIEW.md`; 8 with the real receipt of their apply;
`INDEX.json` lists them). Original sha256 of every moved source file: `reviews/admission-originals.sha256`.

## What was compared

| Check | Inputs | Result | Where |
|---|---|---|---|
| Canonical bytes: `canonical(json) + "\n"` equals the recorded `prepare.out` | all 10 | 10 of 10 byte-equal | `test_native_admission_recorded.py` |
| Digest: native `ops.canonical_sha256`, core `steuermaschine.hashing.canonical_sha256`, and sha256 of the recorded bytes without the newline | all 10 | 10 of 10 equal | same |
| Digest against the real receipt's `transaction_sha256` | the 8 applied ones | 8 of 8 equal | same |
| `prepare` replayed offline: former tool, native `ops.prepare`, and the moved argv form, each against the recorded bytes | the 8 single packets (with and without the protected-producer approval) | 8 of 8: all three byte-equal to the recorded transaction | `test_native_admission_ops.py::test_prepare_parity_old_tool_native_port_and_recorded_bytes` |
| `blockers` | synthetic inventory | same rows as `report_blockers` of the moved module (it is called unchanged) | `test_blockers_lists_unclaimed_active_runs` |

The offline replay (`tests/admission_parity.py`) needs a Core Git repository that still holds the recorded commits:

```
SM_ADMISSION_PARITY_OBJECTS=~/Code/heinzel uv run pytest plugins/heinzel/tests/test_native_admission_ops.py -k "parity or replayed"
```

Run on 2026-11-12: 15 passed (8 prepare, 7 replayed applies) in 75 s. Without the variable these tests are skipped.

The replayed world is rebuilt from the recorded transaction: variables = its `expected` and `presence`; main = its
`main`; no open PR; active runs = exactly the runs of its 67 nonconsumer receipts, queued and without jobs; the cancel
probe is answered in process with the recorded 409 text. Git is real: a shared clone of the objects repository, the
trusted-main archive, the synthetic merge, the isolated resolver and the trusted-main slot codec all run. The fetch of
main into the disposable clone is answered locally with the recorded main. Nothing leaves the process.

## What differs on purpose

- Cancel probes. The former prepare sent one `POST .../cancel` per stuck-queued receipt (67 per prepare on the
  recorded packets) and required the 409 refusal. The native prepare sends none: every read check of those runs
  stays, the probe is left to apply (both guards before the write, as before). Counted by the parity driver:
  former tool 67, argv form 67, native prepare 0, native apply 201 (= 3 x 67, the former apply's number).
  The transaction is byte-equal with and without the probe.
- stdout. The former tool printed the transaction on stdout and events as JSON lines on stderr. The native command
  returns a result model (`PrepareResult`): text form names the stored file and the digest, `--format json` is one
  document that contains the transaction under `prepared`. The transaction bytes themselves are in `prepare.out`,
  byte-equal to what the former tool printed.
- `prepare.out` and `prepare.args.json` are written next to the packet by the command itself (before: shell
  redirection by the caller).
- `blockers --cancel-blocking-pr-runs` is not offered: it started another script. Cancelling PR runs belongs to the
  CI area.

## Not compared

- The two batch transactions (`batch1`, `batch2`): digest only. Their member packets were not kept, and batch has no
  native command yet (the moved argv form still has it and its moved tests pass).
- A packet with `--pr`, `--expected-remote-head` or `--resulting-main-only`: no recorded transaction of these modes
  exists in the fixtures. They are covered by the moved tests only.
- `--wait` against live GitHub: the loop is tested with a fake clock and scripted answers only.
- No run against live GitHub was made.
