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


def fake_api(sha: str, *, head=None, state="success", description="APPROVE reviewer=rita", merged=True, context="Gate review",
             login="owner", runs=None, suites=None, tree=None, repo=None, fail=None):
    """GitHub, faked. `tree` is the head's tree (default: the merged commit's own tree, i.e. exactly what was merged)."""
    def get(path: str):
        if fail and fail in path:
            raise rc.Refused("API down")
        if path.endswith("/pulls"):
            commit = path.split("/")[-2]
            return [{"number": 7, "merge_commit_sha": commit, "merged_at": "2026-11-21T10:00:00Z" if merged else None, "head": {"sha": head or commit}}]
        if "/actions/runs" in path:
            return {"workflow_runs": [w for w in (suites or {}).get(path.rsplit("=", 1)[-1], [])]}
        if "/check-runs" in path:
            return {"check_runs": runs or []}
        if "/statuses" in path:
            return [{"context": context, "state": state, "description": description, "creator": {"login": login}}]
        if "/git/commits/" in path:
            return {"tree": {"sha": tree if tree is not None else git(repo, "rev-parse", f"{path.rsplit('/', 1)[-1]}^{{tree}}")}}
        if path.endswith("GATE_REVIEW_CREATORS"):
            return {"value": "owner, helper"}
        if path.endswith("releases/latest"):
            return {"tag_name": "v0.0.3"}
        raise AssertionError(path)
    return get


@pytest.fixture(autouse=True)
def creators_env(monkeypatch):
    monkeypatch.setenv("SM_GATE_CREATORS", "owner,helper")
    sys.modules["release_ci"] = rc


def live(monkeypatch, getter):
    """The live GitHub that promote_verdict re-verifies against (release_ci.api)."""
    monkeypatch.setattr(rc, "api", lambda path, run=None: getter(path))
    return getter


def no_pypi(version: str) -> bool:
    return False


def no_cz(*cmd, **kw):  # commitizen unavailable: the patch-bump fallback
    if cmd[0] == "uv":
        raise rc.Refused("no cz")
    return rc.sh(*cmd, **kw)


def test_a_green_merge_with_a_verdict_is_released(repo):
    sha = git(repo, "rev-parse", "HEAD")
    out = rc.gate(repo, sha, fake_api(sha, repo=repo), no_cz, no_pypi)
    assert out == {"version": "0.0.3", "reviewer": "rita", "pr": "7", "head": sha}


@pytest.mark.parametrize("over, why", [
    ({"state": "failure"}, "no `Gate review` success"),
    ({"context": "other"}, "no `Gate review` success"),
    ({"description": "looks fine"}, "not `APPROVE reviewer=NAME`"),
    ({"merged": False}, "no review verdict"),
    ({"description": "APPROVE reviewer=Ada"}, "is an author"),
    ({"login": "stranger"}, "not in GATE_REVIEW_CREATORS"),
    ({"login": "github-actions[bot]"}, "not in GATE_REVIEW_CREATORS"),
    ({"tree": "t" * 40}, "is not what was reviewed"),
    ({"fail": "git/commits"}, "API down"),
    ({"fail": "statuses"}, "API down"),
])
def test_without_a_valid_independent_verdict_nothing_is_released(repo, over, why):
    sha = git(repo, "rev-parse", "HEAD")
    with pytest.raises(rc.Refused, match=why):
        rc.gate(repo, sha, fake_api(sha, repo=repo, **over), no_cz, no_pypi)


def test_a_commit_already_in_a_release_is_skipped(repo):
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "v0.0.3")
    with pytest.raises(rc.Refused, match="already in a release"):
        rc.gate(repo, sha, fake_api(sha, repo=repo), no_cz, no_pypi)


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


def test_the_follower_records_the_ci_verdict_promotes_that_commit_and_restarts_loops(follower, monkeypatch):
    stable, clone, home, cand = follower
    live(monkeypatch, fake_api(cand, repo=clone))
    calls: list = []
    msg = rc.follow(stable, clone, home, fake_run(calls), fake_api(cand, repo=clone))
    assert "is live; restarted 1 loop(s)" in msg
    assert (clone / "promoted").read_text().split() == ["--accept-diff", str(stable)]
    assert git(clone, "rev-parse", "HEAD") == cand
    text = (stable / "verdicts" / cand).read_text()
    assert "recorded-by: ci-follower" in text and "source: ci v0.0.3 " in text and text.endswith("VERDICT: APPROVE\n")
    assert calls[-1][:3] == ("launchctl", "kickstart", "-k") and calls[-1][3].endswith("/q")


def test_the_ci_receipt_passes_the_promote_check_and_a_foreign_recorder_does_not(follower, monkeypatch):
    stable, clone, home, cand = follower
    live(monkeypatch, fake_api(cand, repo=clone))
    import promote_verdict as pv
    rc.follow(stable, clone, home, fake_run([]), fake_api(cand, repo=clone))
    assert pv.check(stable, clone, cand)["reviewer"] == "rita"
    path = stable / "verdicts" / cand
    path.write_text(path.read_text().replace("recorded-by: ci-follower", "recorded-by: someone"))
    with pytest.raises(pv.Refused, match="recorded by someone"):
        pv.check(stable, clone, cand)
    path.write_text(path.read_text().replace("recorded-by: someone", "recorded-by: ci-follower").replace("source: ci ", "source: by hand "))
    with pytest.raises(pv.Refused, match="recorded by ci-follower"):
        pv.check(stable, clone, cand)


def test_the_follower_refuses_without_a_verdict_and_never_goes_back(follower, monkeypatch):
    stable, clone, home, cand = follower
    live(monkeypatch, fake_api(cand, repo=clone))
    with pytest.raises(rc.Refused, match="no `Gate review` success"):
        rc.follow(stable, clone, home, fake_run([]), fake_api(cand, repo=clone, state="pending"))
    assert not (clone / "promoted").exists()
    (stable / "current").unlink()
    (stable / "current").symlink_to(f"releases/{cand[:12]}")
    assert "is live" in rc.follow(stable, clone, home, fake_run([]), fake_api(cand, repo=clone))
    (stable / "current").unlink()
    (stable / "releases" / ("0" * 12)).mkdir()
    (stable / "current").symlink_to("releases/" + "0" * 12)
    with pytest.raises(rc.Refused, match="not a commit"):
        rc.follow(stable, clone, home, fake_run([]), fake_api(cand, repo=clone))


def test_a_refused_promote_is_reported_and_loops_stay(follower, monkeypatch):
    stable, clone, home, cand = follower
    monkeypatch.setenv("PROMOTE_RC", "1")
    live(monkeypatch, fake_api(cand, repo=clone))
    calls: list = []
    with pytest.raises(rc.Refused, match=r"bin/promote refused v0\.0\.3"):
        rc.follow(stable, clone, home, fake_run(calls), fake_api(cand, repo=clone))
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
        rc.follow(stable, clone, home, fake_run([]), fake_api(cand, repo=clone))


def test_a_promote_exit_75_is_passed_through(follower, monkeypatch, capsys):
    stable, clone, home, cand = follower
    monkeypatch.setenv("PROMOTE_RC", "75")
    live(monkeypatch, fake_api(cand, repo=clone))
    calls: list = []
    with pytest.raises(rc.Wait, match="exit 75"):
        rc.follow(stable, clone, home, fake_run(calls), fake_api(cand, repo=clone))
    assert not [c for c in calls if c[0] == "launchctl"]
    monkeypatch.setenv("SM_STABLE", str(stable))
    monkeypatch.setenv("SM_FOLLOW_CLONE", str(clone))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(rc.Path, "home", classmethod(lambda cls: home))
    assert rc.main(["follow"]) == 75
    assert "WAIT" in capsys.readouterr().err


def test_a_local_receipt_alone_is_never_trusted(follower, monkeypatch):
    import promote_verdict as pv
    stable, clone, home, cand = follower
    live(monkeypatch, fake_api(cand, repo=clone))
    rc.follow(stable, clone, home, fake_run([]), fake_api(cand, repo=clone))
    assert pv.check(stable, clone, cand)["reviewer"] == "rita"
    for over, why in [({"tree": "t" * 40}, "is not what was reviewed"), ({"state": "failure"}, "no `Gate review` success"),
                      ({"fail": "statuses"}, "API down"), ({"description": "APPROVE reviewer=other"}, "receipt names rita")]:
        live(monkeypatch, fake_api(cand, repo=clone, **over))
        with pytest.raises(pv.Refused, match=why):
            pv.check(stable, clone, cand)
    (stable / "verdicts" / cand).unlink()
    live(monkeypatch, fake_api(cand, repo=clone, fail="statuses"))
    with pytest.raises(pv.Refused, match="cannot be verified live"):
        pv.record_ci(stable, clone, cand, "rita", "ci v0.0.3 url", "now")
    assert not (stable / "verdicts" / cand).exists()


def test_a_hand_receipt_without_the_ci_marks_is_not_a_ci_receipt(follower, monkeypatch):
    import promote_verdict as pv
    stable, clone, _home, cand = follower
    live(monkeypatch, fake_api(cand, repo=clone, fail="pulls"))  # a normal `record` receipt never asks GitHub
    live_commit = git(clone, "rev-parse", (stable / "current").resolve().name)
    (stable / "verdicts").mkdir()
    (stable / "verdicts" / cand).write_text(f"commit: {cand}\nbase: {live_commit}\nreviewer: rita\nat: now\nVERDICT: APPROVE\n")
    assert pv.check(stable, clone, cand)["reviewer"] == "rita"


def test_the_workflow_check_run_is_a_verdict_only_from_the_gate_review_run(repo):
    sha = git(repo, "rev-parse", "HEAD")
    good = [{"id": 5, "name": "Gate review", "conclusion": "success", "app": {"slug": "github-actions"}, "check_suite": {"id": 77}}]
    run = {"path": ".github/workflows/gate-review.yml", "event": "pull_request_target", "head_sha": sha, "conclusion": "success"}
    assert rc.verdict_for(sha, fake_api(sha, repo=repo, runs=good, suites={"77": [run]}, state="failure"))[2] == "gate-review-workflow"
    bad = [
        (good, {"77": [{**run, "path": ".github/workflows/ci.yml"}]}),  # a forged job named `Gate review` in the PR's own ci.yml
        (good, {"77": [{**run, "event": "pull_request"}]}),
        (good, {"77": [{**run, "head_sha": "f" * 40}]}),
        (good, {"77": [{**run, "conclusion": "failure"}]}),
        (good, {}),  # no Actions run behind the check suite
        ([{**good[0], "check_suite": None}], {"77": [run]}),
        ([{**good[0], "app": {"slug": "evil-app"}}], {"77": [run]}),
        ([{**good[0], "conclusion": "failure"}], {"77": [run]}),
        ([good[0], {**good[0], "id": 9, "conclusion": "failure"}], {"77": [run]}),
    ]
    for runs, suites in bad:
        with pytest.raises(rc.Refused, match="no `Gate review` success"):
            rc.verdict_for(sha, fake_api(sha, repo=repo, runs=runs, suites=suites, state="failure"))
    with pytest.raises(rc.Refused, match=r"does not come from \.github/workflows/gate-review\.yml"):
        rc.verdict_for(sha, fake_api(sha, repo=repo, runs=good, suites={"77": [{**run, "path": ".github/workflows/ci.yml"}]}, state="failure"))


def test_the_allowlist_comes_from_the_repo_variable_and_fails_closed(repo, monkeypatch):
    sha = git(repo, "rev-parse", "HEAD")
    monkeypatch.delenv("SM_GATE_CREATORS")
    assert rc.verdict_for(sha, fake_api(sha, repo=repo))[2] == "rita"  # the variable fake names owner and helper
    monkeypatch.setenv("SM_GATE_CREATORS", "")
    with pytest.raises(rc.Refused, match="not in GATE_REVIEW_CREATORS"):
        rc.verdict_for(sha, fake_api(sha, repo=repo))


def test_every_first_parent_commit_since_the_last_tag_needs_its_own_verdict(repo):
    first, tip = git(repo, "rev-parse", "HEAD"), None
    (repo / "c").write_text("c")
    git(repo, "add", "c")
    git(repo, "commit", "-q", "-m", "fix: c")
    tip = git(repo, "rev-parse", "HEAD")
    base = fake_api(tip, repo=repo)

    def get(path: str):  # the older commit has no merged pull request
        if path.endswith(f"commits/{first}/pulls"):
            return []
        return base(path)

    with pytest.raises(rc.Refused, match=f"{first[:12]} was not merged"):
        rc.gate(repo, tip, get, no_cz, no_pypi)
    assert rc.gate(repo, tip, base, no_cz, no_pypi)["pr"] == "7"


def test_a_version_already_on_pypi_refuses(repo):
    sha = git(repo, "rev-parse", "HEAD")
    with pytest.raises(rc.Refused, match=r"0\.0\.3 already exists on PyPI"):
        rc.gate(repo, sha, fake_api(sha, repo=repo), no_cz, lambda v: True)


def test_pypi_answers(monkeypatch):
    import io
    import urllib.error

    def opener(code):
        def fn(url, timeout):
            if code == 200:
                return io.BytesIO(b"{}")
            raise urllib.error.HTTPError(url, code, "x", {}, None)  # type: ignore[arg-type]
        return fn

    monkeypatch.setattr(rc.urllib.request, "urlopen", opener(200))
    assert rc.pypi_has("1.0.0") is True
    monkeypatch.setattr(rc.urllib.request, "urlopen", opener(404))
    assert rc.pypi_has("1.0.0") is False
    monkeypatch.setattr(rc.urllib.request, "urlopen", opener(503))
    with pytest.raises(rc.Refused, match="503"):
        rc.pypi_has("1.0.0")
    monkeypatch.setattr(rc.urllib.request, "urlopen", lambda url, timeout: (_ for _ in ()).throw(urllib.error.URLError("down")))
    with pytest.raises(rc.Refused, match="cannot be read"):
        rc.pypi_has("1.0.0")
