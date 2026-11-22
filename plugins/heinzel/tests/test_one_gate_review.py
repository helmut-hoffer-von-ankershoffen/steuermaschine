"""One blocking review for a gate-touching change (owner/SDLC 2026-11-22): CI's `Gate review`, where main's ruleset requires it.

There `sm ship` asks for no local verdict (no protected-path stop, no --reviewed past the diff guard) and the queue merges
only with every required check, Gate review included, green on the exact head. Where the ruleset does not require it,
or cannot be read, the local review stays (the other tests of the stop and the guard cover that side)."""

from __future__ import annotations

from typing import Any

from test_native_queue_merge_rule import GREEN, RULESET, World, gap  # noqa: F401 - gap is a fixture
from test_native_ship_commands import (  # noqa: F401 - fixtures
    WEAKER,
    gh,
    keys,
    nothing_real,
    rt,
)
from test_pipeline_head_admit import HEAD, DiffCommands, context, native, runtime

from steuermaschine.testing import invoke
from steuermaschine_heinzel.native.queue_lib import ruleset
from steuermaschine_heinzel.native.ship import HeadResult
from steuermaschine_heinzel.pipelines import common, head_admit

GATE = [{"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": True, "required_status_checks": [
    {"context": "check"}, {"context": "Gate review"}]}}]
GATE_RULESET = [{**RULESET[0], "parameters": {**RULESET[0]["parameters"], "required_status_checks": [
    *RULESET[0]["parameters"]["required_status_checks"], {"context": "Gate review"}]}}, RULESET[1]]


def check(name: str, conclusion: str) -> dict[str, str]:
    return {"name": name, "status": "COMPLETED", "conclusion": conclusion}


def test_only_a_ruleset_that_requires_gate_review_lets_it_decide():
    assert ruleset.gate_review_decides(GATE) is True
    assert ruleset.gate_review_decides(RULESET) is False  # heinzel's ruleset today: the local review stays
    assert ruleset.gate_review_decides(None) is False and ruleset.gate_review_decides({"message": "Not Found"}) is False


# ----------------------------------------------------------------------------- sm ship: no local verdict
def test_a_gate_path_head_ships_without_a_local_verdict_when_gate_review_is_required(tmp_path):
    said: list[str] = []
    world = runtime(tmp_path, DiffCommands(["src/a.py", ".github/workflows/ci.yml"]).on("rules/branches/main", GATE))
    ctx = context(world, tmp_path / "wt")
    ctx.say = said.append
    with native(world):
        assert head_admit.protected_review(ctx) == ("", "", "")  # no digest, no brief: the engine records "nothing to review"
    assert said == ["gate paths changed: CI Gate review decides (.github/workflows/ci.yml)"]


def test_without_a_readable_gate_review_rule_the_protected_path_still_stops(tmp_path):
    world = runtime(tmp_path, DiffCommands([".github/workflows/ci.yml"]).on("rules/branches/main", RULESET))
    with native(world):
        digest, brief, _ = head_admit.protected_review(context(world, tmp_path / "wt"))
    assert len(digest) == 64 and ".github/workflows/ci.yml" in brief


def test_the_queue_step_queues_a_flagged_pr_without_a_reviewed_reason_when_gate_review_decides(tmp_path):
    from steuermaschine.github.testing import FakeGh

    world = runtime(tmp_path, FakeGh().on("rules/branches/main", GATE))
    added: list[dict[str, Any]] = []
    common.OVERRIDE["queue add"] = lambda **kw: added.append(kw) or HeadResult(action="add", target="812", verdict="done")
    flagged = context(world, tmp_path / "wt", {"bead": "hz-1"}, {"pr": 812, "guard": "needs_review"})
    with native(world):
        assert head_admit.queue(flagged) == f"PR #812 queued at {HEAD[:12]}"
    assert added == [{"pr": ["812"], "bead": "hz-1", "reviewed": None, "reviewed_head": None}]


def test_sm_head_ship_queues_a_flagged_pr_and_names_gate_review_when_the_ruleset_requires_it(rt, gh):  # noqa: F811
    gh.on("pr diff 801", WEAKER).on("rules/branches/main", GATE)
    out = invoke(rt, ["head", "ship", "801"])
    assert out.exit_code == 0 and "diff guard flagged 801: CI Gate review decides" in out.stdout and "queued 801" in out.stdout, out
    assert keys(rt) == ["801"]
    assert "the CI Gate review decides" in (rt.records / "AGENTS.md").read_text()
    row = [i for i in __import__("json").loads(rt.state_file.read_text())["items"] if i["key"] == "801"][-1]
    assert row["guarded_head"] == HEAD  # the merge refuses any other head


# ----------------------------------------------------------------------------- the queue: Gate review green on the exact head
def test_the_merge_rule_refuses_while_gate_review_is_missing_or_red_on_the_head(gap):  # noqa: F811
    files = ["src/heinzel/a.py", ".github/workflows/ci.yml"]
    missing = gap(World(files=files, ruleset=GATE_RULESET))
    assert "required checks not green: Gate review" in missing
    red = gap(World(files=files, ruleset=GATE_RULESET, rollup=[*GREEN, check("Gate review", "FAILURE")]))
    assert "required checks not green: Gate review" in red


def test_the_merge_rule_needs_no_local_verdict_when_gate_review_is_green_on_the_head(gap):  # noqa: F811
    files = ["src/heinzel/a.py", ".github/workflows/ci.yml"]
    assert gap(World(files=files, ruleset=GATE_RULESET, rollup=[*GREEN, check("Gate review", "SUCCESS")])) == ""
    assert "no verdict is recorded" in gap(World(files=files))  # the ruleset without Gate review: the local verdict stays
