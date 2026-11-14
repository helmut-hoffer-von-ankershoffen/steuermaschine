"""The pull request table: every open pull request of the project's repositories, grouped ready, needs work, held and
draft, with its failing checks, the age of its head and the action and owner from the reviewed action table.

Heinzelmännchen knowledge: the advisory `review` check never holds a pull request; neither does a diagnostic
`Focused ...` run that a hosted dispatch attached to the head.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

STALE_HOURS = 6
GROUPS = ("ready", "needs work", "held", "draft")
HEAD = ["PR", "Summary", "State", "Failing", "Head age", "Action", "Owner"]
SLOT_NAMES = ("FOCUSED_CI_PLANS", *(f"FOCUSED_CI_PLANS_V2_{i}" for i in range(4)))
SLOT_LIMITS = {"FOCUSED_CI_PLANS": 48 * 1024, **{f"FOCUSED_CI_PLANS_V2_{i}": 40000 for i in range(4)}}  # the plan and slot maximum of the Core focused plan
HEAD_BYTES = 1900  # estimated registry bytes per admitted head (measured about 1.9 KB; an estimate, not a guarantee)
PREFIX = re.compile(r"^[a-z]+(\([^)]*\))?!?:\s*")
IGNORE = frozenset({"review"})

QUERY = """query($owner: String!, $repo: String!) { repository(owner: $owner, name: $repo) {
  pullRequests(states: OPEN, first: 100, orderBy: {field: CREATED_AT, direction: DESC}) { nodes {
    number isDraft mergeStateStatus title body updatedAt labels(first: 10) { nodes { name } }
    commits(last: 1) { nodes { commit { committedDate statusCheckRollup { contexts(first: 100) { nodes {
      __typename ... on CheckRun { name status conclusion } } } } } } } } } } }"""


def summary(body: str, title: str, limit: int = 70) -> str:
    """First sentence of the body, cut short; else the title without its conventional-commit prefix."""
    text = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    for line in text.splitlines():
        line = line.strip().lstrip("*- >").strip()
        if len(line) < 12 or line.startswith(("#", "|", "[", "```", "Co-Authored")):
            continue
        sentence = re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0].rstrip(".")
        if len(sentence) < 12:
            continue
        return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"
    return PREFIX.sub("", title)


def failing(rollup: list[dict[str, Any]], ignore: frozenset[str] | set[str] = IGNORE) -> str:
    # a dispatched `Focused ...` run attaches to the head; diagnostics never make a pull request "needs work"
    return ", ".join(c.get("name") or "" for c in rollup if c.get("conclusion") == "FAILURE" and c.get("name") not in ignore
                     and not (c.get("name") or "").lower().startswith("focused "))


def age(then: str, now: datetime) -> str:
    seconds = int((now - datetime.fromisoformat(then.replace("Z", "+00:00"))).total_seconds())
    if seconds < 3600:
        return f"{max(seconds, 0) // 60}m"
    return f"{seconds // 3600}h" if seconds < 172800 else f"{seconds // 86400}d"


def group_of(pr: dict[str, Any], held: bool, ignore: frozenset[str] | set[str] = IGNORE) -> str:
    if pr["isDraft"]:
        return "draft"
    if held or any(label["name"] in ("hold", "do-not-merge") for label in pr["labels"]):
        return "held"
    return "ready" if pr["mergeStateStatus"] in ("CLEAN", "UNSTABLE") and not failing(pr["statusCheckRollup"], ignore) else "needs work"


def rows(prs: dict[str, list[dict[str, Any]]], actions: dict[str, Any], now: datetime, prefixes: dict[str, str],
         ignored: dict[str, set[str]] | None = None) -> list[dict[str, Any]]:
    out = []
    for repo, items in prs.items():
        ignore = (ignored or {}).get(repo, {"review"})
        for pr in items:
            name = f"{prefixes[repo]}#{pr['number']}"
            todo = actions.get(name) or (actions.get(str(pr["number"])) if not prefixes[repo] else None) or {}
            commits = pr["commits"]
            out.append({"pr": name, "number": pr["number"], "group": group_of(pr, bool(todo.get("hold")), ignore),
                        "summary": summary(pr["body"], pr["title"]), "state": pr["mergeStateStatus"],
                        "failing": failing(pr["statusCheckRollup"], ignore),
                        "age": age(commits[-1]["committedDate"], now) if commits else "?",
                        "action": todo.get("action") or "", "owner": todo.get("owner") or "NO OWNER", "updated": pr["updatedAt"]})
    return out


def stale(table: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    return [r for r in table if (now - datetime.fromisoformat(r["updated"].replace("Z", "+00:00"))).total_seconds() > STALE_HOURS * 3600]


def render(table: list[dict[str, Any]], markdown: bool) -> str:
    out: list[str] = []
    for group in GROUPS:
        items = [r for r in table if r["group"] == group]
        if not items:
            continue
        cells = [[r["pr"], r["summary"], r["state"], r["failing"], r["age"], r["action"], r["owner"]] for r in items]
        out.append(f"**{group.capitalize()}** ({len(items)})\n")
        if markdown:
            out += ["| " + " | ".join(HEAD) + " |", "|" + "---|" * len(HEAD)]
            out += ["| " + " | ".join(c.replace("|", "\\|") for c in row) + " |" for row in cells]
        else:
            widths = [min(max(len(row[i]) for row in [HEAD, *cells]), 50) for i in range(len(HEAD))]
            out += ["  ".join(c[: widths[i]].ljust(widths[i]) for i, c in enumerate(row)).rstrip() for row in [HEAD, *cells]]
        out.append("")
    return "\n".join(out)


def slot_free(sizes: dict[str, int]) -> int:
    return sum(max(SLOT_LIMITS[n] - sizes.get(n, 0), 0) for n in SLOT_NAMES)


def slot_line(sizes: dict[str, int]) -> str:
    """`Focused slots: 47970/49152 legacy, V2_0 39935/40000, ...; ~N heads left` from the variable byte sizes."""
    free = slot_free(sizes)
    parts = [f"{'legacy' if n == SLOT_NAMES[0] else n.removeprefix('FOCUSED_CI_PLANS_')} {sizes.get(n, 0)}/{SLOT_LIMITS[n]}" for n in SLOT_NAMES]
    return f"Focused slots: {', '.join(parts)}; {free} B free, ~{free // HEAD_BYTES} heads left (at ~1.9 KB per head)"


def normalise(node: dict[str, Any]) -> dict[str, Any]:
    """The flat shape the table reads: committedDate on a commit, a flat statusCheckRollup, labels as a list."""
    commit = (node["commits"]["nodes"] or [{}])[0].get("commit") or {}
    rollup = ((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or []
    return {**node, "labels": node["labels"]["nodes"], "commits": [{"committedDate": commit["committedDate"]}] if commit else [],
            "statusCheckRollup": [c for c in rollup if c.get("__typename") == "CheckRun"]}
