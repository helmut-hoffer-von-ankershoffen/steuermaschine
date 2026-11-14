"""The rehearsal library. Moved from the former rehearsal tool's tests: the assertions are unchanged except where they
named an old command or its switch (each such line says so). What changed is the fixture: the state is in a memory
store, time is a fake clock, and the seams are the library's (`cancel_run`, `EDGE.lane`, `gated`) instead of processes."""

import json
from types import SimpleNamespace

import pytest

from steuermaschine.testing import FakeClock, MemoryStore

REPO = "example/core"


def load(_name="rehearsal"):
    """A fresh copy of the moved module per test, as the former tests loaded the script: module state never leaks."""
    import importlib.util

    spec = importlib.util.find_spec("steuermaschine_heinzel.native.rehearse_lib.rehearsal")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.R = REPO
    return module


MAIN = "a" * 40
COMMIT = "b" * 40
TREE = "c" * 40


class Hub:
    """A fake GitHub: records every write, serves the reads the rehearsal makes."""

    def __init__(self):
        self.calls, self.refs, self.runs, self.pulls, self.active, self.commit_pulls = [], {}, {}, [], [], []
        self.main = MAIN

    def api(self, path, *args):
        self.calls.append((path, args))
        method = args[args.index("--method") + 1] if "--method" in args else "GET"
        if path == "pulls?state=open&per_page=100":
            return self.pulls
        if path.startswith("actions/runs?status="):
            return {"workflow_runs": self.active if "in_progress" in path else []}
        if path == "git/ref/heads/main":
            return {"object": {"sha": self.main}}
        if path == f"git/commits/{self.main}":
            return {"tree": {"sha": TREE}}
        if path == "git/commits" and method == "POST":
            assert f"tree={TREE}" in args and f"parents[]={self.main}" in args
            return {"sha": COMMIT}
        if path == f"commits/{COMMIT}/pulls":
            return self.commit_pulls
        if path == "git/refs" and method == "POST":
            ref = next(a for a in args if a.startswith("ref=")).removeprefix("ref=refs/heads/")
            self.refs[ref] = next(a for a in args if a.startswith("sha=")).removeprefix("sha=")
            return {}
        if path.startswith("git/refs/heads/") and method == "DELETE":
            del self.refs[path.removeprefix("git/refs/heads/")]
            return None
        if path.startswith("git/ref/heads/"):
            name = path.removeprefix("git/ref/heads/")
            if name not in self.refs:
                raise RuntimeError("gone")
            return {"object": {"sha": self.refs[name]}}
        if path.startswith("actions/runs/"):
            return self.runs[int(path.rsplit("/", 1)[1])]
        if path.startswith("compare/"):
            return {"commits": [{"commit": {"message": "feat(web): chat head (#791)\n\nbody"}}]}
        raise AssertionError(path)


@pytest.fixture
def tool(tmp_path, monkeypatch):
    queue = tmp_path / "queue.json"
    m = load()
    m.bind(m.Edge(store=MemoryStore(), clock=FakeClock()), REPO, tmp_path / "plugins", tmp_path / "work", queue)
    assert m.queue_path() == queue and m.load() == {}  # never the coordinator's real state (was: the two state file paths)
    queue.write_text(json.dumps({"items": [{"key": "393", "status": "merged"}, {"key": "650", "status": "needs work"}]}))
    m.hub = Hub()
    m.notes = []
    monkeypatch.setattr(m, "api", m.hub.api)
    monkeypatch.setattr(m, "note", m.notes.append)
    monkeypatch.setattr(m, "admission_running", lambda: False)  # never the Mini's real process table
    monkeypatch.setattr(m, "gh", lambda *a: pytest.fail(f"unexpected gh {a}"))
    monkeypatch.setattr(m, "cancel_run", lambda run: pytest.fail(f"unexpected cancel of run {run}"))
    monkeypatch.setattr(m, "run", lambda *a, **k: pytest.fail(f"unexpected process {a}"))
    m.next_run = 100
    monkeypatch.setattr(m, "run_consumers", lambda main, only=None, full=False: {"status": "done", "stale": [], "locked": ["heinzel-example"], "failed": [], "passed": []})

    def dispatch(lane):
        if m.next_run >= 100 + getattr(m, "cap", 99):
            return ["refusing: 5 focused runs in flight"]
        m.next_run += 1
        lane.update(run=m.next_run, status="running")
        m.hub.runs[m.next_run] = {"head_sha": COMMIT, "status": "in_progress", "conclusion": None, "html_url": f"https://x/{m.next_run}"}
        return []

    monkeypatch.setattr(m, "dispatch", dispatch)
    return m


def finish_runs(m, failing=()):
    for rid, seen in m.hub.runs.items():
        seen.update(status="completed", conclusion="failure" if rid in failing else "success")


def test_start_uses_one_throwaway_commit_and_one_branch_per_lane_and_os(tool):
    assert tool.start(list(tool.LANES), list(tool.OSES), False) == ""
    assert len(tool.hub.refs) == 8 and set(tool.hub.refs.values()) == {COMMIT}  # never main's or a PR's own SHA
    assert f"rehearsal/{MAIN[:10]}-web-2-macos" in tool.hub.refs
    assert all(name.startswith("rehearsal/") for name in tool.hub.refs)
    assert tool.load()["current"]["commit"] == COMMIT


def test_start_refuses_a_commit_that_belongs_to_a_pull_request(tool):
    tool.hub.commit_pulls = [{"number": 790}]
    with pytest.raises(RuntimeError, match="pull request"):
        tool.start(["cli"], ["ubuntu-24.04"], False)
    assert tool.hub.refs == {} and "current" not in tool.load()


@pytest.mark.parametrize("status", ["queued", "ready", "waiting"])
def test_refuses_while_a_queued_pr_waits_for_admission_or_merge(tool, status):
    tool.queue_path().write_text(json.dumps({"items": [{"key": "795", "status": status}]}))
    why = tool.start(["cli"], ["ubuntu-24.04"], False)
    assert why.startswith("refusing: queued PRs wait") and "795" in why
    assert not any(args for _, args in tool.hub.calls if "POST" in args)


def test_refuses_on_unreadable_queue_state_and_unreadable_github(tool, monkeypatch):
    tool.queue_path().write_text("{not json")
    assert "cannot read the merge queue state" in tool.refusal()
    tool.queue_path().unlink()
    monkeypatch.setattr(tool, "api", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    assert "cannot read GitHub" in tool.refusal()


def test_refuses_while_a_release_pr_is_open_or_a_release_run_is_active(tool):
    tool.hub.pulls = [{"head": {"ref": "release/v0.9.40"}}, {"head": {"ref": "fix/x"}}]
    assert "release is in flight (release/v0.9.40)" in tool.start(["cli"], ["ubuntu-24.04"], False)
    tool.hub.pulls = []
    tool.hub.active = [{"head_branch": "release/v0.9.41"}]
    assert "release/v0.9.41" in tool.refusal()
    tool.hub.active = [{"head_branch": "fix/x"}, {"head_branch": None}]
    assert tool.refusal() == ""


def test_if_moved_skips_an_already_rehearsed_main(tool):
    tool.save({"last": {"main": MAIN, "started": 0}})
    assert "nothing new" in tool.start(["cli"], ["ubuntu-24.04"], False, if_moved=True)
    assert tool.hub.refs == {}


def test_capacity_refusal_leaves_lanes_pending_for_the_next_step(tool):
    tool.cap = 5
    tool.start(list(tool.LANES), list(tool.OSES), False)
    out = tool.step()
    lanes = tool.load()["current"]["lanes"]
    assert [x["status"] for x in lanes].count("running") == 5 and "waiting: refusing: 5 focused" in out
    tool.cap = 99
    finish_runs(tool)
    assert "5/8 lanes done" in tool.step()
    finish_runs(tool)
    assert tool.step().startswith("rehearsal green")


def test_a_failed_lane_is_red_names_the_run_and_the_merges_and_cleans_up(tool):
    tool.save({"last_green": "d" * 40})
    tool.start(["web-2"], list(tool.OSES), False)
    tool.step()
    finish_runs(tool, failing={101})
    out = tool.step()
    state = tool.load()
    assert out.startswith("rehearsal red") and "current" not in state
    assert state["last"]["failures"] == ["End-to-end web-2 (ubuntu-24.04): failure https://x/101"]
    assert state["last"]["suspects"] == ["feat(web): chat head (#791)"]
    assert state["last_green"] == "d" * 40 and tool.hub.refs == {}
    assert "REHEARSAL RED" in tool.notes[-1] and "#791" in tool.notes[-1]
    assert tool.status_code() == 1  # was: main(["status", "--line"]) == 1


def test_green_records_the_main_it_proved(tool):
    tool.start(["cli"], ["ubuntu-24.04"], False)
    tool.step()
    finish_runs(tool)
    tool.step()
    assert tool.load()["last_green"] == MAIN and tool.status_code() == 0  # was: main(["status"]) == 0


def test_a_guard_met_midway_yields_instead_of_dispatching_more(tool):
    tool.cap = 1
    tool.start(["cli", "web-1"], ["ubuntu-24.04"], False)
    tool.step()
    finish_runs(tool)  # nothing of ours runs any more: there is nothing to cancel, only a lane not to start
    tool.queue_path().write_text(json.dumps({"items": [{"key": "796", "status": "queued"}]}))
    tool.cap = 99
    assert tool.step().startswith("rehearsal incomplete")
    assert tool.next_run == 101 and "preempted" not in tool.load()["last"]  # the second lane was never dispatched
    assert "796" in tool.line(tool.load()) and "last_green" not in tool.load()


def test_a_run_on_another_commit_is_refused(tool):
    tool.start(["cli"], ["ubuntu-24.04"], False)
    tool.step()
    tool.hub.runs[101]["head_sha"] = "e" * 40
    with pytest.raises(RuntimeError, match="not on the throwaway commit"):
        tool.step()


def test_cancel_stops_runs_and_deletes_only_branches_still_on_the_throwaway_commit(tool, monkeypatch):
    cancelled = []
    monkeypatch.setattr(tool, "cancel_run", lambda run: cancelled.append(("run", "cancel", str(run), "--repo", tool.R)))
    tool.start(["cli", "web-1"], ["ubuntu-24.04"], False)
    tool.cap = 1
    tool.step()
    moved = f"rehearsal/{MAIN[:10]}-web-1-ubuntu"
    tool.hub.refs[moved] = "f" * 40
    assert tool.cancel() == "rehearsal incomplete"
    assert cancelled == [("run", "cancel", "101", "--repo", tool.R)]
    assert tool.hub.refs == {moved: "f" * 40}


def test_consumer_failures_make_the_verdict_red_and_unavailable_is_incomplete(tool, monkeypatch):
    monkeypatch.setattr(tool, "run_consumers", lambda main, full: {"status": "done", "failed": ["heinzel-github"], "passed": []})
    tool.start(["cli"], ["ubuntu-24.04"], True)
    tool.step()
    finish_runs(tool)
    assert "Consumer (heinzel-github)" in tool.step()
    monkeypatch.setattr(tool, "run_consumers", lambda main, full: {"status": "unavailable", "detail": "no green Edge build", "failed": []})
    tool.hub.main = MAIN  # same main, on demand
    tool.start(["cli"], ["ubuntu-24.04"], True)
    tool.step()
    finish_runs(tool)
    out = tool.step()
    assert out.startswith("rehearsal incomplete") or "INCOMPLETE" in tool.line(tool.load())
    assert "no green Edge build" in tool.line(tool.load())


def test_the_consumer_checks_read_plugins_main_not_a_pin(tool, monkeypatch):
    ran = []
    answers = {"fetch": 0, "rev-parse": (0, "1" * 40 + "\n")}

    def run(argv, **_):
        ran.append(argv)
        code, out = answers["rev-parse"] if "rev-parse" in argv else (answers["fetch"], "")
        return SimpleNamespace(returncode=code, stdout=out, stderr="")

    monkeypatch.setattr(tool, "run", run)
    assert tool.plugins_main() == "1" * 40
    assert ran == [["git", "-C", str(tool.PLUGINS), "fetch", "--quiet", "origin", "main"], ["git", "-C", str(tool.PLUGINS), "rev-parse", "--verify", "FETCH_HEAD^{commit}"]]
    answers["rev-parse"] = (0, "not a sha")
    with pytest.raises(RuntimeError, match="cannot read the commit of plugins main"):
        tool.plugins_main()
    answers["fetch"] = 128
    with pytest.raises(RuntimeError, match="cannot fetch plugins main"):
        tool.plugins_main()


def test_dispatch_goes_through_the_single_lane_library_and_reads_its_run_id(tmp_path, monkeypatch):
    m = load()
    seen = []

    def lane_start(branch, name, runner):  # the library's seam; recorded in the shape of the former command line
        seen.append(["dispatch", branch, "--suite", name, "--os", runner])
        return 0, "dispatch e2e cli x1 on ubuntu-24.04 @ rehearsal/x: run 4242\nwait with: sm ci one watch 4242\n", ""

    m.EDGE = m.Edge(lane=lane_start)
    lane = {"lane": "cli", "os": "ubuntu-24.04", "branch": "rehearsal/x", "run": None, "status": "pending"}
    assert m.dispatch(lane) == [] and lane["run"] == 4242 and lane["status"] == "running"
    assert seen == [["dispatch", "rehearsal/x", "--suite", "cli", "--os", "ubuntu-24.04"]]
    m.EDGE = m.Edge(lane=lambda *a: (1, "", "refusing: a release/* run is active\n"))
    lane = {"lane": "cli", "os": "ubuntu-24.04", "branch": "rehearsal/x", "run": None, "status": "pending"}
    assert m.dispatch(lane) == ["refusing: a release/* run is active"] and lane["status"] == "pending"


def test_tick_waits_for_the_interval(tool):
    assert tool.due({}, 120, 10_000) is True
    assert tool.due({"last": {"started": 10_000 - 119 * 60}}, 120, 10_000) is False
    assert tool.due({"last": {"started": 10_000 - 120 * 60}}, 120, 10_000) is True
    assert tool.due({"last": {"started": 10_000 - 29 * 60}}, 0, 10_000) is False  # --every has a floor of 30 minutes
    assert tool.due({"last": {"started": 10_000 - 30 * 60}}, 0, 10_000) is True


def test_a_gate_refusal_of_the_consumer_check_is_unavailable_not_a_failure(tmp_path, monkeypatch):
    m = load()
    monkeypatch.setattr(m, "WORK", tmp_path / "work")
    monkeypatch.setattr(m, "plugins_main", lambda: "1" * 40)
    wheel = tmp_path / "heinzel-0.9.40.dev1-py3-none-any.whl"
    monkeypatch.setattr(m, "edge_wheel", lambda main, work: work.mkdir(parents=True, exist_ok=True) or wheel)
    verdicts = {"heinzel-example": "", "heinzel-github": ""}
    monkeypatch.setattr(m, "lock_check", lambda tree, core: ("0.9.40", dict(verdicts)))
    ran = []

    def run(argv, **_):
        ran.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def gated(argv, cwd, log):  # the run lock's seam (was: a process of the former gate writing its refusal to the log)
        assert argv[:2] == ["make", "check"] and f"CORE={wheel}" in argv and cwd == m.WORK / f"{MAIN[:10]}-{int(m.EDGE.clock.now())}" / "plugins"
        code = outcomes[argv[2]]
        return code, "`make check` runs the full suite" if code == 75 else ""

    monkeypatch.setattr(m, "run", run)
    monkeypatch.setattr(m, "gated", gated)
    m.EDGE = m.Edge(clock=FakeClock())
    outcomes = {"NAME=heinzel-example": 0, "NAME=heinzel-github": 2}
    result = m.run_consumers(MAIN, full=True)
    assert (result["status"], result["failed"], result["passed"], result["stale"]) == ("done", ["heinzel-github"], ["heinzel-example"], [])
    assert ran[-1][-4:-1] == ["worktree", "remove", "--force"]  # its own detached plugins worktree is removed
    assert m.run_consumers(MAIN, only="heinzel-github", full=True)["passed"] == []
    assert m.run_consumers(MAIN, only="heinzel-nope")["stale"] == ["no consumer named heinzel-nope on plugins main 1111111111"]
    outcomes["NAME=heinzel-example"] = 75
    result = m.run_consumers(MAIN, full=True)
    assert result["status"] == "unavailable" and result["failed"] == [] and "--allow-full" in result["detail"]  # was: the old switch's name
    verdicts["heinzel-github"] = "Core dependencies changed; relock only this consumer."
    result = m.run_consumers(MAIN, full=True)  # a stale lock stays red even when the full checks are refused
    assert result["stale"] == ["heinzel-github: Core dependencies changed; relock only this consumer."] and result["status"] == "unavailable"
    assert m.verdict({"lanes": [], "consumers": result}) == ("red", ["Consumer lock (heinzel-github: Core dependencies changed; relock only this consumer.)"])


def test_the_lock_check_reads_the_last_line_and_a_script_that_fails_is_said(tool, monkeypatch):
    answer = SimpleNamespace(returncode=0, stdout='uv: resolved\n{"version": "0.9.40", "consumers": {"heinzel-example": ""}}\n', stderr="")
    monkeypatch.setattr(tool, "run", lambda argv, **kw: answer)
    assert tool.lock_check("tree", "w.whl") == ("0.9.40", {"heinzel-example": ""})
    answer.returncode, answer.stderr = 2, "ModuleNotFoundError: No module named 'heinzel_plugin_tools'\n"
    with pytest.raises(RuntimeError, match="did not run: ModuleNotFoundError"):
        tool.lock_check("tree", "w.whl")
    answer.returncode, answer.stdout, answer.stderr = 0, '{"consumers": []}', ""
    with pytest.raises(RuntimeError, match="did not run"):
        tool.lock_check("tree", "w.whl")


def test_a_release_starting_mid_flight_preempts_and_an_unreadable_github_does_not(tool, monkeypatch):
    cancelled = cancelling(tool, monkeypatch)
    tool.start(["cli", "web-1"], ["ubuntu-24.04"], False)
    tool.step()
    assert tool.preempted() == ""
    tool.hub.pulls = [{"head": {"ref": "release/v0.9.41"}}]
    assert tool.step().startswith("rehearsal incomplete") and [c[2] for c in cancelled] == ["101", "102"]
    last = tool.load()["last"]
    assert last["preempted"] is True and "a release started (release/v0.9.41)" in last["yielded"] and tool.hub.refs == {}
    tool.hub.pulls = []
    tool.hub.main = MAIN
    tool.start(["cli"], ["ubuntu-24.04"], False)
    tool.step()
    monkeypatch.setattr(tool, "api", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    assert tool.preempted() == ""


def test_refuses_while_a_focused_admission_runs(tool, monkeypatch):
    monkeypatch.setattr(tool, "admission_running", lambda: True)
    assert "focused admission" in tool.refusal()
    assert tool.start(["cli"], ["ubuntu-24.04"], False).startswith("refusing") and tool.hub.refs == {}


def test_a_rehearsal_open_too_long_is_closed_and_its_own_runs_cancelled(tool, monkeypatch):
    cancelled = []
    monkeypatch.setattr(tool, "cancel_run", lambda run: cancelled.append(("run", "cancel", str(run), "--repo", tool.R)))
    tool.cap = 1
    tool.start(["cli", "web-1"], ["ubuntu-24.04"], False)
    tool.step()
    state = tool.load()
    state["current"]["started"] -= (tool.MAX_OPEN_MINUTES + 5) * 60
    tool.save(state)
    tool.cap = 99
    assert tool.step().startswith("rehearsal incomplete")
    assert cancelled == [("run", "cancel", "101", "--repo", tool.R)] and tool.next_run == 101
    assert "still open after" in tool.line(tool.load()) and tool.hub.refs == {} and "current" not in tool.load()


def cancelling(tool, monkeypatch):
    cancelled = []
    monkeypatch.setattr(tool, "cancel_run", lambda run: cancelled.append(("run", "cancel", str(run), "--repo", tool.R)))
    return cancelled


def test_a_pr_entering_the_queue_mid_flight_cancels_only_the_rehearsals_own_lanes(tool, monkeypatch):
    cancelled = cancelling(tool, monkeypatch)
    tool.cap = 2
    tool.start(["cli", "web-1", "web-2"], ["ubuntu-24.04"], False)
    tool.step()
    tool.hub.runs[101].update(status="completed", conclusion="success")  # one lane is already through
    tool.hub.runs[999] = {"head_sha": "9" * 40, "status": "in_progress", "conclusion": None, "html_url": "https://x/999"}  # not ours
    tool.queue_path().write_text(json.dumps({"items": [{"key": "796", "status": "queued"}, {"key": "393", "status": "merged"}]}))
    tool.cap = 99
    out = tool.step()
    last = tool.load()["last"]
    assert out.startswith("rehearsal incomplete") and "current" not in tool.load()
    assert cancelled == [("run", "cancel", "102", "--repo", tool.R)] and tool.next_run == 102  # the third lane never started
    assert [x["status"] for x in last["lanes"]] == ["done", "done", "dropped"] and [x["result"] for x in last["lanes"]][:2] == ["success", "cancelled"]
    assert last["preempted"] is True and "796" in last["yielded"] and "393" not in last["yielded"]
    assert last["failures"] == [] and "last_green" not in tool.load() and tool.hub.refs == {}
    assert tool.status_code() == 0 and "REHEARSAL INCOMPLETE" in tool.notes[-1]


def test_an_admission_starting_mid_flight_cancels_the_running_lanes(tool, monkeypatch):
    cancelled = cancelling(tool, monkeypatch)
    tool.start(["cli", "web-1"], ["ubuntu-24.04"], False)
    tool.step()
    monkeypatch.setattr(tool, "admission_running", lambda: True)
    assert tool.step().startswith("rehearsal incomplete")
    assert [c[2] for c in cancelled] == ["101", "102"]
    last = tool.load()["last"]
    assert last["preempted"] is True and "focused admission prepare or apply started" in last["yielded"]


def test_a_lane_that_already_failed_keeps_the_verdict_red_when_the_rest_gives_way(tool, monkeypatch):
    cancelled = cancelling(tool, monkeypatch)
    tool.start(["cli", "web-1"], ["ubuntu-24.04"], False)
    tool.step()
    tool.hub.runs[101].update(status="completed", conclusion="failure")
    monkeypatch.setattr(tool, "admission_running", lambda: True)
    assert tool.step().startswith("rehearsal red") and len(cancelled) == 1
    assert tool.load()["last"]["failures"] == ["End-to-end cli (ubuntu-24.04): failure https://x/101"]


def test_nothing_is_cancelled_without_a_reason_or_on_an_unreadable_queue(tool):
    tool.start(["cli"], ["ubuntu-24.04"], False)
    tool.step()
    assert tool.preempted() == ""
    tool.queue_path().write_text("{not json")
    assert tool.preempted() == "" and "0/1 lanes done" in tool.step()  # cancel_run is pytest.fail here: no cancel was tried
    assert tool.load()["current"]["lanes"][0]["status"] == "running"
    tool.queue_path().unlink()
    assert tool.preempted() == "" and "0/1 lanes done" in tool.step()
