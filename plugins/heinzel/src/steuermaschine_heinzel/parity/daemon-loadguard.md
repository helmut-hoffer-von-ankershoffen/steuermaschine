# Parity: `sm daemon loadguard` against the former load guard (`tick`, 165 lines)

Caller today: launchd job `io.github.helmut-hoffer-von-ankershoffen.hz-loadguard`, plist `~/Library/LaunchAgents/...hz-loadguard.plist`
(read only here): `ProgramArguments` = `~/.local/bin/<old tool> tick`, `StartInterval` 20, `RunAtLoad`, stdout and stderr to
`~/Library/Logs/hz-loadguard.log`, `WorkingDirectory` the management checkout.
Caller after the switch (not changed here): `ProgramArguments` = `~/.local/bin/sm daemon loadguard` (keep `StartInterval` 20 and
`RunAtLoad`; or `sm daemon loadguard --loop 20` with `KeepAlive` instead of the interval). Nothing else changes.

**What the write side is.** The brief says it writes the run lock's hold file. It does not: the former tool sends SIGSTOP to the
newest ungated pytest/coverage process tree above load 100 and SIGCONT to the paused trees below load 40, one tree per tick, and
records the paused trees in `~/.local/state/<old name>/stopped.json`. It never touches the hold file. This port keeps exactly that
(see the review file); the run lock's hold file (`Policy.hold`) is not used.

| Decision | How compared |
|---|---|
| which trees are candidates (this user, under `~/Code/<worktree>/`, pytest/py.test/coverage, topmost, no test ancestor, no run-lock or release ancestor), newest first | the former 4 tests moved onto the library, assertions unchanged |
| one tree per tick; stop only if not already paused and not state `T`; resume the earliest; between 40 and 100 nothing | same tests |
| a paused root that vanished or now runs another command is forgotten, never continued | same tests |
| order of signals: root first on stop, children first then root on resume | same tests (and the call order of the command test) |

| Edge | Old | New |
|---|---|---|
| process table | `ps -axo pid=,ppid=,uid=,stat=,etime=,command=` (timeout 30) | the same argv through the Commands provider |
| signals | `os.kill` (errors ignored) | `kill -STOP PID` / `kill -CONT PID` through the Commands provider (failures ignored) |
| load | `os.getloadavg()[0]` | the same (`guards.load_now`) |
| paused list | `stopped.json` file | store kind `heinzel.guards.loadguard`, key `stopped`; **while the store has no record the former file is read once** (`legacy.loadguard_stopped`), so a tree paused by the old job is still continued |
| log | one line per action in `~/Library/Logs/...log` | the same text with a timestamp on stdout (launchd's `StandardOutPath` file) and in the model's `lines` |
| `status` | printed the paused list | not a command here: export `guards.loadguard_paused(store, legacy_dir)` for the read slice |

Differs on purpose:
- **`ps` unreadable or empty**: the old tick treated it as "every paused pid is gone" and saved an empty list, so trees it had paused
  stayed stopped for ever. The new tick signals nothing, keeps the list and exits 1.
- `GATED` also knows `sm gate run`, `sm release` and `sm train` (the commands that replace the old gate, release and train tools); without
  this the guard would pause every run under the new run lock above load 100. A test pins the look-alike `sm gates run` as NOT gated.
- thresholds stay constants (100 / 40), not settings: nobody should be able to raise them.
- the home for `~/Code` is the account's home (`Path("~")`); the worktree location is not configurable (as before).

Not compared: a real tick (it would signal real processes; none was run).
