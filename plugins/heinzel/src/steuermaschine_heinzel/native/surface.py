"""The visible surface of this project: twelve verbs a cold agent can guess (the steuermaschine core mounts them, see
`steuermaschine.surface`). Everything else is hidden and keeps its path: the service jobs (`daemon queue`, `daemon
train`, `daemon loadguard`, `mini leakwatch sample`) and the hooks (`guard git`, `guard heavy`, `guard uv`) call it,
and so does the release train (`release resume`, `head ship`, `queue add`, `ci defer`, `mini update`).

An old visible name that has a new verb is a stub (`Renamed`): a read-only one runs the new command and says
`Renamed: ...` on stderr; a writing one says it and exits 2. A name some code or job still runs as a child process
keeps working unchanged (`stub=False`); only texts such as NEXT name the new verb (`steuermaschine.surface.translate`).
"""

from __future__ import annotations

from steuermaschine.helpdoc import H
from steuermaschine.native import Default, Group, Renamed, Surface, Verb

SURFACE = Surface(
    verbs=(
        ("", "where things stand, and the ONE next command (NEXT) with its flags"),
        ("check [WORKTREE]", "the cheap checks of a change, before it goes out: READY, or every failure with its fix"),
        ("ship WORKTREE|PR", "push the change, open its pull request and queue it for merge (--live); --wait to see the merge"),
        ("review next|verdict|sonar PR", "what waits for an independent review; record a verdict; close reviewed Sonar findings"),
        ("release [VERSION]", "release a version in GitHub Actions and watch it (the Mini follows by itself); `release plugin NAME`"),
        ("why [SUBJECT]", "why something does not move: a PR number, a version, a run id, `main`, a branch, a bead"),
        ("status", "every open pull request with its action and owner; --write: the hourly update (--live: push it)"),
        ("work add|list|show|note|done", "the work items (beads); `work brief`, `work refill`, `work sweep` to hand out work"),
        ("mini verify|update", "prove what runs on the Mini; repair it (it updates itself)"),
        ("alarm list|ack", "what needs someone now (--check: read GitHub now); acknowledge one"),
        ("note TEXT", "one dated line in the log of the tracking document"),
        ("self config|tree|stats|lint|tokens|doc", "the tool itself: settings, all commands, use counts, help lint, a reference"),
    ),
    visible=(
        "check", "ship", "why", "status", "note",
        "review", "review next", "review verdict", "review sonar",
        "release", "release plugin", "release drive",
        "work", "work add", "work list", "work show", "work note", "work done", "work brief", "work refill", "work sweep",
        "mini", "mini verify", "mini update",
        "alarm", "alarm list", "alarm ack",
        "self", "self config", "self tree", "self stats", "self lint", "self tokens", "self doc",
    ),
    helps=(("release", H(
        "release: one version from main to Latest, in GitHub Actions; bare: where the recent releases stand.",
        "Use when a release is due, running or stopped: the same command continues where it stopped.",
        "`release VERSION` dispatches Core's workflow \"Release\", which cuts, merges, tags, publishes and accepts; bare: nothing (read-only).",
        "sm release 0.9.55", "sm why 0.9.55 while it runs; the Mini follows an accepted release by itself (sm daemon train)",
        "VERSION is X.Y.Z or `next` (the release in flight, else the next patch), and the release in flight, the latest\n"
        "release or the next patch or minor after it. --dry-run checks the cut and the tail and publishes nothing.\n"
        "`sm release plugin NAME` releases one plugin whose version on main is not published yet.")),),
    rules=("  WORKTREE        a git worktree of one change on its own branch; PR a pull request number of the core repository.",),
)

MINI = Group("mini", H(
    "mini: what runs on the Mini, proved; and its repair.",
    "Use after a release went out, or when the Mini looks wrong.",
    "verify reads only; update installs a release on the Mini and proves it.",
    "sm mini verify", "sm mini update --minimum VERSION   (only when verify says it is behind)"))

WORK_ADD = Verb("work add", "beads tasks add")
WORK_LIST = Verb("work list", "beads tasks ls")
WORK_SHOW = Verb("work show", "beads tasks show")
WORK_NOTE = Verb("work note", "beads tasks note")
WORK_DONE = Verb("work done", "beads tasks done")
REVIEW_NEXT = Verb("review next", "review list")

RELEASE = Default("release", "run", empty="train status", metavar="[VERSION] | plugin NAME")  # `sm release 0.9.55` runs Core's CI release chain; bare: where the releases stand

# Old visible names with a new verb. A stub (the default) where the new verb runs the very same command: a read-only
# one runs it and says `Renamed:` on stderr, a writing one exits 2 and names it. Where the new verb reads more or
# drives a different pipeline (`why PR` beside `ci why PR`, `ship` beside `head pr`), or code and jobs still run the
# old name as a child process, the old name stays hidden and working (`stub=False`) and only texts such as NEXT and
# the resolver name the new verb; `sm self stats --calls` shows when nothing calls it any more (S9 deletes it then).
RENAMES = [
    Renamed("beads tasks add", "work add"), Renamed("beads tasks note", "work note"), Renamed("beads tasks done", "work done"),
    Renamed("beads tasks ls", "work list"), Renamed("beads tasks show", "work show"),
    Renamed("work tasks add", "work add"), Renamed("work tasks note", "work note"), Renamed("work tasks done", "work done"),
    Renamed("work tasks ls", "work list"), Renamed("work tasks show", "work show"),
    Renamed("beads brief", "work brief"), Renamed("beads refill", "work refill"), Renamed("beads sweep", "work sweep"),
    Renamed("review list", "review next"),
    Renamed("ci sonar-close", "review sonar"),
    Renamed("ci main", "why main"),
    Renamed("ci why", "why", stub=False), Renamed("pr why", "why", stub=False), Renamed("queue why", "why", stub=False),
    Renamed("head merge-ready", "why", stub=False), Renamed("head sonar", "why", stub=False), Renamed("pr sonar", "why", stub=False),
    Renamed("release status", "why", stub=False),
    Renamed("head pr", "ship", stub=False), Renamed("head admit", "ship", stub=False), Renamed("release cut", "release", stub=False),
    Renamed("doc hour", "status --write", stub=False), Renamed("doc handoff", "status --write --live", stub=False),
    Renamed("tick", "alarm list --check", stub=False), Renamed("alarm check", "alarm list --check", stub=False), Renamed("doc asks", "alarm list", stub=False),
    Renamed("go", "", stub=False),
    Renamed("release resume", "release drive", stub=False),  # every option of resume; `sm release VERSION` is the CI chain
    Renamed("head ship", "ship --live", stub=False),
    Renamed("queue add", "ship", stub=False),
    Renamed("queue list", "why", stub=False),
    Renamed("ci faillog", "why", stub=False),
    Renamed("ci one status", "why", stub=False),
    Renamed("release drop", "why", stub=False),
    Renamed("release candidate", "why", stub=False),
    Renamed("head status", "status", stub=False),
]
