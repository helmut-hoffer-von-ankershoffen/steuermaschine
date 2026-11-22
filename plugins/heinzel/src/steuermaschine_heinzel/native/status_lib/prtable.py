"""One stateless table of the open pull requests of every repository of the project, over `gh` alone: state, CI, Sonar,
unresolved review threads, age and author. Nothing is stored: every call reads GitHub again. Times are durations."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from steuermaschine.localtime import since
from steuermaschine.models import Model

HEAD = ["Repo", "PR", "State", "CI", "Sonar", "Threads", "Age", "Author", "Title"]
DEFAULT_REPOS = ["heinzel", "heinzel-plugins", "steuermaschine", "heinzel-acme"]
QUERY = """query($owner: String!, $repo: String!) { repository(owner: $owner, name: $repo) {
  pullRequests(states: OPEN, first: 100, orderBy: {field: CREATED_AT, direction: DESC}) { nodes {
    number title isDraft mergeStateStatus createdAt author { login }
    reviewThreads(first: 100) { nodes { isResolved } }
    commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes {
      __typename ... on CheckRun { name status conclusion } ... on StatusContext { context state } } } } } } } } } } }"""
GOOD = {"SUCCESS", "NEUTRAL", "SKIPPED"}


class PrRow(Model):
    repo: str
    number: int
    title: str
    state: str  # GitHub's merge state, DRAFT for a draft
    ci: str  # ok | FAIL n | running n | none
    sonar: str  # ok | FAIL | running | -
    threads: int  # unresolved review threads
    created: str = ""
    age: str = ""
    author: str = ""

    def cells(self) -> list[str]:
        return [self.repo, f"#{self.number}", self.state, self.ci, self.sonar, str(self.threads), self.age, self.author, self.title[:60]]


def contexts(node: dict[str, Any]) -> list[tuple[str, str]]:
    """(name, outcome) of every check of the head commit: outcome is the upper-case conclusion, or RUNNING."""
    commit = (((node.get("commits") or {}).get("nodes") or [{}])[0] or {}).get("commit") or {}
    nodes = (((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes")) or []
    out = []
    for c in nodes:
        if c.get("__typename") == "CheckRun":
            done = str(c.get("status") or "").upper() == "COMPLETED"
            out.append((str(c.get("name") or ""), str(c.get("conclusion") or "?").upper() if done else "RUNNING"))
        elif c.get("__typename") == "StatusContext":
            state = str(c.get("state") or "").upper()
            out.append((str(c.get("context") or ""), "RUNNING" if state in ("PENDING", "EXPECTED") else "SUCCESS" if state == "SUCCESS" else "FAILURE"))
    return out


def ci_of(checks: list[tuple[str, str]]) -> str:
    """The advisory `review` check, diagnostic `Focused ...` runs and the Sonar check (its own column) never decide it."""
    own = [(n, o) for n, o in checks if n != "review" and not n.lower().startswith("focused ") and "sonar" not in n.lower()]
    bad = [n for n, o in own if o not in GOOD and o != "RUNNING"]
    running = [n for n, o in own if o == "RUNNING"]
    return f"FAIL {len(bad)}" if bad else f"running {len(running)}" if running else "ok" if own else "none"


def sonar_of(checks: list[tuple[str, str]]) -> str:
    mine = [o for n, o in checks if "sonar" in n.lower()]
    return "-" if not mine else "running" if "RUNNING" in mine else "ok" if all(o in GOOD for o in mine) else "FAIL"


def row(repo: str, node: dict[str, Any], now: datetime) -> PrRow:
    checks = contexts(node)
    threads = [t for t in (((node.get("reviewThreads") or {}).get("nodes")) or []) if not t.get("isResolved")]
    created = str(node.get("createdAt") or "")
    return PrRow(repo=repo, number=int(node["number"]), title=str(node.get("title") or ""), state="DRAFT" if node.get("isDraft") else str(node.get("mergeStateStatus") or "?"),
                 ci=ci_of(checks), sonar=sonar_of(checks), threads=len(threads), created=created, age=since(created, now) if created else "?",
                 author=str((node.get("author") or {}).get("login") or "?"))


def fetch(gh: Any, owner: str, repo: str, now: datetime) -> tuple[list[PrRow], str]:
    data, why = gh.try_json(["api", "graphql", "-f", f"query={QUERY}", "-F", f"owner={owner}", "-F", f"repo={repo}"], timeout=30)
    if why:
        return [], f"could not list {repo}: {why}"
    try:
        return [row(repo, n, now) for n in data["data"]["repository"]["pullRequests"]["nodes"]], ""
    except (KeyError, TypeError, ValueError):
        return [], f"could not list {repo}: no pull requests in the answer"


def render(rows: list[PrRow]) -> list[str]:
    cells = [HEAD, *[r.cells() for r in rows]]
    widths = [max(len(c[i]) for c in cells) for i in range(len(HEAD))]
    return ["  ".join(v.ljust(widths[i]) for i, v in enumerate(c)).rstrip() for c in cells] if rows else ["no open pull requests"]
