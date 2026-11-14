"""gtco: `sm queue hold --reason` freezes the merge queue (no tick merges anything), `sm head pr --live` and
`sm head ship` refuse with the reason, `sm queue why` names it, and `sm queue unhold` lifts it."""

import json

import pytest
from test_native_queue_commands import HEAD, MAIN, gh, items, nothing_real, queue_one, rt  # noqa: F401 - fixtures

from steuermaschine.engine import Refuse
from steuermaschine.testing import invoke
from steuermaschine_heinzel.native import queue as area
from steuermaschine_heinzel.native import queue_hold

REASON = "release 0.9.42 blocker: fix/clock first"


def test_hold_and_unhold_in_text_and_json(rt):  # noqa: F811
    assert invoke(rt, ["queue", "unhold"]).stdout == "the merge queue was not held\n"
    done = invoke(rt, ["--format", "json", "queue", "hold", "--reason", REASON])
    changed = queue_hold.QueueHoldChanged.model_validate(json.loads(done.stdout))
    assert done.exit_code == 0 and changed.held and changed.hold.reason == REASON and changed.before is None
    again = invoke(rt, ["queue", "hold", "--reason", "another"]).stdout
    assert f"(it replaced the hold: {REASON})" in again and "sm queue unhold" in again
    assert queue_hold.current(rt).reason == "another"
    lifted = invoke(rt, ["queue", "unhold"]).stdout
    assert "the merge queue hold is lifted (it was: another)" in lifted and queue_hold.current(rt) is None
    assert invoke(rt, ["queue", "hold", "--reason", "  "]).exit_code == 2


def test_a_held_queue_runs_no_tick_and_merges_nothing_until_unhold(rt, gh):  # noqa: F811
    queue_one(rt, bead="abc")
    gh.on("pr merge 801", "").on("state,mergeCommit,mergedAt", {"state": "MERGED", "mergeCommit": {"oid": MAIN}, "mergedAt": "2026-11-12T21:00:00Z"})
    gh.on("--json mergeCommit", {"mergeCommit": {"oid": MAIN}})
    gh.on("show heinzel-management-abc", {"labels": []}).on("update heinzel-management-abc", "").on("label add heinzel-management-abc", "")
    invoke(rt, ["queue", "hold", "--reason", REASON])
    out = invoke(rt, ["--format", "json", "daemon", "queue"])
    tick = area.QueueTick.model_validate(json.loads(out.stdout))
    assert out.exit_code == 0 and not tick.ran and tick.held == REASON and tick.queued[-1] == "801"
    assert any(f"queue held, no tick: {REASON}" in line for line in tick.lines)
    assert not [line for line in gh.lines() if line.startswith("pr merge")] and items(rt)[-1]["status"] == "queued"
    why = invoke(rt, ["--format", "json", "queue", "why"])
    doc = area.QueueWhy.model_validate(json.loads(why.stdout))
    assert doc.held == REASON and doc.verdict == f"held: {REASON}" and doc.lines[0].startswith("queue HOLD: the merge queue is held since")
    invoke(rt, ["queue", "unhold"])
    area.reset()
    merged = area.QueueTick.model_validate(json.loads(invoke(rt, ["--format", "json", "daemon", "queue"]).stdout))
    assert merged.ran and merged.held == "" and [line for line in gh.lines() if line.startswith("pr merge")] == [
        f"pr merge 801 -R helmut-hoffer-von-ankershoffen/heinzel --squash --match-head-commit {HEAD}"]


def test_ship_and_a_live_pr_refuse_with_the_reason_while_held(rt, gh):  # noqa: F811
    invoke(rt, ["queue", "hold", "--reason", REASON])
    ship = invoke(rt, ["head", "ship", "801"])
    assert ship.exit_code == 1 and "sm head ship 801 refused: the merge queue is held since" in ship.stderr and REASON in ship.stderr
    pr = invoke(rt, ["head", "pr", "abc", "fix: x", "--live"])
    assert pr.exit_code == 1 and "sm head pr abc --live refused" in pr.stderr and REASON in pr.stderr
    assert not gh.writes() and not [call for call in gh.calls if "push" in call]


def test_hold_and_unhold_share_one_open_review_file():
    for fn in (queue_hold.queue_hold, queue_hold.queue_unhold):
        cmd = fn.__sm_native__
        assert cmd.writes and cmd.review_file == "queue-hold.md"


def test_an_excepted_pr_ships_and_merges_alone_while_the_rest_stays_held(rt, gh):  # noqa: F811
    """S8: `--except PR` is the way through a hold for the critical head, not a manual `gh pr merge`."""
    assert invoke(rt, ["queue", "hold", "--reason", REASON, "--except", "x1"]).exit_code == 2
    invoke(rt, ["queue", "hold", "--reason", REASON, "--except", "802"])
    hold = queue_hold.current(rt)
    core = "helmut-hoffer-von-ankershoffen/heinzel"
    assert hold.excepted == [f"{core}#802"] and f"(except {core}#802)" in hold.describe()
    assert not hold.lets([]) and hold.lets([f"{core}#802"]) and not hold.lets([f"{core}#802", f"{core}#801"])
    # S20: the number alone is not the PR: 802 of another repository is neither let through nor merged
    assert not hold.lets([f"{core}#802", "o/plugins#802"])
    assert queue_hold.queued_key({"key": "802", "pr": "802", "repo": "o/plugins"}) == "o/plugins#802"
    assert queue_hold.queued_key({"key": "x"}) == ""
    queue_hold.refuse_while_held("sm head ship 802", rt, pr="802")  # this repository's 802 passes
    with pytest.raises(Refuse, match="refused"):
        queue_hold.refuse_while_held("ship", rt, pr="802", repo="o/plugins")
    assert invoke(rt, ["queue", "hold", "--reason", REASON, "--except", "o/plugins#9", "--except", "#7"]).exit_code == 0
    assert queue_hold.current(rt).excepted == ["o/plugins#9", f"{core}#7"]
    invoke(rt, ["queue", "hold", "--reason", REASON, "--except", "802"])
    other = invoke(rt, ["head", "ship", "801"])
    assert other.exit_code == 1 and "sm head ship 801 refused" in other.stderr
    queue_one(rt, bead="abc")  # 801 is queued and not excepted: no tick
    out = invoke(rt, ["--format", "json", "daemon", "queue"])
    tick = area.QueueTick.model_validate(json.loads(out.stdout))
    assert not tick.ran and tick.held == REASON and not [line for line in gh.lines() if line.startswith("pr merge")]
    area.reset()
    invoke(rt, ["queue", "hold", "--reason", REASON, "--except", "801"])  # now the queued PR is the excepted one: the tick runs
    gh.on("pr merge 801", "").on("state,mergeCommit,mergedAt", {"state": "MERGED", "mergeCommit": {"oid": MAIN}, "mergedAt": "2026-11-12T21:00:00Z"})
    gh.on("--json mergeCommit", {"mergeCommit": {"oid": MAIN}})
    gh.on("show heinzel-management-abc", {"labels": []}).on("update heinzel-management-abc", "").on("label add heinzel-management-abc", "")
    ran = area.QueueTick.model_validate(json.loads(invoke(rt, ["--format", "json", "daemon", "queue"]).stdout))
    assert ran.ran and any("only excepted PRs are queued" in line for line in ran.lines)
    assert [line for line in gh.lines() if line.startswith("pr merge")] == [
        f"pr merge 801 -R helmut-hoffer-von-ankershoffen/heinzel --squash --match-head-commit {HEAD}"]
