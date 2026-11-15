"""From a committed head in a worktree to a queued PR: checks, one push, the PR from a body file, then the ship step.

New code, not a move. The former route ran the repository's own `make pr` (scripts/pr.sh), which is not run on this
machine any more; its steps are done here, in its order, through the core's runner and the one GitHub client:

  1  the branch and its head SHA, resolved once; a work branch, a clean tree, a title of at most 72 characters, the
     worktree's origin is the repository the PR is opened on, a readable body (read once, into memory)
  2  the CI meta preflight of the local commits and the branch name, and the check of the TITLE and BODY that are
     about to be published (commit-message rule, confidential terms); the preflight area owns both. Publishing a
     confidential term is irreversible: a problem, or a check that could not be made (a refusal), ends here with
     nothing written. Nothing the user wrote is printed before this step passed; its lines name positions only.
     The branch and head are read again: moved means refused
  3  not behind origin/main; every commit of the head on origin/main was checked; the remote branch, if it exists,
     is contained in this head (nothing is rebased or forced)
  4  the push check (main CI starved, release candidate): WAIT ends here with exit 75
  --- without --live the command stops here and prints what it would run ---
  5  ONE push of the checked commit under the run lock: `sm gate run --max-wait N -- git push origin SHA:refs/heads/BRANCH`
     (exit 75 when the lock is not free within the setting head_pr_lock_max_wait_seconds); HEAD moved meanwhile: no PR
  6  the PR: created through the GitHub client from a private copy of the checked body, or the open PR of the branch
     adopted; either is read back and shipped only when it is this repository's (no fork), on the branch, at the
     pushed SHA, into main; an adopted PR's title and body are checked as in step 2 and never printed
  6b the ownership record of the PR (`native/pr_owned.py`): focused admission of a later head for this PR accepts it
  7  the title read back (at most 72 characters)
  8  the ship step: guard, then queue (NEEDS_REVIEW is exit 2); with a reason, pinned to the pushed SHA

Different from the old route on purpose: no full local preflight (it never ran one: NO_PREFLIGHT=1), no rebase onto
a moved remote branch (refused instead), an existing PR keeps its title, no draft PRs, dry by default, and auto-merge
is never switched on: merging is the queue's job.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from steuermaschine.engine import Refuse

from .. import pr_owned
from . import deps, push_gate, repos
from .deps import Done

TITLE_MAX = 72
NOT_A_WORK_BRANCH = ("main", "master", "HEAD", "")
VIEW = "number,state,title,body,headRefName,headRefOid,baseRefName,isCrossRepository,headRepository,headRepositoryOwner,autoMergeRequest"
TIMED_OUT, GAVE_UP = 124, 75  # the run lock: stopped by its timeout; gave up waiting, nothing ran


class Stop(Exception):
    def __init__(self, code: int, text: str):
        self.code, self.text = code, text


def slug(url: str) -> str:
    """OWNER/NAME of a git remote URL (https, ssh or scp form), lower case; "" when it has no two path parts."""
    path = re.sub(r"^[a-z+]+://[^/]+/|^[^@/]+@[^:]+:", "", url.strip()).removesuffix("/").removesuffix(".git")
    parts = [p for p in path.split("/") if p]
    return "/".join(parts[-2:]).lower() if len(parts) >= 2 else ""


def open_pr(worktree: Path, bead: str, title: str, body_file: Path | None, reason: str | None, *, commands, forge, live: bool, max_wait: int = 1800,
            critical: bool = False) -> Done:
    """Run the route. `commands` is the core's process runner, `forge` the GitHub provider (`create_pr`, `.gh`).

    No text the user gave (title, body, branch name, commit subjects) and no text of an adopted PR is printed before
    the checks have passed, and a problem line names where a text is, never the text. The head is resolved once,
    before the checks; what is pushed is that commit (`SHA:refs/heads/BRANCH`), the PR body is the checked text
    (a private copy written after the check), and the PR is shipped only when it is this repository's, on that
    branch, at that commit, into main. `critical`: the head is a declared focus head; a release/* branch is critical too.
    A critical push runs exclusively under the run lock: its pre-push hook's timed session gets the idle machine."""
    lines: list[str] = []
    worktree = Path(worktree).expanduser().resolve()

    def say(text: str) -> None:
        lines.append(f"head pr: {text}")

    def git(*args: str, timeout: float = 120):
        return commands.run(["git", "-C", str(worktree), *args], timeout=timeout)

    def need(done, what: str, code: int = 1) -> str:
        if done.rc:
            raise Stop(code, f"{what}: {(done.err or done.out).strip()[:400]}")
        return done.out.strip()

    def where() -> tuple[str, str]:
        return (need(git("rev-parse", "--abbrev-ref", "HEAD"), f"{worktree} is not a git worktree", 2),
                need(git("rev-parse", "HEAD"), "cannot read HEAD"))

    with tempfile.TemporaryDirectory(prefix="head-pr-") as private:
        try:
            branch, head = where()  # resolved once: everything below is checked against, and pushes, this commit
            if branch in NOT_A_WORK_BRANCH:
                raise Stop(1, f"refusing: branch is '{branch}', not a work branch")
            dirty = need(git("status", "--porcelain"), "git status failed")
            if dirty:
                raise Stop(1, f"refusing: uncommitted changes ({len(dirty.splitlines())} paths; untracked files count), commit or remove them first (git status)")
            if len(title) > TITLE_MAX:
                raise Stop(1, f"title is {len(title)} chars, the limit is {TITLE_MAX}")
            repo, why = forge.gh.repo()
            if why:
                raise Stop(2, f"cannot tell the repository of the PR: {why}")
            origin = slug(need(git("remote", "get-url", "origin"), "the worktree has no remote origin", 2))
            if origin != repo.lower():
                raise Stop(2, f"the worktree's origin is not {repo}, the repository the PR would be opened on; nothing was pushed (setting repo, or the right worktree)")
            if body_file is None:
                body = f"Bead {bead}.\n"
            else:
                try:
                    body = Path(body_file).expanduser().read_text()
                except (OSError, UnicodeDecodeError) as error:
                    raise Stop(2, f"the body file {body_file} cannot be read: {error}") from None
            if not body.strip():
                raise Stop(2, f"the body file {body_file} is empty")
            try:
                problems, checked = deps.commit_meta(worktree, ref=branch)
                if problems:
                    raise Stop(1, "fix the CI meta problems BEFORE pushing (they cost a full CI attempt); nothing was pushed or opened:\n  " + "\n  ".join(problems))
                published = deps.texts_problems(worktree, title, body)
            except Refuse as refused:
                raise Stop(1, f"{refused}; nothing was pushed or opened") from None
            if published:
                raise Stop(1, "the title or body must not be published as it is; nothing was pushed or opened:\n  " + "\n  ".join(published))
            if where() != (branch, head):
                raise Stop(1, "the branch or its head moved while it was checked; nothing was pushed or opened. Run this command again")
            say(f"CI meta preflight clean; title and body may be published; branch {branch} at {head[:8]}")
            body_copy = Path(private) / "body.md"  # what gh reads is the checked text, not the user's file
            body_copy.write_text(body)
            need(git("fetch", "--quiet", "origin", "main"), "cannot fetch origin/main", 2)
            if git("merge-base", "--is-ancestor", "origin/main", head).rc:
                raise Stop(2, "behind origin/main: bring main into the branch first; nothing was pushed")
            ahead = need(git("rev-list", "--count", f"origin/main..{head}"), "cannot count the commits of the head", 2)
            if not ahead.isdigit() or not 0 < int(ahead) <= checked:
                raise Stop(1, f"the head has {ahead} commits on origin/main, the CI meta preflight checked {checked}: not every commit was checked; nothing was pushed")
            remote = need(git("ls-remote", "--heads", "origin", f"refs/heads/{branch}"), "cannot read the remote branch", 2)
            if remote:
                remote_sha = remote.split()[0]
                if remote_sha != head:
                    need(git("fetch", "--quiet", "origin", branch), "cannot fetch the remote branch", 2)
                    if git("merge-base", "--is-ancestor", remote_sha, head).rc:
                        raise Stop(2, f"origin/{branch} ({remote_sha[:8]}) has commits this head does not contain (rewritten here, or updated there). "
                                      "This command never rebases and never forces. Nothing was pushed. Either bring them in "
                                      f"(git merge origin/{branch}, then this command again), or publish this head under a new name, which opens a "
                                      f"new PR (git switch -c {branch}-2, then this command again; close the old PR)")
            say(f"head {head[:8]} contains origin/main" + (" and the remote branch" if remote else "; the branch is new on the remote"))
            code, gate = push_gate.check(branch)
            lines.extend(gate)
            if code:
                raise Stop(code, f"DO NOT PUSH now. Commit locally, batch commits into ONE push, retry: sm head pr {bead} \"{title}\"")
            critical = critical or branch.startswith("release/")
            push = [*deps.gate(max_wait, exclusive=critical), "git", "push", "origin", f"{head}:refs/heads/{branch}"]
            planned = forge.create_pr(branch, title, str(body_copy), live=False)
            if str(planned.state).startswith("unknown"):
                raise Stop(1, f"cannot tell whether {branch} has a pull request: {planned.state}; nothing was pushed")
            if not live:
                say("dry run; with --live it would run, in this order:")
                say("  " + " ".join(["sm", *push[3:]]) + f"   (in {worktree})")
                say("  " + (planned.would_run if planned.state == "dry_run" else f"adopt the open PR #{planned.pr.number} of the branch, if it is this repository's at {head[:8]}"))
                say("  the ship step: guard, then queue" + (f" as reviewed, pinned to {head[:8]}" if reason else ""))
                return Done(0, "".join(f"{line}\n" for line in lines))
            say(f"one push of {head[:8]} under the run lock (waits at most {max_wait}s" + ("; critical path: exclusive, on an idle machine)" if critical else ")"))
            pushed = commands.run(push, cwd=str(worktree), timeout=max_wait + 3600)
            lines.extend((pushed.out + pushed.err).strip().splitlines())
            if pushed.rc == GAVE_UP:
                raise Stop(GAVE_UP, f"the run lock was not free within {max_wait}s (setting head_pr_lock_max_wait_seconds); nothing was pushed. Run this command again later")
            if pushed.rc:
                raise Stop(pushed.rc, f"the push exited {pushed.rc}; no PR was opened")
            if where() != (branch, head):
                raise Stop(1, f"HEAD moved while this command waited for the run lock; the checked head {head[:8]} was pushed, nothing else. No PR was opened: "
                              "run this command again for the new head")
            made = forge.create_pr(branch, title, str(body_copy), live=True)
            if made.pr is None:
                raise Stop(1, f"the branch is pushed but the PR was not opened: {made.state}")
            number = made.pr.number
            view, why = forge.gh.try_json(["pr", "view", str(number), "-R", repo, "--json", VIEW])
            if why or not isinstance(view, dict):
                raise Stop(1, f"the branch is pushed, but PR #{number} cannot be read back ({why or 'no answer'}); it is not shipped")
            owner = f"{(view.get('headRepositoryOwner') or {}).get('login', '')}/{(view.get('headRepository') or {}).get('name', '')}"
            wrong = [what for what, bad in (("not open", str(view.get("state", "")).upper() != "OPEN"),
                                            ("from another repository (a fork)", bool(view.get("isCrossRepository")) or owner.lower() != repo.lower()),
                                            (f"not on the branch {branch}", view.get("headRefName") != branch),
                                            (f"not at the pushed head {head[:8]}", view.get("headRefOid") != head),
                                            ("not into main", view.get("baseRefName") != "main")) if bad]
            if wrong:
                raise Stop(1, f"PR #{number} of the branch is " + ", ".join(wrong) + "; it is not shipped. The pushed head is "
                              f"{head[:8]}; publish it under a new branch name (git switch -c {branch}-2, then this command again)")
            pr_owned.record(repo, number, branch, head, "created" if made.state == "created" else "adopted")  # m598: a later head may update it
            got = str(view.get("title") or "")
            if made.state == "created":
                say(f"PR #{number} opened: {made.pr.url}")
                if got != title:
                    raise Stop(1, f"PR #{number} opened, but its title reads back different from the checked one; it is not shipped")
            else:
                say(f"PR #{number} exists and is adopted: {made.pr.url}")
                try:
                    adopted = deps.texts_problems(worktree, got, str(view.get("body") or ""))
                except Refuse as refused:
                    raise Stop(1, f"{refused}; PR #{number} is not shipped") from None
                if adopted:
                    raise Stop(1, f"the title or body of the open PR #{number} must not be merged as it is; it is not shipped (gh pr edit {number}, then this command again):\n  "
                                  + "\n  ".join(adopted))
                say("its title and body are checked; they stay as they are")
                if view.get("autoMergeRequest"):
                    say("note: auto-merge is already on for this PR (this command did not switch it on)")
            say(f"title read back ({len(got)} chars)")
            if not got or len(got) > TITLE_MAX:
                raise Stop(1, f"PR #{number} opened but title read back as {len(got)} chars (limit {TITLE_MAX}); fix the title, then: sm head ship {number}")
            say(f"ship {number}")
            shipped = deps.ship(repos.pr_key(repo, number), reason or "", head)
            lines.extend(shipped.stdout.splitlines())
            lines.append(str(number))
            return Done(shipped.returncode, "".join(f"{line}\n" for line in lines), shipped.stderr)
        except Stop as stop:
            return Done(stop.code, "".join(f"{line}\n" for line in lines), f"head pr: {stop.text}\n")
