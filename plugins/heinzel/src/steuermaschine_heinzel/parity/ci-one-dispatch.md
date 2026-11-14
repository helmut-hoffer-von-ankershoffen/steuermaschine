# Parity: `sm ci one ...`, `sm head validate`, `sm ci window ...` against the former single-lane tool

No hosted run was started for this record (forbidden for the port). Parity rests on:

| What | How compared | Result |
|---|---|---|
| Every decision (capacity, branch, SHA guard, selection, repeat, exact capability, pending protocol) | the old tool's own 73 tests run against the moved module, assertions unchanged | 73 pass, 1 skipped (needs the admission decoder) |
| The GitHub calls of one dispatch | scripted `gh` (`FakeGh`): workflow on main and branch, tip, open PRs, branches at the tip, active runs, then ONE `POST .../dispatches` with `ref` and the six `inputs[...]`; nothing after it | same endpoints, same fields, same order as the old source (lines 556-634) |
| Side effects of a start, in order | pending marker written -> POST -> receipt appended -> pending removed -> journal line | as before; the receipt file and the journal are new places |
| `status`, `repeats` | same lines as the old output for the same receipts (`status_line`, `rounds_for` are unchanged code) | identical text |

Differs on purpose:
- messages name `sm ci one ...` instead of the old command;
- `--json` is `--format json`, one document of `FocusedRun` / `FocusedStatus` / `Rounds` / `Validation` / `WindowRun`;
- the fail log of a red run comes from the core GitHub provider (job name, failed step, last 25 log lines), not from
  the former fail-log tool;
- receipts live in `dispatch_state_file` (state home), not in the old cache file; old receipts are not read;
- `head validate` waits in the validation window (exit 75, same command again) instead of polling a per-branch lock file.

Not compared: a real dispatch, a real re-run, a real throwaway push and delete.
