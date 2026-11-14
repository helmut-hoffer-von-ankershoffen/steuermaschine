# Parity: `sm release rehearse ...` and `sm daemon rehearse` against the former rehearsal tool

No branch was pushed, no run dispatched or cancelled and no branch deleted for this record (forbidden for the port).
Parity rests on:

| What | How compared | Result |
|---|---|---|
| Every decision (guards, throwaway commit, lanes, give way, 180 minute bound, verdict, suspects, cleanup, tick interval, pin, Consumer checks) | the old tool's own tests run against the moved module, assertions unchanged except 7 lines that named an old command, switch or path | 25 pass; 1 dropped (it read the old launchd template) |
| The GitHub writes of `start`, in order | scripted GitHub: `POST git/commits` (message, tree, parent main), one `POST git/refs` per lane with the throwaway commit, then per lane ONE `POST .../focused-job.yml/dispatches` | same endpoints, fields and order as the old source (lines 180-207, 210-217) plus the single-lane library's own transcript |
| The writes of a verdict, of `cancel`, of giving way | `DELETE git/refs/heads/<branch>` per branch still on the throwaway commit; before it, per running lane, `POST actions/runs/<id>/cancel` | same targets and order; the cancel is the REST call instead of `gh run cancel` |
| Side effects of `start`, in order | guards read -> main read -> throwaway commit made and checked -> state saved -> branches -> journal line -> per lane: single-lane guards, dispatch, state saved -> runs read -> state saved | as before; state and journal are in the store |
| The guard reads against the live world (read-only, 2026-11-12) | the new `refusal()` and `preempted()` on this machine: `ps`, the queue's state file (319 items, none waiting), `pulls?state=open`, `actions/runs?status=` for the five active states; `pinned_plugins(main)` with the derived plugins repository | both empty (nothing refuses), one pin found: the edges work against the real `gh` |
| `status`, `plan` text | `line()` and the three plan lines are unchanged code | identical text, except `Consumer checks on this machine` (was: `on the Mini`) |

Differs on purpose:
- commands are `sm release rehearse plan|status|start|step|consumers|cancel`; the launchd entry is `sm daemon rehearse`
  (one tick; `--loop SECONDS` from the core). `--json` is the root `--format json`: one `RehearsalPlan`,
  `RehearsalStatus`, `RehearsalRun` or `ConsumerChecks` document (times as ISO 8601; a lane's `os` is `operating_system`);
- `status --line` is kept and prints the same line as plain `status` (it always did);
- a start that a guard refuses exits 75 as before, now as a wait on stderr; red exits 1 as before;
- the tick returns a refusal (75) or an error (1) as its result and prints an empty line when nothing was due
  (before: no output);
- `consumers` prints four text lines instead of JSON; its exit code is unchanged (1 on a failure or when unavailable);
- the Consumer checks pass the run lock's full-suite refusal with `--allow-full` (before: a variable in the launchd
  job's environment); `make -C TREE check ...` replaces the working directory; the log per plugin is unchanged;
- the admission guard also sees the native admission commands; the queue file's path comes from settings;
- state: store kind `heinzel.rehearse.state`; the former state file is not read. The lock is the store's;
- messages name `sm` commands; the unreadable-queue message no longer prints the file's path;
- `plan` no longer accepts `--if-moved` and `--every` (they never had an effect there); invalid `--lanes` or `--os`
  is exit 2 on every command that takes them.

Not compared: a real push of the rehearsal branches, a real dispatch, a real cancel, a real branch deletion, a real
Consumer check (`make plan`, `make check` in the plugins repository), a real edge wheel download.
