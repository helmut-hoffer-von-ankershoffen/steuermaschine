"""How `go` spells every command it names. All of them are sm commands. The decision (decide.py) contains no
command text of its own, so the parity tests can swap in the spelling of the old tool and compare byte for byte."""

from __future__ import annotations

import shlex

TRAPS = {
    "sha": "Validation SHA trap: never dispatch a validation run on the exact SHA that is or will be a PR head; "
           "merge-gate and coverage-floor attach to the oldest suite of that SHA and the PR sits BLOCKED with every check green.",
    "batch": "Batch plan trap: pre-open plans prepared at one main are valid for the FIRST merge only; "
             "after any merge every other prepared packet is stale and must be rebuilt on the new main.",
    "truncated": "Truncated inventory: `prepare`/`apply` can refuse with 'truncated inventory' right after a run ends; "
                 "that is a retry of the identical command, not a new packet.",
    "idle": "Idle window: apply needs zero active Actions runs and takes the queue, train and release locks; "
            "start nothing (push, dispatch, rerun) until it reports success.",
    "first_attempt": "First-attempt rule: a failed candidate job is never rerun; read ALL failures first "
                     "(e2e lanes stop at the first one and hide the rest), fix forward, drop the candidate, cut again.",
    "consumer": "Consumer pin: a Core change to pyproject dependencies or extras breaks the ten plugin Consumer jobs "
                "in the candidate until the plugins are relocked and the Core pin PR is in; check before the cut.",
    "stall": "Stall trap: `sm queue why` can say 'no block recorded; the next tick should merge' for hours; "
             "a green queued PR unmerged after 15 minutes is an incident, not a wait.",
    "live": "Merged != published != live: a pinned uv receipt lets the updater exit 0 without installing; "
            "only sm mini verify proves the Mini runs the version.",
    "held": "Held is not broken: the updater installs a release only once its acceptance.json marker says accepted; the release tail "
            "uploads it at the verdict and only then marks it Latest. Never install by hand to make it live.",
    "red": "Red main: read the failing job first, a rerun is never the fix; nothing merges and nothing is cut onto a red main.",
    "cycle": "Every correction costs a full cycle (new head, new packet, review, apply, 25 min CI): "
             "read every failing check before changing the head, and change it once.",
    "review": "The apply review binds the exact bytes: the verdict line must carry the sha256 of the canonical prepared JSON; "
              "any re-prepare changes it and needs a new verdict.",
    "rehearsal": "A red rehearsal is the candidate failing in advance: lanes stop at the first failure and hide the rest; "
                 "fix forward on main and rehearse again, never cut onto it.",
    "focus": "A declared focus head blocks the cut: no rehearsal and no train until it is merged (or `sm focus done`); "
             "validate it on a validation/* commit of its own, never on the SHA that becomes the PR head.",
    "sonar": "sm ci sonar-close handles bugs and vulnerabilities only; code smells need a new head.",
    "plan_stale": "A plan binds one head and one main: the same command voids packet, reviews and prepare and builds them again on the new "
                  "main; the old verdicts do not carry over. Never apply a plan prepared at an older main.",
    "new_head": "A new head is a new run and a whole new cycle: run the command on the worktree, it supersedes this run; never reuse "
                "a verdict of the old head.",
    "interrupted": "The process of that step is gone: the same command continues from the saved state and adopts what the step already did; "
                   "do not redo the step by hand.",
    "compose": "A failed compose is re-adopted by every resume until a fresh compose runs: read why it failed, fix and publish the fix, then "
               "resume. Never rerun the failed run and never drop a release past its tag.",
}


def q(value: object) -> str:
    return shlex.quote(str(value))


class Words:
    def trap(self, key: str | None) -> str:
        return TRAPS.get(key or "", "")

    def drop(self, version: str, has_drop: bool) -> str:
        return f"sm release drop {version}" if has_drop else f"sm release resume {version} --pause"

    def ci_why(self, run: object) -> str:
        return f"sm ci why {run}"

    def release_status(self) -> str:
        return "sm release status"

    def release_finishing(self, version: str) -> str:
        return f"the release runner finishing v{version}"

    def main_evidence(self) -> str:
        return "sm ci main"

    def verify_mini(self, version: str) -> str:
        return f"sm mini verify --minimum {version}"

    def go(self) -> str:
        return "sm go"

    def queue_why_pr(self, number: object) -> str:
        return f"sm queue why {number}"

    def queue_tick(self) -> str:
        return "the queue tick (launchd, every 5 min)"

    def sonar(self, number: object) -> str:
        return f"sm head sonar {number}"

    def faillog(self, target: object) -> str:
        return f"sm ci faillog {target}"

    def ship(self, number: object) -> str:
        return f"sm head ship {number}"

    def pr_open(self, worktree: str, bead: str, subject: str, body: str | None = None) -> str:
        return f"cd {q(worktree)} && sm head pr {q(bead)} {q(subject)}" + (f" --body-file {q(body)}" if body else "")

    def apply_head(self) -> list[str]:
        return ["sm", "head", "apply"]

    def preopen_help(self) -> str:
        return "sm ship --help"

    def focus(self, verb: str, worktree: object = None, entry: dict | None = None) -> str:
        """`sm focus list|done`. An entry that only the old focus file holds cannot be taken off with `done`: the
        first `sm focus add` makes the focus list of sm the only source, and the old file is not read any more."""
        if verb == "done" and entry is not None and entry.get("source") == "old-file":
            return f"sm focus add {q(worktree)} --for {entry.get('for')} && sm focus done {q(worktree)}"
        return f"sm focus {verb}" + (f" {q(worktree)}" if worktree is not None else "")

    def rehearse(self, rest: str, typed: bool = True) -> str:
        return "sm release cut --help"  # no rehearsal any more

    def cut(self, version: str = "") -> str:
        """The cut of the next version: `sm release cut VERSION` (the pipeline needs the exact version)."""
        return f"sm release cut {version or 'VERSION'}"

    def pipeline(self, subject: str, verb: str, arg: str) -> str:
        return f"sm {subject} {verb} {q(arg)}"

    def alarm_ack(self, key: str) -> str:
        return f"sm alarm ack {q(key)}"

    def alarm_text(self, text: str) -> str:
        """A sentence a loop stored: the caller sets `rename` to the tool-map sweep."""
        return self.rename(text)

    def rename(self, text: str) -> str:
        return text

    def undecided(self) -> str:
        return "sm queue why"
