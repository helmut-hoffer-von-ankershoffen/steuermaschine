# Parity: `sm ci quarantine list` against the former quarantine tool (status, `--line`, `--digest` without the note)

| What | How compared | Result |
|---|---|---|
| Registry lines, streaks, last results | the old tool's 5 tests against the moved module | pass |
| The status view | scripted gh (registry, 3 runs of main, their reports): same three lines per entry and the streak line | identical text |
| `--line`, `--digest` | same lines; `--line` with nothing to say prints one empty line, as before | identical text |
| A real read | `sm ci quarantine list` logic against GitHub, 2026-11-12 | `quarantine: 0/8 entries on main` (the registry has no entry) |

Differs on purpose: `--digest` here never writes a bead note (that is `sm ci quarantine flag`); the lane reports are
remembered in the store, not as files; exit 1 for a registry that cannot be parsed is unchanged.

Not compared: a registry with real entries and real lane artifacts (there is none today).
