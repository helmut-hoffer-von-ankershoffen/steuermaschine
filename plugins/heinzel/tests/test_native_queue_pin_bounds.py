"""The queue checks the main plan of the head it pins (reviewed or guarded), and every tick call to gh is bounded:
`gh api` with a field or a body is a POST (write timeout), the freeze wait gives up, the ruleset PUT, the flake reads
and the in-flight run count carry a timeout. GitHub is monkeypatched at hzlib and edge; the state lives in tmp_path."""

import subprocess
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel.native.queue_lib import flakes_read, hzlib, main_plan, merge_gate, merge_train
from steuermaschine_heinzel.native.queue_lib import queue_daemon as qd
from steuermaschine_heinzel.native.ship_lib import ship as ship_mod

PIN = "a" * 40
LIVE = "b" * 40


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HZ_QUEUE_STATE", str(tmp_path / "queue.json"))
    monkeypatch.setenv("HZ_ALARM_STATE", str(tmp_path / "alarm.json"))
    monkeypatch.setenv("HZ_ALARM_NOTIFY", "off")
    (tmp_path / ".cache").mkdir()
    monkeypatch.setattr(hzlib, "note", lambda text: None)


@pytest.mark.parametrize(("row", "pinned"), [({"reviewed_head": PIN}, PIN), ({"guarded_head": PIN}, PIN),
                                             ({"reviewed_head": "", "guarded_head": PIN}, PIN), ({}, "")])
def test_plan_guard_checks_the_pinned_head(monkeypatch, row, pinned):
    seen = []
    monkeypatch.setattr(main_plan, "gap", lambda pr, head="": seen.append((pr, head)) or "")
    refusal: list[str] = []
    assert qd.plan_guard({"pr": "7", **row}, None, refusal)() is True
    assert seen == [("7", pinned)] and refusal == []


def test_queue_add_checks_the_plan_of_the_guarded_head(monkeypatch):
    seen = []
    heads = iter([PIN, PIN])
    monkeypatch.setattr(qd, "pr_head", lambda repo, number: next(heads))
    monkeypatch.setattr(qd, "guard_findings", lambda repo, number: ([], "PASS"))
    monkeypatch.setattr(hzlib, "ghj", lambda *a, default=None: {"title": "fix(x): y", "body": ""})
    monkeypatch.setattr(main_plan, "gap", lambda pr, head="": seen.append((pr, head)) or "")
    args = SimpleNamespace(pr=[["7"]], after=[])
    assert qd.add(args) == 0
    assert seen == [("7", PIN)]
    row = qd.load()["items"][0]
    assert row["guarded_head"] == PIN


@pytest.mark.parametrize(("args", "bound"), [
    (("api", "repos/o/r/issues/1/comments", "-f", "body=x"), hzlib.GH_WRITE_TIMEOUT),
    (("api", "repos/o/r/statuses/x", "-F", "state=success"), hzlib.GH_WRITE_TIMEOUT),
    (("api", "repos/o/r/labels", "--field", "name=x"), hzlib.GH_WRITE_TIMEOUT),
    (("api", "repos/o/r/labels", "--raw-field=name=x"), hzlib.GH_WRITE_TIMEOUT),
    (("api", "repos/o/r/rulesets/1", "--input", "-"), hzlib.GH_WRITE_TIMEOUT),
    (("api", "graphql", "-f", "query=x", "-X", "GET"), hzlib.GH_READ_TIMEOUT),
    (("api", "repos/o/r/pulls", "--method=GET", "-F", "per_page=5"), hzlib.GH_READ_TIMEOUT),
    (("api", "repos/o/r/git/refs/x", "-XDELETE"), hzlib.GH_WRITE_TIMEOUT),
    (("api", "repos/o/r/pulls"), hzlib.GH_READ_TIMEOUT),
    (("pr", "view", "7", "--json", "headRefOid"), hzlib.GH_READ_TIMEOUT),
    (("pr", "merge", "7"), hzlib.GH_WRITE_TIMEOUT),
])
def test_gh_api_with_a_field_and_no_method_is_a_post(args, bound):
    assert hzlib.gh_timeout(args) == bound


def test_freeze_wait_is_bounded(monkeypatch, capsys):
    head = "c" * 40
    info = {"state": "OPEN", "isDraft": False, "headRefOid": head, "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [{"name": "CI OK", "status": "COMPLETED", "conclusion": "SUCCESS"}]}
    monkeypatch.setattr(merge_train, "view", lambda repo, pr, fields: info)
    monkeypatch.setattr(hzlib, "advisory_checks", lambda repo: set())
    monkeypatch.setattr(hzlib, "tool_required_checks", lambda repo: set())
    monkeypatch.setattr(hzlib, "merge_frozen", lambda *a: True)
    monkeypatch.setattr(merge_train, "release_acceptance_waiver", lambda *a: set())
    sleeps = []
    monkeypatch.setattr(merge_train.time, "sleep", sleeps.append)
    merges = []
    monkeypatch.setattr(merge_train, "gh", lambda *a: merges.append(a) or (0, ""))
    merged, detail = merge_train.train(merge_train.CORE, "7", queue_core=True, expected_head=head)
    assert merged is False and detail.startswith("merge freeze still on after 60 min")
    assert len(sleeps) == merge_train.FREEZE_POLLS and not [m for m in merges if m[:2] == ("pr", "merge")]


def test_ruleset_put_is_bounded(monkeypatch, capsys):
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(merge_gate.edge, "run", run)
    rules = {"id": 1, "name": "n", "rules": [{"type": "required_status_checks",
                                              "parameters": {"required_status_checks": [{"context": "CI OK"}]}}]}
    assert merge_gate.write_ruleset("r", rules, {"CI OK"}) is False
    assert seen["timeout"] == hzlib.GH_WRITE_TIMEOUT
    assert "timed out" in capsys.readouterr().err


def test_flakes_read_gh_is_bounded(monkeypatch, capsys):
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(flakes_read.edge, "run", run)
    assert flakes_read.gh("run", "view", "1", "--log-failed") == ""
    assert seen["timeout"] == hzlib.GH_READ_TIMEOUT
    assert "timed out" in capsys.readouterr().err


def test_ship_in_flight_count_is_bounded(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    class Module:
        StateUnreadable = RuntimeError

        @staticmethod
        def load():
            return {"items": []}

    monkeypatch.setattr(ship_mod, "_queue", lambda: (SimpleNamespace(_queue=lambda: Module), None))
    monkeypatch.setattr(hzlib, "_gh_process", lambda *args, timeout=None: run(["gh", *args], timeout=timeout))
    guard = SimpleNamespace(main=None)
    monkeypatch.setattr(hzlib, "load", lambda name: guard)
    monkeypatch.setattr(ship_mod.deps, "captured", lambda main, argv: SimpleNamespace(stdout="FLAG x\n", stderr=""))
    done = ship_mod.ship("7")
    assert seen["timeout"] == hzlib.GH_READ_TIMEOUT
    assert done.returncode == 2 and "in flight" not in done.stdout
