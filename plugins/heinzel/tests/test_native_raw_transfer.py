"""The raw transfer inventories local Git objects with the configured terms."""

import json
import sys
from pathlib import Path

import pytest

from steuermaschine.config import child_environment
from steuermaschine.defaults import Subprocess
from steuermaschine.engine import Refuse
from steuermaschine.native import NativeRuntime, using
from steuermaschine.protocols import Result
from steuermaschine.testing.fakes import FakeCommands, fake_providers
from steuermaschine_heinzel.confidentiality import load_terms, patterns
from steuermaschine_heinzel.native.raw_transfer import GitObjects, inspect, public_base, raw_transfer

TERM = "fictionalmarker"


def git(repo: Path, *args: str) -> str:
    result = Subprocess().run(["git", *args], cwd=str(repo), timeout=10)
    assert result.rc == 0, result.err
    return result.out.strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "Test Author")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "commit.gpgsign", "false")
    return tmp_path


def commit(repo: Path, message: str, *args: str) -> str:
    git(repo, "add", "--all")
    git(repo, "commit", "-qm", message, *args)
    return git(repo, "rev-parse", "HEAD")


def test_new_ref_scans_removed_bytes_full_ancestry_and_public_baseline(repo):
    (repo / "base.txt").write_text(TERM)
    base = commit(repo, "public")
    git(repo, "switch", "-qc", "side")
    leak = repo / f"{TERM}.txt"
    leak.write_bytes(b"\xff\0" + TERM.encode())
    first = commit(repo, TERM, "--author", f"{TERM} <local@host.local>")
    leak.unlink()
    commit(repo, "remove intermediate entry")
    git(repo, "switch", "-q", "main")
    git(repo, "merge", "--no-ff", "--no-edit", "side")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "replace", first, base)
    (repo / "base.txt").write_text("dirty worktree")
    report = inspect(GitObjects(Subprocess(), repo), head, base, "refs/heads/new", patterns([TERM]))
    assert report.outgoing_commits == 3
    assert report.outgoing_paths == 1
    assert report.outgoing_blobs == 1
    assert report.inherited_blobs == 1
    assert {(f.scope, f.kind, f.rule) for f in report.findings} == {
        ("outgoing", "metadata", 1),
        ("outgoing", "metadata", "machine-email"),
        ("outgoing", "message", 1),
        ("outgoing", "path", 1),
        ("outgoing", "blob", 1),
        ("inherited", "blob", 1),
    }
    assert report.exit_code == 1
    assert TERM not in report.model_dump_json()
    assert TERM not in "\n".join(report.render())


def test_public_blob_at_new_path_and_clean_transfer(repo):
    old = repo / "old.txt"
    old.write_text(TERM)
    base = commit(repo, "public")
    old.rename(repo / "new.txt")
    head = commit(repo, "rename public content")
    report = inspect(GitObjects(Subprocess(), repo), head, base, "refs/heads/clean", patterns([TERM]))
    assert (report.outgoing_paths, report.outgoing_blobs, report.inherited_blobs) == (1, 0, 1)
    assert report.exit_code == 0
    assert [(f.scope, f.kind) for f in report.findings] == [("inherited", "blob")]


@pytest.mark.parametrize(("visibility", "ref_sha", "error"), [
    ("public", "a" * 40, None),
    ("private", "a" * 40, "visibility"),
    ("public", "b" * 40, "requested base"),
])
def test_only_public_default_ref_can_supply_the_boundary(visibility, ref_sha, error):
    cmd = FakeCommands()
    prefix = ["gh", "api", "--hostname", "github.com"]
    cmd.on([*prefix, "repos/example/project"], Result(0, json.dumps({
        "full_name": "example/project", "visibility": visibility, "default_branch": "main",
    })))
    cmd.on([*prefix, "repos/example/project/git/ref/heads/main"], Result(0, json.dumps({
        "object": {"type": "commit", "sha": ref_sha},
    })))
    if error:
        with pytest.raises(Refuse, match=error):
            public_base(cmd, "example/project", "a" * 40)
    else:
        public_base(cmd, "example/project", "a" * 40)


def test_terms_reuse_existing_precedence(tmp_path):
    policy = tmp_path / "terms"
    policy.write_text("fromfile # comment\n")
    assert load_terms({"CONFIDENTIAL_TERMS_FILE": str(policy)}) == ["fromfile"]
    assert load_terms({"CONFIDENTIAL_TERMS_FILE": str(policy), "CONFIDENTIAL_TERMS": "fromenvironment"}) == ["fromenvironment"]


def test_destination_ref_is_checked_even_with_no_outgoing_commit(repo):
    base = commit(repo, "public", "--allow-empty")
    report = inspect(GitObjects(Subprocess(), repo), base, base, f"refs/heads/{TERM}", patterns([TERM]))
    assert report.outgoing_commits == 0
    assert report.exit_code == 1
    assert [(f.scope, f.kind) for f in report.findings] == [("outgoing", "ref")]


def test_shallow_repository_cannot_produce_a_transfer_report(repo, tmp_path):
    commit(repo, "older", "--allow-empty")
    head = commit(repo, "newer", "--allow-empty")
    shallow = tmp_path / "shallow"
    git(repo, "clone", "-q", "--depth=1", repo.as_uri(), str(shallow))
    with pytest.raises(Refuse, match="full local repository"):
        inspect(GitObjects(Subprocess(), shallow), head, head, "refs/heads/new", patterns([TERM]))

def test_no_policy_refuses_before_network_or_git(tmp_path, monkeypatch):
    monkeypatch.delenv("CONFIDENTIAL_TERMS", raising=False)
    commands = FakeCommands()
    settings = {"preflight_terms_file": str(tmp_path / "missing")}
    with using(NativeRuntime(fake_providers(commands=commands), settings, None)), pytest.raises(Refuse, match="No confidential terms"):
        raw_transfer(tmp_path, "a" * 40, "b" * 40, "refs/heads/new")
    assert commands.calls == []

def test_push_message_scope_keeps_the_new_merge_and_branch_side(repo):
    script = Path(__file__).resolve().parents[3] / "scripts/check_confidentiality.py"
    commit(repo, "base", "--allow-empty")
    git(repo, "switch", "-qc", "feature")
    before = commit(repo, TERM, "--allow-empty")
    outgoing = commit(repo, TERM + " feature", "--allow-empty")
    git(repo, "switch", "-q", "main")
    inherited = commit(repo, TERM + " public main", "--allow-empty")
    git(repo, "switch", "-q", "feature")
    git(repo, "merge", "--no-ff", "-m", TERM + " merge", "main")
    merge = git(repo, "rev-parse", "HEAD")

    git(repo, "update-ref", "refs/remotes/origin/main", inherited)
    feature = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/feature",
        "GITHUB_SHA": merge,
        "BEFORE": before,
    }

    def checker(context: dict[str, str], revisions: str = f"{before}..HEAD") -> Result:
        return Subprocess().run(
            [sys.executable, str(script), "--require", "--messages", revisions],
            cwd=str(repo),
            timeout=10,
            env=child_environment(drop=("BEFORE",), drop_prefixes=("GITHUB_",), CONFIDENTIAL_TERMS=TERM, **context),
        )

    def found(result: Result) -> set[str]:
        assert result.rc == 1
        assert TERM not in result.err
        return {line.split(":")[0] for line in result.err.splitlines() if line.startswith("commit ")}

    outgoing_commits = {f"commit {outgoing[:12]}", f"commit {merge[:12]}"}
    complete = outgoing_commits | {f"commit {inherited[:12]}"}
    assert found(checker(feature)) == outgoing_commits
    assert found(checker({})) == complete
    assert found(checker(feature | {"GITHUB_EVENT_NAME": "pull_request"})) == complete
    assert found(checker(feature | {"GITHUB_REF": "refs/heads/main"})) == complete
    assert found(checker(feature | {"GITHUB_SHA": outgoing})) == complete
    assert found(checker(feature, f"{before}..{merge}")) == complete
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert checker(feature).rc != 0
