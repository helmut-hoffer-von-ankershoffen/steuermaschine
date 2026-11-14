# Native port of the read-only family (slice heinzel-native-readonly)

Status 2026-11-12 23:10, branch `slice/heinzel-native-readonly`. One row per old tool of the family: where it is now,
what proves that it answers the same, and what differs. Parity records: `plugins/heinzel/src/steuermaschine_heinzel/parity/`.
`sm tools gaps` is the live list; this table explains it.

## Native in this slice

| Old tool | Command | Status | Parity evidence | Differences |
|---|---|---|---|---|
| go front door | `sm go`, `sm go --all`, `sm go --live` | native | 1 recorded real state and all 62 decisions of the old test file, byte for byte in the old spelling | sm commands only; `prepare-running`; predict/bound packets; focus from `sm focus`; see parity/go.md |
| alarm tool: check | `sm alarm check` | native, read-only | 35 recorded rule cases equal | writes nothing, notifies nobody; see parity/alarm-check.md |
| alarm tool: list | `sm alarm list` | native, read-only | same rows as check without the network | takes the path of the core `alarm list`; the plugin also answers the hook `sm_alarms` |
| alarm tool: ack | `sm alarm ack KEY` | core command | test: acknowledges a recorded key, old file untouched | the acknowledgement lives in the sm store; the old loop does not see it |
| one-PR view | `sm why PR` | native | PR 797 and PR 690 line for line | role unknown until `why_combine_members_glob` is set |
| Mini verifier | `sm mini verify [--minimum X.Y.Z]` | native | live: same five lines, LIVE-OK 0.9.39 | needs the setting `mini_remote_url` |
| go focus add/done/list | `sm focus add|done|list` | core commands; go reads them | tests | the old focus file is read only until the first `sm focus add` |

## Moved to another slice (not in this branch)

| Old tool | Command | Owner | Note |
|---|---|---|---|
| queue list, queue why | `sm queue list`, `sm queue why` | queue-release | this slice keeps only what go needs (`go_lib/probes.py`: queue_waits, queue_verdict, main_runs) |
| admission blockers, digest | `sm head blockers`, `sm head hash` | admission | merged on main before this table |
| wave size | `sm head size` | readonly, BLOCKED | needs the packet engine (git reads, base policy, the base's own selector) that the packet slice ports; copying it here would make two |
| status table, slots line, gate status | `sm status ...`, `sm mini gate` | status | uses `go_lib/legacy.py` and `alarm_lib/watch.slot_sizes` |
| ci cause, faillog | `sm ci why`, `sm ci faillog` | status | faillog's optional advisory line calls an outside model service: do not port that path without the owner's word |
| release status, candidate | `sm release status`, `sm release candidate` | release | |
| go views | `sm go --view start|next|prs|wait` | coordinator-tools | |
| tick digest | `sm go --view tick` | ops | the digest defers CI: a write inside a read |

## Write paths that stay off until an independent review

| Path | What it writes | The review must cover |
|---|---|---|
| `alarm clear`, `alarm raise` (not ported) | the alarm file the alarm loop owns, under its file lock; raise pushes the owner once | who owns the alarm state after the loop is native; that a native ack reaches the loop's owner push (today it does not); quiet hours 23:00-07:00 |
| alarm loop itself (ops: tick/watch) | first-seen times, owner push, de-duplication | once it is native, `alarm check` and the loop must share one state and `sm alarm ack` must silence the push |
| queue loop predicates (queue-release) | nothing, but they gate merges | later-main-pass, restored-by-rerun and the main-rerun plan are not re-implemented in a read command on purpose |

## Old state files adopted read-only (`native/go_lib/legacy.py`, directory = setting `legacy_state_dir`)

Queue state and log, deferred runs, incident freeze, gate hold, release holds, release receipt and tails, dropped
candidates, train states, rehearsal state, alarm state, focus file, admission receipts, cached slot sizes; outside
that directory: admission packets (`go_packet_globs`) and the PR action table (`go_pr_actions_file`).
