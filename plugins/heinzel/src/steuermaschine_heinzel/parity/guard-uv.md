# Parity: `sm guard uv` against the former uv shim (58 lines bash) and its PATH shim

Caller today: a PATH shim first on PATH in agent and builder shells (`tools/shim/uv`, 3 lines: `exec <the guard> "$@"`) and the
run lock puts it first on the PATH of what it runs. Not a launchd job, not a hook.
Caller after the switch: the shim becomes `exec sm guard uv -- "$@"` (not changed here).

The former 114-line test file ran the shim with a fake uv that recorded its argv. The same cases run through `invoke` with a
scripted `FakeCommands` standing in for the real uv: 16 refused combinations (4 subcommands x 4 package spellings), upgrade
--all and live-dir cases, isolated passes, the allowed-and-logged case, 5 passthroughs, 12 global-flag, URL, wheel, `--from`,
`--with` and `-a` cases, a local source directory, upgrade --all without a live tool. Assertions unchanged except: "returncode"
is the command's exit code (3 refused, uv's own otherwise), `calls` is the argv the fake real uv got, and the log of an
allowed live use is a record in the store.

| Behaviour | Old | New |
|---|---|---|
| refusal | exit 3, three lines on stderr naming the old guard and `hz-mini` | exit 3, `sm: REFUSED 'uv ...' (bead ppww) ...` with the same advice; no old tool name |
| allowed live use (`HZ_ALLOW_LIVE_UV=1`) | one line appended to a log file under `~/.cache` | one record in the store, kind `heinzel.guards.live_uv` (key: time and pid; fields at, pid, ppid, cwd, args) |
| pass | `exec` of the real uv by absolute path (`HZ_REAL_UV`, default `/opt/homebrew/bin/uv`) | `commands.passthrough([real uv, ...])`; setting `guards_real_uv`; the exit code is uv's |
| home of the live dirs | `HZ_UV_GUARD_HOME` or `$HOME` | setting `guards_uv_home` (default `~`) |
| failure to decide | none possible: the decision reads strings and two `-d` / `grep` tests | same (an unreadable pyproject counts as "does not name heinzel", as before): fail OPEN toward uv |

`UV_TOOL_DIR`, `UV_TOOL_BIN_DIR` and `HZ_ALLOW_LIVE_UV` are read from the environment by the edge and handed to the pure decision
(the same data-passing as `sm gate run`); they are uv's own environment and the opt-in's name, not settings.

Differs on purpose: `--help-guard` is gone (`sm guard uv --help`). The reader of the log of allowed live uses (the digest's
"live uv tool changed" warning, another slice) reads the store records; the old log file is not written any more.
Not compared: a real uv.
