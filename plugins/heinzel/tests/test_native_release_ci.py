"""`sm release VERSION` dispatches Core's workflow "Release" and watches it; `sm release status` reads its runs; `sm daemon
train` only follows an accepted release on the Mini. GitHub is `FakeGh`; nothing is started, nothing sleeps."""

import json
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel.native import release_ci as area
from steuermaschine_heinzel.native import train as train_area
from steuermaschine_heinzel.plugin import plugin

WORKFLOW = "workflow run release-chain.yml"
LISTING = "run list -R helmut-hoffer-von-ankershoffen/heinzel --workflow release-chain.yml --event workflow_dispatch"


def make(tmp_path, monkeypatch, gh):
    monkeypatch.setenv("HOME", str(tmp_path))
    conf = {"release_train_state_dir": str(tmp_path / "train")}
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": conf}, Path("."))
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=True), stack, settings,
                   _providers=fake_providers(commands=gh))


@pytest.fixture
def gh():
    return FakeGh()


@pytest.fixture
def rt(tmp_path, monkeypatch, gh):
    return make(tmp_path, monkeypatch, gh)


def doc(rt, model, *args):
    out = invoke(rt, ["--format", "json", *args])
    return out, model.model_validate(json.loads(out.stdout))


def run_view(status="completed", conclusion="success"):
    return {"databaseId": 5, "status": status, "conclusion": conclusion, "displayTitle": "Release 0.9.54", "url": "u", "event": "workflow_dispatch",
            "jobs": [{"name": "Plan", "status": "completed", "conclusion": "success"}, {"name": "Cut v0.9.54", "status": status, "conclusion": conclusion}]}


def dispatchable(gh, view):
    gh.on(LISTING, [{"databaseId": 4}], [{"databaseId": 4}, {"databaseId": 5, "displayTitle": "Release 0.9.54"}])
    gh.on(WORKFLOW, "")
    gh.on("run view 5", view)


def test_release_version_is_short_for_release_run_and_dispatches_the_ci_chain(rt, gh):
    dispatchable(gh, run_view())
    out, done = doc(rt, area.ChainRun, "release", "0.9.54")
    assert out.exit_code == 0 and done.mode == "ci" and done.run_id == 5 and done.lines[-1] == "RELEASED (run 5)"
    dispatched = next(c for c in gh.calls if "workflow" in c and "run" in c and "release-chain.yml" in c)
    assert dispatched[dispatched.index("--ref") + 1] == "main"
    assert "version=0.9.54" in dispatched and "dry_run=false" in dispatched
    assert gh.writes() == []  # a workflow dispatch is the only write, and it changes no repository state by itself


def test_a_failed_run_exits_1_and_says_how_to_resume(rt, gh):
    dispatchable(gh, run_view(conclusion="failure"))
    out, done = doc(rt, area.ChainRun, "release", "run", "0.9.54")
    assert out.exit_code == 1 and "ended failure" in done.lines[-1] and "dispatch again" in done.lines[-1]
    assert "Cut v0.9.54: failure" in done.lines


def test_dry_run_and_next_reach_the_workflow_inputs(rt, gh):
    dispatchable(gh, run_view())
    out, done = doc(rt, area.ChainRun, "release", "next", "--dry-run")
    dispatched = next(c for c in gh.calls if "release-chain.yml" in c and "workflow" in c)
    assert out.exit_code == 0 and done.lines[-1] == "DRY RUN OK (run 5)"
    assert "version=" in dispatched and "dry_run=true" in dispatched


def test_no_watch_returns_after_the_dispatch(rt, gh):
    dispatchable(gh, run_view(status="in_progress", conclusion=""))
    out, done = doc(rt, area.ChainRun, "release", "0.9.54", "--no-watch")
    assert out.exit_code == 0 and done.status == "in_progress" and "sm release status" in done.lines[0]


def test_a_run_still_going_after_the_timeout_waits(rt, gh):
    dispatchable(gh, run_view(status="in_progress", conclusion=""))
    out, done = doc(rt, area.ChainRun, "release", "0.9.54", "--timeout", "0")
    assert out.exit_code == 75 and "still in_progress" in done.lines[-1]


def test_a_dispatch_without_a_new_run_refuses_instead_of_guessing(rt, gh):
    gh.on(LISTING, [{"databaseId": 4}])
    gh.on(WORKFLOW, "")
    out = invoke(rt, ["release", "0.9.54"])
    assert out.exit_code == 1 and "no new run appeared" in out.stderr + out.stdout


def test_a_bad_version_and_local_dry_run_refuse(rt, gh):
    assert invoke(rt, ["release", "run", "v0.9.54"]).exit_code == 2
    assert invoke(rt, ["release", "0.9.54", "--local", "--dry-run"]).exit_code == 2
    assert gh.calls == []


def test_local_is_hidden_and_the_group_keeps_its_commands(rt, gh):
    gh.on("run list", [])
    assert "--local" not in invoke(rt, ["release", "run", "--help"]).stdout
    assert "run " in invoke(rt, ["release", "--help"]).stdout
    assert invoke(rt, ["release", "status"]).exit_code == 0  # a subcommand name is never taken as a version


def test_local_runs_the_local_step_runner_as_the_train_did(rt, gh, monkeypatch):
    seen = {}

    class Done:
        exit = 0

        def render(self):
            return ["ACCEPTED 0.9.54"]

    def fake(cmd, **kwargs):
        seen.update(kwargs, path=cmd.path)
        return Done()

    monkeypatch.setattr(area, "call", fake)
    out, done = doc(rt, area.ChainRun, "release", "0.9.54", "--local")
    assert out.exit_code == 0 and done.mode == "local" and done.lines == ["ACCEPTED 0.9.54"]
    assert seen["path"] == "release resume" and seen["snapshot_acceptance"] and seen["candidate_gate"] and seen["live"]
    assert gh.calls == []


def test_status_shows_the_running_chain_and_its_open_jobs(rt, gh):
    gh.on("run list -R helmut-hoffer-von-ankershoffen/heinzel --workflow release-chain.yml -L 5", [
        {"databaseId": 7, "status": "in_progress", "conclusion": "", "displayTitle": "Release 0.9.54", "event": "workflow_dispatch"},
        {"databaseId": 6, "status": "completed", "conclusion": "success", "displayTitle": "Release next (dry run)", "event": "pull_request"}])
    gh.on("run view 7", {"jobs": [{"name": "Plan", "status": "completed", "conclusion": "success"},
                                  {"name": "Candidate CI of v0.9.54", "status": "in_progress", "conclusion": ""}]})
    gh.on("pr list", [])
    gh.on("run list -R helmut-hoffer-von-ankershoffen/heinzel --branch", [])
    lines = invoke(rt, ["release", "status"]).stdout.splitlines()
    assert "CI release chain: run 7 Release 0.9.54: in_progress" in lines
    assert "  Candidate CI of v0.9.54: in_progress" in lines and not any("Plan" in line for line in lines)


# ----------------------------------------------------------------------------- the follower
class Proof:
    def __init__(self, live):
        self.exit_code = 0 if live else 1

    def render(self):
        return ["LIVE-OK 0.9.54" if self.exit_code == 0 else "NOT-LIVE: serving 0.9.53"]


def accepted(gh, version="0.9.54", ok=True):
    gh.on("api repos/helmut-hoffer-von-ankershoffen/heinzel/releases/latest", {"tag_name": f"v{version}"})
    gh.on(f"release download v{version}", json.dumps({"schema": 1, "project": "heinzel", "version": version, "accepted": ok}))


def test_the_tick_follows_an_accepted_release_once(rt, gh, monkeypatch):
    accepted(gh)
    updates = []
    monkeypatch.setattr(area.mini, "update", lambda w, version: updates.append(version) or Proof(live=True))
    out, tick = doc(rt, train_area.TrainTick, "daemon", "train")
    assert out.exit_code == 0 and tick.action == "follow" and tick.version == "0.9.54" and updates == ["0.9.54"]
    assert "LIVE-OK 0.9.54" in tick.lines and any("does not exist yet" in line for line in tick.lines)
    _, again = doc(rt, train_area.TrainTick, "daemon", "train")
    assert updates == ["0.9.54"] and any("does not exist yet" in line for line in again.lines)  # live once; iOS looked for again


def test_the_tick_retries_until_live(rt, gh, monkeypatch):
    accepted(gh)
    answers = [Proof(live=False), Proof(live=True)]
    monkeypatch.setattr(area.mini, "update", lambda w, version: answers.pop(0))
    out, tick = doc(rt, train_area.TrainTick, "daemon", "train")
    assert out.exit_code == 1 and "not live yet" in tick.lines[-1]
    out, tick = doc(rt, train_area.TrainTick, "daemon", "train")
    assert out.exit_code == 0 and "LIVE-OK 0.9.54" in tick.lines


def test_the_tick_runs_ios_after_live_and_stops_after_three_failures(rt, gh, monkeypatch):
    accepted(gh)
    monkeypatch.setattr(area.mini, "update", lambda w, version: Proof(live=True))
    monkeypatch.setattr(area, "IOS", "release status")  # any existing command stands in for `sm release ios`
    calls = []

    class Failed:
        exit_code = 1

        def render(self):
            return ["upload failed"]

    monkeypatch.setattr(area, "call", lambda cmd, **kwargs: calls.append(kwargs) or Failed())
    for _ in range(4):
        out, tick = doc(rt, train_area.TrainTick, "daemon", "train")
    assert calls == [{"version": "0.9.54"}] * 3 and "failed 3 times" in tick.lines[-1]


def test_an_unaccepted_latest_is_not_followed(rt, gh, monkeypatch):
    accepted(gh, ok=False)
    monkeypatch.setattr(area.mini, "update", lambda w, version: pytest.fail("followed a release without an accepted marker"))
    out, tick = doc(rt, train_area.TrainTick, "daemon", "train")
    assert out.exit_code == 0 and "no accepted acceptance.json" in tick.lines[0]


def test_a_dry_tick_says_what_it_would_follow(rt, gh, monkeypatch):
    accepted(gh)
    monkeypatch.setattr(area.mini, "update", lambda w, version: pytest.fail("a dry tick updated the Mini"))
    _, tick = doc(rt, train_area.TrainTick, "daemon", "train", "--dry-run")
    assert tick.lines == ["dry-run: would follow v0.9.54: sm mini update, sm release ios"]


def test_now_without_local_refuses(rt, gh):
    out = invoke(rt, ["daemon", "train", "--now"])
    assert out.exit_code == 2 and gh.calls == []


def test_an_unreadable_marker_is_not_followed(rt, gh, monkeypatch):
    gh.on("api repos/helmut-hoffer-von-ankershoffen/heinzel/releases/latest", {"tag_name": "v0.9.54"})
    gh.on("release download v0.9.54", Result(1, "", "release asset not found"))
    monkeypatch.setattr(area.mini, "update", lambda w, version: pytest.fail("followed without a marker"))
    _, tick = doc(rt, train_area.TrainTick, "daemon", "train")
    assert "no accepted acceptance.json" in tick.lines[0]
