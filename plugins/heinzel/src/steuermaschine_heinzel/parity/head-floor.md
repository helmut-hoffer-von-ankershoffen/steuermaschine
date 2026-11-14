# head floor: parity with the former floor check

Code: `native/packet_lib/floor_check.py` is the former script minus its `--help` shortcut and its argv entry
(`packet_verbatim.py diff --only floor_check`). `gather` and `evaluate` are unchanged; the command passes them a git
reader on the process runner and a GitHub reader on the one client. The plan registry is still decoded by the base's
own reader in an isolated interpreter.

Compared: the moved tests (every reason text), plus the same three GitHub reads asserted in
`test_floor_ready_in_text_and_json`. NOT compared against live GitHub in this slice (no authenticated client in the
build session): the first live run should be read beside the old tool's line for the same head.

Different on purpose: the command is `head floor` (the tool map said `head check`); `--repo` defaults to the setting
`repo`; unreadable state is a refusal with exit 1, not exit 2.
