# mini loops (read-only)

COVERS: mini loops

Old: `tools/hz-reload-loops` + `tools/hz_reload_loops.py` [--apply]. New: `native/loops.py`.

## Freshness: by the installed sm release, not by script mtimes
The old check compared the process start time with the newest mtime of `~/.local/bin/<program>` and the `tools/*.py`
it imported. Every management loop now runs `sm` (the live `hz-queue` job runs
`<stable>/releases/<id>/venv/bin/steuermaschine daemon queue --loop 1`), so those mtimes say nothing about the code a
loop runs (the PR #11 review finding). New: the release id in the process's command line against the release the
stable pointer (`current`) resolves to. `test_script_mtimes_do_not_matter` pins this.

## Compared live (2026-11-13 22:3x, read-only, labels set in a scratch config)
```
hz-queue       fresh        process started Fri Nov 13 22:06:33 2026 runs the installed release 903a1b748acf
hz-train       not loaded   no launchd job of this label
hz-leakwatch   not running  no process (interval job or stopped)
hz-loadguard   not running  no process (interval job or stopped)
daemon         skipped      the live service's label, never judged
```
The old tool was not run (the hook refuses the retired tools directory). By its code, for the same rows it would print
`hz-queue` against the mtimes of `~/.local/bin/hz-queue` (no longer the program the job runs), and `not running` for
`hz-train` (it has no `not loaded` state).

## What differs on purpose
| Old | New |
|---|---|
| fixed label list in the code (`hz-queue`, `hz-train`, `hz-leakwatch`, `hz-loadguard`) | setting `mini_loops_labels` (no default: per machine); refuses when empty |
| `stale` by mtime | `stale`: runs another release of the install; `foreign`: runs no sm release (an old program) |
| no process = `not running` | `not running` (loaded, no pid) and `not loaded` (no such job) |
| exit 1 when stale (without --apply) | exit 1 when stale, foreign or unreadable |
| `--apply`: `launchctl kickstart -k` every stale label | retired, see below |

## `--apply` retired, with the evidence
- The core ends a loop whose release pointer moved (`src/steuermaschine/native.py`, the `while loop > 0` block: "another
  release is live now; this loop ends so the service manager starts it on the new one"); the jobs have `KeepAlive`, so
  launchd starts it again on the installed release. A promote needs no restart.
- What stays stale is a loop stuck inside a tick. `kickstart -k` would kill it mid-tick: for the merge queue that can
  cut a merge (the PR #11 reviewer's finding: `--apply` would kickstart the live queue).
- A `foreign` loop runs an old program; a kickstart restarts the same old program. Its plist must change (integrator).
- Callers of `--apply`: only the old `hz-tick` printed it as advice (`run hz-reload-loops --apply`); the native tick
  does not. No launchd job, hook or skill runs it.
