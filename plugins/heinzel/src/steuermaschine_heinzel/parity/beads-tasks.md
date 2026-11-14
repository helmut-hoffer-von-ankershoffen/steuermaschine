# Parity: `sm beads tasks ...`, `sm beads refill`, `sm doc checkpoint` against the former task, refill and start tools

`beads tasks`: the old tool had no test; compared by reading (77 lines): same tracker argument lists, same due label
rule, same list line format, same 1800-character cap for `show`; side effects in reviews/beads-tasks.md.
`beads refill`: the helper module moved unchanged with its four tests (`tests/coord_verbatim.py`); the command
replaces the old main with the tracker and GitHub on the one process runner; target slots, load limit and the
90 minutes are settings.
`doc checkpoint`: the checkpoint reader of the former start tool against the document's new layout (first `### `
block of the section `## 5.` up to `## 6.`, first 14 lines); section, end and line count are settings. Its timer
inventory of another agent runtime is not ported (retired with the tool).
Not compared: runs against the real tracker.
