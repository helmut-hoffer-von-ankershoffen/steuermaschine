"""Briefs: `sm beads brief` (builder worktree and prompt) and `sm head review-brief` (independent reviewer)."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from steuermaschine import USAGE
from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.native import native

from .briefs_lib import builder, reviewer, workbrief
from .briefs_lib.builder import BriefSettings

__all__ = ["BriefSettings"]


@native("beads brief", writes=True, replaces=("hz-brief",), help=H(
    "Create a builder's worktree on a new branch from origin/main and print the exact prompt; with only a BEAD, print a generated brief.",
    "Use when a ready bead gets a builder; check `sm beads refill` first, and never brief the same topic twice.",
    "fetches origin/main and adds a git worktree and branch on this machine; with --force one line in the journal.",
    'sm beads brief logo feat/logo --task "Bigger logo in the header. Done = a PR with a screenshot." --bead ab12   |   sm beads brief ab12',
    "start the builder with the printed prompt; later `sm beads tasks pr BEAD PR`.",
    "`sm beads brief BEAD` (one argument, `sm work brief BEAD` once the work verb exists) creates nothing: it prints the brief generated from the bead: task,\n"
    "the owner's commit identity from git config, the rules, the worktree commands, the proofs and the report format.\n"
    "Refuses (exit 1, nothing created) when the machine is full: the 1-minute load is over the limit, or the limit of\n"
    "working agents is reached (agent processes whose directory is inside a builder's worktree). --force REASON goes\n"
    "ahead and writes the reason to the journal. TOPIC is a plain name. An existing directory is reused (only the prompt\n"
    "is printed) when it is a worktree of this clone on BRANCH; anything else there is a refusal."))
def beads_brief(topic: str = typer.Argument(..., metavar="TOPIC|BEAD", help="Short name; the worktree is <clone>-TOPIC. Alone: a bead id, and the brief is only printed."),
                branch: str | None = typer.Argument(None, metavar="BRANCH", help="The new branch, from origin/main; omit it to print the generated brief of a bead."),
                repo: str | None = typer.Option(None, "--repo", metavar="NAME", help="Short repository name (setting brief_clones); default: the first one."),
                task: str = typer.Option(builder.DEFAULT_TASK, "--task", metavar="TEXT", help="The task in one paragraph, with what done looks like."),
                bead: str = typer.Option("", "--bead", metavar="BEAD", help="The bead this work closes; printed in the prompt so the PR and release can be stamped."),
                force: str = typer.Option("", "--force", metavar="REASON", help="Brief anyway when the machine is full; the reason goes to the journal.")
                ) -> builder.BuilderBrief | workbrief.WorkBrief:
    if branch is None:
        if task != builder.DEFAULT_TASK or bead or force:
            raise Refuse("--task, --bead and --force go with TOPIC BRANCH; a lone BEAD only prints its brief", "sm beads brief TOPIC BRANCH --task TEXT", code=USAGE)
        try:
            return workbrief.generate(topic, repo or "")
        except KeyError as exc:
            raise Refuse(f"unknown repository '{exc.args[0]}'", "sm self config   (setting brief_clones)", code=USAGE) from None
    try:
        return builder.brief(topic, branch, repo or "", task, bead, force)
    except builder.MachineFull as stop:
        raise Refuse(f"{stop}. Stop an agent or wait; or --force \"reason\".", "sm beads refill") from None
    except builder.BadTopic as stop:
        raise Refuse(str(stop), "sm beads brief TOPIC BRANCH", code=USAGE) from None
    except KeyError as exc:
        raise Refuse(f"unknown repository '{exc.args[0]}'", "sm self config   (setting brief_clones)", code=USAGE) from None
    except RuntimeError as exc:
        raise Refuse(f"worktree failed: {exc}", "git worktree list") from None


@native("head review-brief", writes=False, replaces=("hz-review-brief",), help=H(
    "A ready-to-paste brief for the independent reviewer of a pull request the diff guard flagged.",
    "Use when the diff guard flags a pull request, before dispatching a reviewer.",
    "nothing (read-only: the pull request's title and body, existing verdict files).",
    "sm head review-brief 796 --flags guard.txt",
    "start an independent reviewer with the printed brief; it writes research/review-prN.md.",
    "--flags is what the diff guard printed for the pull request (`-` reads standard input). An empty file means the\n"
    "guard passed: the command says so and prints no brief. Member pull requests of a combine are read from the body."))
def head_review_brief(pull_request: int = typer.Argument(..., metavar="PR", help="The pull request number."),
                      flags: Path = typer.Option(..., "--flags", metavar="FILE", help="The diff guard's output for this pull request; `-` for standard input."),
                      repo: str | None = typer.Option(None, "-R", "--repo", metavar="OWNER/NAME", help="Repository; default: the core repository.")) -> reviewer.ReviewBrief:
    try:
        text = sys.stdin.read() if str(flags) == "-" else flags.read_text()
    except OSError as exc:
        raise Refuse(f"cannot read the guard output: {exc}", "sm head review-brief PR --flags FILE", code=USAGE) from None
    return reviewer.build(pull_request, text, repo or "")
