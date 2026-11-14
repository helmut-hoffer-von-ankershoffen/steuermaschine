# Parity: `sm guard git` against the former git hook (10-line sh around a python one-liner)

Caller today: `~/Code/.claude/settings.json`, event `PreToolUse`, matcher `Bash`, command `/Users/<user>/.local/bin/<old hook>`.
Stdin: the hook JSON `{"tool_input": {"command": ...}}`. Exit 2 blocks the call; the reason is on stderr.
Caller after the switch (the lead or owner changes it; not done here): command `sm guard git`.

| Input | Old | New |
|---|---|---|
| a git push or commit with `--no-verify`, `--force`, `--force-with-lease`, `-f`, `-n` (also `=`-joined, also after `&&`/`;`) | exit 2, "Blocked: no --no-verify or force push. If pre-push fails, wait for lower load or report it." plus a line naming the old hook | exit 2, `sm: Blocked: no --no-verify or force push. ...` on stderr (same sentence) |
| `git commit -f ...` | allowed | allowed |
| no command, any other command | exit 0 | exit 0 (`HookVerdict`, nothing printed) |
| unreadable input: not JSON, not an object, `tool_input` not an object, `command` not a string | python raised, the `||` of the sh made it exit 2 | exit 2, "Blocked: the hook input could not be read (...)" |

Fail mode: **CLOSED**, kept. Cases compared: the 8 refusing and 6 allowed commands of `test_native_guards_hooks.py`, the
unreadable inputs above. The regexes are copied unchanged.

Differs on purpose: stderr says `sm:` instead of the old hook's name; `--input FILE` reads a file instead of stdin (for tests
and for a hook runner that cannot pipe).
Not compared: Claude Code itself calling the hook (not run; the hook settings are not touched by this port).
