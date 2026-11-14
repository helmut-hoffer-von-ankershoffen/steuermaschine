"""p0hc: `sm head validate --detach` starts the validation in the background and returns a handle at once;
`sm head validate --wait HANDLE` waits a bounded time for its result (exit 75 while it runs)."""

import json
import socket
from pathlib import Path

import pytest
from test_native_dispatch import Commands, rt  # noqa: F401 - fixture

from steuermaschine.protocols import Result
from steuermaschine.testing import invoke
from steuermaschine_heinzel.native import window


class Spawning(Commands):
    """The fake runner, plus the background start: records the argv and answers a pid."""

    def __init__(self, pid="4321\n", rc=0):
        super().__init__()
        self.spawned, self.answer = [], Result(rc, pid, "" if rc == 0 else "sh: cannot fork")

    def run(self, argv, cwd=None, timeout=None, env=None):
        if argv[0] == "/bin/sh":
            self.spawned.append(list(argv))
            return self.answer
        return super().run(argv, cwd, timeout, env)


@pytest.fixture
def alive(monkeypatch):
    state = {"running": True}
    monkeypatch.setattr(window, "liveness", lambda turn: state["running"])
    return state


def detach(runtime, *extra):
    done = invoke(runtime, ["--format", "json", "head", "validate", ".", "--detach", "--lanes", "web-1", *extra])
    assert done.exit_code == 0, done
    return window.Validation.model_validate(json.loads(done.stdout))


def test_detach_starts_the_same_validation_in_the_background_and_returns_its_handle(rt):  # noqa: F811
    gh = Spawning()
    runtime = rt(commands=gh)
    doc = detach(runtime, "--serial", "--max-wait", "30")
    assert doc.status == "detached" and doc.handle.startswith("feat-x-aaaaaaaa-") and doc.exit_code == 0
    (argv,) = gh.spawned
    assert argv[:3] == ["/bin/sh", "-c", window.SPAWN] and argv[4].endswith(f"validations/{doc.handle}")
    assert argv[-12:] == ["--format", "json", "head", "validate", doc.worktree, "--lanes", "web-1", "--os", "ubuntu-24.04", "--max-wait", "30", "--serial"]
    meta = window.DetachedMeta.model_validate_json((Path(argv[4]).parent / f"{doc.handle}.meta.json").read_text())
    assert meta.pid == 4321 and meta.head == "a" * 40 and meta.lanes == ["web-1"] and meta.host == socket.gethostname()
    assert gh.git == [] and not [call for call in gh.calls if "POST" in call]  # nothing pushed or dispatched by this call
    text = invoke(rt(commands=Spawning()), ["head", "validate", ".", "--detach"]).stdout
    assert "DETACHED feat/x aaaaaaaaa" in text and "Next: sm head validate --wait feat-x-aaaaaaaa-" in text


def test_detach_refuses_what_the_validation_would_refuse_and_a_failed_start(rt):  # noqa: F811
    runtime = rt(commands=Spawning())
    runtime.providers.vcs.dirt = " M x.py"
    assert "dirty tree" in invoke(runtime, ["head", "validate", ".", "--detach"]).stderr and runtime.providers.commands.spawned == []
    failed = invoke(rt(commands=Spawning(pid="", rc=1)), ["head", "validate", ".", "--detach"])
    assert failed.exit_code == 1 and "could not start the detached validation: sh: cannot fork" in failed.stderr
    for argv in (["head", "validate", ".", "--detach", "--dry-run"], ["head", "validate", ".", "--wait", "h"], ["head", "validate"],
                 ["head", "validate", "--wait", "../x"], ["head", "validate", "--wait", "nothing-here"]):
        assert invoke(rt(), argv).exit_code == 2, argv


def test_wait_returns_the_result_or_exit_75_while_it_runs_or_a_refusal_when_it_died(rt, alive):  # noqa: F811
    gh = Spawning()
    runtime = rt(commands=gh)
    doc = detach(runtime)
    out = Path(gh.spawned[0][4] + ".json")
    running = invoke(runtime, ["head", "validate", "--wait", doc.handle, "--max-wait", "0"])
    assert running.exit_code == 75 and "still runs (pid 4321" in running.stderr and f"sm head validate --wait {doc.handle}" in running.stderr
    out.write_text("")  # started, nothing written yet
    assert invoke(runtime, ["head", "validate", "--wait", doc.handle, "--max-wait", "0"]).exit_code == 75
    out.write_text('{"worktree": "')  # half written: still running
    assert invoke(runtime, ["head", "validate", "--wait", doc.handle, "--max-wait", "0"]).exit_code == 75
    green = window.Validation(worktree=doc.worktree, branch="feat/x", head="a" * 40, status="validated", operating_system="ubuntu-24.04",
                              lanes=[window.LaneResult(lane="web-1", run=9, status="completed", conclusion="success")])
    out.write_text(green.model_dump_json() + "\n")
    done = invoke(runtime, ["head", "validate", "--wait", doc.handle])
    assert done.exit_code == 0 and "VALIDATED aaaaaaaaa: web-1 green" in done.stdout and f"detached validation {doc.handle} finished" in done.stdout
    out.write_text(green.model_copy(update={"status": "not_validated"}).model_dump_json())
    assert invoke(runtime, ["head", "validate", "--wait", doc.handle]).exit_code == 1
    out.write_text(json.dumps({"error": "the validation window is not free", "code": 75, "hint": "x"}))
    waited = invoke(runtime, ["head", "validate", "--wait", doc.handle])  # R1: a finished run never answers 75
    assert waited.exit_code == 1 and "ended waiting, nothing was validated: the validation window is not free" in waited.stderr
    assert f"sm head validate {doc.worktree} --detach" in waited.stderr
    out.write_text(json.dumps({"error": "dirty tree", "code": 1, "hint": ""}))
    refused = invoke(runtime, ["head", "validate", "--wait", doc.handle])
    assert refused.exit_code == 1 and "was refused: dirty tree" in refused.stderr
    out.unlink()
    alive["running"] = False
    died = invoke(runtime, ["head", "validate", "--wait", doc.handle])
    assert died.exit_code == 1 and "ended without a result" in died.stderr and ".log" in died.stderr


def test_wait_polls_until_the_result_is_there(rt, alive):  # noqa: F811
    gh = Spawning()
    runtime = rt(commands=gh)
    doc = detach(runtime)
    out = Path(gh.spawned[0][4] + ".json")
    clock = runtime.providers.clock
    real_sleep = clock.sleep

    def finish(seconds):
        real_sleep(seconds)
        out.write_text(window.Validation(worktree=doc.worktree, branch="feat/x", head="a" * 40, status="incomplete").model_dump_json())

    clock.sleep = finish
    ended = invoke(runtime, ["head", "validate", "--wait", doc.handle, "--max-wait", "5"])
    assert ended.exit_code == 1 and f"start it again: sm head validate {doc.worktree} --detach" in ended.stdout  # R1: incomplete and over
    assert invoke(runtime, ["head", "validate", "--wait", doc.handle]).exit_code == 1  # asking again changes nothing, and says so


def test_this_program_is_sm(monkeypatch, tmp_path):
    import sys

    tool = tmp_path / "sm"
    tool.write_text("")
    monkeypatch.setattr(sys, "argv", [str(tool)])
    assert window.this_program() == [str(tool)]
    monkeypatch.setattr(sys, "argv", ["python"])
    assert window.this_program()[0].endswith("sm")


def test_a_reused_pid_does_not_keep_wait_waiting(rt, alive, monkeypatch):  # noqa: F811
    """S5 (the reviewer's probe): the child is gone and its pid runs another process: --wait refuses, it does not answer 75."""
    starts = {4321: "Sat Nov 14 09:00:00 2026"}
    monkeypatch.setattr(window, "process_start", lambda _rt, pid: starts.get(pid, ""))
    gh = Spawning()
    runtime = rt(commands=gh)
    doc = detach(runtime)
    meta = window.DetachedMeta.model_validate_json(Path(gh.spawned[0][4] + ".meta.json").read_text())
    assert meta.pid_started == "Sat Nov 14 09:00:00 2026"
    assert invoke(runtime, ["head", "validate", "--wait", doc.handle, "--max-wait", "0"]).exit_code == 75  # the same process: it runs
    starts[4321] = "Sat Nov 14 11:30:00 2026"  # pid 4321 is another process now
    reused = invoke(runtime, ["head", "validate", "--wait", doc.handle, "--max-wait", "0"])
    assert reused.exit_code == 1 and "ended without a result" in reused.stderr
