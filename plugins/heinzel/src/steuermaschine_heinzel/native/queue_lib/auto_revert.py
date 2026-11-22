"""Auto-revert: when Core's main CI turns red on a merge commit and the failure is not a known flake, revert that merge.

Owner decision 2026-11-22: with auto-merge and no "up to date with main" rule, a red main is reverted by the tool.
One check per queue tick (`auto_arm.tick`):

1. The newest completed push run of Core's CI on main failed, and the completed run before it (an older commit) passed:
   main TURNED red on exactly that commit. Main red before it: no revert (the commit is not proven to be the cause).
2. Every failing job of that attempt is classified against the known-failure registry, as `sm queue main-rerun` does.
   All KNOWN: a flake; the main rerun handles it. One UNKNOWN job: the merge is the cause.
3. The commit is a squash merge of a PR (`subject (#N)`), not a release commit (release/* head or a `release` subject)
   and not itself a revert. Anything else: an alarm only.
4. One revert PR per merge, idempotent by branch name `revert/pr-N`: a blob-less shallow clone in a temporary directory,
   `git revert --no-commit`, one commit as the merge's own author (the owner's noreply identity; any other address
   refuses), a plain push of the new branch (never a force), `gh pr create`, and a queue entry: the queue arms
   auto-merge on it like any other entry (and holds it during a release freeze).
5. An ALERT alarm names the run, the merge and the revert PR.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from . import edge, hzlib, main_lanes

SQUASH = re.compile(r"^(?P<title>.+) \(#(?P<pr>\d+)\)$")
RELEASE = re.compile(r"^(release|chore\(release\)|revert)\b", re.IGNORECASE)
NOREPLY = re.compile(r"^[0-9]+\+[A-Za-z0-9-]+@users\.noreply\.github\.com$")
RED = ("failure", "timed_out")
DEPTH = 50  # the shallow clone: a merge further back than this is not reverted by the tool
MAX_SUBJECT = 72


def subject_of(title: str, number: str) -> str:
    """`revert: TITLE (#N)`, shortened inside TITLE to the 72-character subject rule."""
    tail = f" (#{number})"
    room = MAX_SUBJECT - len("revert: ") - len(tail)
    short = title if len(title) <= room else title[: room - 3].rstrip() + "..."
    return f"revert: {short}{tail}"


def turned_red(q: Any) -> dict[str, Any]:
    """{run, sha, attempt} of the newest completed main push run when it failed and the run before it passed, else {}."""
    rows = hzlib.ghj("run", "list", "-R", hzlib.CORE, "--workflow", q.ci_workflow(), "--branch", "main", "-L", "10",
                     "--json", "status,conclusion,databaseId,headSha,attempt,displayTitle", default=None)
    if not isinstance(rows, list) or not rows or not all(q.valid_main_run(r) for r in rows):
        return {}
    rows, _ = main_lanes.split_runs(rows)
    done = [r for r in rows if r["status"] == "completed"]
    if len(done) < 2 or done[0]["conclusion"] not in RED:
        return {}
    before = next((r for r in done[1:] if r["headSha"] != done[0]["headSha"]), None)
    if before is None or before["conclusion"] != "success":
        return {}  # main was red (or unknown) before this commit: the commit is not proven to be the cause
    return {"run": done[0]["databaseId"], "sha": done[0]["headSha"], "attempt": int(done[0].get("attempt") or 1)}


def unknown_failures(q: Any, red: dict[str, Any]) -> list[str] | None:
    """The failing jobs no in-date registry entry covers; None when the job list cannot be read."""
    jobs = q.attempt_jobs(red["run"], red["attempt"])
    if jobs is None or any(j.get("status") != "completed" for j in jobs):
        return None
    _failed, beads, _infra = q.main_attempt_failures(red["run"], red["sha"], jobs)
    return [job for job, bead in beads.items() if not bead]


def merge_of(sha: str) -> dict[str, Any]:
    """{pr, title, name, email} of a squash merge commit of a PR, or {"why": reason} when the tool must not revert it."""
    commit = hzlib.ghj("api", f"repos/{hzlib.CORE}/commits/{sha}", default=None)
    if not isinstance(commit, dict):
        return {"why": f"commit {sha[:8]} cannot be read"}
    message = str((commit.get("commit") or {}).get("message") or "")
    author = (commit.get("commit") or {}).get("author") or {}
    subject = message.splitlines()[0] if message else ""
    found = SQUASH.match(subject)
    if not found:
        return {"why": f"commit {sha[:8]} is not the squash merge of a PR ({subject[:60]!r})"}
    if RELEASE.match(subject):
        return {"why": f"commit {sha[:8]} is a release or revert commit: never reverted by the tool"}
    pull = hzlib.ghj("pr", "view", found["pr"], "-R", hzlib.CORE, "--json", "headRefName,mergeCommit", default=None)
    if not isinstance(pull, dict) or str((pull.get("mergeCommit") or {}).get("oid") or "") != sha:
        return {"why": f"PR #{found['pr']} cannot be read, or its merge commit is not {sha[:8]}"}
    if str(pull.get("headRefName", "")).startswith("release/"):
        return {"why": f"PR #{found['pr']} is a release PR: never reverted by the tool"}
    email = str(author.get("email") or "")
    if not NOREPLY.fullmatch(email):
        return {"why": f"the author of {sha[:8]} has no GitHub noreply address: the revert commit would publish it"}
    return {"pr": found["pr"], "title": found["title"], "name": str(author.get("name") or ""), "email": email}


def revert_exists(branch: str) -> bool:
    """A PR from BRANCH exists in any state (or the list is unreadable): the tool made it already, never twice."""
    rows = hzlib.ghj("pr", "list", "-R", hzlib.CORE, "--head", branch, "--state", "all", "--json", "number", default=None)
    return not isinstance(rows, list) or bool(rows)


def git(cwd: Path, *argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return edge.run(["git", "-C", str(cwd), *argv], capture_output=True, text=True, timeout=600, env=env)


def make_revert(sha: str, merge: dict[str, Any], branch: str, subject: str, body: str) -> str:
    """Push BRANCH with the one revert commit; '' when done, else why not. Nothing is forced."""
    import os

    with tempfile.TemporaryDirectory(prefix="sm-revert-") as tmp:
        work = Path(tmp) / "core"
        cloned = edge.run(["git", "clone", "--quiet", "--filter=blob:none", f"--depth={DEPTH}", "--branch", "main", "--single-branch",
                           f"https://github.com/{hzlib.CORE}.git", str(work)], capture_output=True, text=True, timeout=900)
        if cloned.returncode != 0:
            return "the clone of Core failed"
        if git(work, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
            return f"{sha[:8]} is not within the last {DEPTH} commits of main"
        who = {"GIT_AUTHOR_NAME": merge["name"], "GIT_AUTHOR_EMAIL": merge["email"],
               "GIT_COMMITTER_NAME": merge["name"], "GIT_COMMITTER_EMAIL": merge["email"]}
        env = {**os.environ, **who}
        if git(work, "revert", "--no-commit", sha, env=env).returncode != 0:
            return f"{sha[:8]} does not revert cleanly on main"
        if git(work, "commit", "-m", subject, "-m", body, env=env).returncode != 0:
            return "the revert commit failed"
        pushed = git(work, "push", "origin", f"HEAD:refs/heads/{branch}")
        return "" if pushed.returncode == 0 else f"the push of {branch} failed"


def check(q: Any) -> str:
    """One look at Core's main; returns what was done (also logged). Mutating callers hold the queue lock."""
    red = turned_red(q)
    if not red:
        return ""
    if str(red["run"]) in q.load().get("auto_reverts", {}):
        return ""
    unknown = unknown_failures(q, red)
    if unknown is None:
        return ""  # the next tick reads the jobs again
    if not unknown:
        return ""  # every failing job is a known flake: the main rerun handles it
    merge = merge_of(red["sha"])
    key = f"main-red:{red['run']}"
    jobs = ", ".join(unknown)
    if why := merge.get("why"):
        text = f"main run {red['run']} turned red at {red['sha'][:8]} ({jobs}); no auto-revert: {why}"
        return finish(q, red, key, text, {"why": why})
    branch = f"revert/pr-{merge['pr']}"
    subject = subject_of(merge["title"], merge["pr"])
    if revert_exists(branch):
        return finish(q, red, key, f"main run {red['run']} red at #{merge['pr']}; the revert branch {branch} has a PR already", {"branch": branch})
    body = (f"This reverts commit {red['sha']} (#{merge['pr']}).\n\nMain CI run {red['run']} turned red on that merge, and "
            f"the failing job(s) {jobs} are not known flakes. Opened by `sm` (auto-revert).")
    if why := make_revert(red["sha"], merge, branch, subject, body):
        return finish(q, red, key, f"main run {red['run']} turned red at #{merge['pr']} ({jobs}); auto-revert failed: {why}", {"why": why})
    code, out = hzlib.gh("pr", "create", "-R", hzlib.CORE, "--head", branch, "--base", "main", "--title", subject, "--body", body)
    found = re.search(r"/pull/(\d+)", out)
    if code != 0 or not found:
        return finish(q, red, key, f"main run {red['run']} red at #{merge['pr']}; {branch} pushed, the PR could not be opened", {"branch": branch})
    number = found.group(1)
    head = (hzlib.ghj("pr", "view", number, "-R", hzlib.CORE, "--json", "headRefOid", default={}) or {}).get("headRefOid", "")
    with q.state_lock():
        data = q.load()
        data["items"] = [i for i in data["items"] if i["key"] != number] + [{
            "key": number, "repo": hzlib.CORE, "pr": number, "after": [], "status": "queued", "detail": "auto-revert", "reruns": 0, "noted": "",
            "added": int(time.time()), "fixes_main": True, "reviewed_head": "", "guarded_head": head, "bead": ""}]
        q.save(data)
    return finish(q, red, key, f"main run {red['run']} turned red at #{merge['pr']} ({jobs}): revert PR #{number} opened, auto-merge follows",
                  {"pr": number, "branch": branch})


def finish(q: Any, red: dict[str, Any], key: str, text: str, record: dict[str, Any]) -> str:
    """Record the run as handled (never twice), log, note and raise the alarm."""
    q.update(auto_reverts={**q.load().get("auto_reverts", {}), str(red["run"]): {"sha": red["sha"], **record}})
    q.log(f"auto-revert: {text}")
    hzlib.note(f"sm queue auto-revert: {text}")
    try:
        edge.raise_alarm(key, "ALERT", text)
    except Exception as error:  # noqa: BLE001 - the log and the note already say it
        q.log(f"alarm failed: {error!r}")
    return text
