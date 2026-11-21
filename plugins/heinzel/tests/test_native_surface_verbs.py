"""The verbs that absorb old commands: `sm why SUBJECT` picks the read by its subject, `sm ship PR` and `sm ship` for a
worktree of another repository, `sm review sonar PR`, `sm alarm list [--check]`. The reads themselves are the old
commands' functions; here they are replaced, so only the routing is tested."""

import json
from types import SimpleNamespace

import pytest
from coord_kit import app_runtime

from steuermaschine.engine import Refuse
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, invoke
from steuermaschine_heinzel.native import alarm, ci_ops, ci_read, ship, ship_driver, why_subject
from steuermaschine_heinzel.native.alarm import AlarmRow, Alarms
from steuermaschine_heinzel.native.ci_read import CiWhy
from steuermaschine_heinzel.native.queue import QueueWhy
from steuermaschine_heinzel.native.release_read import ReleaseStatus
from steuermaschine_heinzel.native.ship import HeadResult
from steuermaschine_heinzel.native.tasks_lib.beads import TaskShown
from steuermaschine_heinzel.native.why import CheckState, PullRequestWhy


def pr(number=7, red=()):
    return PullRequestWhy(number=number, state="OPEN", checks=[CheckState(name=n, latest="FAILURE" if n in red else "SUCCESS") for n in ("CI OK", "merge-gate")],
                          why="x")


@pytest.fixture
def reads(monkeypatch):
    calls = []
    monkeypatch.setattr(why_subject, "world", lambda: SimpleNamespace(repo="o/r", gh=SimpleNamespace(try_json=lambda argv, timeout=30: (calls.append(argv) or [{"number": 9}], ""))))
    monkeypatch.setattr(why_subject, "explain", lambda w, n: pr(n, red={7: ("CI OK", "merge-gate"), 8: ()}.get(n, ())))
    monkeypatch.setattr(ci_read, "explain_ci", lambda w, n: CiWhy(target=n, kind="run" if n > 99999 else "pr", cause=f"cause {n}"))
    monkeypatch.setattr(why_subject.rejudge, "sonar_head", lambda n: (_ for _ in ()).throw(RuntimeError("sonar down")))
    monkeypatch.setattr(why_subject.queue, "queue_why", lambda: QueueWhy(queued=[], merge_freeze=False, lines=["queue: empty"]))
    monkeypatch.setattr(why_subject.release_read, "release_status", lambda line=False: ReleaseStatus(active=True, version="0.9.55", lines=["release 0.9.55: tag"]))
    return calls


def why(tmp_path, *args):
    return invoke(app_runtime(tmp_path), ["why", *args])


def test_a_pr_with_red_checks_adds_the_ci_cause_and_sonar(tmp_path, reads):
    out = why(tmp_path, "7").stdout
    assert "CI\n  CAUSE    cause 7" in out and "SONAR    unknown: RuntimeError: sonar down" in out
    green = why(tmp_path, "8").stdout
    assert "CAUSE" not in green and "SONAR" not in green


def test_a_run_a_version_a_branch_and_nothing_named(tmp_path, reads):
    assert why(tmp_path, "44934734258").stdout.startswith("CAUSE    cause 44934734258")
    assert why(tmp_path, "v0.9.55").stdout == "release 0.9.55: tag\n"
    assert why(tmp_path, "0.9.54").stdout.startswith("v0.9.54 is not in flight; the release in flight is v0.9.55:")
    assert why(tmp_path, "fix/topic").stdout.startswith("PR 9 ") and reads[-1][:6] == ["pr", "list", "-R", "o/r", "--head", "fix/topic"]
    assert why(tmp_path).stdout == "QUEUE\n  queue: empty\nRELEASE\n  release 0.9.55: tag\n"


def test_nothing_named_keeps_what_could_be_read(tmp_path, reads, monkeypatch):
    monkeypatch.setattr(why_subject.queue, "queue_why", lambda: (_ for _ in ()).throw(Refuse("no queue state")))
    out = why(tmp_path).stdout
    assert "UNKNOWN  queue: no queue state" in out and "release 0.9.55: tag" in out


def test_a_bead_or_a_branch_without_a_slash(tmp_path, reads, monkeypatch):
    monkeypatch.setattr(why_subject.beads.Tracker, "show", lambda self, bead: TaskShown(bead=bead, text="the bead"))
    assert why(tmp_path, "lb7p").exit_code == 0
    monkeypatch.setattr(why_subject.beads.Tracker, "show", lambda self, bead: (_ for _ in ()).throw(RuntimeError("no bead")))
    assert why(tmp_path, "topic").stdout.startswith("PR 9 ")
    monkeypatch.setattr(why_subject, "world", lambda: SimpleNamespace(repo="o/r", gh=SimpleNamespace(try_json=lambda argv, timeout=30: ([], ""))))
    gone = why(tmp_path, "nothing")
    assert gone.exit_code == 2 and "no PR number, version, run id, branch or bead" in gone.stderr
    assert why(tmp_path, "fix/none").exit_code == 1


def test_why_main_reads_mains_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(why_subject.ci_evidence, "ci_main", lambda: TaskShown(bead="main", text="main green"))
    assert why(tmp_path, "main").exit_code == 0


def test_ship_pr_needs_live_and_queues_through_the_old_ship(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ship, "head_ship", lambda **kw: seen.append(kw) or HeadResult(action="ship", target=kw["pr"], verdict="queued", lines=["queued 801"]))
    rt = app_runtime(tmp_path)
    dry = invoke(rt, ["ship", "801"])
    assert dry.exit_code == 2 and "sm ship 801 --live" in dry.stderr and not seen
    done = invoke(rt, ["--format", "json", "ship", "801", "--live", "--set", "reviewed=review: FILE"])
    doc = json.loads(done.stdout)
    assert done.exit_code == 0 and doc["pr"] == 801 and doc["status"] == "queued" and seen[0]["reason"] == "review: FILE"


def test_ship_of_another_repository_opens_its_pr_through_the_old_route(tmp_path, monkeypatch):
    c = FakeCommands()
    c.on(["git", "-C", "/wt", "remote", "get-url", "origin"], Result(0, "git@github.com:owner/heinzel-plugins.git\n"))
    c.on(["git", "-C", "/wt", "log", "-1", "--format=%s"], Result(0, "fix(audio): louder\n"))
    seen = []
    monkeypatch.setattr(ship, "head_pr", lambda **kw: seen.append(kw) or HeadResult(action="pr", target="x", verdict="queued", lines=["opened PR #38"]))
    out = invoke(app_runtime(tmp_path, c), ["--format", "json", "ship", "/wt", "--live", "--set", "bead=abc"])
    doc = json.loads(out.stdout)
    assert doc["pr"] == 38 and doc["phase"] == "owner/heinzel-plugins: queued" and seen[0]["title"] == "fix(audio): louder" and seen[0]["bead"] == "abc"
    assert seen[0]["live"] is True


def test_ship_of_another_repository_needs_a_title(tmp_path, monkeypatch):
    c = FakeCommands()
    c.on(["git", "-C", "/wt", "remote", "get-url", "origin"], Result(0, "https://github.com/owner/heinzel-plugins\n"))
    c.on(["git", "-C", "/wt", "log", "-1", "--format=%s"], Result(1, ""))
    out = invoke(app_runtime(tmp_path, c), ["ship", "/wt"])
    assert out.exit_code == 2 and "no title" in out.stderr
    assert ship_driver.other_repo(SimpleNamespace(providers=SimpleNamespace(commands=c)), "/elsewhere") is None


def test_review_sonar_is_the_old_closure_for_one_pr(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ci_ops, "ci_sonar_close", lambda **kw: seen.append(kw) or ci_ops.SonarClose(live=False, project="p", pull_request=kw["pull_request"], open_before=0))
    out = invoke(app_runtime(tmp_path), ["review", "sonar", "485", "--issue", "A,B", "--issue", "C"])
    assert out.exit_code == 0 and seen == [{"config": None, "apply": False, "pull_request": 485, "issue": ["A", "B", "C"]}]


def test_alarm_list_adds_the_asks_and_with_check_the_deadlines(tmp_path, monkeypatch):
    monkeypatch.setattr(alarm, "alarm_check", lambda: Alarms(checked=True, alarms=[AlarmRow(level="WARN", key="k", text="slow")]))
    monkeypatch.setattr(alarm, "owner_asks", lambda: ["message 1"])
    monkeypatch.setattr(alarm, "deadlines", lambda: ["ACTION: crash"])
    out = invoke(app_runtime(tmp_path), ["alarm", "list", "--check"]).stdout
    assert out == "WARN slow [since ?; key k]\nASKS\n  message 1\nDEADLINES\n  ACTION: crash\n"


def test_the_asks_and_the_deadlines_degrade(monkeypatch):
    from steuermaschine_heinzel.native import tick
    from steuermaschine_heinzel.native.asks_lib import asks

    monkeypatch.setattr(asks, "audit", lambda *a: (_ for _ in ()).throw(FileNotFoundError("none")))
    assert alarm.owner_asks() == []
    monkeypatch.setattr(asks, "audit", lambda *a: (_ for _ in ()).throw(ValueError("broken")))
    assert alarm.owner_asks() == ["unknown: ValueError: broken"]
    monkeypatch.setattr(asks, "audit", lambda *a: SimpleNamespace(messages=[1], open=[], render=lambda: ["one", ""]))
    assert alarm.owner_asks() == ["one"]
    monkeypatch.setattr(tick, "tick", lambda raw=False: SimpleNamespace(render=lambda: ["OK quiet", "ACTION: x"]))
    assert alarm.deadlines() == ["ACTION: x"]
    monkeypatch.setattr(tick, "tick", lambda raw=False: (_ for _ in ()).throw(OSError("gone")))
    assert alarm.deadlines() == ["unknown: OSError: gone"]
