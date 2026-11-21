"""`sm ship`: the derived inputs of `head admit`, the hand-over to the pipeline, and `--wait`. Fake providers only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_pipeline_head_admit import HEAD, native, runtime

from steuermaschine.engine import Options, Refuse, Report, Wait
from steuermaschine.github.testing import FakeGh
from steuermaschine.helpdoc import disorder, missing
from steuermaschine_heinzel.native import ship_driver
from steuermaschine_heinzel.native.queue import QueueEntry, QueueList
from steuermaschine_heinzel.pipelines import common


@pytest.fixture(autouse=True)
def clean_overrides():
    common.OVERRIDE.clear()
    yield
    common.OVERRIDE.clear()


def entries(*rows: tuple[int, str, str]) -> Any:
    return lambda **_kw: QueueList(entries=[QueueEntry(key=str(n), repo="core", pr=str(n), status=s, detail=d) for n, s, d in rows])


class FakeEngine:
    def __init__(self, report: Report | None = None):
        self.calls: list[tuple[Any, str, Options]] = []
        self.report = report or Report("t", "head.admit", HEAD, "x", "finished", "done")

    def advance(self, pl: Any, arg: str, opts: Options) -> Report:
        self.calls.append((pl, arg, opts))
        return self.report


def worktree(tmp_path: Path) -> str:
    (tmp_path / "wt").mkdir(exist_ok=True)
    return str(tmp_path / "wt")


# ----------------------------------------------------------------------------- derivation
# ----------------------------------------------------------------------------- the hand-over
def drive(tmp_path, monkeypatch, set_=None, fake=None, **options):
    rt = runtime(tmp_path)
    fake = fake or FakeEngine()
    monkeypatch.setattr(ship_driver, "engine", lambda: fake)
    with native(rt):
        return fake, ship_driver.ship(worktree(tmp_path), live=options.pop("live", False), set_=set_, wait=options.pop("wait", None))


def test_the_driver_hands_the_given_inputs_to_the_pipeline_unchanged(tmp_path, monkeypatch):
    fake, result = drive(tmp_path, monkeypatch, set_=["bead=abc", "title=fix: x"], live=True)
    (pl, arg, opts), = fake.calls
    assert pl.id == "head.admit" and arg == worktree(tmp_path) and opts.live
    assert opts.inputs == {"bead": "abc", "title": "fix: x"} and result.exit_code == 0 and not result.merged


def test_an_unknown_input_name_is_refused(tmp_path, monkeypatch):
    with pytest.raises(Refuse, match="NAME one of"):
        drive(tmp_path, monkeypatch, set_=["nope=1"])


def test_a_stop_of_the_pipeline_is_returned_as_it_is_and_never_waits(tmp_path, monkeypatch):
    stop = Report("t", "head.admit", HEAD, "x", "review", "protected review", next="sm ship WT", exit_code=0)
    _, result = drive(tmp_path, monkeypatch, fake=FakeEngine(stop), wait=5)
    assert result.status == "review" and result.next == "sm ship WT" and not result.merged


# ----------------------------------------------------------------------------- --wait
def waiting(tmp_path, monkeypatch, view, listing, minutes=10):
    rt = runtime(tmp_path, gh=FakeGh().on("pr view 7", view))
    common.OVERRIDE["queue list"] = listing
    monkeypatch.setattr(ship_driver, "engine", lambda: FakeEngine())
    with native(rt):
        rt.providers.store.save("runs.head.admit", HEAD, {"data": {"pr": 7}, "inputs": {}})
        return rt, ship_driver.ship(worktree(tmp_path), live=True, set_=None, wait=minutes)


def test_wait_ends_zero_when_the_pr_merged(tmp_path, monkeypatch):
    _rt, result = waiting(tmp_path, monkeypatch, json.dumps({"state": "MERGED"}), entries((7, "queued", "")))
    assert result.merged and result.pr == 7 and result.exit_code == 0 and "MERGED" in "\n".join(result.render())


def test_wait_polls_the_queue_until_it_merges(tmp_path, monkeypatch):
    seen = iter(["queued", "queued", "merged"])
    rt, result = waiting(tmp_path, monkeypatch, json.dumps({"state": "OPEN"}), lambda **_kw: entries((7, next(seen), ""))())
    assert result.merged and rt.providers.clock.slept == [ship_driver.POLL_SECONDS] * 2


def test_wait_reaches_its_ceiling_with_exit_75(tmp_path, monkeypatch):
    with pytest.raises(Wait, match="not merged after 3 minutes"):
        waiting(tmp_path, monkeypatch, json.dumps({"state": "OPEN"}), entries((7, "queued", "")), minutes=3)


def test_wait_refuses_with_the_reason_for_a_closed_pr(tmp_path, monkeypatch):
    with pytest.raises(Refuse, match="closed without a merge") as caught:
        waiting(tmp_path, monkeypatch, json.dumps({"state": "CLOSED"}), entries((7, "queued", "")))
    assert caught.value.code == 1


@pytest.mark.parametrize("rows,why", [((7, "needs work", "checks red"), "needs work .checks red."), ((8, "queued", ""), "not in the queue any more")])
def test_wait_refuses_when_the_queue_dropped_it(tmp_path, monkeypatch, rows, why):
    with pytest.raises(Refuse, match=why):
        waiting(tmp_path, monkeypatch, json.dumps({"state": "OPEN"}), entries(rows))


def test_an_unreadable_pr_falls_back_to_the_queue(tmp_path, monkeypatch):
    _, result = waiting(tmp_path, monkeypatch, 1, entries((7, "merged", "")))
    assert result.merged


def test_a_repository_is_required_for_the_wait(tmp_path):
    rt = runtime(tmp_path)
    rt.settings["repo"] = ""
    with native(rt), pytest.raises(Refuse, match="no repository"):
        ship_driver.outcome(7)


# ----------------------------------------------------------------------------- help
def test_the_help_is_in_contract_form():
    text = ship_driver.ship.__sm_native__.help
    assert missing(text) == "" and disorder(text, "sm") == ""
    assert ship_driver.ship.__sm_native__.writes  # R3: no per-command review file any more


def test_wait_with_a_finished_run_and_no_pr_refuses(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    monkeypatch.setattr(ship_driver, "engine", lambda: FakeEngine())
    with native(rt), pytest.raises(Refuse, match="no PR is recorded"):
        ship_driver.ship(worktree(tmp_path), live=True, set_=None, wait=5)


def test_an_unreadable_github_and_no_queue_entry_is_unknown_not_dropped(tmp_path, monkeypatch):
    with pytest.raises(Wait, match=r"unknown|could not be read"):
        waiting(tmp_path, monkeypatch, 1, entries((8, "queued", "")), minutes=2)


def test_wait_ends_with_exit_75_when_a_promote_is_switching(tmp_path, monkeypatch):
    monkeypatch.setattr(ship_driver, "switch_pending", lambda commands, now: 4242)
    with pytest.raises(Wait, match=r"promote \(pid 4242\) is switching"):
        waiting(tmp_path, monkeypatch, json.dumps({"state": "OPEN"}), entries((7, "queued", "")))
