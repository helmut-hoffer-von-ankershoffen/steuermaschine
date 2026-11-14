"""Core ADR 0149 items e and f: `sm head validate` takes the merge-tier lane names, a full-set head has no focused lane to
validate, and a whole browser e2e suite lane covers every web journey in preflight."""

import json

import pytest

from steuermaschine.engine import Refuse
from steuermaschine_heinzel.native import window
from steuermaschine_heinzel.native.preflight_lib import head


def test_lane_names_of_the_merge_tier_map_to_the_hosted_suites():
    assert window.lanes_of("e2e-web-1,web-1,e2e-web-2,,cli") == ["web-1", "web-2", "cli"]
    for lane in ("desktop", "consumers", "managed"):
        with pytest.raises(Refuse, match="hosted PR CI is the validation") as stop:
            window.lanes_of(f"web-1,{lane}")
        assert stop.value.code == 2


def test_a_whole_browser_suite_lane_covers_every_web_journey():
    owners = {"tests/e2e/web_ui_test.py": ["x"], "tests/e2e/chat_test.py": ["y"]}
    assert head.missing_journeys(owners, {"browser": ["tests/e2e/chat_test.py"]}, {}) == ["tests/e2e/web_ui_test.py"]
    assert head.missing_journeys(owners, {"e2e-web-2": ["tests/e2e/web-2"]}, {}) == []
    assert head.missing_journeys(owners, {"e2e-web-1": ["tests/e2e/web-2"]}, {}) == list(owners)


@pytest.mark.parametrize(("result", "expected"), [
    ((0, json.dumps({"files": ["Makefile"], "lanes": None, "full": True, "reason": "the selector selects the full set"})),
     "the selector selects the full set"),
    ((0, json.dumps({"full": True})), "the selector selects the full set"),
    ((0, json.dumps({"files": ["web/src/a.ts"], "lanes": {"web": ["web/ALL"]}, "full": False, "reason": "focused"})), None),
    ((1, "KeyError: GITHUB_EVENT_NAME"), None),  # an older Core: no --suggest
    ((0, "not json"), None),
])
def test_full_set_reads_the_checkouts_own_suggestion(tmp_path, result, expected):
    calls = []

    def sh(argv, cwd=None, input=None):
        calls.append((argv, cwd))
        return result

    assert head.full_set(head.Ctx(wt=tmp_path, sh=sh, base_sha="b" * 40)) == expected
    assert calls == [(["python3", "scripts/focused_plan.py", "--suggest", "b" * 40], tmp_path)]
