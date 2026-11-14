# go

Old tool: the go front door of the scripted era. Compared on 2026-11-12.

| Input | Compared | Result |
|---|---|---|
| The real state of 2026-11-12 22:33 (an apply running on a predict/bound packet; 60 runs, 4 PRs, 21 packets, 61 receipts), `tests/fixtures/go/go-apply-running.json` | decision dict and rendered text, native decision spelled with the old tool names | equal byte for byte |
| Every `decide()` call of the old tool's own 48 tests (62 states, 20+ phases), `tests/fixtures/go/go-matrix.json` | decision dict and rendered text | 62 of 62 equal byte for byte |
| The same 62 states and the real state in sm words | no line names an old tool | holds |
| Live, 22:44 | dev build against the real state | answered in 2.1 s with no unknown probe (the stable sm took 12 s at 22:31); the two were not run in the same minute, so their phases are not compared |

Different on purpose:

- Every command is an sm command: `sm ci why`, `sm queue why N`, `sm head ship N`, `sm head sonar N` (was a curl line),
  `sm head prepare ... --wait --out FILE` (was a shell redirect), `sm release cut`, `sm focus done WT`.
- New phase `prepare-running`: a running `sm head prepare` or `sm head admit` (and a prepare of the old admission tool)
  is reported for its packet or focus worktree. Before, the packet read "built, not prepared" or the focus head "no packet".
- Packets of the predict -> bind route (`.../predict/bound-<main12>/wave.json`) are found, and any packet a running
  prepare or apply names is read even outside the globs.
- The queue and train lines are computed from their state files; no script is started. `running` of a train comes
  from the process list (the old tool probed the train's lock file).
- Focus comes from `sm focus` once that list exists, else from the old focus file (read-only). Acknowledgements of
  `sm alarm ack` count.
- PR holds come from the setting `go_pr_actions_file`; without it no PR hold is known (the old tool read the table
  next to its scripts).
- `tools` probe: the old tool looked for script files; here rehearsal and drop are taken as available.
- json: one document of model `Go` (was `{"decision": ..., "state": ...}`); the probes are under `probes`.

Not ported: `--view start|next|prs|wait` (coordinator-tools slice), `--view tick` (ops slice; its digest defers CI).
