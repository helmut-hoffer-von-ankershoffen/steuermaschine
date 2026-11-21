"""The verdict of a promote: one independent review of the exact candidate commit, kept outside the tree.

    promote_verdict.py check STABLE REPO COMMIT
    promote_verdict.py record STABLE REPO COMMIT --reviewer NAME [--base SHA|none] [--notes TEXT]
    promote_verdict.py record-ci STABLE REPO COMMIT --reviewer NAME --source "ci TAG URL"

The receipt is the file STABLE/verdicts/<full commit SHA> (STABLE is $SM_STABLE of bin/promote). It lives next to the
gate receipts of the stable install, not in any repository: no snapshot or push can publish it, the promote that
switches is the one that reads it, and a commit cannot carry its own verdict. Its lines, in this order:

    commit: <the candidate, 40 hex>
    base: <the commit the reviewer diffed from: the last promoted one, 40 hex; `none` before the first promote>
    reviewer: <who reviewed: the reviewer's agent id>
    recorded-by: <$STEUERMASCHINE_AGENT of the process that recorded it, or `unset`>
    at: <UTC time of the record>
    notes: <optional, one line>
    VERDICT: APPROVE

Every record is also appended to STABLE/verdicts/records.log, so a replaced receipt stays as evidence.

`check` (run by bin/promote) exits 0 only when that file exists, parses exactly, names COMMIT, names as base the commit
the live release was built from (STABLE/current -> releases/<first 12 of the commit>), its reviewer is none of the
git identities of the commits from base to COMMIT (author, committer and Co-Authored-By trailers, by name or email,
case-insensitive), and `recorded-by`, when set, is the reviewer. Anything else exits 1 and names why; there is no
option and no setting that skips it. `record` writes the file after the same checks and refuses when
$STEUERMASCHINE_AGENT is set and is not --reviewer: the agent that records is the reviewer.

`record-ci` is for the Mini follower (bin/release_ci.py follow): the CI of a release already holds the independent
verdict (the `Gate review` status on the merged pull request's head; the release exists only because it was present).
The follower looks it up and records it here, with an extra line `source: ci TAG URL`. Such a receipt is checked exactly
like any other (commit, base = the live release, reviewer none of the authors) except that `recorded-by` is `ci-follower`
instead of the reviewer.

The limit, stated plainly: every agent commits under the owner's git identity with a model trailer and runs under one
uid that can write STABLE/verdicts. The identity check therefore catches only a verdict recorded under a committer's
name or email; it cannot tell one agent from another. Unlike the apply verdict (bound to agent ids of the runs that
produced the bytes), independence here rests on the coordinator dispatching a different agent as reviewer, whose agent
id is `reviewer:` and `recorded-by:`.
Exit 2: bad arguments.
"""

from __future__ import annotations

import datetime
import os
import re
import subprocess
import sys
from pathlib import Path

HEX40 = re.compile(r"[0-9a-f]{40}")
KEYS = ("commit", "base", "reviewer", "recorded-by", "at", "notes", "source")
CI_RECORDER = "ci-follower"
REQUIRED = ("commit", "base", "reviewer", "at")
VERDICT = "VERDICT: APPROVE"


class Refused(Exception):
    """The verdict does not hold; the message says why."""


def receipt_path(stable: Path, commit: str) -> Path:
    return stable / "verdicts" / commit


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False, text=True, timeout=60)
    if done.returncode != 0:
        raise Refused(f"git {' '.join(args[:3])} failed in {repo}: {done.stderr.strip()[:200]}")
    return done.stdout


def full_sha(repo: Path, rev: str) -> str:
    return git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").strip()


def promoted_base(stable: Path, repo: Path) -> str:
    """The full commit the live release was built from, or `none` when nothing was promoted yet."""
    current = stable / "current"
    if not current.is_symlink() and not current.exists():
        return "none"
    name = Path(os.readlink(current)).name if current.is_symlink() else current.resolve().name
    if not re.fullmatch(r"[0-9a-f]{12}", name):
        raise Refused(f"{current} names {name!r}, not a release of a commit: the reviewed base cannot be checked")
    try:
        return full_sha(repo, name)
    except Refused:
        raise Refused(f"the live release {name} is not a commit of {repo}: the reviewed base cannot be checked") from None


def identities(repo: Path, base: str, commit: str) -> set[str]:
    """Every author, committer and Co-Authored-By of the commits from base to commit, as names and emails."""
    span = commit if base == "none" else f"{base}..{commit}"
    out = git(repo, "log", "--format=%an%x1f%ae%x1f%cn%x1f%ce%x1f%(trailers:key=Co-Authored-By,valueonly,separator=%x1f)%x1e", span)
    found: set[str] = set()
    for field in re.split(r"[\x1e\x1f]", out):
        found |= _forms(field)
    return found


def _forms(who: str) -> set[str]:
    """`Name <email>` -> {name, email}; anything else -> itself. Case-folded, empty parts dropped."""
    who = who.strip()
    match = re.fullmatch(r"(.*?)\s*<([^>]*)>", who)
    parts = [match.group(1), match.group(2)] if match else [who]
    return {p.strip().casefold() for p in parts if p.strip()}


def parse(text: str) -> dict[str, str]:
    """The receipt's fields; Refused when it is not exactly the documented form."""
    lines = [line.rstrip("\r") for line in text.splitlines() if line.strip()]
    if not lines or lines[-1] != VERDICT:
        raise Refused(f"its last line is not exactly `{VERDICT}`")
    fields: dict[str, str] = {}
    for line in lines[:-1]:
        key, sep, value = line.partition(": ")
        if not sep or key not in KEYS:
            raise Refused(f"it has a line that is not one of {', '.join(KEYS)}: {line[:80]!r}")
        if key in fields:
            raise Refused(f"it names {key} twice")
        fields[key] = value.strip()
    missing = [k for k in REQUIRED if not fields.get(k)]
    if missing:
        raise Refused(f"it lacks {', '.join(missing)}")
    if not HEX40.fullmatch(fields["commit"]):
        raise Refused(f"its commit {fields['commit'][:50]!r} is not a full commit SHA")
    if fields["base"] != "none" and not HEX40.fullmatch(fields["base"]):
        raise Refused(f"its base {fields['base'][:50]!r} is neither a full commit SHA nor `none`")
    return fields


def independent(repo: Path, base: str, commit: str, reviewer: str) -> None:
    hit = _forms(reviewer) & identities(repo, base, commit)
    if hit:
        raise Refused(f"the reviewer {reviewer} is an author of the commits from {base[:12]} to {commit[:12]} ({', '.join(sorted(hit))}): "
                      "a different agent must review")


def check(stable: Path, repo: Path, commit: str) -> dict[str, str]:
    """The receipt of COMMIT when it holds; Refused otherwise."""
    if not HEX40.fullmatch(commit):
        raise Refused(f"{commit!r} is not a full commit SHA")
    path = receipt_path(stable, commit)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise Refused(f"no verdict is recorded for {commit[:12]} ({path})") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise Refused(f"the verdict {path} cannot be read: {exc}") from None
    try:
        fields = parse(text)
    except Refused as exc:
        raise Refused(f"the verdict {path} is not valid: {exc}") from None
    if fields["commit"] != commit:
        raise Refused(f"the verdict {path} names commit {fields['commit'][:12]}, not the candidate {commit[:12]}")
    want = promoted_base(stable, repo)
    if fields["base"] != want:
        raise Refused(f"the verdict reviewed the diff from {fields['base'][:12]}, but the live release is {want[:12]}: "
                      "the reviewer must read the diff from the live release (sm self reviews), then record again")
    by = fields.get("recorded-by", "unset")
    ci = fields.get("source", "").startswith("ci ") and by == CI_RECORDER
    if by not in ("unset", fields["reviewer"]) and not ci:
        raise Refused(f"the verdict was recorded by {by}, not by its reviewer {fields['reviewer']}: the agent that records is the reviewer")
    independent(repo, fields["base"], commit, fields["reviewer"])
    return fields


def record(stable: Path, repo: Path, commit: str, reviewer: str, base: str, notes: str, now: str, agent: str = "") -> Path:
    full = full_sha(repo, commit)
    if not full:
        raise Refused(f"{commit} is not a commit of {repo}")
    if not reviewer.strip() or any(c in reviewer + notes for c in "\r\n"):
        raise Refused("--reviewer must be a name on one line (and --notes one line)")
    if any(c in agent for c in "\r\n"):
        raise Refused("STEUERMASCHINE_AGENT must be one line")
    if agent.strip() and agent.strip() != reviewer.strip():
        raise Refused(f"STEUERMASCHINE_AGENT is {agent.strip()}, not --reviewer {reviewer.strip()}: the agent that records the verdict is its reviewer")
    want = promoted_base(stable, repo)
    given = want if not base else ("none" if base == "none" else full_sha(repo, base))
    if given != want:
        raise Refused(f"--base {base} is not the live release {want[:12]}: a promote checks the diff from the live release")
    independent(repo, given, full, reviewer)
    lines = [f"commit: {full}", f"base: {given}", f"reviewer: {reviewer.strip()}", f"recorded-by: {agent.strip() or 'unset'}", f"at: {now}"]
    lines += [f"notes: {notes.strip()}"] if notes.strip() else []
    path = receipt_path(stable, full)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{full}.{os.getpid()}")
    tmp.write_text("\n".join([*lines, VERDICT]) + "\n", encoding="utf-8")
    tmp.replace(path)
    with (path.parent / "records.log").open("a", encoding="utf-8") as log:  # evidence: a later record replaces the receipt
        log.write(" | ".join(lines) + "\n")
    return path


def record_ci(stable: Path, repo: Path, commit: str, reviewer: str, source: str, now: str) -> Path:
    """A receipt for the verdict the release's CI recorded (`source` is `ci TAG URL`); base is the live release."""
    full = full_sha(repo, commit)
    if not full:
        raise Refused(f"{commit} is not a commit of {repo}")
    if not source.startswith("ci ") or not reviewer.strip() or any(c in reviewer + source for c in "\r\n"):
        raise Refused("--reviewer must be a name on one line and --source one line starting with `ci `")
    base = promoted_base(stable, repo)
    independent(repo, base, full, reviewer)
    lines = [f"commit: {full}", f"base: {base}", f"reviewer: {reviewer.strip()}", f"recorded-by: {CI_RECORDER}", f"at: {now}", f"source: {source.strip()}"]
    path = receipt_path(stable, full)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{full}.{os.getpid()}")
    tmp.write_text("\n".join([*lines, VERDICT]) + "\n", encoding="utf-8")
    tmp.replace(path)
    with (path.parent / "records.log").open("a", encoding="utf-8") as log:
        log.write(" | ".join(lines) + "\n")
    return path


def _options(rest: list[str]) -> dict[str, str] | None:
    opts: dict[str, str] = {}
    while rest:
        name = rest.pop(0)
        if name not in ("--reviewer", "--base", "--notes", "--source") or not rest or name[2:] in opts:
            return None
        opts[name[2:]] = rest.pop(0)
    return opts if "reviewer" in opts else None


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[0] not in ("check", "record", "record-ci"):
        print(__doc__, file=sys.stderr)
        return 2
    verb, stable, repo, commit, rest = argv[0], Path(argv[1]), Path(argv[2]), argv[3], argv[4:]
    try:
        if verb == "check":
            if rest:
                print(__doc__, file=sys.stderr)
                return 2
            fields = check(stable, repo, commit)
            print(f"verdict: APPROVE of {commit[:12]} by {fields['reviewer']} (diff from {fields['base'][:12]}, recorded {fields['at']})", file=sys.stderr)
            return 0
        opts = _options(rest)
        if opts is None:
            print(__doc__, file=sys.stderr)
            return 2
        now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if verb == "record-ci":
            path = record_ci(stable, repo, commit, opts["reviewer"], opts.get("source", ""), now)
            print(f"verdict: recorded {path}", file=sys.stderr)
            return 0
        path = record(stable, repo, commit, opts["reviewer"], opts.get("base", ""), opts.get("notes", ""), now, os.environ.get("STEUERMASCHINE_AGENT", ""))
        print(f"verdict: recorded {path}", file=sys.stderr)
        return 0
    except Refused as exc:
        print(f"verdict: REFUSED {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
