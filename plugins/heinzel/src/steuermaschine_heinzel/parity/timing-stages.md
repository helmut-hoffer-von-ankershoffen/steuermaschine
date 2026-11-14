# Parity: `sm timing stages` against `hz-wait stages`

See [timing-gate.md](timing-gate.md) for the verdict on every part of the old tool.

Moved unchanged: `stage_bead`, `stage_api`, `stage_publication`, `stage_interval`, `stage_snapshot`, `stages` (part of
the 246 moved lines counted in [timing-hosted.md](timing-hosted.md)). All eight tests of `hz_wait_stages_test.py`
pass with their assertions (27 cases); the test of invalid options goes through the command tree (exit 2, no read).
The fixture answers the same argv as before, through the `Commands` provider.

Read-only run against the real tracker and repository on 2026-11-12 (bead h17g with PR 793 and tag v0.9.39, a pair
that does not belong together): bead to PR and PR to merge measured, publication `pending ... (GitHub comparison does
not prove the release tag contains the merge)`: the fail-closed answer.

| Behaviour | Result |
|---|---|
| one tracker read (`bd --directory DIR --readonly --sandbox show ID --json`), then PR, release, tag ref, annotated tag, comparison | identical argv and order |
| publication counts only for a final release whose tag commit contains the merge commit | identical |
| `pending` with the elapsed time and the reason; `unavailable` never becomes zero | identical |
| exit 1 when an interval is unavailable or a record cannot be read | identical |
| two JSON progress events on stderr (`wait.stages.start`, `wait.stages.outcome`) | identical |

Differs on purpose:
- json: one `StageTiming` document; an interval always has the keys state, seconds, start_at, end_at, reason (the
  old document left out the ones that did not apply);
- "now" is the clock provider's;
- the tracker directory and the id prefix are settings (`tick_beads_dir`, `tick_bead_prefix`);
- an unreadable record is a refusal with the reason (text: stderr; json: the error object), exit 1.
