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

Usage: uv run --script scripts/check_confidentiality.py [OPTION] [FILE ...]
    [FILE ...]          files, or every tracked file
    --commit-msg FILE   a commit message (commit-msg hook)
    --messages A..B     the messages of a range of commits
    --text TEXT         a string, such as a pull request title or branch
    --identity          the author and committer git will record, and that their emails are not machine hostnames
    --branch            the current branch's name (pre-push hook)
    --require           fail instead of skipping when no terms are set
"""

import argparse
import contextlib
import os
import subprocess
import sys
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


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True, timeout=60).stdout


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


def sources(args: argparse.Namespace) -> Iterator[tuple[str, str]]:
    """Collect what the command line selects.

    Yields:
        `(where, text)` pairs to scan; a file contributes its path and its content.
    """
    if args.commit_msg:
        yield "commit message", Path(args.commit_msg).read_text(encoding="utf-8")
    elif args.messages:
        for sha in _git("rev-list", args.messages).split():
            yield f"commit {sha[:12]}", _git("log", "-1", "--format=%an <%ae>%n%cn <%ce>%n%B", sha)
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
    args = parser.parse_args(argv)

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
    for where, text in sources(args):
        findings += scan(where, text, compiled)
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"{len(findings)} confidential term(s) found; remove them before committing.", file=sys.stderr)
        return 1
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
