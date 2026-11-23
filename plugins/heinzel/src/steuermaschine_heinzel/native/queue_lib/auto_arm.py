"""The queue's tick since 2026-11-22: arm GitHub auto-merge on every queued PR; GitHub merges it on green.

Owner decision 2026-11-22: GitHub merges a PR once armed; sm keeps the old checks as conditions of the arm (below), and a
red main is reverted (`auto_revert.py`). The serial loop that updated each branch, waited for its checks and merged it
(`merge_train.train`) no longer runs from the queue loop.

One tick, for each queued entry whose `after` PRs merged:

    MERGED on GitHub          -> the row is merged (merge commit recorded, bead stamped)
    CLOSED, or the head moved -> needs work (the diff guard read another head: ship it again)
    merge freeze (Core only)  -> auto-merge switched off on the PR; armed again once the freeze lifts
    main red or unknown       -> not armed (a PR that fixes main goes alone); an armed one is switched off
    checks not settled/failed -> `queue_daemon.assess` (the train's checks, every repository): wait, or needs work
    tool-required checks      -> `hzlib.tool_required_checks` (a private repository has no ruleset) not passed: wait
    another Core PR armed     -> not armed (one Core PR is armed at a time: its merge moves main before the next arms)
    merge rule not met        -> not armed: checks pending or unreadable wait, any other refusal needs work
    otherwise                 -> `gh pr merge N --squash --auto --match-head-commit PINNED`

The merge rule is `main_plan.gap`, applied before every arm of a Core PR: the ruleset's required checks green on the
pinned head, main an ancestor of it, and a recorded independent verdict for protected paths (always, whatever the
ruleset requires). The rule is read again every tick on an armed PR: one that no longer meets it is switched off. The red-main hold is the old `gate()`: main's latest run not green or unknown holds all
Core arming except a PR that fixes main (fixes_main, fix-forward, or a reviewed safety fix, mitigation, diagnostic).

The freeze is the release freeze (8i4oi): a release hold, the incident sentinel, or an open release/* PR of Core.
Unreadable evidence holds (fails closed). Other repositories are not held by Core's release, as before.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from . import hzlib

ARMED = "auto-merge armed"
HELD = "merge freeze: auto-merge off until it lifts"
SHA = re.compile(r"[0-9a-f]{40}")


def release_pr_open() -> str:
    """The open release/* PR of Core that holds merges, '' when none; unreadable counts as open (fails closed)."""
    rows = hzlib.ghj("pr", "list", "-R", hzlib.CORE, "--state", "open", "--limit", "100", "--json", "number,headRefName", default=None)
    if not isinstance(rows, list):
        return "the open pull requests of Core cannot be read"
    hit = next((r for r in rows if isinstance(r, dict) and str(r.get("headRefName", "")).startswith("release/")), None)
    return f"release PR #{hit.get('number')} is open" if hit else ""


def frozen(repo: str) -> str:
    """Why REPO may not merge now, or ''. Only Core is held by the release freeze."""
    if repo != hzlib.CORE:
        return ""
    if hzlib.merge_frozen():
        return "merge freeze is on"
    return release_pr_open()


def arm(repo: str, number: str, head: str) -> tuple[bool, str]:
    """Switch GitHub auto-merge on for exactly HEAD (squash). (done, gh's error line)."""
    code, out = hzlib.gh("pr", "merge", number, "-R", repo, "--squash", "--auto", "--match-head-commit", head)
    return code == 0, "" if code == 0 else (out.strip().splitlines() or [f"gh exit {code}"])[-1][:160]


def disarm(repo: str, number: str) -> bool:
    code, _ = hzlib.gh("pr", "merge", number, "-R", repo, "--disable-auto")
    return code == 0


def armed_on_github(repo: str) -> list[str] | None:
    """The numbers of the open PRs of REPO that have auto-merge on, read from GitHub; None when unreadable."""
    rows = hzlib.ghj("pr", "list", "-R", repo, "--state", "open", "--limit", "200", "--json", "number,autoMergeRequest", default=None)
    if not isinstance(rows, list):
        return None
    return [str(r.get("number")) for r in rows if isinstance(r, dict) and r.get("autoMergeRequest")]


def hold(q: Any, why: str, excepted: Callable[[dict[str, Any]], bool]) -> None:
    """`sm queue hold`: no tick runs, so switch auto-merge off on every PR armed ON GITHUB (queued or not, whatever sm
    recorded) but the hold's exceptions."""
    items = q.load()["items"]
    queued = {(i["repo"], str(i["pr"])): i for i in items if i["status"] == "queued"}
    for repo in sorted({hzlib.CORE, *(i["repo"] for i in queued.values())}):
        armed = armed_on_github(repo)
        if armed is None:
            q.log(f"queue held ({why}), and the armed PRs of {repo} cannot be read; the next tick tries again")
            armed = [pr for (r, pr), i in queued.items() if r == repo and i.get("detail") == ARMED]  # fall back to the record
        for pr in armed:
            item = queued.get((repo, pr))
            if item is not None and excepted(item):
                continue
            if disarm(repo, pr):
                if item is not None:
                    q.update(item["key"], detail=HELD)
                q.log(f"{repo}#{pr}: queue held ({why}); auto-merge is off")
            else:
                q.log(f"{repo}#{pr}: queue held ({why}), and auto-merge could not be switched off; the next tick tries again")


def authors() -> set[str]:
    """The logins whose PRs the queue may arm (the setting auto_queue_authors; default the owner)."""
    from . import edge

    return {str(a).lower() for a in (edge.setting("auto_queue_authors") or [hzlib.OWNER])}


def disarm_all(repo: str = hzlib.CORE) -> list[str]:
    """Switch auto-merge off on every open PR of REPO that has it, then read again: the PRs still armed (empty when none).
    The release cut calls this before it proceeds (8i4oi); an unreadable list is one entry that says so (fails closed)."""
    fields = ("pr", "list", "-R", repo, "--state", "open", "--limit", "200", "--json", "number,autoMergeRequest")
    rows = hzlib.ghj(*fields, default=None)
    if not isinstance(rows, list):
        return [f"the open pull requests of {repo} cannot be read"]
    for row in rows:
        if isinstance(row, dict) and row.get("autoMergeRequest"):
            disarm(repo, str(row.get("number")))
    rows = hzlib.ghj(*fields, default=None)
    if not isinstance(rows, list):
        return [f"the open pull requests of {repo} cannot be read again"]
    return [f"#{r.get('number')}" for r in rows if isinstance(r, dict) and r.get("autoMergeRequest")]


def main_hold(q: Any, item: dict[str, Any]) -> str:
    """Why Core's red or unknown main holds ITEM (the old `gate()`), or ''. A PR that fixes main is not held."""
    if item["repo"] != hzlib.CORE:
        return ""
    state = q.main_state()
    if state == "red" and q.strict_red_recovery():
        return "" if q.reviewed_red_recovery(item) else "main is red; only a reviewed fix or measured mitigation is armed"
    if state == "red" and not q.main_checkpoint.permits(q.state_path(), q.load().get("main_failure", {})) and not q.later_main_passes() and not q.restored_by_rerun():
        if q.red_main_fixes([item]) or q.fix_forward_ready([item]) or q.red_main_exceptions([item]):
            return ""
        return "main's latest run is not green; only a PR that fixes it is armed"
    if state not in ("green", "red"):
        return "main CI evidence is unknown"
    return ""


def merge_rule(item: dict[str, Any], pinned: str) -> tuple[str, bool]:
    """(refusal, wait) of the merge rule for a Core PR at PINNED; ('', False) when it holds. WAIT: pending or unreadable."""
    if item["repo"] != hzlib.CORE:
        return "", False
    from . import main_plan

    reason = main_plan.gap(item["pr"], pinned)
    return reason, bool(reason) and ("required checks not green" in reason or "cannot" in reason)


def one(q: Any, item: dict[str, Any], items: list[dict[str, Any]]) -> None:
    """One queued entry through the rules above; `q` is the queue module (state, log, records)."""
    if not q.deps_met(item, items):
        return
    pinned = item.get("reviewed_head") or item.get("guarded_head") or ""
    if not SHA.fullmatch(pinned):
        q.stuck_or_merged(item, "needs work", "no guarded head recorded; ship it again so the diff guard reads the head that merges")
        return
    info = hzlib.ghj("pr", "view", item["pr"], "-R", item["repo"], "--json", "state,baseRefName,headRefOid,mergeCommit,autoMergeRequest,author,statusCheckRollup", default=None)
    if not isinstance(info, dict) or not info.get("state"):
        q.log(f"{item['key']}: the PR cannot be read; the next tick reads it again")
        return
    if info["state"] == "MERGED":
        if not q.record_reviewed_main_fix(item, info):
            q.log(f"{item['key']}: merged fix cannot be bound to its reviewed head and squash commit; the red pin stays")
            return
        q.stuck_or_merged(item, "merged", pinned[:8])
        q.record_merge(item)
        return
    login = str((info.get("author") or {}).get("login") or "")
    if info["state"] == "OPEN" and login.lower() not in authors():
        if info.get("autoMergeRequest"):
            disarm(item["repo"], item["pr"])
        q.stuck_or_merged(item, "needs work", f"author {login or 'unknown'} is not on auto_queue_authors: the queue never arms it")
        return
    if info["state"] != "OPEN":
        q.stuck_or_merged(item, "needs work", f"the PR is {str(info['state']).lower()}")
        return
    if info.get("headRefOid") != pinned:
        if info.get("autoMergeRequest"):
            disarm(item["repo"], item["pr"])
        q.stuck_or_merged(item, "needs work", f"head moved from {pinned[:8]} after the diff guard read it; ship it again")
        return
    armed = bool(info.get("autoMergeRequest"))
    if why := frozen(item["repo"]):
        if armed and not disarm(item["repo"], item["pr"]):
            q.log(f"{item['key']}: {why}, and auto-merge could not be switched off; the next tick tries again")
            return
        if item.get("detail") != HELD:
            q.update(item["key"], detail=HELD)
            q.log(f"{item['key']}: {why}; auto-merge is off")
        return
    if why := main_hold(q, item):
        if armed and not disarm(item["repo"], item["pr"]):
            q.log(f"{item['key']}: {why}, and auto-merge could not be switched off; the next tick tries again")
            return
        if item.get("detail") != f"not armed: {why}":
            q.update(item["key"], detail=f"not armed: {why}")
            q.log(f"{item['key']}: not armed: {why}")
        return
    verdict = q.assess(item, hzlib.load("merge_train"))  # the checks the serial train demanded before a merge: every repository
    if verdict == "done":  # it marked the row merged or needs work
        if armed:
            disarm(item["repo"], item["pr"])
        return
    if verdict != "ready" and not armed:
        if item.get("detail") != "not armed: checks not settled":
            q.update(item["key"], detail="not armed: checks not settled")
        return
    passed = {c.get("name") for c in info.get("statusCheckRollup") or [] if c.get("conclusion") == "SUCCESS"}
    if absent := hzlib.tool_required_checks(item["repo"]) - passed:
        if armed and not disarm(item["repo"], item["pr"]):
            q.log(f"{item['key']}: required by the tools, not passed: {', '.join(sorted(absent))}; auto-merge could not be switched off")
            return
        q.update(item["key"], detail=f"not armed: required by the tools, not passed: {', '.join(sorted(absent))}")
        return
    if item.get("reviewed_progress_main") and not q.progress_anchor_unchanged(item.get("progress_anchor")):
        if armed:
            disarm(item["repo"], item["pr"])
        q.stuck_or_merged(item, "needs work", "completed main observation changed or unavailable; reassessment and requeue required")
        return
    reason, wait = merge_rule(item, pinned)
    if reason and not (armed and wait):  # an armed PR whose checks are pending or unreadable keeps its arm: GitHub waits for the checks itself
        if armed and not disarm(item["repo"], item["pr"]):
            q.log(f"{item['key']}: {reason}, and auto-merge could not be switched off; the next tick tries again")
            return
        if wait:
            if item.get("detail") != f"not armed: {reason}":
                q.update(item["key"], detail=f"not armed: {reason}")
                q.log(f"{item['key']}: not armed: {reason}")
        else:
            q.stuck_or_merged(item, "needs work", reason)
        return
    if armed:
        if item.get("detail") != ARMED:
            q.update(item["key"], detail=ARMED)
        return
    if item["repo"] == hzlib.CORE:
        armed_now = armed_on_github(hzlib.CORE)
        others = ["?"] if armed_now is None else [n for n in armed_now if n != str(item["pr"])]  # unreadable: fail closed
        if others:
            q.update(item["key"], detail=f"not armed: #{others[0]} is armed; one Core PR at a time")
            return
    done, error = arm(item["repo"], item["pr"], pinned)
    if done and (late := frozen(item["repo"])):
        # the freeze started between the check above and the arm, possibly after the cut's disarm_all: undo it now
        if disarm(item["repo"], item["pr"]):
            q.update(item["key"], detail=HELD)
            q.log(f"{item['key']}: {late} began while it was armed; auto-merge is off again")
        else:
            q.update(item["key"], detail=ARMED)
            q.log(f"{item['key']}: {late} began while it was armed, and auto-merge could not be switched off; the next tick tries again")
    elif done:
        q.update(item["key"], detail=ARMED)
        q.log(f"{item['key']}: {ARMED} at {pinned[:8]}")
    else:
        q.update(item["key"], detail=f"auto-merge not armed: {error}")
        q.log(f"{item['key']}: auto-merge not armed: {error}")


def tick(q: Any) -> None:
    """Every queued entry once, then the auto-revert of a red Core main. A failure of one never stops the others."""
    items = q.load()["items"]
    for item in [i for i in items if i["status"] == "queued"]:
        try:
            one(q, item, items)
        except Exception as error:  # noqa: BLE001 - one unreadable PR must not stop the others
            q.log(f"{item['key']}: arm failed: {error!r}")
    from . import auto_revert

    try:
        auto_revert.check(q)
    except Exception as error:  # noqa: BLE001 - the revert is best effort; the alarm of a red main stays
        q.log(f"auto-revert failed: {error!r}")
