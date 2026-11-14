"""New, not moved: the queue re-checks a needs-work row at its unchanged pinned head and requeues it once its checks are
green (RCA 2026-11-13, PR #803: a re-judged merge gate went green and the queue said "nothing queued")."""

import json

import pytest
from steuermaschine_heinzel import state as statedir

PIN = "2c6e817a" + "0" * 32
CORE = "helmut-hoffer-von-ankershoffen/heinzel"


def check(name, conclusion="SUCCESS", status="COMPLETED"):
    return {"name": name, "status": status, "conclusion": conclusion, "detailsUrl": ""}


@pytest.fixture
def queue(tool, home, monkeypatch):
    from steuermaschine_heinzel.native.queue_lib import hzlib

    monkeypatch.setenv("HZ_QUEUE_STATE", str(statedir.root() / "queue.json"))
    q = tool("hz-queue")
    train = tool("hz-merge-train")
    merges = []
    monkeypatch.setattr(train, "train", lambda repo, pr, **kw: merges.append((pr, kw)) or (True, "abc12345"))
    monkeypatch.setattr(hzlib, "load", lambda name: train)
    monkeypatch.setattr(hzlib, "note", lambda text: None)
    monkeypatch.setattr(hzlib, "sleep", lambda s: None)
    monkeypatch.setattr(hzlib, "advisory_checks", lambda repo: {"review"})
    monkeypatch.setattr(q, "main_state", lambda: "green")
    q.merges, q.lib = merges, hzlib
    return q


def needs_work(q, detail="checks merge-gate; no failing test found in the log", pin=PIN, **extra):
    item = {"key": "803", "repo": CORE, "pr": "803", "status": "needs work", "detail": detail, "after": [], "reviewed_head": pin, "noted": "needs work", **extra}
    q.save({"items": [item]})


def view(monkeypatch, q, head=PIN, gate="SUCCESS", state="OPEN", draft=False, gate_status="COMPLETED"):
    seen = []

    def ghj(*args, default=None):
        seen.append(args)
        return {"state": state, "isDraft": draft, "headRefOid": head, "mergeStateStatus": "CLEAN",
                "statusCheckRollup": [check("CI OK"), check("merge-gate", gate, gate_status), check("coverage-floor"), check("review", "FAILURE")]}

    monkeypatch.setattr(q.lib, "ghj", ghj)
    return seen


def test_a_green_needs_work_row_at_its_pinned_head_is_requeued_and_merges(queue, monkeypatch, home):
    needs_work(queue)
    view(monkeypatch, queue)
    queue.tick(now=10_000)
    item = queue.load()["items"][0]
    assert item["status"] == "merged" and [(key, {k: v for k, v in kw.items() if k != "pre_merge_guard"}) for key, kw in queue.merges] == [("803", {"queue_core": True, "expected_head": PIN})]
    assert all(callable(kw.get("pre_merge_guard")) for _, kw in queue.merges)  # the main-plan guard rides along (stale-main-plan)
    assert f"803: requeued: checks green again at the pinned head {PIN[:8]}" in (statedir.root() / "queue.log").read_text()


@pytest.mark.parametrize(("change", "row"), [
    ({"gate": "FAILURE"}, {}),
    ({"gate": "", "gate_status": "IN_PROGRESS"}, {}),  # still judging
    ({"head": "f" * 40}, {}),
    ({"state": "CLOSED"}, {}),
    ({"draft": True}, {}),
    ({}, {"pin": None}),
    ({}, {"detail": "merge conflicts"}),
    ({}, {"detail": "reviewed head changed; independent review and requeue required"}),
])
def test_a_row_stays_needs_work_unless_its_pin_holds_and_every_check_is_green(queue, monkeypatch, change, row):
    needs_work(queue, **row)
    view(monkeypatch, queue, **change)
    queue.tick(now=10_000)
    item = queue.load()["items"][0]
    assert item["status"] == "needs work" and not queue.merges
    assert json.dumps(item).count("requeued") == 0


def test_a_failing_recheck_never_stops_the_tick(queue, monkeypatch, home):
    needs_work(queue)

    def broken(*args, default=None):
        raise RuntimeError("gh down")

    monkeypatch.setattr(queue.lib, "ghj", broken)
    queue.tick(now=10_000)
    assert "needs-work re-check failed: RuntimeError('gh down')" in (statedir.root() / "queue.log").read_text()
    assert queue.load()["items"][0]["status"] == "needs work"
