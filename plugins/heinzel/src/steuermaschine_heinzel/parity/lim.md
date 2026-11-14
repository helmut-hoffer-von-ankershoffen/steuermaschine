# Parity: `sm lim` against the former output limiter (62 lines python)

Callers: used by hand and by agents in command lines; not a hook, not launchd. (`grep` of the tools tree: only its own tests.)
After the switch: `sm lim [-n N] [-t N] [-g REGEX] [-m N] [-w N] [-o FILE] [-s PIPELINE] -- COMMAND...`.

| What | Old | New |
|---|---|---|
| `condense` | the function | moved unchanged (the former smoke tests call it as before) |
| running the command | `subprocess.run(cmd, capture_output=True, text=True, errors="replace")` (`shell=True` for `-s`) | `commands.run(argv)` (`/bin/sh -c PIPELINE` for `-s`); the output is captured in both, nothing is streamed (as before) |
| text printed | stdout then stderr, condensed | the same |
| the log | `~/.cache/hz-lim/<HHMMSS>-<pid>.log`, or `-o FILE`, written only when something was cut | `<guards_lim_dir>/<HHMMSS>-<pid>.log` or `-o FILE`, written only when something was cut |
| last line when cut | `[<old name>: full output N lines, B bytes in LOG; exit RC]` | `[lim: full output N lines, B bytes in LOG; exit RC]` |
| exit code | the command's | the command's (`Limited.exit_code`) |
| no command and no `-s` | argparse error, exit 2 | exit 2, "give -s 'cmd' or -- COMMAND ARGS" |

Compared: the two former tests (condense; `seq 1 100; exit 3` with `-n 1 -t 1 -o FILE -s`: exit 3, 100 log lines, "full output
100 lines"), the latter against the real process provider. Short output is printed unchanged and writes no log; empty output prints one empty line, as before.

Differs on purpose / known: output that is not valid UTF-8 raises in the core process runner (`text=True` without
`errors="replace"`, a core detail not changed here); the old limiter replaced bad bytes. Options are Typer options
(`-n -t -g -m -w -o -s` kept), the command after `--`.
