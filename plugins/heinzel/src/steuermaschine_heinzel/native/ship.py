"""Shipping one head, native: `sm head push | pr | ship | guard`.

The logic is in `ship_lib/`: the moved push gate (verbatim; `port/move.py verify`), and `ship_lib/ship.py`, the
port of the former ship shell script. This module is the edge:
typed options, a result model, the plugin's settings. What a moved tool printed stays on its stream; hints name `sm`.
"""

from __future__ import annotations

from pathlib import Path

import typer
from pydantic import BaseModel, Field

from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import Group, native, runtime
from steuermaschine.plugins import old_variable

from . import branch_owner, queue_hold
from .journal_view import mirrored
from .queue_lib import hzlib
from .ship_lib import deps, pr, push_gate, repos, ship
from .ship_lib.deps import Done


class ShipSettings(BaseModel):
    """Settings of `sm head pr`, a mixin of the plugin's table."""

    head_pr_lock_max_wait_seconds: int = Field(1800, ge=1, description="How long `sm head pr` waits for the run lock before its one push; "
                                                                        "then it gives up with exit 75 and nothing pushed.")
    head_push_minimum: int | None = Field(None, ge=0, description="The coordinator's waiver of the release hold of `sm head push`, as if --min MINUTES were given: "
                                                                  "a push may then run while a release candidate run is active.", json_schema_extra=old_variable("HZ_PUSH_GATE_MIN"))

GROUP = Group("head", H(
    "head: take one head from a worktree to a queued PR (push check, PR, guard, queue).",
    "Use when a head is committed and should go out, and before every push that starts PR CI.",
    "depends on the command; push and guard only read, pr pushes and opens the PR, ship queues it.",
    "sm head push 801", "the NEXT line the command prints"))

# What the write commands execute: ship_lib calls the queue's add (native/queue.py),
# the diff guard and the edges (queue_lib), and the preflight area's CI meta preflight (`meta_of`) and its check of
# the title and body a PR would publish (`texts_check`) in native/preflight.py + preflight_lib. `head combine-close`
# (native/merges.py) and `head merge-ready` (native/merge_ready.py) are other areas' commands; nothing here runs them.
def _translate(text: str) -> list[str]:
    """The lines of the moved code's output (it names the sm commands itself)."""
    return text.splitlines()


class HeadResult(Model):
    """What a `head` command of this area did: the lines it printed, and its verdict as a word."""

    action: str
    target: str
    verdict: str = Field(description="ok | wait | queued | needs_review | cleared | go | no_go | pass | flag | unreadable | refused | usage | done | dry_run | failed")
    lines: list[str] = []
    errors: list[str] = Field([], description="What the command wrote to stderr, one line each.")
    exit: int = 0

    def render(self) -> list[str]:
        return self.lines

    @property
    def exit_code(self) -> int:
        return self.exit


def _result(action: str, target: str, done: Done, verdicts: dict[int, str], default: str = "failed") -> HeadResult:
    errors = _translate(done.stderr)
    for line in errors:
        runtime().say(line)
    return HeadResult(action=action, target=target, verdict=verdicts.get(done.returncode, default), lines=_translate(done.stdout), errors=errors, exit=done.returncode)


@native("head push", replaces=("hz-push-gate",), help=H(
    "Before a push that starts PR CI: always OK; info lines name a starved main CI or an active release candidate (PR CI queues behind them).",
    "Use before every `git push` to a PR branch; commit locally and batch commits into one push.",
    "nothing (read-only).",
    "sm head push 801", "push once (`sm gate run -- git push`)",
    "Exit 0 push now, 1 fix the CI meta problems first. Without --gate-only the CI meta preflight runs first.\n"
    "A release candidate and a merge freeze never refuse a push: they hold main merges."))
def head_push(
    target: str = typer.Argument(..., metavar="PR|BRANCH", help="PR number, or a branch that has no PR yet."),
    gate_only: bool = typer.Option(False, "--gate-only", help="Only the push gate (main CI starvation, release hold); skip the CI meta preflight."),
    minimum: int | None = typer.Option(None, "--min", metavar="MINUTES", help="Main CI queue age that triggers the info line (default 15)."),
) -> HeadResult:
    if gate_only or minimum is not None:
        code, lines = push_gate.check(target, minimum)
        done = Done(code, "".join(f"{line}\n" for line in lines))
    else:
        deps.configure()
        done = ship.push(target)
    return _result("push", target, done, {0: "ok", 75: "wait", 1: "refused", 70: "failed"})


@native("head guard", replaces=("hz-diff-guard",), help=H(
    "PASS or FLAG for a PR's diff: does it weaken a check (timeout, continue-on-error, a lowered threshold)?",
    "Use before queueing a PR by hand, or to see why `sm head ship` answered NEEDS_REVIEW.",
    "nothing (read-only).",
    "sm head guard 801", "PASS: sm head ship PR; FLAG: an independent review, then sm head ship PR \"reason\""))
def head_guard(pr: list[str] = typer.Argument(..., metavar="PR", help="A PR number; `plugins 38` names a PR of another repository.")) -> HeadResult:
    deps.configure()
    done = deps.captured(hzlib.load("diff_guard").main, list(pr))
    return _result("guard", " ".join(pr), done, {0: "pass", 1: "flag", 2: "unreadable"})


@native("head ship", writes=True, replaces=("hz-ship",), help=H(
    "Guard the diff, then queue the PR for merge; with a reason from an independent review, queue as reviewed.",
    "Use when a PR is open and its head is final.",
    "adds the PR to the merge queue (and a note when a reason is given); nothing when the guard answers NEEDS_REVIEW.",
    "sm head ship 801", "sm go   (it shows the queue wait and flags a stall)",
    "Exit 0 queued or already cleared, 2 NEEDS_REVIEW (start an independent reviewer; ship again with its written reason),\n"
    "1 the queue refused (moved head, combine notes, `sm queue hold`). --fix-forward passes PR, REASON and every option to the queue's\n"
    "fix-forward lane, which refuses REASON and --reviewed-head there (exit 1, nothing queued)."))
@mirrored  # it runs a program that writes the working files: the subject journals follow (journal_view.py)
def head_ship(
    pr: str = typer.Argument(..., metavar="PR", help="The pull request number."),
    reason: str | None = typer.Argument(None, metavar="REASON", help="The written reason of an independent review: queue past a FLAG."),
    reviewed_head: str | None = typer.Option(None, "--reviewed-head", metavar="SHA", help="Pin the PR to its independently reviewed full SHA; needs REASON."),
    fix_forward: bool = typer.Option(False, "--fix-forward", help="The fix-forward lane while main is red; needs --for."),
    fix_for: list[str] = typer.Option([], "--for", metavar="JOB=BEAD[@PR]", help="Fix-forward: which bead addresses each failing main job."),
    reviewed: str | None = typer.Option(None, "--reviewed", metavar="REASON", help="Fix-forward: the reason of an independent review."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fix-forward: print every check and the verdict, queue nothing."),
) -> HeadResult:
    queue_hold.refuse_while_held(f"sm head ship {pr}", pr=pr)
    deps.configure()
    if fix_forward:  # every option reaches the lane, as the script's "$@" did: the queue refuses what the lane does not take
        options = [x for value in fix_for for x in ("--for", value)]
        if reviewed is not None:
            options += ["--reviewed", reviewed]
        if reviewed_head is not None:
            options += ["--reviewed-head", reviewed_head]
        done = ship.fix_forward(pr, [*options, *(["--dry-run"] if dry_run else [])], reason)
        return _result("ship", pr, done, {0: "queued", 1: "refused"})
    if fix_for or reviewed is not None or dry_run:
        done = Done(2, "", "usage: --for, --reviewed and --dry-run belong to --fix-forward\n")
        return _result("ship", pr, done, {2: "usage"})
    done = ship.ship(pr, reason, reviewed_head)
    verdict = "cleared" if done.stdout.startswith("CLEARED") else "needs_review" if "NEEDS_REVIEW" in done.stdout else "usage" if done.stderr.startswith("usage:") else "refused"
    return _result("ship", pr, done, {0: "cleared" if verdict == "cleared" else "queued"}, default=verdict)


def is_focus(worktree: Path) -> bool:
    """Whether the worktree is a declared focus head (`sm focus add`): its push is on the critical path."""
    listed = runtime().providers.store.load("focus", "list") or {}
    here = Path(worktree).expanduser().resolve()
    return any(str(key).startswith(("/", "~")) and Path(str(key)).expanduser().resolve() == here for key in listed)


@native("head pr", writes=True, replaces=("hz-pr-open",), help=H(
    "From a clean worktree to a queued PR: checks, one pinned push under the run lock, the PR, then ship.",
    "Use when the head is committed on a work branch. Without --live it checks and prints what it would run.",
    "nothing without --live. With --live: pushes the branch, opens the PR, queues it. It never switches auto-merge on.",
    "sm head pr abc \"fix(web): keep the draft when the tab reloads\" --body-file pr-body.md --live", "sm go",
    "Refuses on main, with uncommitted changes, for a title over 72 characters, behind origin/main, and when the remote\n"
    "branch has commits this head lacks (it never rebases or forces). The title and the body are checked against the\n"
    "commit-message rule and the confidential terms before anything is pushed or opened; nothing you wrote is printed\n"
    "before that. It pushes the checked commit (SHA:refs/heads/BRANCH) and ships only this repository's PR at that SHA.\n"
    "Exit 75: the push check says WAIT, or the run lock was not free within head_pr_lock_max_wait_seconds.\n"
    "The push takes a fast slot of the run lock (up to three at once, fewer under load, no load bar; a focus head goes ahead of the\n"
    "others): its pre-push hook runs the fast checks. Full suites run on hosted PR CI. A release/* push is a full, exclusive run:\n"
    "it starts only on an idle machine below the load bars and no other command starts beside it.\n"
    "Exit 2 after the PR is open: the guard answered NEEDS_REVIEW. It does not run the repository's `make pr`.\n"
    "While `sm queue hold` holds the queue, --live refuses with the hold's reason (exit 1, nothing pushed)."))
def head_pr(
    bead: str = typer.Argument(..., metavar="BEAD", help="The bead this head closes; the default body names it."),
    title: str = typer.Argument(..., metavar="TITLE", help="The PR title, at most 72 characters."),
    body_file: Path | None = typer.Option(None, "--body-file", metavar="FILE", help="The PR body; default: `Bead BEAD.`"),
    reason: str | None = typer.Option(None, "--reason", metavar="TEXT", help="Review reason passed to the ship step."),
    worktree: Path = typer.Option(Path("."), "--worktree", metavar="DIR", help="The worktree of the head (default: here)."),
    live: bool = typer.Option(False, "--live", help="Push, open the PR and queue it. Without it: checks and the plan only."),
) -> HeadResult:
    from steuermaschine.github import GitHub

    if live:
        queue_hold.refuse_while_held(f"sm head pr {bead} --live")
    conf = deps.configure()
    commands = runtime().providers.commands
    branch_owner.guard_write(commands, worktree, record=live)  # one writer per branch: another agent's branch is refused, naming its owner
    # the repository is the one the worktree's origin names (a plugins worktree opens a plugins PR), else the configured one
    forge = GitHub(commands, repo=repos.repo_of_checkout(commands, str(Path(worktree).expanduser().resolve()), ship=True) or str(conf.get("repo") or hzlib.CORE))
    done = pr.open_pr(worktree, bead, title, body_file, reason, commands=commands, forge=forge, live=live,
                      max_wait=int(conf.get("head_pr_lock_max_wait_seconds") or ShipSettings.model_fields["head_pr_lock_max_wait_seconds"].default),
                      critical=is_focus(worktree))
    verdict = "done" if live and done.returncode == 0 else {0: "dry_run", 2: "needs_review" if "NEEDS_REVIEW" in done.stdout else "refused", 75: "wait"}.get(done.returncode, "refused")
    return _result("pr", bead, done, {}, default=verdict)

