"""Clean file trees do not make committed identities or message bodies public."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from steuermaschine.config import Project, child_environment
from steuermaschine.defaults import Subprocess
from steuermaschine.engine import Refuse
from steuermaschine.native import NativeRuntime, using
from steuermaschine.testing import fake_providers
from steuermaschine_heinzel.native import preflight
from steuermaschine_heinzel.native.ship_lib import deps, push_gate, ship
from steuermaschine_heinzel.native.ship_lib.public_identity import check

TERM = "fictionalmarker"


def git(repo, *args, env=None):
    result = Subprocess().run(["git", *args], cwd=str(repo), env=env, timeout=10)
    assert result.rc == 0, "fixture Git command failed"
    return result.out.strip()


def commit(repo, message="fix: clean content", **identity):
    git(repo, "commit", "--allow-empty", "-qm", message, env=child_environment(**identity))
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for name in ("CONFIDENTIAL_TERMS", "CONFIDENTIAL_TERMS_FILE", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-qb", "main")
    git(repo, "config", "user.name", "Public Author")
    git(repo, "config", "user.email", "123+public@users.noreply.github.com")
    git(repo, "config", "commit.gpgsign", "false")
    base = commit(repo, f"fix: inherited {TERM}")
    git(repo, "update-ref", "refs/remotes/origin/main", base)
    git(repo, "switch", "-qc", "fix/proposed")
    terms = tmp_path / "terms"
    terms.write_text(TERM)
    rt = NativeRuntime(fake_providers(commands=Subprocess()), {"preflight_terms_file": str(terms)}, Project(name="test", plugin="heinzel", root=repo))
    return repo, rt


@pytest.mark.parametrize("field", ["GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "message"])
def test_push_refuses_private_metadata_before_ci_or_network_mutation(prepared, monkeypatch, field):
    repo, rt = prepared
    kwargs = {} if field == "message" else {field: f"{TERM}@example.invalid" if field.endswith("EMAIL") else TERM}
    commit(repo, f"fix: clean subject\n\nPrivate body {TERM}" if field == "message" else "fix: clean subject", **kwargs)
    assert git(repo, "diff", "origin/main", "HEAD") == ""  # the failed-public-PR case: identical clean tree
    monkeypatch.setattr(preflight, "meta_of", lambda *a, **k: SimpleNamespace(problems=[], commits=0, branch="fix/proposed"))
    monkeypatch.setattr(push_gate, "check", lambda *a: pytest.fail("CI gate must not be reached"))
    with using(rt):
        result = ship.push("24", str(repo))
    assert result.returncode == 1
    assert "commit 1" in result.stdout and ("message" if field == "message" else "metadata") in result.stdout
    assert TERM not in result.stdout + result.stderr


def test_local_proposed_range_includes_merge_sides_and_ignores_replacements(prepared, monkeypatch):
    repo, rt = prepared
    bad = commit(repo, f"fix: clean subject\n\nTrailer: {TERM}")
    git(repo, "switch", "-qc", "side")
    commit(repo, GIT_COMMITTER_EMAIL="public@host.local")
    git(repo, "switch", "-q", "fix/proposed")
    commit(repo)
    git(repo, "merge", "--no-ff", "--no-edit", "side")
    git(repo, "replace", bad, git(repo, "rev-parse", "origin/main"))
    git(repo, "switch", "-q", "main")  # HEAD is clean, but this is not the proposed branch
    monkeypatch.setattr(preflight, "meta_of", lambda *a, **k: SimpleNamespace(problems=[], commits=0, branch="fix/proposed"))
    with using(rt):
        findings, count = deps.commit_meta(repo, "24")
    assert count == 4
    assert any("message" in f for f in findings) and any("machine-email" in f for f in findings)
    assert TERM not in str(findings)


def test_clean_metadata_reaches_existing_push_gate(prepared, monkeypatch):
    repo, rt = prepared
    commit(repo)
    monkeypatch.setattr(preflight, "meta_of", lambda *a, **k: SimpleNamespace(problems=[], commits=1, branch="fix/proposed"))
    monkeypatch.setattr(push_gate, "check", lambda pr: (75, ["WAIT: existing gate"]))
    with using(rt):
        result = ship.push("fix/proposed", str(repo))
    assert result.returncode == 75 and "WAIT: existing gate" in result.stdout
    assert check(rt, repo, "fix/proposed") == ([], 1)


@pytest.mark.parametrize("failure", ["terms", "missing-ref", "missing-base", "shallow", "moved"])
def test_unknown_range_or_policy_refuses(prepared, monkeypatch, failure):
    repo, rt = prepared
    head = commit(repo)
    ref = "fix/proposed"
    if failure == "terms":
        Path(rt.settings["preflight_terms_file"]).write_text("")
    elif failure == "missing-ref":
        ref = "fix/missing"
    elif failure == "missing-base":
        git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    elif failure == "shallow":
        (repo / ".git" / "shallow").write_text(head + "\n")
    else:
        from steuermaschine_heinzel.native.ship_lib import public_identity
        original = public_identity.commit_findings

        def move(*args):
            commit(repo)
            return original(*args)

        monkeypatch.setattr(public_identity, "commit_findings", move)
    with pytest.raises(Refuse) as error:
        check(rt, repo, ref)
    assert TERM not in str(error.value) and str(repo) not in str(error.value)
