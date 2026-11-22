"""New, not moved: 8i4oi with auto-merge. The release cut switches GitHub auto-merge off on every Core PR right after it
takes the freeze and before any gate reads main; a PR that stays armed stops the cut (review of sm #70)."""

import json

import pytest

RELEASE = "hz-" + "release"
ARMED = {"mergeMethod": "SQUASH"}


def scripted(rel, monkeypatch, order):
    monkeypatch.setattr(rel, "next_version", lambda request: ("0.9.16", "patch"))
    monkeypatch.setattr(rel, "has_tag", lambda repo, tag: False)
    real_step = rel.step

    def step(name, *a, **kw):
        order.append(name)
        if name == "freeze":
            return real_step(name, *a, **kw)
        raise rel.Stop(f"stopped at {name}")  # the test ends at the first step after the freeze

    monkeypatch.setattr(rel, "step", step)


def test_arm_then_cut_disarms_before_the_main_gate(tool, home, monkeypatch, real_disarm):
    rel = tool(RELEASE)
    order, calls = [], []
    lists = iter([[{"number": 801, "autoMergeRequest": ARMED}, {"number": 802, "autoMergeRequest": None}],
                  [{"number": 801, "autoMergeRequest": None}, {"number": 802, "autoMergeRequest": None}]])

    def gh(*a):
        calls.append(a)
        if a[:2] == ("pr", "list"):
            order.append("pr list")
            return 0, json.dumps(next(lists))
        if a[:2] == ("pr", "merge"):
            order.append(f"pr merge {a[2]} {a[-1]}")
            return 0, ""
        return 1, ""

    monkeypatch.setattr(real_disarm.hzlib, "gh", gh)
    scripted(rel, monkeypatch, order)
    with pytest.raises(rel.Stop, match="stopped at main tip green"):
        rel.release_core("0.9.16", "", 0)
    assert order == ["freeze", "pr list", "pr merge 801 --disable-auto", "pr list", "main tip green"]
    assert not [c for c in calls if c[:2] == ("pr", "merge") and c[2] == "802"]


@pytest.mark.parametrize("second", [[{"number": 801, "autoMergeRequest": ARMED}], None])
def test_the_cut_stops_while_a_core_pr_stays_armed_or_the_list_cannot_be_read(tool, home, monkeypatch, real_disarm, second):
    rel = tool(RELEASE)
    order = []
    lists = iter([[{"number": 801, "autoMergeRequest": ARMED}], second])

    def gh(*a):
        if a[:2] == ("pr", "list"):
            answer = next(lists)
            return (0, json.dumps(answer)) if answer is not None else (1, "")
        return 0, ""

    monkeypatch.setattr(real_disarm.hzlib, "gh", gh)
    scripted(rel, monkeypatch, order)
    with pytest.raises(rel.Stop, match=r"auto-merge is still on for Core PR\(s\) .* nothing proceeds"):
        rel.release_core("0.9.16", "", 0)
    assert order == ["freeze"]  # no gate, no release PR


def test_a_resumed_hold_disarms_before_any_other_remote_step(tool, home, monkeypatch):
    rel = tool(RELEASE)
    order = []
    rel.remember(version="0.9.16", freeze_token="a" * 32,
                 hold_events=[{"action": "pause", "version": "0.9.16", "token": "a" * 32, "at": "2026-11-22T10:00:00Z"}])
    monkeypatch.setattr(rel, "acquire_snapshot_freeze", lambda version: order.append("hold"))
    monkeypatch.setattr(rel, "disarm_core_auto_merge", lambda: order.append("disarm"))
    monkeypatch.setattr(rel, "pause_request", lambda events: order.append("pause request") or None)
    with pytest.raises(rel.Stop, match="no durable pause request"):
        rel.resume_paused_hold()
    assert order == ["hold", "disarm", "pause request"]


def test_a_resumed_hold_stops_while_a_core_pr_stays_armed(tool, home, monkeypatch, real_disarm):
    rel = tool(RELEASE)
    rel.remember(version="0.9.16", freeze_token="a" * 32,
                 hold_events=[{"action": "pause", "version": "0.9.16", "token": "a" * 32, "at": "2026-11-22T10:00:00Z"}])
    monkeypatch.setattr(rel, "acquire_snapshot_freeze", lambda version: None)
    monkeypatch.setattr(rel, "pause_request", lambda events: pytest.fail("the resume went on with an armed PR"))
    monkeypatch.setattr(real_disarm.hzlib, "gh", lambda *a: (0, json.dumps([{"number": 801, "autoMergeRequest": ARMED}])) if a[:2] == ("pr", "list") else (0, ""))
    with pytest.raises(rel.Stop, match=r"auto-merge is still on for Core PR\(s\) #801"):
        rel.resume_paused_hold()


def test_the_release_pr_merge_disarms_once_more_first(tool, home, monkeypatch):
    """A tick that armed a PR after the cut's disarm is caught right before the release PR merges."""
    rel = tool(RELEASE)
    order = []
    monkeypatch.setattr(rel, "release_pr", lambda tag: order.append("release pr") or {"number": 900, "state": "OPEN"})
    monkeypatch.setattr(rel, "disarm_core_auto_merge", lambda: order.append("disarm") or (_ for _ in ()).throw(rel.Stop("armed")))
    with pytest.raises(rel.Stop, match="armed"):
        rel.merge_release_pr("v0.9.16")
    assert order == ["release pr", "disarm"]
