# Parity: `sm guard heavy` against the former heavy-run hook (74 lines python)

Caller today: **none found.** `~/Code/.claude/settings.json` registers only the git hook; `~/.claude/settings.json` has no hooks;
no launchd job; the tool's own notes say "no testguard binding in inspected provider configs". It is described as a PreToolUse
hook on Bash with the same JSON on stdin (`{"tool_input": {"command": ...}, "cwd": ...}`) and exit 2 = block.
Caller after the switch, if the owner binds it: command `sm guard heavy` on the same event and matcher as the git hook.

Moved: `HEAVY`, `RUNNERS`, `BANNED`, `segments`, the decision order (banned names first, then the management exemption,
then each segment). The former tests (14 cases: bare heavy runs and the banned desktop tests refused, gated and light
commands allowed, bare tests allowed in the management checkout but not the banned ones) run through `invoke` with the same
commands and assertions (exit 2 and "Blocked" in stderr, exit 0).

| Input | Old | New |
|---|---|---|
| bare `pytest`, `vitest`, `tsc`, `mypy`, `playwright`, `make`, also behind `uv`, `npx`, `python -m`, `env`, ... | exit 2, "heavy runs go through hz-gate" | exit 2, "heavy runs go through the run lock ... Prefix the command with `sm gate run --`" |
| the same behind `hz-gate` | allowed | allowed (still) |
| the same behind `sm gate run --` | not a case (the tool did not exist); `env X=1 sm gate run -- pytest` would have been refused as a bare run | allowed: a segment starting with `sm gate run` is gated |
| the two desktop tests anywhere | exit 2 | exit 2 (the guard words `hz-gate`, `sm` join `pytest`, `python`, `uv`) |
| management checkout | any directory above the cwd holding `tools/<the hook's own file>` | any directory at or above the cwd holding the file of the setting `guards_management_marker` (default `HLD.md`) |
| unreadable input | exit 0 for non-JSON; a traceback (exit 1, not blocking) for JSON of another shape | exit 0 for both |

Fail mode: **OPEN**, kept (the hook protocol treats exit 1 as non-blocking, so the old traceback case was open too).

Differs on purpose: the marker (the old one named a file of the old tool tree, which retires with it; `HLD.md` exists in the
management checkout and not in the core clone: checked with `ls`). The owner can set the setting to anything else.
