"""`sm ship`: the derived inputs of `head admit`, the hand-over to the pipeline, and `--wait`. Fake providers only."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_pipeline_cov_head_admit import BRANCH, HEAD, native, runtime

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


def select(**fields: Any) -> Any:
    base = dict(full=False, pytest=["tests/a_test.py"], vitest=["web/b.test.ts"], e2e_lanes=[], exit_code=0)
    return lambda **_kw: SimpleNamespace(**{**base, **fields}, render=lambda: ["select"])


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
def test_every_input_is_derived_and_the_selection_is_stored_per_branch(tmp_path):
    rt = runtime(tmp_path)
    common.OVERRIDE["head select"] = select()
    with native(rt):
        got = ship_driver.derive(worktree(tmp_path), BRANCH, HEAD, {})
    assert set(got) == {"core_repo", "review_root", "sel"}
    assert got["core_repo"] == str(tmp_path / "core")  # the setting, never a default path
    sel = Path(got["sel"])
    assert sel.name == f"fix-update-all-{HEAD[:12]}.json" and json.loads(sel.read_text()) == {"python": ["tests/a_test.py"], "web": ["web/b.test.ts"]}
    assert Path(got["review_root"]).name == "reviews"


def test_a_given_or_remembered_input_is_not_derived(tmp_path):
    rt = runtime(tmp_path)
    common.OVERRIDE["head select"] = lambda **_kw: pytest.fail("the selection was given: head select must not run")
    sel = tmp_path / "mine.json"
    sel.write_text("{}")
    with native(rt):
        got = ship_driver.derive(worktree(tmp_path), BRANCH, HEAD, {"core_repo": "/x", "review_root": "/y", "sel": str(sel)})
    assert got == {}


def test_a_remembered_selection_whose_file_is_gone_is_derived_again(tmp_path):
    rt = runtime(tmp_path)
    common.OVERRIDE["head select"] = select()
    with native(rt):
        got = ship_driver.derive(worktree(tmp_path), BRANCH, HEAD, {"core_repo": "/x", "review_root": "/y", "sel": str(tmp_path / "gone.json")})
    assert set(got) == {"sel"}


def test_an_unset_core_repo_refuses_naming_the_setting(tmp_path):
    rt = runtime(tmp_path)
    rt.settings["core_repo_dir"] = None
    with native(rt), pytest.raises(Refuse, match="core_repo_dir"):
        ship_driver.derive(worktree(tmp_path), BRANCH, HEAD, {})


@pytest.mark.parametrize("found,why", [(dict(full=True), "full suite"), (dict(pytest=[], vitest=[]), "names no test"),
                                       (dict(exit_code=2), "head select failed"),
                                       (dict(e2e_lanes=["web-1"]), "browser journeys")])
def test_a_selection_that_cannot_be_narrowed_refuses(tmp_path, found, why):
    rt = runtime(tmp_path)
    common.OVERRIDE["head select"] = select(**found)
    with native(rt), pytest.raises(Refuse, match=why):
        ship_driver.derive(worktree(tmp_path), BRANCH, HEAD, {"core_repo": "/x"})


# ----------------------------------------------------------------------------- the hand-over
def drive(tmp_path, monkeypatch, set_=None, fake=None, **options):
    rt = runtime(tmp_path)
    common.OVERRIDE["head select"] = select()
    fake = fake or FakeEngine()
    monkeypatch.setattr(ship_driver, "engine", lambda: fake)
    with native(rt):
        return fake, ship_driver.ship(worktree(tmp_path), live=options.pop("live", False), approved="", author="", set_=set_, wait=options.pop("wait", None))


def test_the_driver_passes_the_derived_inputs_and_the_given_ones_unchanged(tmp_path, monkeypatch):
    fake, result = drive(tmp_path, monkeypatch, set_=["bead=abc", "title=fix: x"], live=True)
    (pl, arg, opts), = fake.calls
    assert pl.id == "head.admit" and arg == worktree(tmp_path) and opts.live and not opts.plan
    assert set(opts.inputs) == {"core_repo", "review_root", "sel", "bead", "title"}
    assert opts.inputs["bead"] == "abc" and opts.inputs["core_repo"] == str(tmp_path / "core")
    assert result.derived == ["core_repo", "review_root", "sel"] and result.exit_code == 0 and not result.merged


def test_a_set_input_replaces_the_derived_one(tmp_path, monkeypatch):
    fake, _ = drive(tmp_path, monkeypatch, set_=["review_root=/mine", "core_repo=/core2"])
    assert fake.calls[0][2].inputs["review_root"] == "/mine" and fake.calls[0][2].inputs["core_repo"] == "/core2"


def test_an_unknown_input_name_is_refused(tmp_path, monkeypatch):
    with pytest.raises(Refuse, match="NAME one of"):
        drive(tmp_path, monkeypatch, set_=["nope=1"])


def test_a_stop_of_the_pipeline_is_returned_as_it_is_and_never_waits(tmp_path, monkeypatch):
    stop = Report("t", "head.admit", HEAD, "x", "review", "packet review", next="sm ship WT", exit_code=0)
    _, result = drive(tmp_path, monkeypatch, fake=FakeEngine(stop), wait=5)
    assert result.status == "review" and result.next == "sm ship WT" and not result.merged


def test_the_pipeline_review_gate_is_checked_first(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    monkeypatch.setattr(ship_driver, "gate_problem", lambda cmd, project: "`sm head admit` writes and its independent review is open")
    fake = FakeEngine()
    monkeypatch.setattr(ship_driver, "engine", lambda: fake)
    with native(rt), pytest.raises(Refuse, match="nothing was run"):
        ship_driver.ship(worktree(tmp_path), live=False, approved="", author="", set_=None, wait=None)
    assert not fake.calls


# ----------------------------------------------------------------------------- --wait
def waiting(tmp_path, monkeypatch, view, listing, minutes=10):
    rt = runtime(tmp_path, gh=FakeGh().on("pr view 7", view))
    common.OVERRIDE["queue list"] = listing
    monkeypatch.setattr(ship_driver, "engine", lambda: FakeEngine())
    common.OVERRIDE["head select"] = select()
    with native(rt):
        rt.providers.store.save("runs.head.admit", HEAD, {"data": {"pr": 7}, "inputs": {}})
        return rt, ship_driver.ship(worktree(tmp_path), live=True, approved="", author="", set_=None, wait=minutes)


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
    assert ship_driver.ship.__sm_native__.writes and ship_driver.ship.__sm_native__.review == "ship.md"


def test_a_new_head_of_the_same_branch_does_not_rewrite_the_remembered_selection(tmp_path):
    rt = runtime(tmp_path)
    common.OVERRIDE["head select"] = select()
    with native(rt):
        first = ship_driver.selection(Path(worktree(tmp_path)), "fix/a-b", HEAD)
        other = ship_driver.selection(Path(worktree(tmp_path)), "fix-a/b", HEAD)
        later = ship_driver.selection(Path(worktree(tmp_path)), "fix/a-b", "f" * 40)
    assert first != later and first.name.endswith(f"{HEAD[:12]}.json") and other.name.endswith(f"{HEAD[:12]}.json")  # the head is part of the name


def test_wait_with_a_finished_run_and_no_pr_refuses(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    common.OVERRIDE["head select"] = select()
    monkeypatch.setattr(ship_driver, "engine", lambda: FakeEngine())
    with native(rt), pytest.raises(Refuse, match="no PR is recorded"):
        ship_driver.ship(worktree(tmp_path), live=True, approved="", author="", set_=None, wait=5)


def test_an_unreadable_github_and_no_queue_entry_is_unknown_not_dropped(tmp_path, monkeypatch):
    with pytest.raises(Wait, match=r"unknown|could not be read"):
        waiting(tmp_path, monkeypatch, 1, entries((8, "queued", "")), minutes=2)


def test_wait_ends_with_exit_75_when_a_promote_is_switching(tmp_path, monkeypatch):
    monkeypatch.setattr(ship_driver, "switch_pending", lambda commands, now: 4242)
    with pytest.raises(Wait, match=r"promote \(pid 4242\) is switching"):
        waiting(tmp_path, monkeypatch, json.dumps({"state": "OPEN"}), entries((7, "queued", "")))
