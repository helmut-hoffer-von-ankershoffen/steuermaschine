# why PR

Old tool: the one-PR view of the scripted era. Compared on 2026-11-12 22:57.

| Input | Compared | Result |
|---|---|---|
| PR 797 (open, CLEAN, three required checks green, not queued): old output and the two GitHub answers recorded in `tests/fixtures/why/` | every line | equal after renaming old tools to sm commands |
| PR 690 (open, BLOCKED, no required check on the head) | every line | equal |
| Nine verdict branches, synthetic | the WHY text | as in the old source |

Different on purpose: the role line says `unknown (no combine member lists configured)` until the setting
`why_combine_members_glob` is set (the old tool read the lists from the management checkout); main's run and the
last tick's wait come from the queue state and one GitHub read instead of the output of the queue tool.
