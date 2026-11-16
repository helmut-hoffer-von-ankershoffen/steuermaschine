# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Fail when a confidential term appears in files or commit messages.

The terms never live in the repository: a list in a public repository, even hashed, gives them away. They come from
`$CONFIDENTIAL_TERMS` (newline-separated; CI sets it from the repository secret of the same name) or from the file
`$CONFIDENTIAL_TERMS_FILE`, default both `~/.config/heinzel-dev/confidential-terms` and
`~/.config/steuermaschine/private-terms`. The list is a deny-list: nothing on it may appear in the repository. One term per line, `#` comments.
Findings name the location and the term's number, never the term, because CI logs of a public repository are public.

Matching is case-insensitive after Unicode normalization (NFKC, invisible characters removed). Words of six letters or
more match anywhere, also inside identifiers (`term_cli`, `termdb`); shorter ones, typically first names, only where
no letter touches them, so they don't fire inside ordinary words. The words of a phrase may be joined by anything
that isn't a letter or digit, line breaks included, or by nothing. Binary files are scanned through their printable
strings, as `strings` would show them.

`--identity` also refuses an email git made up from the machine's name (`name@Some-MacBook-Air.local`), with or
without terms: it names a machine, not a mailbox, and would be published with every commit.

CI main may opt into `--github-owner-squash`: only the unchanged author email of the approved public baseline
is exempt, after GitHub verifies both commits' owner squash provenance. Names, committer fields, messages,
trailers and files are still scanned. Local hooks never enable this exception.

Usage: uv run --script scripts/check_confidentiality.py [OPTION] [FILE ...]
    [FILE ...]          files, or every tracked file
    --commit-msg FILE   a commit message (commit-msg hook)
    --messages A..B     the messages of a range of commits
    --text TEXT         a string, such as a pull request title or branch
    --identity          the author and committer git will record, and that their emails are not machine hostnames
    --branch            the current branch's name (pre-push hook)
    --require           fail instead of skipping when no terms are set
    --github-owner-squash  with --messages only: CI main may exempt author email after GitHub provenance checks
"""

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

# The standalone check runs before workspace dependencies are installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/heinzel/src"))
from steuermaschine_heinzel.confidentiality import (
    Finding,
    hostname_email,
    load_terms,
    patterns,
    scan,
    text_bytes,
)

# Reviewed public baseline for the unchanged author-email exception. The address itself is never stored here.
APPROVED_EMAIL_BASELINE = "686250077c0568d4787da1b48a6db23084c2b324"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True, timeout=60).stdout


def _github_api(path: str):
    """Read only the official API; never print response bodies or authentication errors."""
    result = subprocess.run(
        ["gh", "api", "--hostname", "github.com", path],
        check=True, capture_output=True, text=True, timeout=30,
    )
    return json.loads(result.stdout)


class OwnerSquashProof:
    """CI-only provenance for author email, never a message or another identity field.

    Only the unchanged email from the reviewed baseline is eligible. GitHub must attest both that baseline
    and the candidate: exact single-parent commit, verified signature, owner account, web-flow committer,
    and merged PR on this project's default branch. Other owner-account emails remain scanned.
    Missing or inconsistent evidence means scan the email normally.
    """

    def __init__(self):
        self.repo = None
        self.approved_email = None

    def eligible(self, sha: str, author_email: str) -> bool:
        """Fail closed on failed lookups or malformed proof without leaking API data."""
        try:
            if self.approved_email is None:
                baseline_email = _git("show", "-s", "--format=%ae", APPROVED_EMAIL_BASELINE).strip()
                if author_email != baseline_email or not self._eligible(APPROVED_EMAIL_BASELINE, baseline_email):
                    return False
                self.approved_email = baseline_email
            if author_email != self.approved_email:
                return False
            return self._eligible(sha, author_email)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
            return False

    def _eligible(self, sha: str, author_email: str) -> bool:
        if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_EVENT_NAME") != "push"
                or os.environ.get("GITHUB_REF") != "refs/heads/main"
                or hostname_email(f"<{author_email}>") is not None):
            return False
        source = tomllib.loads(Path("pyproject.toml").read_text())["project"]["urls"]["Source"]
        match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", source)
        if not match or os.environ.get("GITHUB_REPOSITORY") != match[1]:
            return False
        name = match[1]
        if self.repo is None:
            self.repo = _github_api(f"repos/{name}")
        repo = self.repo
        owner = repo["owner"]
        if (repo["full_name"] != name or repo["fork"] is not False or repo["default_branch"] != "main"
                or owner["type"] != "User" or owner["login"] != name.split("/")[0]
                or str(repo["id"]) != os.environ.get("GITHUB_REPOSITORY_ID")
                or str(owner["id"]) != os.environ.get("GITHUB_REPOSITORY_OWNER_ID")):
            return False
        commit = _github_api(f"repos/{name}/commits/{sha}")
        verification = commit["commit"]["verification"]
        if (commit["sha"] != sha or commit["author"]["id"] != owner["id"]
                or commit["author"]["login"] != owner["login"]
                or commit["committer"]["login"] != "web-flow"
                or commit["commit"]["author"]["email"] != author_email
                or commit["commit"]["committer"]["name"] != "GitHub"
                or commit["commit"]["committer"]["email"] != "noreply@github.com"
                or verification["verified"] is not True or verification["reason"] != "valid"
                or not verification["signature"] or not verification["payload"]
                or len(commit["parents"]) != 1
                or commit["parents"][0]["sha"] != _git("show", "-s", "--format=%P", sha).strip()):
            return False
        # Associated PRs alone are not proof: bind the merged PR's exact result and repository.
        pulls = _github_api(f"repos/{name}/commits/{sha}/pulls?per_page=100")
        for candidate in pulls:
            number = candidate["number"]
            if type(number) is not int or number < 1 or candidate["merge_commit_sha"] != sha:
                continue
            pull = _github_api(f"repos/{name}/pulls/{number}")
            if (pull["number"] == number and pull["merged"] is True and pull["state"] == "closed"
                    and pull["merged_at"] and pull["merge_commit_sha"] == sha
                    and pull["base"]["repo"]["id"] == repo["id"]
                    and pull["base"]["repo"]["full_name"] == name and pull["base"]["ref"] == "main"
                    and pull["merged_by"]["id"] == owner["id"]
                    and pull["merged_by"]["login"] == owner["login"]):
                return True
        return False


def _read(path: Path) -> str | None:
    """A file's text; for a binary file its printable strings, one per line; `None` if unreadable."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return text_bytes(data)


def identity_problems() -> list[str]:
    """Why the author or committer email git will record is not one to publish."""
    problems: list[str] = []
    for role in ("author", "committer"):
        with contextlib.suppress(subprocess.CalledProcessError):  # no identity: git refuses to commit anyway
            if email := hostname_email(_git("var", f"GIT_{role.upper()}_IDENT")):
                problems.append(
                    f"The {role} email {email} is a machine's hostname, not a mailbox, and would be published with "
                    "the commit. Set yours: git config --global user.email you@example.com (GitHub's noreply address "
                    "works: ID+USERNAME@users.noreply.github.com, see https://github.com/settings/emails)."
                )
    return problems


def _message_exclusion(revisions: str) -> list[str]:
    """Exclude the public main ancestry from the workflow's incremental feature-push range."""
    before = os.environ.get("BEFORE", "")
    ref = os.environ.get("GITHUB_REF", "")
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_EVENT_NAME") != "push"
        or not ref.startswith("refs/heads/")
        or ref == "refs/heads/main"
        or not re.fullmatch(r"[0-9a-f]{40}", before)
        or revisions != f"{before}..HEAD"
        or os.environ.get("GITHUB_SHA") != _git("rev-parse", "--verify", "HEAD").strip()
    ):
        return []
    public_main = _git("rev-parse", "--verify", "refs/remotes/origin/main^{commit}").strip()
    return [f"^{public_main}"]


def sources(args: argparse.Namespace, compiled: list[re.Pattern[str]] | None = None) -> Iterator[tuple[str, str]]:
    """Collect what the command line selects.

    Yields:
        `(where, text)` pairs to scan; a file contributes its path and its content.
    """
    if args.commit_msg:
        yield "commit message", Path(args.commit_msg).read_text(encoding="utf-8")
    elif args.messages:
        exclude = _message_exclusion(args.messages)
        proof = OwnerSquashProof() if getattr(args, "github_owner_squash", False) else None
        for sha in _git("rev-list", args.messages, *exclude, "--").split():
            if proof is None:
                yield f"commit {sha[:12]}", _git("log", "-1", "--format=%an <%ae>%n%cn <%ce>%n%B", sha)
                continue
            author, email, committer, committer_email, message = _git(
                "log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce%x00%B", sha,
            ).split("\0", 4)
            # Only a matching author email needs proof; preserve names, committer and complete body/trailers.
            if any(scan("email", email, compiled or [])) and proof.eligible(sha, email):
                email = ""
            yield f"commit {sha[:12]}", f"{author} <{email}>\n{committer} <{committer_email}>\n{message}"
    elif args.branch:
        yield "branch", _git("rev-parse", "--abbrev-ref", "HEAD")
    elif args.identity:
        for role in ("author", "committer"):
            with contextlib.suppress(subprocess.CalledProcessError):  # no identity: git refuses to commit anyway
                yield role, _git("var", f"GIT_{role.upper()}_IDENT")
    elif args.text is not None:
        yield "text", args.text
    else:
        for name in args.files or _git("ls-files", "-z").split("\0"):
            path = Path(name)
            yield f"{name} (path)", name
            if name and (text := _read(path)) is not None:
                yield name, text


def main(argv: list[str] | None = None) -> int:
    """Scan what the arguments select and report findings."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*")
    parser.add_argument("--commit-msg")
    parser.add_argument("--messages")
    parser.add_argument("--text")
    parser.add_argument("--identity", action="store_true")
    parser.add_argument("--branch", action="store_true")
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--github-owner-squash", action="store_true",
                        help="CI main only: verify GitHub owner squash provenance before exempting its author email")
    args = parser.parse_args(argv)
    if args.github_owner_squash and not args.messages:
        parser.error("--github-owner-squash requires --messages")

    # Independent of the term list, which plugin developers usually don't have.
    problems = identity_problems() if args.identity else []
    for problem in problems:
        print(problem, file=sys.stderr)
    terms = load_terms(os.environ)
    if not terms:
        if args.require:
            print("No confidential terms configured; set CONFIDENTIAL_TERMS.", file=sys.stderr)
            return 1
        print("Confidentiality check skipped: no terms configured (see scripts/check_confidentiality.py).")
        return 1 if problems else 0
    compiled = patterns(terms)
    findings: list[Finding] = []
    for where, text in sources(args, compiled):
        findings += scan(where, text, compiled)
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"{len(findings)} confidential term(s) found; remove them before committing.", file=sys.stderr)
        return 1
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
