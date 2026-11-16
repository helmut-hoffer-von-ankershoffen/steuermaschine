"""`sm work ...`: the owner's work list under one verb. It is a second name for `beads` and `focus`: the same commands,
the same review gate and help. Both old names keep working."""

from __future__ import annotations

from steuermaschine.helpdoc import H
from steuermaschine.native import Alias

WORK = Alias("work", "beads", H(
    "work: the beads, the briefs and the critical path (alias of `beads` and `focus`).",
    "Use for every owner ask, bead state change, brief, sweep and for what ships next.",
    "tasks add, note, claim, due-set, pr, done, brief, stamp and focus add|done write; the rest reads.",
    "sm work tasks ls",
    "sm work focus list"))
WORK_FOCUS = Alias("work focus", "focus")
