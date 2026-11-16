"""Background runs (`steuermaschine.background`) and their commands `sm gate run --bg`, `sm gate wait`: the job files
with a scripted starter and a fake clock, the signal reset with one real process, the commands in process."""

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from steuermaschine import background
from steuermaschine.app import Runtime
from steuermaschine.commands import gate
from steuermaschine.config import Project, Settings
from steuermaschine.protocols import Result
from steuermaschine.runlock import Status
from steuermaschine.testing import FakeClock, FakeCommands, fake_providers, invoke


class Starter:
    def __init__(self, pid=4242):
        self.pid, self.calls = pid, []

    def __call__(self, argv, env, cwd, log):
        self.calls.append((list(argv), dict(env), cwd, log))
        log.write_text("started\n")
        return self.pid


def test_a_job_is_recorded_at_the_start_and_done_once_its_exit_code_is_written(tmp_path):
    clock, spawn = FakeClock(), Starter()
    job = background.start(tmp_path / "bg", lambda name: ["sm", "gate", "run", "--bg-job", name, "--", "pytest", "t.py"], ["pytest", "t.py"], "/work/a", {"KEEP": "1"},
                           clock, spawn=spawn, pid=lambda: 77)
    assert job.id == f"{int(clock.now())}-77" and (job.pid, job.state, job.cwd, job.command) == (4242, "running", "/work/a", ["pytest", "t.py"])
    argv, env, cwd, log = spawn.calls[0]
    assert argv == ["sm", "gate", "run", "--bg-job", job.id, "--", "pytest", "t.py"] and env == {"KEEP": "1"} and cwd == "/work/a" and str(log) == job.log
    again = background.start(tmp_path / "bg", lambda name: ["x"], ["true"], "", {}, clock, spawn=spawn, pid=lambda: 77)
    assert again.id == f"{job.id}-1"  # the same second and starter: a name of its own
    alive = lambda pid: True  # noqa: E731
    running = background.read(tmp_path / "bg", job.id, alive)
    assert running.state == "running" and running.exit_code == 75 and "is running" in running.render()[0] and f"sm gate wait {job.id}" in running.render()[2]
    assert background.wait(tmp_path / "bg", job.id, clock, timeout_seconds=5, is_alive=alive).state == "running" and sum(clock.slept) >= 5
    background.finish(tmp_path / "bg", job.id, 3)
    done = background.wait(tmp_path / "bg", job.id, clock, is_alive=alive)
    assert (done.state, done.code, done.exit_code) == ("done", 3, 3) and f"exit 3: pytest t.py (log {job.log})" in done.render()[0]
    assert [found.id for found in background.jobs(tmp_path / "bg", clock, alive)] == [job.id, again.id]
    assert background.read(tmp_path / "bg", "nope") is None and background.wait(tmp_path / "bg", "nope", clock) is None
    assert background.jobs(tmp_path / "absent", clock) == []


def test_a_job_whose_process_is_gone_without_an_exit_code_is_lost_and_old_jobs_are_swept(tmp_path):
    clock = FakeClock()
    job = background.start(tmp_path, lambda name: ["x"], ["pytest", "t.py"], "", {}, clock, spawn=Starter(pid=999999999))
    lost = background.wait(tmp_path, job.id, clock)
    assert (lost.state, lost.code, lost.exit_code) == ("lost", None, background.LOST) and "LOST" in lost.render()[0]
    (tmp_path / "broken.json").write_text("not json")
    assert background.read(tmp_path, "broken") is None
    clock.t += background.LISTED_SECONDS + 1
    assert background.jobs(tmp_path, clock) == []  # ended more than an hour ago: no longer listed
    clock.t += background.KEEP_SECONDS
    later = background.start(tmp_path, lambda name: ["x"], ["true"], "", {}, clock, spawn=Starter(pid=os.getpid()))
    assert not (tmp_path / f"{job.id}.json").exists() and not (tmp_path / f"{job.id}.log").exists() and (tmp_path / f"{later.id}.json").exists()
    assert background.alive(os.getpid()) and not background.alive(999999999) and background.alive(1)  # pid 1 exists and is not ours


def test_a_detached_start_gives_the_command_default_signals_although_the_starter_ignores_them(tmp_path):
    """The trap of `nohup sh -c "... &"`: the job inherits SIGINT ignored. Here the starter ignores it too, and the
    child still sees the default, in a session of its own, with its output in the log."""
    show = ("import os, signal; print(int(signal.getsignal(signal.SIGINT) == signal.SIG_IGN), int(signal.getsignal(signal.SIGQUIT) == signal.SIG_IGN), "
            "int(os.getsid(0) == os.getpid()), flush=True)")
    before = {number: signal.signal(number, signal.SIG_IGN) for number in background.RESET}
    try:
        inherited = background.subprocess.run([sys.executable, "-c", show], capture_output=True, text=True, check=True).stdout.split()
        pid = background.spawn_detached([sys.executable, "-c", show], dict(os.environ), str(tmp_path), tmp_path / "out.log")
        assert all(signal.getsignal(number) == signal.SIG_IGN for number in background.RESET)  # the starter's own handlers are put back
    finally:
        for number, handler in before.items():
            signal.signal(number, handler)
    assert inherited[:2] == ["1", "1"]  # what a plain child of such a starter gets: the trap
    deadline = time.monotonic() + 30
    while not (tmp_path / "out.log").read_text().strip() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pid > 0 and (tmp_path / "out.log").read_text().split() == ["0", "0", "1"]


def test_reset_signals_puts_both_back_to_their_default():
    before = {number: signal.signal(number, signal.SIG_IGN) for number in background.RESET}
    try:
        background.reset_signals()
        assert all(signal.getsignal(number) == signal.SIG_DFL for number in background.RESET)
    finally:
        for number, handler in before.items():
            signal.signal(number, handler)


# ---- the commands


def runtime(commands=None):
    project = Project(name="bg", plugin="none", root=Path("."), allow_unreviewed=True)
    return Runtime(project, None, Settings({}), _providers=fake_providers(commands=commands or FakeCommands()))


@pytest.fixture
def started(monkeypatch):
    spawn = Starter(pid=os.getpid())  # a pid that is alive
    monkeypatch.setattr(gate, "SPAWN", spawn)
    resets = []
    monkeypatch.setattr(background, "reset_signals", lambda: resets.append(1))  # never change the test runner's own handlers
    return spawn, resets


def test_gate_run_bg_starts_this_sm_detached_and_gate_wait_returns_the_commands_exit_code(started):
    spawn, resets = started
    commands = FakeCommands()
    commands.on(["git"], Result(1, ""))
    commands.on(["ps"], Result(0, "1\n"))
    commands.on(["pytest"], Result(3, ""))
    rt = runtime(commands)
    out = invoke(rt, ["--format", "json", "gate", "run", "--bg", "--priority", "1", "--timeout", "90", "--exclusive", "--", "pytest", "tests/a_test.py", "-n", "2"])
    ran = gate.GateRun.model_validate(json.loads(out.stdout))
    assert out.exit_code == 0 and ran.status == "started" and ran.pid == os.getpid() and ran.command == ["pytest", "tests/a_test.py", "-n", "2"]
    assert commands.calls == [] and resets == []  # nothing ran in the starter: the detached run takes its own turn at the lock
    argv, env, cwd, log = spawn.calls[0]
    assert argv[:3] == [sys.executable, "-c", "from steuermaschine.app import main; main()"]
    assert argv[3:] == ["gate", "run", "--bg-job", ran.job, "--priority", "1", "--timeout", "90", "--exclusive", "--", "pytest", "tests/a_test.py", "-n", "2"]
    assert cwd == os.getcwd() and str(log) == ran.log and Path(ran.log).parent == Path(rt.project.home) / "gate" / "sm-gate.bg" and env["HOME"] == os.environ["HOME"]
    text = invoke(rt, ["gate", "run", "--bg", "--", "pytest", "tests/b_test.py"])
    assert "started" in text.stdout and "sm gate wait " in text.stdout and "log:" in text.stdout
    waiting = invoke(rt, ["gate", "wait", ran.job, "--timeout", "3"])
    assert waiting.exit_code == 75 and "still runs after 3s" in waiting.stderr
    listed = Status.model_validate(json.loads(invoke(rt, ["--format", "json", "gate", "status"]).stdout))
    assert len(listed.background) == 2 and f"{ran.job} pid {os.getpid()} running: pytest tests/a_test.py -n 2" in listed.background[0]
    assert f"background: {ran.job} pid" in invoke(rt, ["gate", "status"]).stdout
    child = invoke(rt, argv[3:])  # what the detached process runs
    assert child.exit_code == 3 and resets == [1] and commands.calls[-1] == ["pytest", "tests/a_test.py", "-n", "2"]
    done = invoke(rt, ["--format", "json", "gate", "wait", ran.job])
    found = background.Job.model_validate(json.loads(done.stdout))
    assert done.exit_code == 3 and (found.state, found.code) == ("done", 3)
    assert "exit 3" in invoke(rt, ["gate", "wait", ran.job]).stdout
    missing = invoke(rt, ["gate", "wait", "nope"])
    assert missing.exit_code == 2 and "no background run nope" in missing.stderr


def test_a_background_run_records_its_exit_code_whatever_it_is(started):
    rt = runtime()
    out = gate.GateRun.model_validate(json.loads(invoke(rt, ["--format", "json", "gate", "run", "--bg", "--force-rerun", "fixture fixed by hand", "--", "true"]).stdout))
    assert "--force-rerun" in started[0].calls[0][0] and "fixture fixed by hand" in started[0].calls[0][0]
    directory = Path(rt.project.home) / "gate" / "sm-gate.bg"
    child = invoke(rt, ["gate", "run", "--bg-job", out.job, "--max-wait", "1", "--exclusive", "--", "true"])
    assert child.exit_code == 127 and background.read(directory, out.job).code == 127  # the fake has no `true`: the code is recorded all the same


def test_the_launcher_passes_an_absolute_project_root_on_and_the_help_names_the_trap():
    assert gate.launcher(["--light"], ["true"], "/work/project")("J")[3:] == ["--project", "/work/project", "gate", "run", "--bg-job", "J", "--light", "--", "true"]
    assert gate.launcher([], ["true"], ".")("J")[3:] == ["gate", "run", "--bg-job", "J", "--", "true"]
    rt = runtime()
    text = invoke(rt, ["gate", "run", "--help"]).stdout
    assert "--bg" in text and "nohup" in text and "--bg-job" not in text
    assert invoke(rt, ["gate", "wait", "--help"]).exit_code == 0
    lint = json.loads(invoke(rt, ["--format", "json", "self", "lint"]).stdout)
    assert [row for row in lint if "gate" in json.dumps(row)] == []


def test_a_command_inside_a_focus_worktree_reaches_the_focus_head(tmp_path):
    from steuermaschine import runlock
    from steuermaschine.runlock import Policy
    from steuermaschine.testing import MemoryStore

    store = MemoryStore()
    head = tmp_path / "release-fix"
    (head / "tests").mkdir(parents=True)
    store.save("focus", "list", {str(head): {"for": "1.4.0"}, "PR 12": {}, "1.4.0": {}})
    focus = runlock.focus_of(store)
    assert focus(str(head)) == str(head) and focus(str(head / "tests")) == str(head) and focus(str(tmp_path)) == "" and focus(str(tmp_path / "release-fix-2")) == ""
    assert runlock.focus_of(MemoryStore())(str(head)) == ""
    plain = Policy(directory=tmp_path)
    assert runlock.with_focus(plain, store).focus(str(head)) == str(head) and runlock.with_focus(plain, None) is plain
    own = Policy(directory=tmp_path, focus=lambda cwd: "mine")
    assert runlock.with_focus(own, store) is own
