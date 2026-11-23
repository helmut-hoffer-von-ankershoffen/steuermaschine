"""bin/release_ci.py: the CI release gate, the draft resume point, the PyPI match and the Mini follower. GitHub, PyPI and uv
are faked; git is a real repository; the switch runs the real bin/promote_lib.sh."""

from __future__ import annotations

import hashlib
import importlib.util
import json
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
             login="owner", runs=None, suites=None, tree=None, repo=None, fail=None, releases=None):
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
        if "/releases?" in path:
            return releases or []
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


def no_cz(*cmd, **kw):  # commitizen unavailable: the patch-bump fallback
    if cmd[0] == "uv":
        raise rc.Refused("no cz")
    return rc.sh(*cmd, **kw)


@pytest.mark.parametrize(("proposed", "expected"), [
    ("0.2.0", "0.2.0"), ("0.1.0", "0.1.1"), ("0.0.9", "0.1.1"), ("unknown", "0.1.1"),
])
def test_next_version_uses_release_tags_instead_of_the_package_version(repo, proposed, expected):
    git(repo, "tag", "v0.1.0", "HEAD~1")
    pyproject = repo / "pyproject.toml"
    source = ('[project]\nversion = "0.0.2"\n[tool.commitizen]\nversion_provider = "uv"\n'
              'major_version_zero = true\ntag_format = "v$version"\n')
    pyproject.write_text(source)

    def run(*cmd, **kw):
        if cmd[0] == "uv":
            config = Path(cmd[cmd.index("--config") + 1])
            assert json.loads(config.read_bytes()) == {"commitizen": {
                "version_provider": "scm", "major_version_zero": True, "tag_format": "v$version",
            }}
            assert cmd[-3:] == ("bump", "--get-next", "--yes")
            return proposed
        return rc.sh(*cmd, **kw)

    assert rc.next_version(repo, run) == expected
    assert rc.next_version(repo, run) == expected  # the same commit and tags resume at the same version
    assert pyproject.read_text() == source


def test_a_green_merge_with_a_verdict_is_released(repo):
    sha = git(repo, "rev-parse", "HEAD")
    out = rc.gate(repo, sha, fake_api(sha, repo=repo), no_cz)
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
        rc.gate(repo, sha, fake_api(sha, repo=repo, **over), no_cz)


def test_a_commit_already_in_a_release_is_skipped(repo):
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "v0.0.3")
    with pytest.raises(rc.AlreadyReleased, match="already in a release"):
        rc.gate(repo, sha, fake_api(sha, repo=repo), no_cz)


def test_a_direct_push_has_no_pull_request(repo):
    sha = git(repo, "rev-parse", "HEAD")
    with pytest.raises(rc.Refused, match="not merged from a pull request"):
        rc.verdict_for(sha, lambda path: [])


def test_the_gate_writes_the_workflow_outputs(repo, tmp_path, monkeypatch, capsys):
    sha = git(repo, "rev-parse", "HEAD")
    out = tmp_path / "out"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(rc, "gate", lambda r, s: {"version": "9.9.9", "reviewer": "rita", "pr": "7", "head": "x"})
    assert rc.main(["gate", "--sha", sha]) == 0
    assert "release=true" in out.read_text() and "version=9.9.9" in out.read_text()
    monkeypatch.setattr(rc, "gate", lambda r, s: (_ for _ in ()).throw(rc.AlreadyReleased("in v0.0.3")))
    assert rc.main(["gate", "--sha", sha]) == 0 and out.read_text().endswith("release=false\n")
    out.write_text("")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(rc, "gate", lambda r, s: (_ for _ in ()).throw(rc.Refused("no verdict")))
    assert rc.main(["gate", "--sha", sha]) == 1 and out.read_text() == "release=false\n"  # a refusal is red, never a quiet green
    assert "::error title=Not released::no verdict" in capsys.readouterr().out


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


WHEELS = {"steuermaschine": b"core-wheel", "steuermaschine-heinzel": b"plugin-wheel"}
T0 = 1_800_000_000.0  # the follower's clock in these tests


def wheel_name(name: str, version: str = "0.0.3") -> str:
    return f"{name.replace('-', '_')}-{version}-py3-none-any.whl"


def fake_pypi(blobs=None, version="0.0.3"):
    blobs = blobs or WHEELS

    def files(name: str, v: str):
        if v != version or name not in blobs:
            return None
        return [{"filename": wheel_name(name, v), "sha256": hashlib.sha256(blobs[name]).hexdigest(),
                 "url": f"https://files.pythonhosted.org/{wheel_name(name, v)}", "kind": "bdist_wheel"}]
    return files


def release_assets(blobs=None, version="0.0.3", draft=False, target=""):
    blobs = blobs or WHEELS
    assets = [{"name": wheel_name(n, version), "digest": "sha256:" + hashlib.sha256(b).hexdigest()} for n, b in blobs.items()]
    return [{"tag_name": f"v{version}", "draft": draft, "target_commitish": target, "assets": assets}]


@pytest.fixture
def follower(repo, tmp_path):
    """A stable install whose live release is the first commit (built from a commit, the old layout), a clone (the repo
    itself) holding the release's bin/promote_lib.sh and migrate_config.py, and a launchd loop that runs the stable sm."""
    live_commit = git(repo, "rev-parse", "HEAD~1")
    stable = tmp_path / "stable"
    (stable / "releases" / live_commit[:12]).mkdir(parents=True)
    (stable / "current").symlink_to(f"releases/{live_commit[:12]}")
    (repo / "bin").mkdir()
    # the real switch helpers, with no admission step running (the Mini's own steps must not hold up the test)
    (repo / "bin" / "promote_lib.sh").write_text(f". {BIN / 'promote_lib.sh'}\nbusy_steps() {{ :; }}\n")
    (repo / "bin" / "migrate_config.py").write_text("")
    (repo / ".python-version").write_text("3.15\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore: helpers")
    git(repo, "tag", "v0.0.3")
    cand = git(repo, "rev-parse", "HEAD")
    git(repo, "remote", "add", "origin", str(repo))
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    (home / "Library" / "LaunchAgents" / "q.plist").write_text("<string>/Users/x/.local/bin/sm</string>")
    return stable, repo, home, cand


FAKE_SM = """#!/bin/sh
case "$*" in
  --help) echo "Commands, in the order of a working day" ;;
  "self config") echo "plugin: heinzel (loaded)" ;;
  "self plugins") echo "1. heinzel (base)" ;;
  *) exit ${SMOKE_RC:-1} ;;
esac
"""


def fake_run(calls, smoke=FAKE_SM):
    """gh and launchctl are recorded; `uv venv` makes a venv whose sm and python are scripts; `uv pip install` and the
    config migration are recorded and write what they would; git runs."""
    def run(*cmd, **kw):
        if cmd[0] in ("gh", "launchctl"):
            calls.append(cmd)
            return ""
        if cmd[:2] == ("uv", "venv"):
            calls.append(cmd)
            vbin = Path(cmd[-1]) / "bin"
            vbin.mkdir(parents=True)
            (vbin / "steuermaschine").write_text(smoke)
            (vbin / "python").write_text("#!/bin/sh\nexit 0\n")
            for f in vbin.iterdir():
                f.chmod(0o755)
            return ""
        if cmd[:3] == ("uv", "pip", "install"):
            calls.append(cmd)
            return ""
        if cmd[0].endswith("/venv/bin/python"):
            calls.append(cmd)
            Path(cmd[-1]).write_text("[project]\n")
            return ""
        return rc.sh(*cmd, **kw)
    return run


def fetcher(blobs=None):
    def fetch(url: str, path: Path) -> None:
        name = next(n for n in (blobs or WHEELS) if url.endswith(wheel_name(n)))
        path.write_bytes((blobs or WHEELS)[name])
    return fetch


def test_the_follower_installs_the_pypi_wheel_pair_switches_and_restarts_loops(follower, monkeypatch):
    stable, clone, home, cand = follower
    api_get = fake_api(cand, repo=clone, releases=release_assets())
    calls: list = []
    msg = rc.follow(stable, clone, home, fake_run(calls), api_get, fake_pypi(), fetcher(), now=T0)
    assert msg.startswith("v0.0.3 (") and "is the canary" in msg and "builders stay on" in msg
    assert os.readlink(stable / "current") != "releases/v0.0.3" and os.readlink(stable / "canary") == "releases/v0.0.3"
    assert not (stable / "bin" / "sm").exists() and (stable / "bin" / "sm-canary").is_file()  # builders' launcher untouched
    assert not [c for c in calls if c[0] == "launchctl"]  # installed beside the live one; nothing restarted
    assert "is the canary for 100s more" in rc.follow(stable, clone, home, fake_run([]), api_get, fake_pypi(), fetcher(), now=T0 + 200)
    msg = rc.follow(stable, clone, home, fake_run(calls), api_get, fake_pypi(), fetcher(), now=T0 + 300)
    assert "passed its canary tick and is live for all from the PyPI wheels; restarted 1 loop(s)" in msg
    assert not (stable / "canary").exists() and not (stable / "canary.json").exists()
    assert os.readlink(stable / "current") == "releases/v0.0.3" and (stable / "previous").is_symlink()
    assert os.readlink(home / ".local" / "bin" / "sm") == str(stable / "bin" / "sm")
    meta = json.loads((stable / "releases" / "v0.0.3" / "release.json").read_text())
    assert meta["commit"] == cand and meta["reviewer"] == "rita" and set(meta["wheels"]) == {wheel_name(n) for n in WHEELS}
    install = next(c for c in calls if c[:3] == ("uv", "pip", "install"))
    assert [Path(a).name for a in install[-2:]] == [wheel_name("steuermaschine"), wheel_name("steuermaschine-heinzel")]
    assert ("uv", "venv", "--quiet", "--managed-python", "--python", "3.15", str(stable / "releases" / "v0.0.3" / "venv")) in calls
    assert calls[-1][:3] == ("launchctl", "kickstart", "-k") and calls[-1][3].endswith("/q")
    assert (stable / "releases" / "v0.0.3" / "live-at").is_file()
    assert rc.follow(stable, clone, home, fake_run([]), api_get, fake_pypi(), fetcher(), now=T0 + 400) == "v0.0.3 is live"
    import promote_verdict as pv
    assert pv.promoted_base(stable, clone) == cand  # the fallback bin/promote still finds the reviewed base of a wheel release


@pytest.mark.parametrize("over, why", [
    ({"pypi": fake_pypi({"steuermaschine": b"core-wheel"})}, "steuermaschine-heinzel 0.0.3 is not on PyPI"),
    ({"pypi": fake_pypi({**WHEELS, "steuermaschine": b"other"})}, "PyPI sha256 .* differs from the release asset"),
    ({"releases": release_assets({"steuermaschine": b"core-wheel"})}, "is on PyPI but not on the release"),
    ({"fetch": fetcher({**WHEELS, "steuermaschine-heinzel": b"tampered"})}, "downloaded from PyPI does not have the sha256"),
    ({"state": "pending"}, "no `Gate review` success"),
    ({"smoke": FAKE_SM.replace("plugin: heinzel (loaded)", "plugin: None")}, "smoke: sm self config"),
])
def test_the_follower_switches_nothing_unless_pypi_the_release_the_verdict_and_the_smoke_agree(follower, over, why):
    stable, clone, home, cand = follower
    api_get = fake_api(cand, repo=clone, releases=over.get("releases") or release_assets(),
                       **{k: v for k, v in over.items() if k == "state"})
    calls: list = []
    with pytest.raises(rc.Refused, match=why):
        rc.follow(stable, clone, home, fake_run(calls, over.get("smoke", FAKE_SM)), api_get, over.get("pypi") or fake_pypi(),
                  over.get("fetch") or fetcher())
    assert os.readlink(stable / "current") != "releases/v0.0.3"
    assert not [c for c in calls if c[0] == "launchctl"]


def test_the_follower_never_goes_back(follower):
    stable, clone, home, cand = follower
    git(clone, "checkout", "-q", "-b", "side", "v0.0.2")
    (clone / "side").write_text("s")
    git(clone, "add", "side")
    git(clone, "commit", "-q", "-m", "fix: side")
    side = git(clone, "rev-parse", "HEAD")
    (stable / "current").unlink()
    (stable / "releases" / side[:12]).mkdir()
    (stable / "current").symlink_to(f"releases/{side[:12]}")
    api_get = fake_api(cand, repo=clone, releases=release_assets())
    with pytest.raises(rc.Refused, match="does not descend"):
        rc.follow(stable, clone, home, fake_run([]), api_get, fake_pypi(), fetcher())
    newer = stable / "releases" / "v0.0.4"
    newer.mkdir()
    (newer / "release.json").write_text(json.dumps({"version": "0.0.4", "commit": side}))
    (stable / "current").unlink()
    (stable / "current").symlink_to("releases/v0.0.4")
    with pytest.raises(rc.Refused, match=r"not newer than the live v0\.0\.4"):
        rc.follow(stable, clone, home, fake_run([]), api_get, fake_pypi(), fetcher())


def test_a_switch_held_by_a_running_step_waits_and_the_next_run_switches(follower, monkeypatch, capsys):
    stable, clone, home, cand = follower
    lib = clone / "bin" / "promote_lib.sh"
    lib.write_text(lib.read_text() + "wait_idle() { return 1; }\n")  # a write step runs for longer than the wait
    git(clone, "commit", "-q", "-am", "chore: busy")
    git(clone, "tag", "-f", "v0.0.3")  # the follower runs the helpers of the release it installs
    cand = git(clone, "rev-parse", "HEAD")
    api_get = fake_api(cand, repo=clone, releases=release_assets())
    calls: list = []
    assert "is the canary" in rc.follow(stable, clone, home, fake_run(calls), api_get, fake_pypi(), fetcher(), now=T0)
    with pytest.raises(rc.Wait, match="the next run swaps"):
        rc.follow(stable, clone, home, fake_run(calls), api_get, fake_pypi(), fetcher(), now=T0 + 300)
    assert not [c for c in calls if c[0] == "launchctl"] and (stable / "current").resolve().name != "v0.0.3"
    lib.write_text(lib.read_text().replace("wait_idle() { return 1; }\n", ""))
    git(clone, "commit", "-q", "-am", "chore: idle")
    git(clone, "tag", "-f", "v0.0.3")
    api_get = fake_api(git(clone, "rev-parse", "HEAD"), repo=clone, releases=release_assets())
    calls.clear()
    assert "is live" in rc.follow(stable, clone, home, fake_run(calls), api_get, fake_pypi(), fetcher(), now=T0 + 600)
    assert not [c for c in calls if c[:2] == ("uv", "venv")]  # the installed, smoked release is switched, not rebuilt


def test_the_ci_receipt_passes_the_promote_check_and_a_foreign_recorder_does_not(follower, monkeypatch):
    stable, clone, _home, cand = follower
    live(monkeypatch, fake_api(cand, repo=clone))
    import promote_verdict as pv
    pv.record_ci(stable, clone, cand, "rita", "ci v0.0.3 url", "now")
    assert pv.check(stable, clone, cand)["reviewer"] == "rita"
    path = stable / "verdicts" / cand
    path.write_text(path.read_text().replace("recorded-by: ci-follower", "recorded-by: someone"))
    with pytest.raises(pv.Refused, match="recorded by someone"):
        pv.check(stable, clone, cand)
    path.write_text(path.read_text().replace("recorded-by: someone", "recorded-by: ci-follower").replace("source: ci ", "source: by hand "))
    with pytest.raises(pv.Refused, match="recorded by ci-follower"):
        pv.check(stable, clone, cand)


def test_a_local_receipt_alone_is_never_trusted(follower, monkeypatch):
    import promote_verdict as pv
    stable, clone, _home, cand = follower
    live(monkeypatch, fake_api(cand, repo=clone))
    pv.record_ci(stable, clone, cand, "rita", "ci v0.0.3 url", "now")
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


def test_only_the_released_merge_needs_its_verdict(repo):
    first = git(repo, "rev-parse", "HEAD")
    (repo / "c").write_text("c")
    git(repo, "add", "c")
    git(repo, "commit", "-q", "-m", "fix: c")
    tip = git(repo, "rev-parse", "HEAD")
    base = fake_api(tip, repo=repo)

    def get(path: str):  # the older merge has no verdict (it predates the Gate review check): it is released with the tip
        if f"commits/{first}/" in path:
            raise AssertionError("an earlier merge was asked for its verdict")
        return base(path)

    assert rc.gate(repo, tip, get, no_cz)["pr"] == "7"


def built(dist: Path, version: str = "0.0.3", names=("steuermaschine", "steuermaschine-heinzel")) -> Path:
    dist.mkdir(exist_ok=True)
    for n in names:
        (dist / wheel_name(n, version)).write_bytes(WHEELS[n])
    return dist


def test_the_draft_keeps_the_built_files_before_pypi(tmp_path):
    dist = built(tmp_path / "dist")
    calls: list = []
    out = rc.draft("0.0.3", "a" * 40, dist, fake_api("x"), fake_run(calls), lambda n, v: None)
    assert out == f"draft v0.0.3 of {'a' * 12} with 2 file(s)"
    cmd = calls[-1]
    assert cmd[:4] == ("gh", "release", "create", "v0.0.3") and "--draft" in cmd and cmd[cmd.index("--target") + 1] == "a" * 40
    assert hashlib.sha256(WHEELS["steuermaschine"]).hexdigest() in cmd[cmd.index("--notes") + 1]


@pytest.mark.parametrize("releases, pypi, names, why", [
    (release_assets(draft=True, target="b" * 40), None, None, "is a draft of bbbbbbbbbbbb, not of aaaaaaaaaaaa"),
    (release_assets(draft=False, target="a" * 40), None, None, "is published already"),
    ([], fake_pypi(), None, "steuermaschine 0.0.3 is on PyPI but there is no release v0.0.3"),
    ([], None, ("steuermaschine",), "dist has no wheel of steuermaschine-heinzel 0.0.3"),
])
def test_the_draft_refuses_rather_than_overwrite(tmp_path, releases, pypi, names, why):
    dist = built(tmp_path / "dist", names=names or ("steuermaschine", "steuermaschine-heinzel"))
    calls: list = []
    with pytest.raises(rc.Refused, match=why):
        rc.draft("0.0.3", "a" * 40, dist, fake_api("x", releases=releases), fake_run(calls), pypi or (lambda n, v: None))
    assert not calls


def test_a_rerun_of_the_same_merge_resumes_with_the_draft_files(tmp_path):
    dist = built(tmp_path / "dist")
    (dist / "stale.whl").write_text("rebuilt, not the same bytes")
    calls: list = []
    out = rc.draft("0.0.3", "a" * 40, dist, fake_api("x", releases=release_assets(draft=True, target="a" * 40)), fake_run(calls), fake_pypi())
    assert out.startswith("resume v0.0.3")
    assert calls == [("gh", "release", "download", "v0.0.3", "--repo", rc.REPO, "--dir", str(dist), "--clobber")]
    assert not list(dist.glob("*"))  # the rebuilt files are gone; the draft's own are downloaded


def test_the_acceptance_match_compares_pypi_with_the_release():
    get = fake_api("x", releases=release_assets())
    assert [w["filename"] for w in rc.matching_wheels("0.0.3", get, fake_run([]), fake_pypi())] == [wheel_name(n) for n in WHEELS]
    with pytest.raises(rc.Refused, match="differs from the release asset"):
        rc.matching_wheels("0.0.3", get, fake_run([]), fake_pypi({**WHEELS, "steuermaschine-heinzel": b"x"}))


def test_pypi_answers(monkeypatch):
    import io
    import urllib.error

    def opener(code):
        def fn(url, timeout):
            if code == 200:
                return io.BytesIO(json.dumps({"urls": [{"filename": "f.whl", "digests": {"sha256": "ab"}, "url": "u", "packagetype": "bdist_wheel"}]}).encode())
            raise urllib.error.HTTPError(url, code, "x", {}, None)  # type: ignore[arg-type]
        return fn

    monkeypatch.setattr(rc.urllib.request, "urlopen", opener(200))
    assert rc.pypi_has("1.0.0") is True
    assert rc.pypi_files("steuermaschine-heinzel", "1.0.0") == [{"filename": "f.whl", "sha256": "ab", "url": "u", "kind": "bdist_wheel"}]
    monkeypatch.setattr(rc.urllib.request, "urlopen", opener(404))
    assert rc.pypi_has("1.0.0") is False
    monkeypatch.setattr(rc.urllib.request, "urlopen", opener(503))
    with pytest.raises(rc.Refused, match="503"):
        rc.pypi_has("1.0.0")
    monkeypatch.setattr(rc.urllib.request, "urlopen", lambda url, timeout: (_ for _ in ()).throw(urllib.error.URLError("down")))
    with pytest.raises(rc.Refused, match="cannot be read"):
        rc.pypi_has("1.0.0")


def test_the_follower_downloads_only_pypi_files(tmp_path):
    with pytest.raises(rc.Refused, match="not a PyPI file URL"):
        rc.download("https://example.org/x.whl", tmp_path / "x.whl")


UVX = 'uvx --refresh --python 3.15 --from "steuermaschine==$VERSION" --with "steuermaschine-heinzel==$VERSION" sm -h'


def test_the_workflow_releases_both_packages_in_lockstep_and_marks_latest_only_after_acceptance():
    flow = (BIN.parent / ".github" / "workflows" / "release.yml").read_text()
    order = [flow.index(s) for s in ("release_ci.py gate", "release_ci.py draft", "packages-dir: out/core/", "packages-dir: out/plugin/",
                                     "--draft=false --latest=false", "release_ci.py match", UVX, "--latest\n")]
    assert order == sorted(order)
    assert flow.count("skip-existing: true") == 2 and "workflow_dispatch" not in flow
    assert "uv version --package steuermaschine-heinzel" in flow and "Requires-Dist: steuermaschine==${VERSION}" in flow
    assert "timings.json" in flow


# ----------------------------------------------------------------------------- the gate: a squash of a branch behind main
def behind_branch_squash(repo: Path, extra: bool = False) -> tuple[str, str]:
    """PR head c branched from v0.0.2 (main moved on to b since); the squash on main is b plus c (plus d when EXTRA)."""
    git(repo, "checkout", "-q", "-b", "pr", "v0.0.2")
    (repo / "c").write_text("c")
    git(repo, "add", "c")
    git(repo, "commit", "-q", "-m", "fix: c")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    (repo / "c").write_text("c")
    if extra:
        (repo / "d").write_text("not reviewed")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fix: c (#7)")
    return git(repo, "rev-parse", "HEAD"), head


@pytest.mark.parametrize("extra", [False, True])
def test_a_squash_of_a_branch_behind_main_is_refused_whatever_it_adds(repo, extra):
    """Only the exact tree of the reviewed head is a release: the ruleset requires up-to-date branches, so the merged tree is that head."""
    sha, head = behind_branch_squash(repo, extra=extra)
    assert git(repo, "rev-parse", f"{sha}^{{tree}}") != git(repo, "rev-parse", f"{head}^{{tree}}")
    with pytest.raises(rc.Refused, match=r"is not what was reviewed: its tree .* differs from the head"):
        rc.verify(repo, sha, fake_api(sha, head=head, repo=repo), rc.sh)


# ----------------------------------------------------------------------------- canary, Core window, rollback
def to_canary(follower, calls, smoke=FAKE_SM):
    stable, clone, home, cand = follower
    api_get = fake_api(cand, repo=clone, releases=release_assets())
    assert "is the canary" in rc.follow(stable, clone, home, fake_run(calls, smoke), api_get, fake_pypi(), fetcher(), now=T0)
    return api_get


def test_an_exit_70_of_the_canary_rejects_it_and_builders_never_leave_the_live_release(follower):
    stable, clone, home, _cand = follower
    calls: list = []
    api_get = to_canary(follower, calls)
    before = os.readlink(stable / "current")
    (stable / "exits.log").write_text(f"{int(T0) + 10} v0.0.3 70\n{int(T0) + 20} {before.split('/')[-1]} 70\n")
    with pytest.raises(rc.Alarm, match="REJECTED by its canary tick: 1 exit 70 of the canary"):
        rc.follow(stable, clone, home, fake_run(calls), api_get, fake_pypi(), fetcher(), now=T0 + 300)
    assert os.readlink(stable / "current") == before and not (stable / "canary").exists()
    assert not (stable / "bin" / "sm").exists()  # the rejected release never wrote the builders' launcher
    assert ("gh", "release", "edit", "v0.0.3", "--repo", rc.REPO, "--prerelease", "--latest=false") in calls
    assert "was rejected here" in rc.follow(stable, clone, home, fake_run([]), api_get, fake_pypi(), fetcher(), now=T0 + 600)
    assert rc.main.__module__ and not [c for c in calls if c[0] == "launchctl"]


def test_a_red_probe_of_the_canary_rejects_it(follower):
    stable, clone, home, _cand = follower
    calls: list = []
    api_get = to_canary(follower, calls)
    bad = stable / "releases" / "v0.0.3" / "venv" / "bin" / "steuermaschine"
    bad.write_text(FAKE_SM.replace("plugin: heinzel (loaded)", "plugin: None"))
    with pytest.raises(rc.Alarm, match="REJECTED by its canary tick: smoke: sm self config"):
        rc.follow(stable, clone, home, fake_run(calls), api_get, fake_pypi(), fetcher(), now=T0 + 300)


def test_no_swap_between_a_core_cut_and_its_live_ok(follower, monkeypatch):
    stable, clone, home, _cand = follower
    api_get = to_canary(follower, [])
    window = home / ".cache" / "sm-core-release-window"
    window.parent.mkdir(parents=True)
    window.write_text(json.dumps({"version": "0.9.56", "opened": rc.now_stamp(T0)}))
    held = rc.follow(stable, clone, home, fake_run([]), api_get, fake_pypi(), fetcher(), now=T0 + 300)
    assert "the swap waits: Core v0.9.56 is between its cut" in held and os.readlink(stable / "canary") == "releases/v0.0.3"
    window.write_text(json.dumps({"version": "0.9.56", "opened": rc.now_stamp(T0 - rc.WINDOW_MAX_SECONDS - 1)}))  # stale: holds nothing
    assert "is live for all" in rc.follow(stable, clone, home, fake_run([]), api_get, fake_pypi(), fetcher(), now=T0 + 600)


def test_a_canary_that_is_no_longer_latest_is_dropped_not_swapped(follower):
    """Gate review of PR 63, finding 1: Latest is read again on every tick; a demoted, replaced or rejected release never swaps."""
    stable, clone, home, _cand = follower
    api_get = to_canary(follower, [])
    before = os.readlink(stable / "current")
    moved = lambda path: {"tag_name": "v0.0.4"} if path.endswith("releases/latest") else api_get(path)  # noqa: E731
    msg = rc.follow(stable, clone, home, fake_run([]), moved, fake_pypi(), fetcher(), now=T0 + 300)
    assert "is no longer Latest (v0.0.4)" in msg and not (stable / "canary").exists() and os.readlink(stable / "current") == before


def test_a_canary_whose_latest_cannot_be_read_waits_and_a_rejected_one_is_dropped(follower):
    stable, clone, home, _cand = follower
    api_get = to_canary(follower, [])

    def broken(path):
        if path.endswith("releases/latest"):
            raise rc.Refused("gh: HTTP 502")
        return api_get(path)

    assert "Latest cannot be read" in rc.follow(stable, clone, home, fake_run([]), broken, fake_pypi(), fetcher(), now=T0 + 300)
    assert os.readlink(stable / "canary") == "releases/v0.0.3"  # kept, no swap
    (stable / "rejected").mkdir()
    (stable / "rejected" / "v0.0.3").write_text("by hand\n")
    assert "was rejected here" in rc.follow(stable, clone, home, fake_run([]), api_get, fake_pypi(), fetcher(), now=T0 + 400)
    assert not (stable / "canary").exists()


def test_a_tool_stall_after_the_swap_rolls_back_by_symlink(follower):
    stable, clone, home, _cand = follower
    (clone / "bin" / "rollback").write_text((BIN / "rollback").read_text())
    calls: list = []
    api_get = to_canary(follower, calls)
    before = os.readlink(stable / "current")
    assert "is live for all" in rc.follow(stable, clone, home, fake_run(calls), api_get, fake_pypi(), fetcher(), now=T0 + 300)
    (stable / "exits.log").write_text(f"{int(T0) + 400} v0.0.3 70\n")
    assert rc.follow(stable, clone, home, fake_run([]), api_get, fake_pypi(), fetcher(), now=T0 + 500) == "v0.0.3 is live"  # one is no stall
    with (stable / "exits.log").open("a") as fh:
        fh.write(f"{int(T0) + 700} v0.0.3 70\n")
    with pytest.raises(rc.Alarm, match=r"ROLLED BACK to .*tool-stall alarm, 2 exit 70"):
        rc.follow(stable, clone, home, fake_run(calls), api_get, fake_pypi(), fetcher(), now=T0 + 800)
    assert os.readlink(stable / "current") == before and (stable / "rejected" / "v0.0.3").is_file()
    assert "was rejected here" in rc.follow(stable, clone, home, fake_run([]), api_get, fake_pypi(), fetcher(), now=T0 + 900)


def test_the_stall_rule_counts_two_tool_errors_within_ten_minutes():
    assert not rc.stalled([]) and not rc.stalled([0.0]) and not rc.stalled([0.0, 601.0])
    assert rc.stalled([0.0, 600.0]) and rc.stalled([0.0, 1000.0, 1300.0])
