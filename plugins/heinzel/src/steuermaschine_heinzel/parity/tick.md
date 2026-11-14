# Parity: `sm tick` against the former digest tool (`hz-tick`, 225 lines) and its watch module (`hz_watch.py`, 202 lines)

Verdict per mode of the old tool:

| Old mode | Verdict | Where it is now |
|---|---|---|
| `--watch` (deadline watch) | **NATIVE in part**: `sm tick`. Crash reports, tool environment, the reviewed/queued filter and the noise rules moved. Alarm lines MERGED into `sm go` / `sm alarm check`. The digest it started as a child process is not started (see the write table). | this file |
| `--ack-crash`, `--ack-uv` | **moved to the core**: `sm alarm ack tick:crash`, `sm alarm ack tick:venv` (the tick is strictly read-only, review T1) | the core's acknowledged list (store `alarms`/`acks`); records of the former `sm tick --ack-*` (kind `heinzel.tick.ack`) are still read |
| (none): full digest | **MERGED**: every line is in `sm go` or in the owning command | section table 1 |
| `--digest`: anomalies only | **MERGED / owned elsewhere**; it also wrote (table 3) | section table 2 |
| `--pr N` | **MERGED**: `sm queue why N` (state, required checks, queue, holds) and the PR's line in `sm go`; Sonar `sm head sonar N`, guard `sm head guard N`, focused tally `sm ci one status --branch B`, failing lines `sm ci faillog N` | section table 1, last rows |
| `--triage` | alias of the triage tool: `sm ci triage prs` | tool map row `hz-triage` |
| `--merged-since HH:MM` | **dead**: no caller in skills, timers, launchd, hooks or tools; one function of the plan module (`hz_plan.merged_since`), belongs to the release train's port if wanted | caller grep below |
| `--lead [N]` | **dead**: no caller; the same number (PR created to merged, median and p90) was also in the wait tool's log block; `sm timing stages` measures one bead exactly | caller grep below |
| first line `RULES: run sm first` | dead: it pointed at the front door | |

## What moved, and how it was compared

- `watch.py`: `new_crashes`, `scan_crashes`, `live_ids`, `reachable`, `reviewed`, `filter_lines`, `load_cfg`,
  `pr_actions_holds`, `summarize`, `live_uv_warning`, `starved_age` (64 of the 83 moved lines identical, 73 when the
  type annotations on `def` lines are ignored). All six tests of `hz_watch_test.py` and the detector test of the tool
  environment guard pass with their assertions; two assertions changed because they named an old command
  (`sm ci triage deadline` in the cancellation INFO line).
- Through the command tree on fakes: quiet tick, crash report until acknowledged, an acknowledgement made the old way
  (the marker file under `legacy_state_dir`), changed tool environment until acknowledged, digest lines through the
  filter and the rules, a held PR, a starved line with and without a waiting main run.
- Read-only run against the real machine state on 2026-11-12 (in-memory store, nothing acknowledged):
  `WARN: live heinzel tool venv changed 11-12 06:02, not acknowledged (...)`, the line the old watch printed since
  the update of that morning; no new crash report (last acknowledgement 11-11 16:22).

Differs on purpose:
- **No alarm lines and no exit 3.** The old watch called the alarm module, which derived the alarms, wrote the alarm
  state and pushed the owner once per ALERT. `sm go` prints unacknowledged ALERTs first (`go.py` field `alarms`);
  deriving, pushing and exit 3 are `sm alarm check` (area `alarm`). So the last line is never a bare `OK`: it says
  what was looked at and points at `sm go`.
- **The digest is not started.** The old watch ran `--digest` as a child process and filtered its lines. The anomaly
  digest is not native; `tick.DIGEST` is the seam for it (a function `(runtime) -> list[str]`). Until it is bound the
  output ends with `NOT READ: anomaly digest ...` and the json field `not_read` names it. The filter and the rules
  run on whatever lines there are (today: the crash line).
- The INFO line about beads that can be closed after a release is not printed: `sm beads sweep` owns it.
- Queued PRs come from the queue's state file (statuses queued, ready, merging), not from the text of the queue listing.
- Acknowledgements: `sm tick` has no `--ack-*` option. It reads the time of `sm alarm ack tick:crash|tick:venv` from
  the core's acknowledged list. `sm alarm ack` takes a key only while that alarm holds, so the alarm area has to raise
  `tick:crash` and `tick:venv` (open, see the hand-over). The old marker files
  (`hz-crash-ack.ts`, `hz-uv-ack.ts`, `hz-live-uv.log` under `legacy_state_dir`) are read, never written
  (`tick_lib/legacy.py`), so nothing acknowledged the old way is reported again.
- A WARN line is not followed by `OK`.
- GitHub is asked for the queue age of main only when a line contains `starved` (the old watch asked every time).

## 1. The full digest (no option) and `--pr N`: every output section

Evidence column: file and line in this worktree, or the tool map row (`toolmap.py`) that names the owning command.

| Old output section | Where it is now / why it is gone | Evidence |
|---|---|---|
| clock, `load N` | `sm gate status` (`load`) | `runlock.Status.load` |
| `web CODE tail CODE`, `daemon:` line, `doctor:` line | `sm mini verify` (live proof); `go` only says published-not-live | tool map `hz-verify-mini`; `go_lib/decide.py:207-209` |
| `hz VERSION`, `latest release` | `go` STATE: `latest release vX; Mini CLI Y` | `go_lib/decide.py:425`, `probes.probe_releases`, `probe_mini` |
| beads summary line (sweep) | `sm beads sweep --summary` | tool map `hz-bead-sweep` |
| `STALE LOOPS` | `sm mini loops` | tool map `hz-reload-loops`, `native/guards.py` |
| `queue idle N min` | `go`: queue verdict in STATE, PHASE `green and queued for N min: STALLED`; alarm `stall:N` | `go_lib/probes.py:123-164`, `decide.py:239`, `decide.py:433` |
| `latest crash report` | `sm tick`: only when new since the acknowledgement | `tick_lib/watch.py scan_crashes` |
| `popup guard on/off` | dead with the guard (temporary since 11-07) | tool map `hz-popup-guard` (retired: parity/retired-popup-guard.md) |
| one line per open PR: merge state, pending count, failing names | `go` ALSO / ASIDE: one line per PR with its one next command | `go_lib/decide.py:226-254` (`held`, `conflict`, `pr-queued`, `pr-red`, `pr-pending`, `pr-green`) |
| `recently merged (UTC)` | dead: `gh pr list --state merged`; the hourly update lists merges | tool map `hz-hour` |
| `main CI: sha:status/conclusion`, job counts | `go` STATE `main SHA CI state (run N)`, PHASE `main-red` / `main-pending`; job detail `sm ci main` | `go_lib/decide.py:197-201`, `:420`; tool map `hz-main-evidence` |
| `queue: N N N` | `go` STATE `queue: ...; freeze ...` | `go_lib/decide.py:433` |
| `beads in_progress N; due within 90 min or overdue` | `sm beads tasks due 90`, `sm beads refill` | tool map `hz-tasks`, `hz-refill` |
| `owner msgs:` | `sm doc asks` | tool map `hz-asks-ingest` |
| `--pr N`: header (state, merge state, head, title) | `sm queue why N`; `go` line of the PR | tool map `hz-why`; `decide.classify_pr` |
| `--pr N`: `failing:`, `pending:` | `go` line (`red: names`, `N checks pending`), `sm queue why N` (required checks), `sm ci why N` | `decide.py:246-248`; tool map `hz-ci-cause` |
| `--pr N`: verify hint | printed by `sm ci faillog N` (same function, native) | `dispatch_lib/rules.py:171 verify_hint` |
| `--pr N`: `sonar:` | `sm head sonar N`; `go` names a PR red only on Sonar | `decide.py:244` |
| `--pr N`: focused checks and runs | `sm ci one status --branch B` | `native/dispatch.py:224` |
| `--pr N`: `guard:` and `queue:` | `sm head guard N`, `sm queue why N` | tool map `hz-diff-guard`, `hz-why` |

## 2. `--digest`: every output section

| Old output section | Where it is now / why it is gone | Evidence |
|---|---|---|
| release line(s) | `go` PHASE `candidate ...`, STATE `candidate:`, `train:`; `sm release status` | `decide.py:183-195`, `:427`, `:440` |
| `ANNOUNCE: N followers ready` | internal to the merge announcements; it wrote files (table 3) | tool map `hz-announce-merge` (internal) |
| `ASSETS:` line | `sm ci assets` | tool map `hz-assets-health` |
| `AUTO-DEFER:` lines | **a write**, not ported (table 3); by hand `sm ci defer` | `native/ci_ops.py` `ci defer` |
| `MAIN RED` with the rerun command, `CAUSE` | `go` PHASE `main SHA is RED`, NEXT `sm ci main`; cause `sm ci why RUN`; ALERT from the alarm state | `decide.py:199`; `go.py:108` |
| `OVERDUE-KNOWN`, invalid registry lines | the known-failures registry belongs to the digest and triage libraries (`sm ci triage`) | tool map `hz-triage` |
| `MERGED-VIA #N` | digest library (`merged_via`); `go` treats a packet whose head is merged as history | `decide.packet_state` |
| `PUSHED? branch` | `go`: `fix admitted, not pushed`, `focus ... local, unpushed` | `decide.py:274-279`, `:340-357` |
| load, web, tailnet, version mismatch, doctor anomalies | `sm mini verify`, `sm gate status` | tool map `hz-verify-mini` |
| PR CI runs in flight vs 5, push budget | `go` STATE `active runs: ...`; `sm ci one status` (cap 5); push budget is the push check of `sm head push` | `decide.py:442`; tool map `hz-push-gate` |
| `main starved N min` | alarm of the queue/alarm area; `sm ci deferred`, `sm ci defer --if-main-starved` | `native/ci_ops.py` |
| per PR: FAIL by bead, STALE, READY, NEEDS_REVIEW, FLAG, DEFERRED, KNOWN, owner | READY, red, pending, held, conflict: `go` lines; exact classes and guard FLAGs: `sm ci triage prs`; owners: `sm go --view prs --check` | `decide.py:226-254`; tool map `hz-triage`, `hz-status` |
| `QUARANTINE` lines | `sm ci quarantine list` | `native/ci_ops.py:388` |
| `REFILL` line | `sm beads refill` | tool map `hz-refill` |
| `GOV` line | `sm mini governor` | `native/guards.py:356` |
| `OVERDUE beads N` | `sm beads tasks due` | tool map `hz-tasks` |
| `JEV off` | dead: the experiment is retired | tool map `hz-jev` (retired) |

## 3. Writes found in the old read-looking modes (none is ported; each needs an owner)

| Old write | Triggered by | Now |
|---|---|---|
| **cancelled PR CI runs** (`hz-ci-defer --if-main-starved 15 --keep ...`) when main CI was queued 15 min, with a cooldown file `~/.cache/hz-auto-defer.ts` | every `--digest`, so every `--watch` (the 10-minute timer) unless `HZ_AUTO_DEFER=0` | nothing does this automatically any more. `sm ci defer` is the reviewed write command; an automatic trigger belongs in a daemon tick or an alarm, with its own review |
| owner push (once per ALERT) and the alarm state file | `--watch` through the alarm module | `sm alarm check` (area `alarm`); `providers.notifier` |
| announcement files `~/.cache/hz-announce/*.txt` | `--digest` | internal to the announce tool |
| a bead note once a day for a quarantine streak | `--digest` through the quarantine module | `sm ci quarantine flag` (a reviewed write command) |
| acknowledgement markers `~/.cache/hz-crash-ack.ts`, `hz-uv-ack.ts` | `--ack-crash`, `--ack-uv` | the core's acknowledged list (`sm alarm ack tick:crash|tick:venv`); old markers and `heinzel.tick.ack` records still honoured |

## Callers of the old tool (read-only grep, 2026-11-12)

| Place | Finding | Repoint to |
|---|---|---|
| `~/Library/LaunchAgents/*.plist` | none names the digest, watch or wait tool (jobs: queue, train, leakwatch, loadguard, product daemon and web) | - |
| `~/Code/.claude/settings.json` hooks | none (one hook: the git guard) | - |
| `research/timers/deadline-watch.md` (10 min) | `hz-tick --watch`, `hz-tick --ack-crash`, `hz-tick --pr N` | bare `sm`, **`sm alarm check` (exit 3 on an unacknowledged ALERT: the timer must call it)**, `sm tick`, `sm alarm ack tick:crash`, `sm queue why N` |
| `research/timers/tick.md` (30 min) | `hz-tick` | bare `sm` |
| `skills/hz-coordinate/references/rules.md` lines 11, 15, 16, 22 | `sm go --view tick [--watch|--digest|--triage|--pr N]` | `sm tick`; `sm go`; `sm ci triage prs`; `sm queue why N`. Line 11 ("can cancel CI") no longer holds for `sm tick` |
| `skills/hz-build/SKILL.md` line 53, `references/rules.md` lines 11, 47, 49 | `sm go --view tick --pr N`, `--digest` | `sm queue why N` (and `sm ci why N`) |
| `skills/hz-ci/references/rules.md` lines 9, 30, 55 | `sm go --view tick --pr N`, `--digest` (queue age of main), `queue idle` | `sm queue why N`; `sm ci deferred` / `sm go`; `sm go` |
| `research/sm-quickref.md` line 62 | `sm go --view tick (digest; can defer CI)` | `sm tick` (never defers) |
| other tools | `hz_next.py:127` reads `hz-tick --digest`; `hz_announce.py:22` names `hz-tick --pr N` in a message | the ports of those tools |
| `--merged-since`, `--lead`, `--ack-uv` | no caller outside the tool and its help | - |

## After the independent review (2026-11-12)

- T1: `--ack-crash` and `--ack-venv` are gone; `sm tick` writes nothing at all.
- T2: the digest seam `tick.DIGEST` names registered READ commands by path; code cannot be bound. A path that is not a
  registered read command is refused before anything runs.
- G1: no path default. `tick_diagnostics_dir`, `tick_tool_venv`, `tick_research_dir`, `tick_beads_dir` are unset until
  configured; what is not configured is printed as `NOT READ: ...`; `sm timing stages` refuses without
  `tick_beads_dir`. The bead prefix is empty by default. No literal `~/.cache` fallback: without `legacy_state_dir` no
  marker file is read.
- `timing` takes the unfinished run statuses from the core forge (`steuermaschine.github.forge.ACTIVE`), so this
  area does not import the single-lane library.
- Caller to repoint: the deadline-watch timer must run `sm alarm check` (exit 3) besides `sm tick`.
