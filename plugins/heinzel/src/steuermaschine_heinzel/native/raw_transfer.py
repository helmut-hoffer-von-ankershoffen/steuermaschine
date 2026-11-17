"""Inspect the literal Git objects of a proposed public branch transfer."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import typer

from steuermaschine.config import child_environment
from steuermaschine.engine import Refuse
from steuermaschine.github import Gh
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime
from steuermaschine.protocols import BinaryCommands, Commands

from ..confidentiality import hostname_email, load_terms, patterns, scan, text_bytes

OID = re.compile(rb"(?:[a-f0-9]{40}|[a-f0-9]{64})")
REPO = re.compile(r"[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+")
Scope = Literal["outgoing", "inherited"]
Kind = Literal["ref", "metadata", "message", "path", "blob"]


class Match(Model):
    scope: Scope
    kind: Kind
    index: int
    line: int
    rule: int | Literal["machine-email"]


class Transfer(Model):
    head: str
    base: str
    term_count: int
    outgoing_commits: int
    outgoing_paths: int
    outgoing_blobs: int
    inherited_paths: int
    inherited_blobs: int
    findings: list[Match]

    @property
    def exit_code(self) -> int:
        return int(any(f.scope == "outgoing" for f in self.findings))

    def render(self) -> list[str]:
        return [
            f"Head {self.head}, public base {self.base}, {self.term_count} configured rules",
            f"Outgoing: {self.outgoing_commits} commits, {self.outgoing_paths} paths, {self.outgoing_blobs} blobs",
            f"Inherited: {self.inherited_paths} paths, {self.inherited_blobs} blobs",
            *(f"{f.scope} {f.kind} #{f.index}, line {f.line}, rule {f.rule}" for f in self.findings),
            "REFUSED: outgoing matches" if self.exit_code else "No outgoing matches",
        ]


def public_base(commands: Commands, repo: str, base: str) -> None:
    """Only the configured public repository's current default ref can exclude ancestry."""
    if not REPO.fullmatch(repo):
        raise Refuse("Configure the destination repository before scanning.")
    gh = Gh(commands)
    info, _ = gh.try_json(["api", "--hostname", "github.com", f"repos/{repo}"])
    if not isinstance(info, dict) or info.get("visibility") != "public" or str(info.get("full_name", "")).lower() != repo.lower():
        raise Refuse("The destination's public visibility could not be verified.")
    branch = info.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise Refuse("The destination's default branch could not be verified.")
    ref, _ = gh.try_json(["api", "--hostname", "github.com", f"repos/{repo}/git/ref/heads/{quote(branch, safe='/')}"])
    obj = ref.get("object") if isinstance(ref, dict) else None
    if not isinstance(obj, dict) or obj.get("type") != "commit" or obj.get("sha") != base:
        raise Refuse("The public default branch does not match the requested base.")


class GitObjects:
    def __init__(self, commands: BinaryCommands, worktree: Path) -> None:
        self.commands = commands
        self.cwd = str(worktree.expanduser())
        self.env = child_environment(drop_prefixes=("GIT_",), GIT_GRAFT_FILE="/dev/null", GIT_NO_LAZY_FETCH="1")

    def read(self, *args: str) -> bytes:
        rc, raw = self.commands.run_bytes(["git", "--no-replace-objects", *args], self.cwd, 60, self.env)
        if rc:
            raise Refuse("Git could not read the complete object range.")
        return raw

    def ids(self, *args: str) -> list[str]:
        rows = self.read("rev-list", *args).splitlines()
        if any(not OID.fullmatch(row) for row in rows):
            raise Refuse("Git returned an invalid object inventory.")
        return [row.decode("ascii") for row in rows]

    def snapshot(self, revision: str) -> tuple[set[bytes], set[str]]:
        paths: set[bytes] = set()
        blobs: set[str] = set()
        raw = self.read("ls-tree", "-r", "-t", "-z", revision)
        if raw and not raw.endswith(b"\0"):
            raise Refuse("Git returned an incomplete path inventory.")
        for row in raw.split(b"\0")[:-1]:
            header, tab, path = row.partition(b"\t")
            parts = header.split()
            if not tab or not path or len(parts) != 3 or not OID.fullmatch(parts[2]):
                raise Refuse("Git returned an invalid path inventory.")
            paths.add(path)
            if parts[1] == b"blob":
                blobs.add(parts[2].decode("ascii"))
            elif parts[1] not in {b"tree", b"commit"}:
                raise Refuse("Git returned an unsupported tree entry.")
        return paths, blobs


def commit_findings(git: GitObjects, commits: list[str], terms: list[re.Pattern[str]]) -> list[Match]:
    """Scan literal committed identities and complete messages, never returning their contents."""
    findings: list[Match] = []
    for index, sha in enumerate(commits, 1):
        header, separator, message = git.read("cat-file", "commit", sha).partition(b"\n\n")
        if not separator:
            raise Refuse("A raw commit has no message boundary.")
        sections: tuple[tuple[Kind, bytes], ...] = (("metadata", header), ("message", message))
        for kind, data in sections:
            for finding in scan("", data.decode("utf-8", errors="surrogateescape"), terms):
                findings.append(Match(scope="outgoing", kind=kind, index=index, line=finding.line, rule=finding.term))
        for number, line in enumerate(header.split(b"\n"), 1):
            if line.startswith((b"author ", b"committer ")) and hostname_email(line.decode("utf-8", errors="replace")):
                findings.append(Match(scope="outgoing", kind="metadata", index=index, line=number, rule="machine-email"))
    return findings


def inspect(git: GitObjects, head: str, base: str, ref: str, terms: list[re.Pattern[str]]) -> Transfer:
    if git.read("rev-parse", "--is-shallow-repository").strip() != b"false":
        raise Refuse("A full local repository is required.")
    for revision in (head, base):
        if git.read("cat-file", "-t", revision).strip() != b"commit":
            raise Refuse("The head and public base must be commit objects.")
    git.read("check-ref-format", ref)
    commits = git.ids(head, f"^{base}", "--")
    objects = set(git.ids("--objects", "--no-object-names", head, f"^{base}", "--"))
    base_paths, _ = git.snapshot(base)
    paths: set[bytes] = set()
    blobs: set[str] = set()
    for revision in commits or [head]:
        entries, contents = git.snapshot(revision)
        paths.update(entries)
        blobs.update(contents)
    outgoing_paths = paths - base_paths
    outgoing_blobs = blobs & objects
    findings: list[Match] = []

    def match(scope: Scope, kind: Kind, index: int, data: bytes) -> None:
        text = text_bytes(data) if kind == "blob" else data.decode("utf-8", errors="surrogateescape")
        for finding in scan("", text, terms):
            findings.append(Match(scope=scope, kind=kind, index=index, line=finding.line, rule=finding.term))

    match("outgoing", "ref", 1, ref.encode())
    findings.extend(commit_findings(git, commits, terms))

    inventories: tuple[tuple[Scope, set[bytes], set[str]], ...] = (
        ("outgoing", outgoing_paths, outgoing_blobs),
        ("inherited", paths & base_paths, blobs - objects),
    )
    for scope, path_set, blob_set in inventories:
        for index, path in enumerate(sorted(path_set), 1):
            match(scope, "path", index, path)
        for index, sha in enumerate(sorted(blob_set), 1):
            match(scope, "blob", index, git.read("cat-file", "blob", sha))

    return Transfer(head=head, base=base, term_count=len(terms), outgoing_commits=len(commits),
                    outgoing_paths=len(outgoing_paths), outgoing_blobs=len(outgoing_blobs),
                    inherited_paths=len(paths & base_paths), inherited_blobs=len(blobs - objects), findings=findings)


@native("head raw-transfer", writes=False, replaces=("raw-transfer",), help=H(
    "Scan a public branch transfer for configured confidential terms.",
    "Use before a push to inspect every outgoing commit and its paths and blobs, including content removed later.",
    "nothing (read-only; asks GitHub for the public default branch and reads local Git objects).",
    "sm head raw-transfer WORKTREE --head SHA --base SHA --ref refs/heads/fix/example",
    "Review the outgoing and inherited findings before a public transfer.",
))
def raw_transfer(
    worktree: Path = typer.Argument(..., help="Local repository containing both commits."),
    head: str = typer.Option(..., "--head", help="Full outgoing commit ID."),
    base: str = typer.Option(..., "--base", help="Full current public default branch commit ID."),
    ref: str = typer.Option(..., "--ref", help="Full destination branch ref."),
) -> Transfer:
    if not all(OID.fullmatch(oid.encode()) for oid in (head, base)):
        raise Refuse("Provide full lowercase commit IDs for the head and base.")
    if not ref.startswith("refs/heads/"):
        raise Refuse("The destination must be a full branch ref.")
    rt = runtime()
    configured = rt.settings.get("preflight_terms_file")
    env = child_environment()
    if configured:
        env["CONFIDENTIAL_TERMS_FILE"] = str(configured)
    try:
        terms = load_terms(env)
        compiled = patterns(terms)
    except (OSError, UnicodeError, IndexError):
        raise Refuse("The configured confidentiality policy could not be read.") from None
    if not terms:
        raise Refuse("No confidential terms are configured.")
    commands = rt.providers.commands
    if not isinstance(commands, BinaryCommands):
        raise Refuse("The process provider cannot read literal Git bytes.")
    public_base(commands, str(rt.settings.get("repo") or ""), base)
    return inspect(GitObjects(commands, worktree), head, base, ref, compiled)
