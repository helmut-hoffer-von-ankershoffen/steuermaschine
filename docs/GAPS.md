# Gaps: missing or unclear `sm` commands

Source: the agent that rewrote the skills, agent definitions and quick reference to `sm` (2026-11-12), plus what the
reviews and the merges found. One line per gap, with its owner. `sm tools gaps` is the count of old tools not ported;
this page is the list of things a user of `sm` trips over.

State: open, in work (a slice has it), done (on main), live (promoted). Checked against main `903a1b7`.

| # | Gap | Owner | State |
|---|---|---|---|
| 1 | `doc hour` says read-only but commits and pushes unless `--no-handoff`: register `writes=True`, fix the help | coord slice | done on main: a gated write command; its Changes line names the commit and push |
| 2 | `head admit` printed an old script command in its NEXT line | admission slice (pipeline `head admit` on the engine) | done on main: `head admit` is a pipeline of native steps; `sm tools gaps` counts it as ported (gap 20) |
| 3 | `release drop` for a paused or failed candidate | release slice | done on main |
| 4 | Every option must appear in `--help`; no pass-through arguments | integrator | done by construction: a native command is a typed function, there is no pass-through |
| 4b | The generated tools reference must show `sm` usage | coord slice (skills generation) from `sm self tree --format json` | open |
| 5 | A plugin release command: `sm release plugin NAME` | release slice | done on main; `--redispatch` after a failed run |
| 6 | `queue checkpoint`, a way to stop the train | queue-release slice (`queue_main.py`) | done on main |
| 7 | Push check for a branch without a PR, and `--min` | queue-release slice | partly: `sm head push --min` is on main; a branch without a PR is open |
| 8 | One place each for focus and for a review verdict | integrator | decided, see below; not built |
| 9 | `sm tools gaps --wrapped` | integrator | done: prints 0 by construction, the help says so |
| 10 | note, ask, start, next parse the tracking document, which changed layout (147 lines, anchors kept) | coord slice | in work (`sm note` delivered) |
| 11 | Exit 70 documented in every skill-facing help | integrator: it is in the root help (`sm --help`) and README; per-command only where a command adds a meaning | done on main |
| 12 | `head pr` must not run `make pr` on this machine: open the PR through the GitHub client from a body file | admission slice or ops slice | done on main: `sm head pr` opens the PR through the GitHub client |
| 13 | `--format json` after the command (`sm pr status 796 --format json`) is a usage error | core-framework slice | done on `slice/core-framework`: every command and group takes `--format text|json` (core, native, mounted, pipelines); in json a parser error or an unexpected exception is one `{error, code, hint}` document (`steuermaschine.dispatch`) |
| 14 | The run lock is `sm gate run COMMAND...`; callers that type `sm gate COMMAND...` break | skills agent (callers), integrator (a clear error for the old form) | open |
| 15 | `sm why PR` and core `sm pr why PR` say overlapping things | integrator | decided, see below; not built |
| 16 | A running `--loop` daemon keeps the old release after a promote | integrator | done on main: the loop ends when the release pointer changes; promote prints the kickstart lines |
| 17 | `sm tools gaps`: a per-command count beside the per-tool count, "partial (n of m)", and who still calls the old tool | integrator | open |
| 18 | Core `go` and `alarm` as the frame, the plugin contributing through `sm_go_items` and `sm_alarms` | integrator | partly: `sm_alarms` is used; `go` is still the plugin's own command |
| 19 | Default forge and CI from `steuermaschine.github` when `[github]` is configured; `[github]`, `[sonar]` in `sm self config` | integrator | partly: `sm self config` lists `[github]` and `[sonar]`; the default forge and CI are not wired |
| 20 | `sm tools gaps` counts only native commands as ported, so the row of the old focused-admission tool shows missing although `sm head admit` runs | integrator (core `app.py`, a reviewed unit) | done on integration/sm-dod: a pipeline's subject and verb count as a ported path (`test_a_pipeline_of_the_project_counts_as_ported`, `test_tools_gaps_counts_the_project_pipelines_as_ported`) |

## Decisions (gap 8 and 15)

- **Focus**: one place, the core's `sm focus add|done|list` (state in the core store). The plugin's `go` reads it
  from there. `sm go focus ...` is not ported and will not be.
- **Review verdict**, two different things, one place each:
  - a verdict on a **run** (a review stop of a pipeline: prepared bytes of one head, the pins of one release) is
    recorded with `sm review verdict DIGEST --approve --reviewer NAME`. The line in an `APPLY-REVIEW.md` file is
    not read by `sm`.
  - a verdict on **code** (may a native write command run at all) is the review file in the plugin's `reviews/`
    directory, shown by `sm self reviews`.
- **`why`**: the user types `sm pr why PR`. The core prints what the forge knows; the plugin adds its lines (merge
  queue, admission plan, known failures) through a hook. `sm why PR` stays for one release as the plugin's command
  and then goes.
