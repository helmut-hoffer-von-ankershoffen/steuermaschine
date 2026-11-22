"""Guard the diff, then queue the PR: the ship step. A port of the former shell script, same order, same lines.

    ship(PR)                          guard PASS -> queue; FLAG or unreadable -> NEEDS_REVIEW and the guard's first lines, exit 2,
                                      unless main's ruleset requires the CI Gate review: then queue, and that check decides
    ship(PR, reason)                  queue as reviewed, with the written reason of an independent review
    ship(PR, reason, reviewed_head)   the same, pinned to the reviewed head
    push(PR)                          before a push that starts PR CI: OK (0) or WAIT (75)

Each returns what the script printed (stdout, stderr) and its exit code.
"""

from __future__ import annotations

from ..queue_lib import hzlib
from . import deps, push_gate
from .deps import Done

USAGE = "usage: sm head ship PR [reviewed-reason [--reviewed-head SHA]]"
IN_FLIGHT = '[.[]|select(.status=="queued" or .status=="in_progress")]|length'


def _queue():
    conf = deps.configure()
    from .. import queue as area

    return area, conf


def _add(argv: list[str]) -> Done:
    area, _ = _queue()
    code, lines, errors = area._run(area._queue(), ["add", *argv])
    return Done(code, "".join(f"{line}\n" for line in lines), "".join(f"{line}\n" for line in errors))


def _done(code: int, out: str = "", err: str = "") -> Done:
    return Done(code, out, err)


def ship(pr: str, reason: str | None = None, reviewed_head: str | None = None) -> Done:
    """Without a reason: the guard decides. With one: queued as reviewed (the queue still refuses a moved head)."""
    if reason == "--reviewed-head" or (reviewed_head is not None and not reason):
        return _done(2, err=USAGE + "\n")
    if reason:
        return _add(["--reviewed", reason, *(["--reviewed-head", reviewed_head] if reviewed_head is not None else []), pr])
    area, _ = _queue()
    from ..queue_lib import known_failures

    module = area._queue()
    try:
        items = module.load().get("items", [])
    except module.StateUnreadable as error:  # a broken queue file is no evidence of an empty queue: refuse loudly
        return _done(1, err=f"REFUSED {pr}: {error}\n")
    try:
        repo, number, _ = hzlib.split_key(pr)
    except ValueError:
        repo, number = "", ""
    queued_before = repo and any(str(i.get("pr")) == number and i.get("repo") == repo for i in items)
    if queued_before and known_failures.guard_cleared(number, items, repo, module.pr_head(repo, number)):
        return _done(0, f"CLEARED {pr}: guard already cleared at this head (queued or reviewed); nothing to review\n")
    out = ""
    try:  # as the script: a failed read counts as 0 runs in flight; hzlib.gh bounds it (a timeout is exit 124)
        code, counted = hzlib.gh("run", "list", "-R", hzlib.CORE, "--event", "pull_request", "--limit", "60", "--json", "status", "-q", IN_FLIGHT)
        in_flight = int(counted.strip() or 0) if code == 0 else 0
    except (OSError, ValueError):
        in_flight = 0
    if in_flight > 5:
        out += f"NOTE: {in_flight} PR CI runs in flight > 5: queue is fine, but push nothing new until it drops\n"
    guard = deps.captured(hzlib.load("diff_guard").main, [pr])
    verdict = (guard.stdout + guard.stderr).rstrip("\n")
    if verdict.startswith("PASS"):  # the queue guards again and binds that verdict to the head it read
        added = _add([pr])
        return _done(added.returncode, out + added.stdout, added.stderr)
    if repo and hzlib.gate_review_decides(repo):  # main's ruleset requires the CI Gate review: it decides, not a local review
        added = _add([pr])
        return _done(added.returncode, out + f"diff guard flagged {pr}: CI Gate review decides\n" + added.stdout, added.stderr)
    return _done(2, out + f"NEEDS_REVIEW {pr}\n" + "".join(f"{line}\n" for line in verdict.splitlines()[:6]))


def fix_forward(pr: str, options: list[str], reason: str | None = None) -> Done:
    """`ship PR --fix-forward ...`: the queue's fix-forward lane with the given options, as the script passed them
    (`"$@"`): a `--reviewed-head` among them and a positional REASON reach the queue, which refuses both in that lane
    (one PR only; no pinned head: the lane pins the head it checks)."""
    return _add(["--fix-forward", *options, *([reason] if reason is not None else []), pr])


def push(pr: str, worktree: str = ".") -> Done:
    """Before any push that starts PR CI: the CI meta preflight, then the push gate. 0 push, 75 wait, 1 fix first."""
    problems = deps.ci_meta(worktree, pr, ref="" if str(pr).isdigit() else str(pr))  # a branch: its commits, not the worktree's HEAD
    if problems:
        return _done(1, "".join(f"{line}\n" for line in problems), "sm head ship: fix the CI meta problems above BEFORE pushing (they cost a full CI attempt)\n")
    code, lines = push_gate.check(pr)
    err = ""
    if code == push_gate.WAIT:
        err = (f"sm head ship: DO NOT PUSH #{pr} now (a push starts ~60 jobs that starve main CI). Commit locally, batch commits into ONE push, "
               f"retry: sm head push {pr}\n")
    return _done(code, "".join(f"{line}\n" for line in lines), err)
