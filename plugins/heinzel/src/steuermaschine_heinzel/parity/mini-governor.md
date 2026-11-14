# Parity: `sm mini governor` against the former governor's `status` (`hz-governor` 6 lines + `hz_governor.py` 170 lines)

Caller today: by hand (`status`) and the coordinator's digest, which imports `digest_line()` (`GOV: stopped N pids, load L`).
No launchd job runs the governor's loop (checked: `launchctl list` and `~/Library/LaunchAgents`); a state and log file exist in
`~/.cache`, so it was run by hand. After the switch: `sm mini governor` for the status; the digest reader (another slice) calls
`guards.governor_digest(store, load)`.

| Output | Old | New |
|---|---|---|
| line 1 | `load 12.3 high=45 low=25 off=False stopped=3` | the same text (`GovernorStatus.render`) |
| lines after | `  PID ARGS` per stopped pid | the same |
| digest line | `GOV: stopped N pids, load L` when something is stopped or the load is above high | the same (`governor.digest_line(state, load)`) |

State: the store (kind `heinzel.guards.governor`, key `state`, written by `sm daemon governor`); while the store has no
record the former file under the setting `legacy_state_dir` is read (`legacy.governor_state`), unreadable = nothing stopped.
Thresholds: settings `guards_governor_high` (45) and `guards_governor_low` (25), replacing `HZ_GOV_HIGH` / `HZ_GOV_LOW`.

Differs on purpose: `stop` (touch the kill-switch file) and `resume-all` are not commands. The kill switch is the file of the setting
`guards_governor_off_file` (the name changed: the old name carried the old tool's); creating it and running one `sm daemon governor` continues
everything. Compared: the former tests of `digest_line` (state dict instead of a path) and a recorded-state read in the command test.
