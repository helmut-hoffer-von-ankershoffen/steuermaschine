# mini verify

Old tool: the Mini verifier of the scripted era. Compared live on 2026-11-12 23:00 (read-only on both sides).

| Input | Compared | Result |
|---|---|---|
| The live Mini, no arguments, remote URL set through the environment | all five lines and the exit code | equal: `LIVE-OK 0.9.39`, acceptance run 43158281029 |
| `--minimum 0.9.39` | all five lines | equal |
| Synthetic refusals in `tests/test_native_mini.py` (version below target, receipts negative, absorbed, conflicting or a symlink, web or daemon not ready, stale tick, HTTP not 200, doctor down, command missing) | the `NOT-LIVE:` reason | as in the old source |

Different on purpose: the remote URL is the setting `mini_remote_url` and has no default; without it the result is
`NOT-LIVE: the remote URL is not configured`. The live status documents are private and are not recorded as fixtures.
