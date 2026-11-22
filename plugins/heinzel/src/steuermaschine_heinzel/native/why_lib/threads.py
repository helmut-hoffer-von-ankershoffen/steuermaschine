"""Unresolved review threads of a pull request. They hold a merge when the ruleset requires conversation resolution,
and GitHub then says BLOCKED with every check green; CodeQL (code scanning) opens its threads as review comments of the
`github-advanced-security` bot, so they look like nothing else in the check list."""

from __future__ import annotations

from typing import Any

from steuermaschine.models import Model

QUERY = """query($owner: String!, $repo: String!, $number: Int!) { repository(owner: $owner, name: $repo) { pullRequest(number: $number) {
  reviewThreads(first: 100) { nodes { isResolved isOutdated path line comments(first: 1) { nodes { url body author { login } } } } } } } }"""
BOTS = {"github-advanced-security": "CodeQL"}


class Thread(Model):
    path: str = ""
    line: int | None = None
    author: str = ""
    source: str = ""  # CodeQL for the code scanning bot, else the author's login
    outdated: bool = False
    url: str = ""
    first_line: str = ""

    def line_text(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{self.source} {where}{' (outdated)' if self.outdated else ''}: {self.first_line}".strip()


def parse(data: Any) -> list[Thread]:
    """The unresolved threads out of the GraphQL answer; anything else in it is ignored."""
    try:
        nodes = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (KeyError, TypeError):
        return []
    out = []
    for node in nodes or []:
        if not isinstance(node, dict) or node.get("isResolved"):
            continue
        comments = ((node.get("comments") or {}).get("nodes")) or [{}]
        first = comments[0] if isinstance(comments[0], dict) else {}
        login = str((first.get("author") or {}).get("login") or "")
        body = str(first.get("body") or "").strip().splitlines()
        out.append(Thread(path=str(node.get("path") or ""), line=node.get("line") if isinstance(node.get("line"), int) else None, author=login,
                          source=BOTS.get(login, login or "?"), outdated=bool(node.get("isOutdated")), url=str(first.get("url") or ""),
                          first_line=(body[0] if body else "")[:80]))
    return out


def read(gh: Any, repo: str, number: int) -> tuple[list[Thread], str]:
    """(unresolved threads, why they could not be read). Read-only."""
    owner, _, name = repo.partition("/")
    data, why = gh.try_json(["api", "graphql", "-f", f"query={QUERY}", "-F", f"owner={owner}", "-F", f"repo={name}", "-F", f"number={number}"], timeout=30)
    return (parse(data), "") if not why else ([], why)


def reason(threads: list[Thread]) -> str:
    """The block reason for a PR whose checks are green but whose merge state is BLOCKED."""
    shown = "; ".join(t.line_text() for t in threads[:3]) + (f"; and {len(threads) - 3} more" if len(threads) > 3 else "")
    return (f"GitHub BLOCKED with all required checks green: {len(threads)} unresolved review thread(s), not a CI problem ({shown}). "
            "Fix or dismiss each and resolve it (CodeQL threads resolve when the alert is fixed or dismissed); do not rerun CI")
