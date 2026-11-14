import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest
from steuermaschine_heinzel import state as statedir

MAIN_COMMIT = "d" * 40
TAG_COMMIT = "c" * 40
NOT_FOUND = json.dumps({"message": "Not Found", "status": "404"})  # what `gh api` prints for a missing ref
WHEEL_SHA256 = "a" * 64

PLUGINS = "helmut-hoffer-von-ankershoffen/heinzel-plugins"


@pytest.fixture
def lib(tool):
    sys.path.insert(0, str(tool.__globals__["TOOLS"]))
    from steuermaschine_heinzel.native.queue_lib import hzlib

    return hzlib


class World:
    """A fake GitHub: what exists, and what dispatching does. Each dispatch makes the next stage appear."""

    def __init__(self):
        self.tags, self.pypi, self.app, self.calls = set(), set(), False, []
        self.pr = None
        self.ci = [{"displayTitle": "Legacy full CI", "attempt": 1, "databaseId": 1, "status": "completed", "conclusion": "success", "createdAt": "2020-01-01T00:00:00Z", "headSha": MAIN_COMMIT}]
        self.runs = {}
        self.profile = json.dumps({"format": 2, "core_version": "0.9.12", "artifacts": [{
            "name": "heinzel", "version": "0.9.12", "filename": "heinzel-0.9.12-py3-none-any.whl",
            "sha256": WHEEL_SHA256,
            "url": f"https://github.com/{PLUGINS}/releases/download/app-v0.9.12/heinzel-0.9.12-py3-none-any.whl",
        }]}).encode()
        self.profile_sha256 = hashlib.sha256(self.profile).hexdigest()

    def release(self, app=False):
        assets = [{"name": "heinzel-0.9.12-py3-none-any.whl", "digest": "sha256:" + WHEEL_SHA256}]
        if app:
            assets.append({"name": "standard.json", "digest": "sha256:" + self.profile_sha256})
        return {"tag_name": "app-v0.9.12" if app else "v0.9.12", "draft": False, "prerelease": False, "assets": assets}

    def gh(self, *a):
        self.calls.append(a)
        if a[0] == "api" and "git/ref/tags" in a[1]:
            tag = a[1].split("/")[-1]
            return (0, json.dumps({"ref": f"refs/tags/{tag}", "object": {"type": "commit", "sha": TAG_COMMIT}})) if tag in self.tags else (1, NOT_FOUND)
        if a[0] == "api" and "/releases/tags/" in a[1]:
            return (0, "false") if a[1].split("/")[-1] in self.tags else (1, "")
        if a[0] == "api" and a[1].endswith("releases/latest"):
            return 0, "v0.9.11\n"
        if a[0] == "api" and a[1].endswith("commits/main"):
            return 0, MAIN_COMMIT
        if a[0] == "api" and a[1].endswith("commits/v0.9.12"):
            return 0, TAG_COMMIT
        if a[0] == "api" and "/contents/.github/workflows/pypi.yml?ref=" in a[1]:
            return 0, (Path(__file__).parent / "fixtures/pypi-legacy.yml").read_text()
        if a[0] == "api" and "/contents/.github/workflows/acceptance.yml?ref=" in a[1]:
            source = (Path(__file__).parent / "fixtures/acceptance-snapshot.yml").read_bytes()
            return 0, base64.b64encode(source).decode()
        if a[:2] == ("release", "view"):
            return 0, "https://x/heinzel-0.9.12-py3-none-any.whl\n"
        if a[:2] == ("workflow", "run"):
            wf = a[2]
            self.runs[wf] = {"databaseId": 700 + len(self.runs), "status": "completed", "conclusion": "success", "createdAt": "2999-01-01T00:00:00Z", "headSha": "x"}
            if wf == "acceptance.yml":
                inputs = dict(value.split("=", 1) for flag, value in zip(a, a[1:]) if flag == "-f")
                self.runs[wf].update(attempt=1, headSha=MAIN_COMMIT if a[a.index("--ref") + 1] == "main" else TAG_COMMIT,
                    displayTitle=f"Acceptance {inputs['expected_version']} {inputs['release_attempt']}")
            if wf == "pypi.yml":
                self.pypi.add(a[a.index("--ref") + 1].lstrip("v"))
            if wf == "compose.yml":
                self.app = True
            return 0, ""
        if a[:2] == ("run", "download"):
            row = self.runs["acceptance.yml"]
            assert str(row["databaseId"]) == a[2]
            for os_name in ("Linux", "macOS"):
                path = Path(a[-1]) / f"acceptance-result-{os_name}" / "result.json"
                path.parent.mkdir()
                result = {"verdict": "ACCEPTED", "public_path_proof": True,
                    "ci": {"repository": "helmut-hoffer-von-ankershoffen/heinzel", "sha": row["headSha"],
                        "run_id": str(row["databaseId"]), "run_attempt": "1", "runner_os": os_name,
                        "event": "workflow_dispatch"},
                    "published": {"expect_version": "0.9.12", "expect_app_version": "0.9.12",
                        "core_wheel_sha256": WHEEL_SHA256, "profile_sha256": self.profile_sha256}}
                path.write_text(json.dumps(result))
            return 0, ""
        if a[:2] == ("run", "view"):
            return 0, 'Acceptance (ubuntu-24.04)\tstep\t2026Z {"verdict": "ACCEPTED", "why": ""}\nAcceptance (macos-latest)\tstep\t2026Z {"verdict": "ACCEPTED_EXCEPT_UNPROVABLE"}\n'
        return 0, ""

    def ghj(self, *a, default=None):
        if a[0] == "api" and "/git/ref/tags/" in a[1]:
            code, content = self.gh(*a)
            return json.loads(content) if code == 0 else default
        if a[0] == "api" and "/releases/tags/" in a[1]:
            tag = a[1].split("/")[-1]
            app = tag.startswith("app-v")
            return self.release(app=app) if (self.app if app else tag in self.tags) else default
        if a[:2] == ("run", "view"):
            return {"status": "completed", "conclusion": "success", "jobs": [
                {"name": "Acceptance (ubuntu-24.04)", "conclusion": "success"},
                {"name": "Acceptance (macos-latest)", "conclusion": "success"}]}
        if a[0] == "api" and "/actions/runs/" in a[1] and a[1].rsplit("/", 1)[-1].isdigit():
            run_id = int(a[1].rsplit("/", 1)[-1])
            row = next((run for run in self.runs.values() if run["databaseId"] == run_id), None)
            return None if row is None else {"id": run_id, "path": ".github/workflows/acceptance.yml",
                "head_sha": row["headSha"], "display_title": row.get("displayTitle", ""),
                "run_attempt": row["attempt"], "created_at": row["createdAt"], "status": row["status"], "conclusion": row.get("conclusion")}
        if a[:2] == ("run", "list"):
            wf = a[a.index("--workflow") + 1]
            return self.ci if wf == "ci.yml" else ([self.runs[wf]] if wf in self.runs else [])
        if a[:2] == ("pr", "list"):
            return [self.pr] if self.pr else []
        if a[:2] == ("release", "list"):
            return [{"tagName": "app-v0.9.12", "isLatest": True}] if self.app and PLUGINS in a else []
        return default


@pytest.fixture
def rel(tool, lib, home, monkeypatch):
    r = tool("hz-release")
    world = World()
    monkeypatch.setattr(r.release_snapshot, "fetch_profile", lambda url: world.profile)
    monkeypatch.setattr(lib, "gh", world.gh)
    monkeypatch.setattr(lib, "ghj", world.ghj)
    monkeypatch.setattr(lib, "sleep", lambda s: None)
    monkeypatch.setattr(r, "pypi_has", lambda name, v: v in world.pypi)
    train = tool("hz-merge-train")

    def merge(repo, pr, ignore_freeze=False, pre_merge_guard=None):
        if pre_merge_guard is not None and not pre_merge_guard():
            return False, "candidate evidence refused"
        world.pr["state"] = "MERGED"
        world.tags.add("v0.9.12")
        return True, "ok"

    monkeypatch.setattr(r.release_candidate_policy, "verify_pr", lambda number, waiver=None: {"run_id": 900, "head": MAIN_COMMIT, "job_count": 64})
    monkeypatch.setattr(train, "train", merge)
    monkeypatch.setattr(lib, "load", lambda name: train)
    monkeypatch.setattr(r, "open_release_pr", lambda tag, bump, intro: setattr(world, "pr", {"number": 5, "state": "OPEN"}) or world.calls.append(("open_pr", bump, intro)))
    monkeypatch.setattr(lib, "failing_tests", lambda *a: [])
    r.world = world
    return r


def test_next_version_checks_the_increment(rel):
    assert rel.next_version("0.9.12") == ("0.9.12", "patch")
    assert rel.next_version("0.10.0") == ("0.10.0", "minor")
    with pytest.raises(rel.Stop):
        rel.next_version("0.9.14")


def test_a_full_release_runs_every_step_and_unfreezes(rel, home, capsys):
    assert rel.release_core("patch", "Plainer words.", 60) == 0
    out = capsys.readouterr().out
    for word in ("freeze", "main tip green", "release PR merged", "tag", "PyPI heinzel 0.9.12", "compose app-v0.9.12", "acceptance", "unfreeze", "ACCEPTED 0.9.12"):
        assert word in out
    assert not (statedir.root() / "merge-freeze").exists()
    assert ("open_pr", "patch", "Plainer words.") in rel.world.calls


def test_rerun_after_a_stop_continues_without_redoing_steps(rel, home, capsys):
    w = rel.world
    w.tags.add("v0.9.12")
    w.pr = {"number": 5, "state": "MERGED"}
    w.pypi.add("0.9.12")
    w.ci[0]["headSha"] = "old"  # a stale tip must not matter once the tag exists
    rel.release_core("0.9.12", "", 60)
    assert not [c for c in w.calls if c[0] == "open_pr"]
    assert not [c for c in w.calls if c[:3] == ("workflow", "run", "pypi.yml")]
    assert "(already)" in capsys.readouterr().out


def test_a_red_main_tip_stops_before_anything_is_merged(rel):
    rel.world.ci[0]["conclusion"] = "failure"
    with pytest.raises(rel.Stop, match="red"):
        rel.release_core("patch", "x", 60)


def test_missing_intro_stops_with_the_remedy(tool, rel, lib, monkeypatch):
    fresh = tool("hz-release")
    with pytest.raises(fresh.Stop, match="--intro"):
        fresh.open_release_pr("v0.9.12", "patch", "")


def test_a_compose_already_running_is_waited_for(rel, monkeypatch, lib):
    w = rel.world
    w.tags.add("v0.9.12")  # compose runs after the core release is published
    busy = [{"databaseId": 1, "status": "in_progress", "conclusion": "", "createdAt": "2020-01-01T00:00:00Z", "headSha": "x"}]
    seq = iter([busy, busy, []])
    real = w.ghj
    monkeypatch.setattr(lib, "ghj", lambda *a, default=None: next(seq, []) if a[:2] == ("run", "list") and "compose.yml" in a else real(*a, default=default))
    rel.compose_step("0.9.12", 60)
    assert any(c[:3] == ("workflow", "run", "compose.yml") for c in w.calls)


def test_plugin_release_verifies_pypi(rel, monkeypatch, lib, capsys):
    import base64

    toml = base64.b64encode(b'[project]\nname="heinzel-clock"\nversion="0.9.1"\n').decode()
    real = rel.world.gh
    run = {"id": 801, "path": ".github/workflows/release.yml", "event": "workflow_dispatch", "head_sha": "c" * 40, "status": "completed",
           "conclusion": "success", "created_at": "2999-01-01T00:00:00Z", "display_title": "Release one plugin", "run_attempt": 1}
    jobs = {"jobs": [{"id": 9, "name": "publish", "conclusion": "success"}]}

    def gh(*a):
        rel.world.calls.append(a)
        if a[:3] == ("api", "-X", "POST"):  # the dispatch names the run it created (heinzel-management-ovvg)
            return 0, json.dumps({"workflow_run_id": 801})
        if a[0] == "api" and a[1].endswith("/runs/801"):
            return 0, json.dumps(run)
        if a[0] == "api" and a[1].endswith("/runs/801/jobs"):
            return 0, json.dumps(jobs)
        if a[:2] == ("run", "view"):
            return 0, "publish\tPublish\t2999-01-01T00:00:00Z   RELEASE_TAG: heinzel-clock/v0.9.1\n"
        # the dispatch resolves its ref to the exact commit it binds the run to (Fable review F7)
        return (0, "c" * 40) if a[0] == "api" and "/commits/" in a[1] else (0, toml) if a[0] == "api" else real(*a)
    monkeypatch.setattr(lib, "gh", gh)
    monkeypatch.setattr(lib, "ghj", lambda *a, default=None: json.loads(gh(*a)[1]))  # only the run and its jobs are read as JSON here
    monkeypatch.setattr(rel, "pypi_has", lambda n, v: any(c[:3] == ("api", "-X", "POST") for c in rel.world.calls))
    assert rel.release_plugin("clock", 60) == 0
    assert "RELEASED heinzel-clock 0.9.1" in capsys.readouterr().out


def test_verdict_lists_each_job(rel):
    rel.world.tags.add("v0.9.12")  # acceptance runs after the core release and its app are published
    rel.world.app = True
    run_id = rel.acceptance_step("0.9.12", 60)
    ok, lines = rel.verdict(run_id)
    assert ok and lines == ["(ubuntu-24.04): ACCEPTED", "(macos-latest): ACCEPTED_EXCEPT_UNPROVABLE"]


# ask, verify, brief


def test_ask_adds_a_row_and_check_finds_it_until_a_pr_is_named(tool, home):
    ask = tool("hz-ask")
    doc = home / "Code/heinzel-management/AGENTS.md"
    doc.write_text("### Now, in this order\n\n| Item | Status | Next step | Where |\n|---|---|---|---|\n| old | doing | x | |\n\n### Next\n")
    ask.add("Make the logo bigger")
    text = doc.read_text()
    assert text.index("Make the logo bigger") < text.index("### Next") and "| old |" in text
    assert ask.open_asks(hours=0)[0][1] == "Make the logo bigger"
    assert ask.open_asks(hours=1) == []
    assert len(ask.open_asks(hours=1, now=time.time() + 7200)) == 1
    doc.write_text(text.replace("within the hour; small text or UI goes through the small-change lane | |", "#407 | |"))
    assert ask.open_asks(hours=0) == []


def test_verify_lines(tool, monkeypatch, capsys):
    v = tool("hz-verify")
    pages = {
        "https://pypi.org/pypi/heinzel/0.9.11/json": (200, json.dumps({"info": {"home_page": "", "project_urls": {"Home": "https://heinzel.team"}}})),
        "https://heinzel.team/imprint/": (200, "<p>Impressum Musterstadt</p>"),
    }
    monkeypatch.setattr(v, "fetch", lambda url: pages.get(url, (404, "")))
    assert v.main(["pypi", "heinzel", "0.9.11", "--homepage", "heinzel.team", "--and", "site", "/imprint/", "--contains", "Impressum"]) == 0
    out = capsys.readouterr().out
    assert out.count("PASS") == 4 and "FAIL" not in out
    assert v.main(["site", "/imprint/", "--contains", "missing"]) == 1
    assert v.main(["pypi", "heinzel", "9.9.9"]) == 1
    assert "FAIL pypi heinzel 9.9.9 (HTTP 404)" in capsys.readouterr().out


def test_verify_release_latest(tool, monkeypatch, capsys):
    v = tool("hz-verify")
    monkeypatch.setattr(v.hzlib, "ghj", lambda *a, default=None: [{"tagName": "app-v0.9.11", "isLatest": True}] if PLUGINS in a else [])
    assert v.main(["release", "app-v0.9.11", "--latest"]) == 0
    assert "release app-v0.9.11 is latest" in capsys.readouterr().out


def test_brief_makes_the_worktree_and_prints_the_prompt(tool, home, capsys, monkeypatch):
    clone = home / "Code/heinzel"
    clone.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(clone), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    run("remote", "add", "origin", str(clone))
    run("fetch", "-q", "origin", "main")
    brief = tool("hz-brief")
    monkeypatch.setattr(brief, "working_agents", lambda code: [])
    monkeypatch.setattr(brief.os, "getloadavg", lambda: (1.0, 1.0, 1.0))
    assert brief.main(["logo", "feat/logo", "--task", "Bigger logo."]) == 0
    out = capsys.readouterr().out
    assert (home / "Code/heinzel-logo/.git").exists()
    assert f"Worktree: {home}/Code/heinzel-logo" in out and "section 6" in out and "Bigger logo." in out
    assert brief.main(["logo", "feat/logo"]) == 0  # reuse


def test_a_snapshot_release_unfreezes_only_during_its_pinned_acceptance(rel, home, monkeypatch):
    snapshot = {"version": "0.9.12", "tag": "v0.9.12", "commit": "c" * 40,
                "wheel_sha256": WHEEL_SHA256, "profile_sha256": rel.world.profile_sha256}
    monkeypatch.setattr(rel, "pinned_snapshot", lambda version: snapshot)
    gh = rel.world.gh
    def dispatch(*args):
        if args[:3] != ("workflow", "run", "acceptance.yml"):
            return gh(*args)
        assert not rel.hzlib.merge_frozen()  # the freeze is lifted at the tag (43a1b73)
        assert args[args.index("--ref") + 1] == "v0.9.12"
        inputs = dict(value.split("=", 1) for flag, value in zip(args, args[1:]) if flag == "-f")
        rel.world.runs["acceptance.yml"] = {"attempt": 1, "databaseId": 900, "status": "queued", "conclusion": "",
            "createdAt": "2999-01-01T00:00:00Z", "headSha": snapshot["commit"],
            "displayTitle": "Acceptance 0.9.12 " + inputs["release_attempt"]}
        return 0, ""
    def acceptance_runs_while_merging_is_allowed(seconds):
        assert not rel.hzlib.merge_frozen()
        rel.world.runs["acceptance.yml"]["status"] = "completed"
        rel.world.runs["acceptance.yml"]["conclusion"] = "success"
    monkeypatch.setattr(rel.hzlib, "gh", dispatch)
    monkeypatch.setattr(rel.hzlib, "sleep", acceptance_runs_while_merging_is_allowed)
    assert rel.release_core("patch", "Snapshot release.", 60, True) == 0
    assert rel.state()["acceptance_commit"] == snapshot["commit"]
    assert rel.state()["finished"] is True


def test_finished_snapshot_can_start_a_new_default_release_without_losing_state_on_rejection(rel):
    rel.remember(version="0.9.11", finished=True, accepted=True, snapshot={"commit": "c" * 40})
    assert rel.release_core("patch", "Next release.", 60) == 0
    assert rel.state()["version"] == "0.9.12"
    assert rel.state()["snapshot"] != {"commit": "c" * 40}
    assert rel.state()["snapshot"] == rel.state()["acceptance_identity"]["snapshot"]


def test_unsupported_snapshot_workflow_can_resume_default_and_remove_only_its_owned_hold(rel, home, monkeypatch, capsys):
    from steuermaschine_heinzel.native.release_lib import snapshot as release_snapshot

    captured = {"version": "0.9.12", "tag": "v0.9.12", "commit": "c" * 40,
                "wheel_sha256": WHEEL_SHA256, "profile_sha256": rel.world.profile_sha256}
    monkeypatch.setattr(release_snapshot, "capture", lambda version: captured)
    supports_snapshot = release_snapshot.workflow_supports_snapshot
    monkeypatch.setattr(release_snapshot, "workflow_supports_snapshot", lambda ref: ref != TAG_COMMIT and supports_snapshot(ref))
    with pytest.raises(rel.Stop, match="cannot enforce"):
        rel.release_core("patch", "Snapshot release.", 60, True)
    owned = rel.snapshot_hold_path()
    assert not owned.exists()  # lifted at the tag, before acceptance (43a1b73)
    assert "snapshot" not in rel.state()
    independent = rel.hzlib.release_holds() / ("d" * 32)
    independent.write_text("another release hold")
    legacy = rel.paths()[0]
    legacy.write_text("incident hold")
    real_gh = rel.world.gh
    observed_acceptance = []
    def dispatch(*args):
        if args[:3] == ("workflow", "run", "acceptance.yml"):
            observed_acceptance.append(owned.exists())
            assert independent.exists() and legacy.read_text() == "incident hold"
        result = real_gh(*args)
        if args[:3] == ("workflow", "run", "acceptance.yml"):
            rel.world.runs["acceptance.yml"]["status"] = "in_progress"
        return result
    observed_wait = []
    def finish_acceptance(seconds):
        observed_wait.append(owned.exists())
        assert independent.exists() and legacy.read_text() == "incident hold"
        rel.world.runs["acceptance.yml"]["status"] = "completed"
    monkeypatch.setattr(rel.hzlib, "gh", dispatch)
    monkeypatch.setattr(rel.hzlib, "sleep", finish_acceptance)
    assert rel.release_core("0.9.12", "", 60) == 0
    assert observed_acceptance == [False]
    assert observed_wait == [False]
    assert not owned.exists()
    assert independent.exists()
    assert legacy.read_text() == "incident hold"
    assert rel.hzlib.merge_frozen()
    output = capsys.readouterr().out
    assert "other merge holds remain" in output
    assert "ok   unfreeze" not in output


def test_full_candidate_refusal_blocks_the_existing_merge_and_all_publication(rel, monkeypatch):
    def refuse(number):
        raise rel.release_candidate_policy.release_candidate.CandidateError("short candidate matrix")

    monkeypatch.setattr(rel.release_candidate_policy, "verify_pr", refuse)
    with pytest.raises(rel.Stop, match="candidate evidence refused"):
        rel.release_core("patch", "Plainer words.", 60)
    assert rel.world.pr["state"] == "OPEN"
    assert not rel.world.tags
    assert not rel.state().get("candidate_ci")
    assert not [call for call in rel.world.calls if call[:2] == ("workflow", "run")]


def test_full_candidate_success_is_recorded_without_aliasing_checks_or_dispatching_ci(rel):
    assert rel.release_core("patch", "Plainer words.", 60) == 0
    assert rel.state()["candidate_ci"] == {"run_id": 900, "head": MAIN_COMMIT, "job_count": 64}
    assert not [call for call in rel.world.calls if call[:3] == ("workflow", "run", "ci.yml")]
