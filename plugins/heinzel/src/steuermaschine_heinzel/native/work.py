"""`sm work ...`: the owner's work list under one verb. It starts as a second name for `beads` and `focus` (the same
commands, the same review gate); surface.py mounts the visible verbs (`work add`, `work list`, ...) and hides the rest."""

from __future__ import annotations

from steuermaschine.helpdoc import H
from steuermaschine.native import Alias

WORK = Alias("work", "beads", H(
    "work: the work items (beads): add, list, show, note, done; hand out work with brief, refill and sweep.",
    "Use for every owner ask, every change of a work item, and to start a builder on the next one.",
    "add, note, done and brief write (the tracker, a worktree); list, show, refill and sweep read.",
    "sm work list ready",
    "sm work show ID"))
WORK_FOCUS = Alias("work focus", "focus")
