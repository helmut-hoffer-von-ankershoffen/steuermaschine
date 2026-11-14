# Parity: `sm doc handoff`, `sm doc hour` against the former handoff and hourly tools

Not the old side effects: the handoff commits only the snapshot it wrote, checks first, never rebases or stashes, and
pushes fast-forward only (reviews/doc-handoff.md lists every difference and why). The old tests (`test_release_handoff.py`, `test_flakes_and_handoff.py`)
exercised the status tool's tables and the flake counts, which are other areas; the local-work rule (worktrees
changed within three days, not on main, with unpushed commits or uncommitted files) and the snapshot's sections are
covered by new tests on the fake process runner. Differs on purpose: reviews/doc-handoff.md and reviews/doc-hour.md
(no ruleset flip, facts from native commands, plain pull request table, no Sonar fetch).
Not compared: a real handoff (nothing was committed or pushed during the port).
