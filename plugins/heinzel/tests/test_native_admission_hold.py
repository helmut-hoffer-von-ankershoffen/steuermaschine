"""The admission hold (heinzel-management-qji9) and the waiting entry that nothing removed (5zb9), on fakes.

`sm head prepare`, `sm head apply` and `sm release admit prepare|apply` hold the validation window quiet while they
run: no new validation starts, the runs going finish, `--wait` waits for them. `sm ci window drop` removes an entry.
"""
import os
import socket
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel.native import window
from steuermaschine_heinzel.native.admission_lib import focused_admit as a
from steuermaschine_heinzel.native.admission_lib import ops
from steuermaschine_heinzel.native.release_admit_lib import release_ops
from steuermaschine_heinzel.plugin import plugin

A, B = "a" * 40, "b" * 40
UNCOVERED = "unknown active Actions run; admission refused: 9 in_progress validation/x focused-job.yml"


@pytest.fixture
def rt():
    stack = stack_of(plugin)
    settings, _ = stack.validate({}, Path("."))
    project = Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=True)
    providers = stack.providers(settings, fake_providers())  # the plugin's extras, as in a real run: `sm ci window status` reads the same window
    runtime = Runtime(project, stack, settings, _providers=providers)
    runtime.providers.store.put_receipt("verdicts", "a" * 64, {"verdict": "approve", "reviewer": "reviewer-1"})  # apply needs a recorded verdict
    return runtime


def line(rt):
    return window.the_window(rt)


def applied():
    return ops.ApplyResult(receipt_path="/r", sha256="s" * 64, head=A)


class Probe:
    """Stands in for the admission step: records what the validation line says while it runs."""

    def __init__(self, rt, script):
        self.rt, self.script, self.seen = rt, list(script), []

    def __call__(self, *args, **kwargs):
        self.seen.append(([h.label for h in line(self.rt).status().holds], line(self.rt).enter("fix/x", B, 1, in_flight=0, owner="v", pid=os.getpid(),
                                                                                              host=socket.gethostname()).state))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def test_head_apply_holds_the_window_while_it_runs_and_lets_go_after(rt, monkeypatch, tmp_path):
    probe = Probe(rt, [applied()])
    monkeypatch.setattr(ops, "apply", probe)
    done = invoke(rt, ["head", "apply", str(tmp_path / "packet.json"), "--approved", "a" * 64])
    assert done.exit_code == 0
    assert probe.seen == [([f"head apply {tmp_path / 'packet.json'}"], "wait")]  # a validation asking now is kept waiting
    assert line(rt).status().holds == []  # the hold ended with the command
    line(rt).leave("fix/x", B, owner="v")


def test_the_hold_ends_when_the_admission_fails(rt, monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "apply", Probe(rt, [a.AdmissionError("boom")]))
    done = invoke(rt, ["head", "apply", str(tmp_path / "packet.json"), "--approved", "a" * 64])
    assert done.exit_code == 1 and "boom" in done.stdout + done.stderr and line(rt).status().holds == []


def test_apply_wait_waits_for_the_running_validation_with_the_hold_kept(rt, monkeypatch, tmp_path):
    probe = Probe(rt, [a.AdmissionError(UNCOVERED), a.AdmissionError(UNCOVERED), applied()])
    monkeypatch.setattr(ops, "apply", probe)
    done = invoke(rt, ["head", "apply", str(tmp_path / "packet.json"), "--approved", "a" * 64, "--wait"])
    assert done.exit_code == 0 and len(probe.seen) == 3
    assert all(holds and state == "wait" for holds, state in probe.seen)  # held at every look: nothing new started in between
    assert line(rt).status().holds == []


def test_apply_without_wait_refuses_as_before(rt, monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "apply", Probe(rt, [a.AdmissionError(UNCOVERED)]))
    done = invoke(rt, ["head", "apply", str(tmp_path / "packet.json"), "--approved", "a" * 64])
    assert done.exit_code == 1 and "unknown active Actions run" in done.stdout + done.stderr and line(rt).status().holds == []


def test_apply_wait_gives_up_with_75_after_the_limit_and_still_lets_go(rt, monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "apply", Probe(rt, [a.AdmissionError(UNCOVERED)] * 80))
    done = invoke(rt, ["head", "apply", str(tmp_path / "packet.json"), "--approved", "a" * 64, "--wait"])
    assert done.exit_code == 75 and line(rt).status().holds == []


def test_head_prepare_holds_the_window(rt, monkeypatch, tmp_path):
    probe = Probe(rt, [a.AdmissionError("boom")])
    monkeypatch.setattr(ops, "prepare", probe)
    done = invoke(rt, ["head", "prepare", str(tmp_path / "packet.json"), "--core-repo", str(tmp_path), "--review-root", str(tmp_path)])
    assert done.exit_code == 1 and probe.seen[0][1] == "wait" and probe.seen[0][0][0].startswith("head prepare ") and line(rt).status().holds == []


@pytest.mark.parametrize("path, target, extra", [(["release", "admit", "apply"], "apply", ["--approved", "a" * 64]),
                                                 (["release", "admit", "prepare"], "prepare", ["--core-repo", ".", "--review-root", "."])])
def test_release_admit_holds_the_window_and_waits_on_request(rt, monkeypatch, tmp_path, path, target, extra):
    result = applied() if target == "apply" else release_ops.ReleasePrepareResult(
        sha256="s" * 64, version="1.0.0", head=A, main=B, slot="x", recovery=False, prepared_path="p", args_path="q", prepared={})
    probe = Probe(rt, [a.AdmissionError(UNCOVERED), result])
    monkeypatch.setattr(release_ops, target, probe)
    done = invoke(rt, [*path, str(tmp_path / "packet.json"), *extra, "--wait"])
    assert len(probe.seen) == 2 and all(holds and holds[0].startswith(f"release admit {target} ") and state == "wait" for holds, state in probe.seen)
    assert line(rt).status().holds == [], done.stdout + done.stderr


def test_a_hold_does_not_stop_a_validation_that_already_runs(rt):
    first = line(rt).enter("fix/one", A, 2, in_flight=0, owner="one", pid=os.getpid(), host=socket.gethostname())
    assert first.state == "go"
    line(rt).hold("admit", "head apply P", pid=os.getpid(), host=socket.gethostname())
    assert line(rt).enter("fix/one", A, 2, in_flight=2, owner="one", pid=os.getpid(), host=socket.gethostname()).state == "go"


def test_a_validation_wait_names_the_pending_admission(rt):
    line(rt).hold("admit", "head apply P", pid=os.getpid(), host=socket.gethostname())
    out = invoke(rt, ["ci", "window", "status"])
    assert "hold     head apply P" in out.stdout


def test_status_marks_a_gone_waiter_and_drop_removes_it(rt):
    dead = os.getpid() + 7_000_000  # no such process
    line(rt).enter("fix/one", A, 1, in_flight=0, owner="one", pid=os.getpid(), host=socket.gethostname())
    line(rt).enter("fix/dead", B, 1, in_flight=0, owner="dead", pid=dead, host=socket.gethostname())
    text = invoke(rt, ["ci", "window", "status"]).stdout
    assert "waiting  1." not in text and "gone     fix/dead bbbbbbbbb" in text and "ci window drop bbbbbbbbb" in text
    done = invoke(rt, ["ci", "window", "drop", B[:9]])
    assert done.exit_code == 0 and "dropped fix/dead" in done.stdout
    assert "gone" not in invoke(rt, ["ci", "window", "status"]).stdout
    assert invoke(rt, ["ci", "window", "drop", B[:9]]).exit_code == 1  # nothing left to drop


def test_drop_never_removes_a_live_or_unknown_waiter_without_proof_of_ownership(rt, tmp_path):  # review R2: unknown liveness is not a dead command
    line(rt).enter("fix/one", A, 1, in_flight=0, owner="one", pid=os.getpid(), host=socket.gethostname())
    line(rt).enter("fix/live", B, 1, in_flight=0, owner="live", pid=os.getpid(), host=socket.gethostname())
    line(rt).enter("fix/away", "c" * 40, 1, in_flight=0, owner="away", pid=5, host="another-machine")
    live = invoke(rt, ["ci", "window", "drop", B])
    assert live.exit_code == 1 and "only its owner drops it" in live.stdout + live.stderr
    away = invoke(rt, ["ci", "window", "drop", "c" * 9])
    assert away.exit_code == 1 and "another machine" in away.stdout + away.stderr
    assert [t.branch for t in line(rt).status().waiting] == ["fix/live", "fix/away"]


def test_a_dead_waiter_does_not_take_the_turn_through_the_command(rt):
    dead = os.getpid() + 7_000_000
    line(rt).enter("fix/one", A, 1, in_flight=0, owner="one", pid=os.getpid(), host=socket.gethostname())
    line(rt).enter("fix/dead", B, 1, in_flight=0, owner="dead", pid=dead, host=socket.gethostname())
    line(rt).enter("fix/live", "c" * 40, 1, in_flight=0, owner="live", pid=os.getpid(), host=socket.gethostname())
    line(rt).leave("fix/one", A, owner="one")
    assert line(rt).enter("fix/live", "c" * 40, 1, in_flight=0, owner="live", pid=os.getpid(), host=socket.gethostname()).state == "go"


# ---------------------------------------------------------------- review R1: a refusal after the registry write is never waited out
class Writer:
    def __init__(self):
        self.written = []

    def set_variable(self, name, value):
        self.written.append(name)


def test_a_refusal_after_the_write_stops_at_once_with_written_no_receipt():
    from steuermaschine.testing import FakeClock
    from steuermaschine_heinzel.native.admission_lib import wait

    rt, calls = Writer(), []

    def once():
        rt.set_variable("FOCUSED_CI_PLANS_V2_0", "x")  # registry_set happened ...
        calls.append("write")
        raise a.AdmissionError(wait.UNCOVERED + "42 queued workflow_dispatch x")  # ... then the check after it refused

    clock = FakeClock(0.0)
    with pytest.raises(wait.WrittenWithoutReceipt) as stop:
        wait.retry_when_clear(lambda: wait.tracked(rt, once), clock=clock, say=lambda s: None, max_minutes=45, poll_seconds=90)
    assert calls == ["write"] and clock.slept == []  # one write, no wait
    assert "WAS WRITTEN" in str(stop.value) and "no receipt" in str(stop.value) and "tell the coordinator" in str(stop.value)


def test_a_refusal_before_the_write_is_still_retried_and_the_give_up_names_apply():
    from steuermaschine.testing import FakeClock
    from steuermaschine_heinzel.native.admission_lib import wait

    rt, calls = Writer(), []

    def once():
        calls.append(1)
        if len(calls) == 1:
            raise a.AdmissionError(UNCOVERED)
        rt.set_variable("FOCUSED_CI_PLANS_V2_0", "x")
        return "RECEIPT"

    assert wait.retry_when_clear(lambda: wait.tracked(rt, once), clock=FakeClock(0.0), say=lambda s: None, max_minutes=45, poll_seconds=90) == "RECEIPT"
    never = lambda: (_ for _ in ()).throw(a.AdmissionError(UNCOVERED))  # noqa: E731
    with pytest.raises(wait.StillWaiting, match=r"^apply did not get a clear window"):
        wait.retry_when_clear(never, clock=FakeClock(0.0), say=lambda s: None, max_minutes=1, poll_seconds=90)


def test_head_apply_command_reports_written_no_receipt_as_a_refusal_without_the_blockers_hint(rt, monkeypatch, tmp_path):
    from steuermaschine_heinzel.native.admission_lib import wait

    def boom(*args, **kwargs):
        raise wait.WrittenWithoutReceipt("the registry slot WAS WRITTEN but there is no receipt: x (unknown active Actions run)")

    monkeypatch.setattr(ops, "apply", boom)
    done = invoke(rt, ["head", "apply", str(tmp_path / "packet.json"), "--approved", "a" * 64, "--wait"])
    assert done.exit_code == 1 and "WAS WRITTEN" in done.stdout + done.stderr and "head blockers" not in done.stdout + done.stderr.replace("sm head blockers, then", "")


def test_release_hold_command_removes_a_stale_hold_and_status_prints_its_owner(rt):
    dead = os.getpid() + 7_000_000
    line(rt).hold("owner-1", "head apply P", pid=dead, host=socket.gethostname())
    line(rt).hold("owner-2", "head apply Q", pid=os.getpid(), host=socket.gethostname())
    assert "[owner-2]" in invoke(rt, ["ci", "window", "status"]).stdout
    assert "may still run" in invoke(rt, ["ci", "window", "release-hold", "owner-2"]).stdout
    assert "released the admission hold" in invoke(rt, ["ci", "window", "release-hold", "owner-2", "--force"]).stdout
    assert line(rt).status().holds == []
