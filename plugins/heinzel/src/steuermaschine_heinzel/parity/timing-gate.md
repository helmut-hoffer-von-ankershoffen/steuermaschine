# Parity: `sm timing gate|hosted|stages` against the former wait tool (`hz-wait`, 534 lines)

Verdict per subcommand of the old tool:

| Old subcommand | Verdict | Where it is now |
|---|---|---|
| (none) / `--json`: diagnose the biggest wait | **MERGED into `sm go`** | table below |
| `log`: append the numbers to `research/velocity-log.md` | **retired**: a write into a tracked document of another repository; no caller (the velocity review writes its own entry) | caller grep below |
| `gate [--json]` | **NATIVE**: `sm timing gate` | this file |
| `hosted RUN...` | **NATIVE**: `sm timing hosted RUN...` ([timing-hosted.md](timing-hosted.md)) | |
| `stages --bead --pr [--tag]` | **NATIVE**: `sm timing stages` ([timing-stages.md](timing-stages.md)) | |

The path is `timing`, not `go wait`: `go` is one command and cannot have subcommands in the native registry, and
the old name was misleading (the tool never waited).

## `sm timing gate`

Moved unchanged: `pctl`, `gate_stats` (27 lines; the clock and the history path are the edges). The two library tests
of `hz_wait_test.py` pass with their assertions; the third (`gate` asks nobody) is the command test
`test_gate_waits_in_text_and_json_without_any_command`. Read-only run on the real history file on 2026-11-12:
`gate 60 min: 5 commands; waited median 0s, p90 0s, total 0s; ran median 3s, p90 4s, total 14s; 0 with a non-zero exit`.

Differs on purpose:
- text: one readable line; the old tool printed the Python dict (`gate 1h: {'runs': 5, ...}`);
- json: one `GateWaits` document with the unit spelled out (`wait_median_seconds` for `wait_med_s`, and so on);
- the history file is the run lock's setting `lock_history` (same default path as before).

## The diagnosis (`hz-wait` without a subcommand): every output section

| Old output section | Where it is now / why it is gone | Evidence |
|---|---|---|
| `biggest wait:` and `unblock:` | `go` PHASE, WAITING, NEXT, THEN: the ranked items decide what is waited on and the one command | `go_lib/decide.py:162-411` (`decide`), `go.py` fields `phase`, `waiting_on`, `next`, `then` |
| "N PRs with deferred CI during the release" / Acceptance running | `go` PHASE `candidate vX running` with WAITING `candidate CI run N`; deferred runs `sm ci deferred` | `decide.py:183-195`; `native/ci_ops.py` `ci deferred` |
| "Merges blocked: release X (N min old); incident hold" | `go` STATE `holds: ...`, `queue: ...; freeze ON`; the fail-closed read of each hold is `sm head blockers` | `decide.py:433-437`, `go_lib/legacy.py hold_files`, `native/admission.py:41` |
| "N PRs fail CI for real" | `go` lines `PR #N red: names` with NEXT `sm ci faillog N` | `decide.py:246` |
| "N PRs have cancellations with unverified cause", deferred, known | exact-head classes: `sm ci triage deadline` | tool map `hz-triage` |
| "N PRs are check-ready; merge readiness unverified" | `go` `PR #N green, not queued` with NEXT, or `green but BLOCKED` | `decide.py:250-252` |
| "Mini load N slows the gate" | `sm gate status` (load and the bars) | `runlock.Status` |
| "N PRs pending hosted CI" | `go` `PR #N CI running (K checks pending)`, STATE `active runs` | `decide.py:248`, `:442` |
| `buckets:` (PR numbers per class), `--json` buckets | `sm go` json `probes.prs` (number, checks, failing, pending per PR) | `go.py:105` |
| `gate hold N min` | `go` STATE `holds:` names the gate hold file; `sm gate status` says `a hold is set` | `go_lib/legacy.py:112-118` |
| `merge hold ...` | as "Merges blocked" above | |
| `load`, `gate 1h {...}` | `sm gate status`; `sm timing gate` | |
| `log`: lead times (PR open to merged, last 30) | not ported; one bead exactly: `sm timing stages` | |

Not moved with it: `pr_buckets`, `hold_directory`, `merge_hold_observation`, `merge_hold_text`, `diagnose`,
`lead_times` and their eight tests in `hz_wait_test.py` (they test the diagnosis and the log block).

## Callers of the old tool (read-only grep, 2026-11-12)

| Place | Finding | Repoint to |
|---|---|---|
| `~/Library/LaunchAgents/*.plist`, `~/Code/.claude/settings.json` hooks | none | - |
| `research/timers/velocity.md` line 2 (hourly) | `hz-wait gate --json` | `sm --format json timing gate` |
| `tools/README.md` line 5 | `hz-wait gate --json` | `sm timing gate` |
| `research/sm-quickref.md` line 62 | `sm go --view wait` | `sm go` (WAITING line), `sm timing gate` |
| `tools/hz_push_gate.py:62` | a message says "retry after `hz-wait` shows main CI running" | `sm go` (the push check's port) |
| velocity checkpoints of 11-10 and 11-11 in `research/log/` and `research/velocity-log.md` | quote hosted dispatch waits, job walls and bead-to-PR times: the numbers of `hosted` and `stages` | `sm timing hosted`, `sm timing stages` |
| `hz-wait log` | named only in research notes of 11-07 and 11-08 as a planned metric | - |
