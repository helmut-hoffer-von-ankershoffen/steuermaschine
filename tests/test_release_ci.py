"""bin/release_ci.py: the CI release gate and the Mini follower. GitHub and promote are faked; git is a real repository."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
spec = importlib.util.spec_from_file_location("release_ci", BIN / "release_ci.py")
assert spec and spec.loader
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)

ENV = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_AUTHOR_NAME": "Ada", "GIT_AUTHOR_EMAIL": "ada@example.org", "GIT_COMMITTER_NAME": "Ada", "GIT_COMMITTER_EMAIL": "ada@example.org"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True, text=True, env=ENV, timeout=30).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    for name in ("a", "b"):
        (r / name).write_text(name)
        git(r, "add", name)
        git(r, "commit", "-q", "-m", f"fix: {name}")
        if name == "a":
            git(r, "tag", "v0.0.2")
    return r


def fake_api(sha: str, *, head="h" * 40, state="success", description="APPROVE reviewer=rita", merged=True, context="Gate review"):
    def get(path: str):
        if path.endswith(f"commits/{sha}/pulls"):
            return [{"number": 7, "merge_commit_sha": sha, "merged_at": "2026-11-21T10:00:00Z" if merged else None, "head": {"sha": head}}]
        if path.startswith(f"repos/{rc.REPO}/commits/{head}/statuses"):
            return [{"context": context, "state": state, "description": description}]
        if path.endswith("releases/latest"):
            return {"tag_name": "v0.0.3"}
        raise AssertionError(path)
    return get


def no_cz(*cmd, **kw):  # commitizen unavailable: the patch-bump fallback
    if cmd[0] == "uv":
        raise rc.Refused("no cz")
    return rc.sh(*cmd, **kw)


def test_a_green_merge_with_a_verdict_is_released(repo):
    sha = git(repo, "rev-parse", "HEAD")
    out = rc.gate(repo, sha, fake_api(sha), no_cz)
    assert out == {"version": "0.0.3", "reviewer": "rita", "pr": "7", "head": "h" * 40}


@pytest.mark.parametrize("over, why", [
    ({"state": "failure"}, "no `Gate review` success"),
    ({"context": "other"}, "no `Gate review` success"),
    ({"description": "looks fine"}, "not `APPROVE reviewer=NAME`"),
    ({"merged": False}, "no review verdict"),
    ({"description": "APPROVE reviewer=Ada"}, "is an author"),
])
def test_without_a_valid_independent_verdict_nothing_is_released(repo, over, why):
    sha = git(repo, "rev-parse", "HEAD")
    with pytest.raises(rc.Refused, match=why):
        rc.gate(repo, sha, fake_api(sha, **over), no_cz)


def test_a_commit_already_in_a_release_is_skipped(repo):
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "v0.0.3")
    with pytest.raises(rc.Refused, match="already in a release"):
        rc.gate(repo, sha, fake_api(sha), no_cz)


def test_a_direct_push_has_no_pull_request(repo):
    sha = git(repo, "rev-parse", "HEAD")
    with pytest.raises(rc.Refused, match="not merged from a pull request"):
        rc.verdict_for(sha, lambda path: [])


def test_the_gate_writes_the_workflow_outputs(repo, tmp_path, monkeypatch):
    sha = git(repo, "rev-parse", "HEAD")
    out = tmp_path / "out"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(rc, "gate", lambda r, s: {"version": "9.9.9", "reviewer": "rita", "pr": "7", "head": "x"})
    assert rc.main(["gate", "--sha", sha]) == 0
    assert "release=true" in out.read_text() and "version=9.9.9" in out.read_text()
    monkeypatch.setattr(rc, "gate", lambda r, s: (_ for _ in ()).throw(rc.Refused("no verdict")))
    assert rc.main(["gate", "--sha", sha]) == 0 and out.read_text().endswith("release=false\n")


def test_record_sets_the_status_on_the_pr_head():
    calls = []

    def run(*cmd, **kw):
        calls.append(cmd)
        return '{"head": {"sha": "%s"}}' % ("c" * 40) if "statuses" not in cmd[2] else ""

    assert rc.record(7, "rita", run) == "c" * 40
    assert calls[-1][2] == f"repos/{rc.REPO}/statuses/{'c' * 40}" and "description=APPROVE reviewer=rita" in calls[-1]


def test_loops_are_the_agents_that_run_the_stable_sm(tmp_path):
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "a.queue.plist").write_text("<string>/Users/x/.local/bin/sm</string>")
    (agents / "b.follow.plist").write_text("<string>/Users/x/Code/steuermaschine-follow/bin/release_ci.py</string>")
    (agents / "c.other.plist").write_text("<string>/usr/bin/true</string>")
    assert rc.loops(tmp_path) == ["a.queue"]


@pytest.fixture
def follower(repo, tmp_path):
    """A stable install whose live release is the first commit, a clone (the repo itself) and a fake promote."""
    live, cand = git(repo, "rev-parse", "HEAD~1"), git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "v0.0.3")
    stable = tmp_path / "stable"
    (stable / "releases" / live[:12]).mkdir(parents=True)
    (stable / "current").symlink_to(f"releases/{live[:12]}")
    (repo / "bin").mkdir()
    (repo / "bin" / "promote").write_text("#!/bin/sh\necho \"$@ $SM_STABLE\" > promoted\nexit ${PROMOTE_RC:-0}\n")
    (repo / "bin" / "promote").chmod(0o755)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore: promote")
    git(repo, "tag", "-f", "v0.0.3")  # the release tag is on the commit holding the fake promote
    cand = git(repo, "rev-parse", "HEAD")
    git(repo, "remote", "add", "origin", str(repo))
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    (home / "Library" / "LaunchAgents" / "q.plist").write_text("<string>/Users/x/.local/bin/sm</string>")
    return stable, repo, home, cand


def fake_run(calls):
    def run(*cmd, **kw):
        if cmd[0] in ("gh", "launchctl"):
            calls.append(cmd)
            return ""
        return rc.sh(*cmd, **kw)
    return run


def test_the_follower_records_the_ci_verdict_promotes_that_commit_and_restarts_loops(follower):
    stable, clone, home, cand = follower
    calls: list = []
    msg = rc.follow(stable, clone, home, fake_run(calls), fake_api(cand))
    assert "is live; restarted 1 loop(s)" in msg
    assert (clone / "promoted").read_text().split() == ["--accept-diff", str(stable)]
    assert git(clone, "rev-parse", "HEAD") == cand
    text = (stable / "verdicts" / cand).read_text()
    assert "recorded-by: ci-follower" in text and "source: ci v0.0.3 " in text and text.endswith("VERDICT: APPROVE\n")
    assert calls[-1][:3] == ("launchctl", "kickstart", "-k") and calls[-1][3].endswith("/q")


def test_the_ci_receipt_passes_the_promote_check_and_a_foreign_recorder_does_not(follower):
    stable, clone, home, cand = follower
    import promote_verdict as pv
    rc.follow(stable, clone, home, fake_run([]), fake_api(cand))
    assert pv.check(stable, clone, cand)["reviewer"] == "rita"
    path = stable / "verdicts" / cand
    path.write_text(path.read_text().replace("recorded-by: ci-follower", "recorded-by: someone"))
    with pytest.raises(pv.Refused, match="recorded by someone"):
        pv.check(stable, clone, cand)
    path.write_text(path.read_text().replace("recorded-by: someone", "recorded-by: ci-follower").replace("source: ci ", "source: by hand "))
    with pytest.raises(pv.Refused, match="recorded by ci-follower"):
        pv.check(stable, clone, cand)


def test_the_follower_refuses_without_a_verdict_and_never_goes_back(follower):
    stable, clone, home, cand = follower
    with pytest.raises(rc.Refused, match="no `Gate review` success"):
        rc.follow(stable, clone, home, fake_run([]), fake_api(cand, state="pending"))
    assert not (clone / "promoted").exists()
    (stable / "current").unlink()
    (stable / "current").symlink_to(f"releases/{cand[:12]}")
    assert "is live" in rc.follow(stable, clone, home, fake_run([]), fake_api(cand))
    (stable / "current").unlink()
    (stable / "releases" / ("0" * 12)).mkdir()
    (stable / "current").symlink_to("releases/" + "0" * 12)
    with pytest.raises(rc.Refused, match="not a commit"):
        rc.follow(stable, clone, home, fake_run([]), fake_api(cand))


def test_a_refused_promote_is_reported_and_loops_stay(follower, monkeypatch):
    stable, clone, home, cand = follower
    monkeypatch.setenv("PROMOTE_RC", "1")
    calls: list = []
    with pytest.raises(rc.Refused, match=r"bin/promote refused v0\.0\.3"):
        rc.follow(stable, clone, home, fake_run(calls), fake_api(cand))
    assert not [c for c in calls if c[0] == "launchctl"]


def test_a_release_that_does_not_descend_from_the_live_commit_is_refused(follower):
    stable, clone, home, cand = follower
    git(clone, "checkout", "-q", "-b", "side", "v0.0.2")
    (clone / "side").write_text("s")
    git(clone, "add", "side")
    git(clone, "commit", "-q", "-m", "fix: side")
    side = git(clone, "rev-parse", "HEAD")
    (stable / "current").unlink()
    (stable / "releases" / side[:12]).mkdir()
    (stable / "current").symlink_to(f"releases/{side[:12]}")
    with pytest.raises(rc.Refused, match="does not descend"):
        rc.follow(stable, clone, home, fake_run([]), fake_api(cand))
