# Parity: `sm daemon governor` against the former governor's `run` loop

Caller today: none scheduled (no plist, no hook); started by hand as `run [--dry-run] [--once]`. The brief put only `mini governor` (read)
in scope "unless the source writes something": it does (SIGSTOP/SIGCONT, a state file, a kill-switch file), so its write side is
this command, with a review file. After the switch, if the owner wants the loop again: a launchd job `sm daemon governor --loop 15`.
`--once` is the command without `--loop`; `--dry-run` is kept.

| Decision | How compared |
|---|---|
| stop in descending CPU order (6 per tick), only eligible processes (own uid, under builder worktrees by args or cwd, never the NEVER list, never itself or its parent, cpu > 0.5) | the former tests moved onto the library: stops and skips protected, dry run signals nothing, 6 resumed per 20 s, mid load holds, kill switch resumes all and drops dead pids, the never-pause list (6 cases) |
| thresholds | bound from settings by `governor.bind` |

| Edge | Old | New |
|---|---|---|
| `ps -axo pid=,ppid=,pcpu=,uid=,args=` | `subprocess` | the same argv through the Commands provider |
| cwd of a candidate whose name is a build tool | `lsof -a -d cwd -Fn -p PID` | the same argv through the provider (only for the candidates that need it) |
| signals | `os.kill` with `safe_kill` | `kill -STOP/-CONT PID` through the provider, failures ignored |
| state, log, kill switch | `~/.cache/<old name>.json/.log/.off` | store `heinzel.guards.governor`/`state`; text on stdout; file of `guards_governor_off_file` |
| loop | `while True: ... sleep(15)` | the core's `--loop SECONDS` |

Differs on purpose: unreadable `ps` signals nothing and exits 1 (the old code would have dropped every stopped pid from its state);
`BUILDER` matches any user's `pytest-of-<user>` temporary directory instead of the owner's (processes of other users are never eligible anyway);
`NEVER` also protects `sm daemon|queue|release|train|go|gate|mini` command lines. Two moved tests are not here: they sent a real signal to a
vanished pid through `safe_kill`, which no longer exists (the command test asserts a failing `kill` is ignored).
Not compared: a real tick with real processes.
