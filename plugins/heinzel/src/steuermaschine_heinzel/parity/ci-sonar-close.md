# Parity: `sm ci sonar-close` against the former closing tool

No call against Sonar was made for this record, not even a read.

| What | How compared | Result |
|---|---|---|
| Every check and refusal | the old tool's 16 tests (52 cases), bodies and assertions unchanged, against the command through the whole command tree | pass |
| The calls of `--apply` | mocked CLI: `sonar api get /api/issues/search?...` -> per issue `post /api/issues/add_comment --data {issue, text}` -> `post /api/issues/do_transition --data {issue, transition}` -> `get` again | same argv, same order, same timeout (60 s) |
| Output | `open before: N`, `would close|closing KEY file:line rule`, `open after: N` | identical text |

Differs on purpose: a refusal is the core's error line (exit 1) instead of `refused: ...`; after a failure half way it
also names the writes that already happened; the default manifest is a setting; json is one `SonarClose` document
that lists each closure and the write calls made or due.

Not compared: a real preview, a real closure.
