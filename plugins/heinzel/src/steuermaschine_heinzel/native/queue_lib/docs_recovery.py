"""Conservative evidence for carrying a full main recovery across direct docs-only pushes."""
from __future__ import annotations

from ..affected_lib.select import DOCS, FULL
from . import core_policy, main_lanes


def first_push(row: dict) -> bool:
    return (row.get("event") == "push" and not row.get("batch") and row["conclusion"] == "success"
            and type(row.get("attempt")) is int and row["attempt"] == 1)


def green_docs_jobs(jobs: list[dict] | None, policy: core_policy.Policy) -> bool:
    if not jobs or any(job["conclusion"] not in {"success", "skipped", "neutral"} for job in jobs):
        return False
    passed = {job["name"] for job in jobs if job["conclusion"] == "success"}
    required = {policy.aggregate, "Changed paths", "Main batch admission", "Secret scan", "Quarantine registry", main_lanes.DISPATCH_JOB}
    return required.issubset(passed) and not passed.intersection(policy.test_jobs)


def docs_child(commit: object, parent: str, child: str) -> bool:
    """One direct parent, one complete GitHub page, documented paths on both sides of renames."""
    if not isinstance(commit, dict) or commit.get("sha") != child:
        return False
    parents = commit.get("parents")
    if not isinstance(parents, list) or len(parents) != 1 or not isinstance(parents[0], dict) or parents[0].get("sha") != parent:
        return False
    files = commit.get("files")
    if not isinstance(files, list) or not 0 < len(files) < 100:
        return False
    for changed in files:
        if not isinstance(changed, dict) or changed.get("status") not in {"added", "removed", "modified", "renamed"}:
            return False
        paths = [changed.get("filename")]
        if changed["status"] == "renamed" or "previous_filename" in changed:
            paths.append(changed.get("previous_filename"))
        for path in paths:
            if not (core_policy.repo_path(path) and (path.startswith("docs/") or path.endswith(".md"))
                    and path.endswith((".md", ".html", ".css", ".png", ".jpg", ".jpeg", ".webp"))
                    and DOCS.fullmatch(path) and not FULL.match(path) and path.rsplit("/", 1)[-1] != "CHANGELOG.md"):
                return False
    return True
