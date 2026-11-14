"""The validation window: one head at a time, one request per branch, the cap, and the rule against a PR head's commit."""

from steuermaschine.testing import FakeClock, MemoryStore
from steuermaschine.window import Rules, Window, sha_uses, throwaway_branch

A, B, C = "a" * 40, "b" * 40, "c" * 40


def window(cap=5, alive=lambda turn: None, **rules):
    clock = FakeClock()
    return Window(MemoryStore(), clock, Rules(cap=cap, **rules), "test", alive=alive), clock


def test_the_first_head_goes_and_holds_the_window_until_it_leaves():
    w, _ = window()
    assert w.enter("fix/one", A, 2, in_flight=0).state == "go"
    assert w.enter("fix/one", A, 2, in_flight=2).state == "go"  # the holder asking again is still the holder
    second = w.enter("fix/two", B, 2, in_flight=2)
    assert second.state == "wait" and second.holder == "fix/one" and second.position == 1 and "holds the window" in second.reason
    assert w.leave("fix/one", A) is True
    assert w.enter("fix/two", B, 2, in_flight=0).state == "go"


def test_three_builders_at_the_cap_are_served_one_after_another_in_order():
    w, _ = window(cap=5)
    assert w.enter("fix/one", A, 2, in_flight=4).state == "wait"  # 4 + 2 > 5: nobody starts half its lanes
    assert w.enter("fix/two", B, 2, in_flight=4).position == 1
    assert w.enter("fix/three", C, 2, in_flight=4).position == 2
    assert w.enter("fix/two", B, 2, in_flight=0).state == "wait"  # room now, but fix/one asked first
    assert w.enter("fix/one", A, 2, in_flight=0).state == "go"
    assert [turn.branch for turn in w.status().waiting] == ["fix/two", "fix/three"]
    w.leave("fix/one", A)
    assert w.enter("fix/three", C, 2, in_flight=0).state == "wait"
    assert w.enter("fix/two", B, 2, in_flight=0).state == "go"


def test_a_newer_head_of_a_branch_takes_the_waiting_place_of_the_older_one():
    w, _ = window()
    w.enter("fix/one", A, 1, in_flight=0)
    assert w.enter("fix/two", B, 2, in_flight=1).state == "wait"
    newer = w.enter("fix/two", C, 3, in_flight=1)
    assert newer.replaced == B and newer.position == 1
    waiting = w.status().waiting
    assert len(waiting) == 1 and waiting[0].head == C and waiting[0].runs == 3
    again = w.enter("fix/one", B, 1, in_flight=1)  # a new head of the branch that holds the window waits for its own older head
    assert again.state == "wait" and "older head of this branch" in again.reason


def test_more_runs_than_the_cap_never_enter_the_line():
    w, _ = window(cap=2)
    assert w.enter("fix/one", A, 3, in_flight=0).state == "wait"
    assert w.status().waiting == []


def test_a_holder_that_never_left_is_passed_after_its_lease_and_a_silent_waiter_is_dropped():
    w, clock = window(lease_minutes=10, waiting_minutes=5)
    w.enter("fix/one", A, 1, in_flight=0)
    w.enter("fix/two", B, 1, in_flight=1)
    clock.sleep(6 * 60)
    assert w.status().waiting == []  # fix/two stopped asking
    assert w.enter("fix/three", C, 1, in_flight=1).state == "wait"
    clock.sleep(5 * 60)
    assert w.status().active is None
    assert w.enter("fix/three", C, 1, in_flight=0).state == "go"


def test_leaving_the_line_gives_up_the_place_and_status_renders():
    w, _ = window()
    w.enter("fix/one", A, 1, in_flight=0)
    w.enter("fix/two", B, 1, in_flight=1)
    assert w.leave("fix/two", B) is False
    text = "\n".join(w.status().render())
    assert "active   fix/one aaaaaaaaa" in text and "waiting  " not in text and "cap 5" in text
    assert w.enter("x", C, 1, 0).render() == ["wait: fix/one aaaaaaaaa holds the window"]


def test_sha_uses_names_every_other_use_of_a_commit():
    assert sha_uses(A, "validation/x-aaaaaaaa", [], set(), ["validation/x-aaaaaaaa"]) == []
    reasons = sha_uses(A, "fix/x", [(7, A), (8, B)], {f"pull_request:{A}"}, ["fix/x", "other"])
    assert reasons[0] == "it is the head of open PR #7" and "pull_request:aaaaaaaa" in reasons[1]
    assert reasons[2:] == ["it is the tip of the non-validation branch fix/x", "it is the tip of the non-validation branch other"]
    assert throwaway_branch("fix/e2e details", A) == "validation/fix-e2e-details-aaaaaaaa"
    assert throwaway_branch("validation/x", A) == "validation/x-aaaaaaaa"


def test_a_second_command_for_the_same_head_waits_and_cannot_end_the_first_ones_turn():
    w, _ = window()
    assert w.enter("fix/one", A, 2, 0, owner="first", pid=11, host="here").state == "go"
    second = w.enter("fix/one", A, 2, 2, owner="second", pid=12, host="here")
    assert second.state == "wait" and "already being validated by another command (pid 11 on here)" in second.reason
    assert w.leave("fix/one", A, owner="second") is False  # it gives up its own place only
    assert w.status().active.owner == "first" and w.status().waiting == []
    assert w.enter("fix/two", B, 1, 2, owner="third").state == "wait"  # the window is still held
    assert w.enter("fix/one", A, 2, 2, owner="first").state == "go"  # the owner asking again is still the holder
    assert w.leave("fix/one", A, owner="first") is True


def test_a_holder_whose_command_is_gone_is_passed_and_names_what_it_left_behind():
    dead = set()
    w, _ = window(alive=lambda turn: turn.owner not in dead)
    w.enter("fix/one", A, 2, 0, owner="first", pid=11, host="here")
    w.note("first", {"validation_branch": "validation/fix-one-aaaaaaaa", "commit": C})
    w.note("someone-else", {"x": "y"})  # only the holder writes its notes
    assert w.enter("fix/two", B, 1, 2, owner="second").state == "wait"
    assert w.status().holder_alive is True
    dead.add("first")
    assert "ITS COMMAND IS GONE" in "\n".join(w.status().render())
    taken = w.enter("fix/two", B, 1, 0, owner="second")
    assert taken.state == "go" and taken.passed.branch == "fix/one" and taken.passed.data == {"validation_branch": "validation/fix-one-aaaaaaaa", "commit": C}


def test_release_ends_a_dead_or_expired_turn_and_a_live_one_only_with_force():
    state = {"alive": True}
    w, clock = window(alive=lambda turn: state["alive"], lease_minutes=10)
    assert w.release().reason == "nobody holds the window"
    w.enter("fix/one", A, 1, 0, owner="first", pid=11, host="here")
    kept = w.release()
    assert kept.released is False and "its command still runs (pid 11 on here)" in kept.reason and w.status().active is not None
    state["alive"] = None
    assert "cannot be checked from here" in w.release().reason
    forced = w.release(force=True)
    assert forced.released is True and "(forced)" in forced.reason and forced.turn.branch == "fix/one" and w.status().active is None
    w.enter("fix/one", A, 1, 0, owner="first")
    state["alive"] = False
    assert "its command is gone" in w.release().reason
    w.enter("fix/one", A, 1, 0, owner="first")
    state["alive"] = True
    clock.sleep(11 * 60)
    assert "its lease ran out" in w.release().reason and w.release().render() == ["nobody holds the window"]


def test_process_alive_knows_this_machine_only():
    import os
    import socket

    from steuermaschine.window import Turn, process_alive

    here = Turn(branch="b", head=A, runs=1, requested=0, asked=0, pid=os.getpid(), host=socket.gethostname())
    assert process_alive(here) is True
    assert process_alive(here.model_copy(update={"pid": 999_999_999})) is False
    assert process_alive(here.model_copy(update={"host": "elsewhere"})) is None and process_alive(here.model_copy(update={"pid": 0})) is None
    assert process_alive(here.model_copy(update={"pid": 1})) is True  # exists, not ours


# ---------------------------------------------------------------- the admission hold, dead waiters
def test_an_admission_hold_stops_a_new_validation_but_not_the_holder_that_runs():
    w, _ = window()
    assert w.enter("fix/one", A, 1, in_flight=0, owner="one").state == "go"
    w.hold("admit", "head apply P", pid=1, host="h")
    assert w.enter("fix/one", A, 1, in_flight=1, owner="one").state == "go"  # the validation that runs finishes
    assert w.enter("fix/two", B, 1, in_flight=1, owner="two").state == "wait"
    w.leave("fix/one", A, owner="one")
    blocked = w.enter("fix/two", B, 1, in_flight=0, owner="two")
    assert blocked.state == "wait" and blocked.held_by == ["head apply P"] and "admission is pending" in blocked.reason
    assert [t.branch for t in w.status().waiting] == ["fix/two"]  # it keeps its place
    assert [h.label for h in w.status().holds] == ["head apply P"] and "hold     head apply P" in "\n".join(w.status().render())
    assert w.unhold("admit") is True and w.unhold("admit") is False
    assert w.enter("fix/two", B, 1, in_flight=0, owner="two").state == "go"


def test_a_hold_whose_command_is_gone_or_whose_lease_ran_out_is_ignored():
    gone = {"admit": False}
    w, clock = window(alive=lambda row: not gone.get(getattr(row, "owner", ""), False) and None, lease_minutes=60)
    w.hold("admit", "x", pid=9)
    assert w.enter("fix/one", A, 1, in_flight=0, owner="one").state == "wait"
    gone["admit"] = True
    assert w.status().holds == [] and w.enter("fix/one", A, 1, in_flight=0, owner="one").state == "go"
    w.leave("fix/one", A, owner="one")
    gone["admit"] = False
    w.hold("late", "y", pid=9)
    clock.sleep(61 * 60)
    assert w.enter("fix/two", B, 1, in_flight=0, owner="two").state == "go"


def test_holds_of_several_commands_are_kept_apart():
    w, _ = window()
    w.hold("a", "one")
    w.hold("b", "two")
    w.hold("a", "one again")  # the same owner: one hold
    assert sorted(h.label for h in w.status().holds) == ["one again", "two"]
    w.unhold("a")
    assert [h.label for h in w.status().holds] == ["two"]


def test_a_waiter_whose_pid_is_gone_is_skipped_marked_and_never_gets_the_turn():
    dead = set()
    w, _ = window(alive=lambda row: False if row.pid in dead else None)
    w.enter("fix/one", A, 1, in_flight=0, owner="one", pid=1)
    w.enter("fix/dead", B, 1, in_flight=0, owner="dead", pid=2)
    w.enter("fix/three", C, 1, in_flight=0, owner="three", pid=3)
    dead.add(2)
    status = w.status()
    assert [t.branch for t in status.waiting] == ["fix/three"] and [t.branch for t in status.gone] == ["fix/dead"]
    assert "gone" in "\n".join(status.render()) and "drop bbbbbbbbb" in "\n".join(status.render())
    w.leave("fix/one", A, owner="one")
    assert w.enter("fix/three", C, 1, in_flight=0, owner="three", pid=3).state == "go"  # not behind the dead entry
    assert w.status().gone == []  # the turn dropped it for good


def test_release_hold_removes_a_stale_hold_and_refuses_a_live_or_unknown_owner_without_force():
    live = {"a": True, "b": None}
    w, _ = window(alive=lambda row: live.get(row.owner, False))
    w.hold("a", "one", pid=1)
    w.hold("b", "two", pid=2)
    w.hold("c", "three", pid=3)
    assert not w.release_hold("a").withdrawn and not w.release_hold("b").withdrawn
    assert w.release_hold("c").withdrawn and "no admission hold" in w.release_hold("c").reason  # c is dead: its hold was ignored and now removed
    assert w.release_hold("a", force=True).withdrawn and [h.label for h in w.status().holds] == ["two"]
