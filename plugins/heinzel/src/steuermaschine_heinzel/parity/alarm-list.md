# alarm check, alarm list

Old tool: the alarm tool of the scripted era (`check --no-notify`, `list`). Compared on 2026-11-12.

| Input | Compared | Result |
|---|---|---|
| 35 cases over every pure rule (stall, main red, candidate, admission, registry slots, release, rehearsal, required contexts), `tests/alarm_cases.py`, old answers in `tests/fixtures/alarm/rules.json` | alarm class, key and text after renaming old tools to sm commands | 35 of 35 equal |
| Live, 22:48 | old `check --no-notify` and native `alarm check` | both `OK`, exit 0 (no alarm held, so the live run proves little) |

Different on purpose:

- `alarm check` writes nothing and notifies nobody. The old check recorded first-seen times and could push the owner;
  the alarm loop still does both. An alarm the loop has not recorded yet prints as `new`.
- An alarm only the watch recorded and that no longer holds is not printed (the old check deleted it).
- Acknowledged = acknowledged in the old alarm file OR with `sm alarm ack` (the sm store). The old loop does not see
  an `sm alarm ack`: it can still push the owner again after its renotify time.
- Job names of a run are read without a jq filter; registry sizes are computed here (values never leave the process).
- Texts name sm commands; "sm status slots" where the old text named the hourly table.

Not ported: `alarm clear`, `alarm raise` (they change the alarm file the loop owns). `alarm ack` is the core command.
