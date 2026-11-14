"""dispatch_lib.focused: wait, re-run start and repeat, the rerun command and the status/watch commands, with the
GitHub reads replaced at the module's own seams (gh, ghj, run_state, jobs_of, admit) and a fake clock."""

import importlib.util
import json
from types import SimpleNamespace

import pytest

HEAD = "a" * 40


def load():
    spec = importlib.util.find_spec("steuermaschine_heinzel.native.dispatch_lib.focused")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.R = "example/core"
    return module


class Clock:
    def __init__(self):
        self.t, self.sleeps = 1_800_000_000.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


@pytest.fixture
def f(tmp_path):
    mod = load()
    mod.EDGE = mod.Edge(clock=Clock())
    mod.STATE = tmp_path / "runs" / "focused.jsonl"
    mod.SCRATCH = tmp_path / "scratch"
    mod.STATE.parent.mkdir()
    lines = []
    mod.EDGE.out = lines.append
    mod.EDGE.err = lines.append
    mod.lines = lines
    mod.EDGE.journal = lambda text: lines.append("J:" + text)
    return mod


def job(name, concl="failure", status="completed", labels=("ubuntu-24.04",), id=1):
    return {"id": id, "name": name, "conclusion": concl, "status": status, "labels": list(labels)}


def run_row(rid=5, attempt=1, status="completed", conclusion="failure", branch="fix/x", **more):
    return {"id": rid, "status": status, "conclusion": conclusion, "run_attempt": attempt, "head_branch": branch, "head_sha": HEAD, "path": ".github/workflows/ci.yml", **more}


def test_out_and_warn_default_to_the_process_streams(capsys):
    mod = load()
    mod.out("hello")
    mod.warn("careful")
    seen = capsys.readouterr()
    assert seen.out == "hello\n" and seen.err == "careful\n"


def test_fail_log_reads_failed_jobs(f, monkeypatch):
    jobs = SimpleNamespace(jobs=[SimpleNamespace(id=1, name="tests", failed_steps=["pytest"]), SimpleNamespace(id=2, name="lint", failed_steps=[]), SimpleNamespace(id=3, name="ok", failed_steps=[])], failed=["tests", "lint"])  # noqa: E501
    logs = {1: SimpleNamespace(state="ok", lines=["boom"]), 2: SimpleNamespace(state="unavailable", lines=[])}
    hub = SimpleNamespace(jobs=lambda rid: jobs, log_tail=lambda jid, n: logs[jid])
    monkeypatch.setattr(f, "GitHub", lambda *a, **kw: hub)
    assert f.fail_log(5) == "--- tests (failed step: pytest)\nboom\n--- lint\n    log unavailable"


def test_note_and_stamp(f):
    f.note("hi")
    assert f.lines == ["J:ci one: hi"] and len(f.stamp()) == 19


def test_wait_polls_until_the_attempt_completes_and_reports(f, monkeypatch):
    views = iter([{"status": "in_progress", "attempt": 1}, {"status": "completed", "attempt": 1, "conclusion": "success", "url": "https://x"}])
    monkeypatch.setattr(f, "ghj", lambda *a, ok=False: next(views) if a[0] == "run" else [{"name": "dump"}])
    monkeypatch.setattr(f, "jobs_of", lambda rid: [job("tests", "success"), job("other", "success")])
    monkeypatch.setattr(f, "tally_of", lambda rid: "FOCUSED TALLY 3/3 green")
    assert f.wait(5, ["tests"], False, "label") == 0
    text = "\n".join(f.lines)
    assert "run 5 attempt 1: completed/success" in text and "tally: FOCUSED TALLY 3/3 green" in text and "artifacts: dump" in text and "gh run download 5" in text
    assert f.EDGE.result["jobs"] == [{"name": "tests", "conclusion": "success"}] and f.EDGE.clock.sleeps == [f.POLL]


def test_wait_failure_prints_the_log_and_json_mode(f, monkeypatch):
    monkeypatch.setattr(f, "ghj", lambda *a, ok=False: {"status": "completed", "attempt": 2, "conclusion": "failure", "url": "u"} if a[0] == "run" else None)
    monkeypatch.setattr(f, "jobs_of", lambda rid: [job("tests")])
    monkeypatch.setattr(f, "tally_of", lambda rid: "")
    monkeypatch.setattr(f, "fail_log", lambda rid: "LOG TAIL\n")
    assert f.wait(5, None, False, "label", attempt=2) == 1
    assert "LOG TAIL" in f.lines and f.EDGE.result["faillog"] == "LOG TAIL\n" and "(no matching jobs)" not in f.lines
    f.lines.clear()
    assert f.wait(5, None, True, "label", attempt=2) == 1
    assert json.loads(next(line for line in f.lines if line.startswith("{")))["conclusion"] == "failure"


def test_wait_without_matching_jobs_and_timeout(f, monkeypatch):
    monkeypatch.setattr(f, "ghj", lambda *a, ok=False: {"status": "completed", "attempt": 1, "conclusion": "success"} if a[0] == "run" else None)
    monkeypatch.setattr(f, "jobs_of", lambda rid: [])
    monkeypatch.setattr(f, "tally_of", lambda rid: "")
    assert f.wait(5, ["x"], False, "l") == 0 and "(no matching jobs)" in f.lines
    monkeypatch.setattr(f, "ghj", lambda *a, ok=False: {"status": "in_progress", "attempt": 1})
    with pytest.raises(SystemExit, match="did not complete within the wait limit"):
        f.wait(5, None, False, "l")


def start_world(f, monkeypatch, current=None, admitted=True):
    calls = []
    monkeypatch.setattr(f, "run_state", lambda rid: current or run_row())
    monkeypatch.setattr(f, "gh", lambda *a, ok=False, timeout=60: calls.append(a) or "")
    monkeypatch.setattr(f, "admit", lambda start, arm, wait, **kw: calls.append(("admit", arm, wait, kw)) or start(f.STATE.with_suffix(".pending")))
    return calls


def test_start_rerun_records_the_attempt(f, monkeypatch):
    calls = start_world(f, monkeypatch)
    targets = [job("tests", id=11, labels=("macos-latest",)), job("lint", id=12)]
    assert f.start_rerun(run_row(), targets, False, True) == 2
    assert ("run", "rerun", "5", "-R", "example/core", "--failed") in calls
    assert calls[0][3] == {"new_jobs": 2, "new_arm_jobs": 1, "rerun_id": 5}
    row = json.loads(f.STATE.read_text())
    assert row["kind"] == "rerun" and row["attempt"] == 2 and row["what"] == "tests, lint" and row["arm_jobs"] == 1
    assert not f.STATE.with_suffix(".pending").exists()
    f.start_rerun(run_row(), targets[:1], True, False)
    assert ("run", "rerun", "5", "-R", "example/core", "--job", "11") in calls


@pytest.mark.parametrize("change", [{"status": "in_progress", "conclusion": None}, {"head_sha": "b" * 40}, {"head_branch": "other"}, {"run_attempt": 2}])
def test_start_rerun_refuses_a_changed_run(f, monkeypatch, change):
    start_world(f, monkeypatch, current=run_row(**change))
    with pytest.raises(SystemExit, match="changed before re-run admission"):
        f.start_rerun(run_row(), [job("tests")], True, True)


def test_rerun_repeat_rounds_and_tally(f, monkeypatch):
    attempts = iter([2, 3])
    monkeypatch.setattr(f, "start_rerun", lambda run, targets, one, wait: next(attempts))
    states = iter([run_row(status="in_progress", conclusion=None, attempt=2), run_row(attempt=2), run_row(attempt=3)])
    monkeypatch.setattr(f, "run_state", lambda rid: next(states))
    results = iter([[job("tests", "success")], [job("tests", "failure")]])
    monkeypatch.setattr(f, "jobs_of", lambda rid: next(results))
    assert f.rerun_repeat(run_row(), job("tests"), 2, False) == 1
    assert "round 1/2: success" in f.lines and "round 2/2: failure" in f.lines


def test_rerun_repeat_json_all_green_and_unknown_job_and_timeout(f, monkeypatch):
    monkeypatch.setattr(f, "start_rerun", lambda *a: 2)
    monkeypatch.setattr(f, "run_state", lambda rid: run_row(attempt=2))
    monkeypatch.setattr(f, "jobs_of", lambda rid: [job("tests", "success")])
    assert f.rerun_repeat(run_row(), job("tests"), 1, True) == 0
    assert json.loads(f.lines[-1])["results"] == ["success"]
    monkeypatch.setattr(f, "jobs_of", lambda rid: [])
    assert f.rerun_repeat(run_row(), job("tests"), 1, False) == 1 and "round 1/1: unknown" in f.lines
    monkeypatch.setattr(f, "run_state", lambda rid: run_row(status="in_progress", conclusion=None, attempt=2))
    with pytest.raises(SystemExit, match="did not complete"):
        f.rerun_repeat(run_row(), job("tests"), 1, False)


def test_cmd_repeats(f):
    assert f.cmd_repeats(SimpleNamespace(fail_rate="10%")) == 0 and f.lines


def args(**more):
    return SimpleNamespace(**{"target": "5", "force": False, "job": None, "repeat": 1, "wait": False, "json": False, **more})


def rerun_world(f, monkeypatch, run=None, jobs=None):
    pr = {"headRefName": "fix/x", "headRefOid": HEAD}
    run_list = [{"databaseId": 5, "headSha": HEAD, "workflowName": "CI", "createdAt": "2026-11-13T10:00:00Z", "status": "completed"}]

    def ghj(*a, ok=False):
        return pr if a[0] == "pr" else run_list

    monkeypatch.setattr(f, "ghj", ghj)
    monkeypatch.setattr(f, "run_state", lambda rid: run or run_row())
    monkeypatch.setattr(f, "jobs_of", lambda rid: jobs if jobs is not None else [job("tests"), job("lint", "success")])
    started = []
    monkeypatch.setattr(f, "start_rerun", lambda r, targets, one, wait: started.append((targets, one, wait)) or 2)
    monkeypatch.setattr(f, "wait", lambda rid, names, as_json, label, attempt: ("waited", rid, names, attempt))
    return started


def test_cmd_rerun_by_pr_without_waiting(f, monkeypatch):
    started = rerun_world(f, monkeypatch)
    assert f.cmd_rerun(args()) == 0
    assert started[0][0][0]["name"] == "tests" and started[0][1] is False
    assert "re-running 1 job(s) of run 5 (fix/x): tests" in f.lines and "wait with: sm ci one watch 5" in f.lines


def test_cmd_rerun_by_run_id_waits_for_a_named_job(f, monkeypatch):
    started = rerun_world(f, monkeypatch)
    assert f.cmd_rerun(args(target="987654", job="lint", wait=True)) == ("waited", 987654, ["lint", "CI OK"], 2)
    assert started[0][1] is True


def test_cmd_rerun_repeat_paths(f, monkeypatch):
    rerun_world(f, monkeypatch)
    monkeypatch.setattr(f, "rerun_repeat", lambda run, job, n, as_json: ("repeat", job["name"], n))
    assert f.cmd_rerun(args(job="tests", repeat=3)) == ("repeat", "tests", 3)
    with pytest.raises(SystemExit, match="--repeat needs --job"):
        f.cmd_rerun(args(repeat=3))
    with pytest.raises(SystemExit, match="--repeat at most"):
        f.cmd_rerun(args(job="tests", repeat=f.c.MAX_RERUN_REPEAT + 1))


@pytest.mark.parametrize("kwargs,message", [
    (dict(run=run_row(branch="other")), "does not match the PR branch"),
    (dict(run=run_row(head_sha="b" * 40)), "does not match the PR branch"),
    (dict(run=run_row(path=".github/workflows/other.yml")), "outside the CI workflow"),
    (dict(run=run_row(status="in_progress", conclusion=None)), "still in_progress"),
    (dict(jobs=[job("lint", "success")]), "no failed job to re-run"),
])
def test_cmd_rerun_refusals(f, monkeypatch, kwargs, message):
    rerun_world(f, monkeypatch, **kwargs)
    with pytest.raises(SystemExit, match=message):
        f.cmd_rerun(args())


def test_cmd_rerun_refuses_main_unknown_job_and_unknown_pr_run(f, monkeypatch):
    rerun_world(f, monkeypatch, run=run_row(branch="main"))
    with pytest.raises(SystemExit, match="refusing"):
        f.cmd_rerun(args(target="987654"))
    rerun_world(f, monkeypatch)
    with pytest.raises(SystemExit):
        f.cmd_rerun(args(job="nonexistent job"))
    monkeypatch.setattr(f.c, "pick_run", lambda runs, head: None)
    with pytest.raises(SystemExit, match="no CI run for head"):
        f.cmd_rerun(args())


class Store:
    def lock(self, name):
        import contextlib

        return contextlib.nullcontext()


def test_cmd_status_and_watch(f, monkeypatch):
    f.EDGE.store = Store()
    done = {"run": 5, "kind": "rerun", "what": "w", "branch": "fix/x", "attempt": 2, "jobs": 1, "arm_jobs": 0, "at": "t", "t": f.EDGE.clock.now(), "result": "success", "status": "completed"}
    f.STATE.write_text(json.dumps(done) + "\n" + json.dumps({**done, "run": 6, "branch": "other"}) + "\n")
    f.STATE.with_suffix(".pending").write_text("x")
    assert f.cmd_status(args(branch=None, json=False)) == 0
    assert any("reconcile the uncertain focused start" in line for line in f.lines)
    f.lines.clear()
    assert f.cmd_status(args(branch="fix/x", json=True)) == 0
    assert [r["run"] for r in json.loads(f.lines[-1])] == [5]
    f.lines.clear()
    assert f.cmd_status(args(branch="nobody", json=False)) == 0 and f.lines[-1].startswith("no focused run in the last 24 h")
    seen = []
    monkeypatch.setattr(f, "wait", lambda rid, names, as_json, label, attempt: seen.append((rid, attempt)) or 0)
    assert f.cmd_watch(SimpleNamespace(run=5, json=False)) == 0 and seen == [(5, 2)]
    assert f.cmd_watch(SimpleNamespace(run=99, json=False)) == 0 and seen[-1] == (99, 1)
