# Parity: `sm release resume`, `sm release plugin`

Recorded old behaviour: `tests/fixtures/release/transcripts/*.json`, written by
`tests/record_release_transcripts.py` on a copy of the old tools (2026-11-12 23:24 CET; throwaway HOME, scripted `gh`
with the rules stored in each transcript, scripted PyPI; no network, nothing real touched). Replay:
`tests/test_native_release_writes.py::test_native_does_what_the_old_step_runner_did`.

| Scenario | Old command line | Native command line | Compared and equal |
|---|---|---|---|
| resume-main-red | `0.9.41 --timeout 1` | `sm release resume 0.9.41 --timeout 1` | exit 1; lines `release v0.9.41`, `ok   freeze`; STOPPED main tip red; 7 `gh` calls in order; receipt `{"version": "0.9.41"}`, lock, flow record, freeze file |
| absorb | `--absorb 0.9.40 --reason ...` | `sm release resume 0.9.40 --absorb --reason ...` | exit 0; `ABSORBED 0.9.40 by 0.9.41: ...`; 1 `gh` call; tail receipt with absorbed_by, absorb_reason, absorbed_at |
| absorb-accepted-refused | same on an ACCEPTED tail | same | exit 1; STOPPED nothing to absorb; no `gh` call; receipt unchanged |

Side effects of `resume-main-red`, in order: release lock taken; newest core release read; receipt written; flow
record written; tag read (absent); freeze file created; main tip and its runs read; STOPPED; locks released.

Differs on purpose: steps appear on stderr as progress and in `lines` of the json document, stdout in text form is
the closing statement (STOPPED line or the last statement); hints name `sm` commands; `--absorb` is a flag on the
version; `--plugin NAME` is `sm release plugin NAME`; waits sleep on the clock provider.

Not compared: any scenario past `main tip green` (release PR, merge, tag, PyPI, compose, acceptance, verdict), pause,
prepare-only, reject, candidate capacity, plugin release. Their steps are covered by the moved unit tests only.
