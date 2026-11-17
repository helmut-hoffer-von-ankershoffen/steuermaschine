"""Refuse private committed identities or messages before a push or PR mutation."""

from pathlib import Path

from steuermaschine.config import child_environment
from steuermaschine.engine import Refuse
from steuermaschine.native import NativeRuntime
from steuermaschine.protocols import BinaryCommands

from ...confidentiality import load_terms, patterns
from ..raw_transfer import OID, GitObjects, commit_findings


def check(rt: NativeRuntime, worktree: Path, ref: str) -> tuple[list[str], int]:
    """Inspect the local proposed branch, not the already published PR commit list.

    Public main ancestry is excluded; every other commit, including merge sides and
    commits whose changes were reverted, is scanned. No CI-only identity exception.
    """
    env = child_environment()
    if configured := rt.settings.get("preflight_terms_file"):
        env["CONFIDENTIAL_TERMS_FILE"] = str(configured)
    try:
        terms = load_terms(env)
        compiled = patterns(terms)
    except (OSError, UnicodeError, IndexError):
        raise Refuse("The configured confidentiality policy could not be read.") from None
    if not terms:
        raise Refuse("No confidential terms are configured; set preflight_terms_file.")
    commands = rt.providers.commands
    if not isinstance(commands, BinaryCommands):
        raise Refuse("The process provider cannot read literal Git bytes.")
    git = GitObjects(commands, worktree)
    if git.read("rev-parse", "--is-shallow-repository").strip() != b"false":
        raise Refuse("A full local repository is required for push metadata checks.")

    def resolve(name: str) -> str:
        raw = git.read("rev-parse", "--verify", "--end-of-options", f"{name}^{{commit}}").strip()
        if not OID.fullmatch(raw):
            raise Refuse("The proposed push range could not be resolved.")
        return raw.decode("ascii")

    if not OID.fullmatch(ref.encode()) and not ref.startswith("refs/heads/"):
        ref = f"refs/heads/{ref}"
    head, base = resolve(ref), resolve("refs/remotes/origin/main")
    commits = git.ids(head, f"^{base}", "--")
    findings = commit_findings(git, commits, compiled)
    if (resolve(ref), resolve("refs/remotes/origin/main")) != (head, base):
        raise Refuse("The proposed push range changed during metadata checks; retry.")
    return ([f"confidential: commit {f.index} {f.kind}, line {f.line}, rule {f.rule}; correct before pushing"
             for f in findings], len(commits))
