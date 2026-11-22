"""The queue's tick since 2026-11-22: arm GitHub auto-merge on every queued PR; GitHub merges it on green.

Owner decision 2026-11-22: a PR merges as soon as its required checks are green and it has no conflict; no branch has
to be up to date with main, and a red main is reverted (`auto_revert.py`). The serial loop that updated each branch,
waited for its checks and merged it (`merge_train.train`) no longer runs from the queue loop.

One tick, for each queued entry whose `after` PRs merged:

    MERGED on GitHub          -> the row is merged (merge commit recorded, bead stamped)
    CLOSED, or the head moved -> needs work (the diff guard read another head: ship it again)
    merge freeze (Core only)  -> auto-merge switched off on the PR; armed again once the freeze lifts
    otherwise                 -> `gh pr merge N --squash --auto --match-head-commit PINNED`

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


def hold(q: Any, why: str, excepted: Callable[[dict[str, Any]], bool]) -> None:
    """`sm queue hold`: no tick runs, so switch auto-merge off on every armed entry but the hold's exceptions."""
    for item in [i for i in q.load()["items"] if i["status"] == "queued" and i.get("detail") == ARMED and not excepted(i)]:
        if disarm(item["repo"], item["pr"]):
            q.update(item["key"], detail=HELD)
            q.log(f"{item['key']}: queue held ({why}); auto-merge is off")
        else:
            q.log(f"{item['key']}: queue held ({why}), and auto-merge could not be switched off; the next tick tries again")


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


def one(q: Any, item: dict[str, Any], items: list[dict[str, Any]]) -> None:
    """One queued entry through the rules above; `q` is the queue module (state, log, records)."""
    if not q.deps_met(item, items):
        return
    pinned = item.get("reviewed_head") or item.get("guarded_head") or ""
    if not SHA.fullmatch(pinned):
        q.stuck_or_merged(item, "needs work", "no guarded head recorded; ship it again so the diff guard reads the head that merges")
        return
    info = hzlib.ghj("pr", "view", item["pr"], "-R", item["repo"], "--json", "state,headRefOid,autoMergeRequest,author", default=None)
    if not isinstance(info, dict) or not info.get("state"):
        q.log(f"{item['key']}: the PR cannot be read; the next tick reads it again")
        return
    if info["state"] == "MERGED":
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
    if armed:
        if item.get("detail") != ARMED:
            q.update(item["key"], detail=ARMED)
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
