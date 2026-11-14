"""`sm head prepare --wait`: the wait loop, with a fake clock (no sleep, no network)."""
import pytest

from steuermaschine.testing import FakeClock
from steuermaschine_heinzel.native.admission_lib import focused_admit as a
from steuermaschine_heinzel.native.admission_lib import ops, wait


def run(row_id=7, status="in_progress", branch="main", event="push"):
    return ops.BlockingRun(id=row_id, status=status, head_branch=branch, event=event, path=".github/workflows/ci.yml", head_sha="a" * 40)


class Script:
    """Answers of the inventory poll and of prepare, in order; an exception instance is raised."""

    def __init__(self, polls, prepares):
        self.polls, self.prepares, self.calls, self.said, self.clock = list(polls), list(prepares), [], [], FakeClock()

    def _next(self, name, queue):
        self.calls.append(name)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def go(self, max_minutes=45.0, poll_seconds=90.0):
        return wait.prepare_when_clear(lambda: self._next("prepare", self.prepares), lambda: self._next("poll", self.polls),
                                       clock=self.clock, say=self.said.append, max_minutes=max_minutes, poll_seconds=poll_seconds)


def test_clear_inventory_prepares_at_once_without_sleeping():
    script = Script([[]], ["PREPARED"])
    assert script.go() == "PREPARED" and script.calls == ["poll", "prepare"] and script.clock.slept == [] and script.said == []


def test_waits_while_an_uncovered_run_is_active_and_names_it():
    script = Script([[run()], [run()], []], ["PREPARED"])
    assert script.go() == "PREPARED"
    assert script.calls == ["poll", "poll", "poll", "prepare"] and script.clock.slept == [90.0, 90.0]
    assert script.said == ["waiting (0 min) for uncovered active run 7 in_progress main (push); next look in 90 s",
                           "waiting (2 min) for uncovered active run 7 in_progress main (push); next look in 90 s"]


def test_prepare_does_not_run_while_a_run_blocks():
    script = Script([[run()]] * 3, [])
    with pytest.raises(wait.StillWaiting):
        script.go(max_minutes=3)
    assert "prepare" not in script.calls


def test_truncated_inventory_retries_the_identical_prepare():
    script = Script([[], []], [a.AdmissionError("truncated inventory"), "PREPARED"])
    assert script.go() == "PREPARED" and script.calls == ["poll", "prepare", "poll", "prepare"]
    assert script.said == ["waiting (0 min) for a complete inventory (truncated inventory); the identical prepare is retried; next look in 90 s"]


def test_truncated_or_drifting_poll_is_waited_out():
    script = Script([a.AdmissionError("truncated inventory"), a.AdmissionError("active run membership drift; reprepare when stable"), []], ["PREPARED"])
    assert script.go() == "PREPARED" and script.calls == ["poll", "poll", "poll", "prepare"] and len(script.said) == 2
    assert "a stable run inventory (truncated inventory)" in script.said[0]


def test_a_run_that_appears_between_poll_and_prepare_is_waited_out_and_named():
    refusal = a.AdmissionError("unknown active Actions run; admission refused: 9 queued pull_request x .github/workflows/ci.yml")
    script = Script([[], []], [refusal, "PREPARED"])
    assert script.go() == "PREPARED"
    assert script.said == ["waiting (0 min) for uncovered active run 9 queued pull_request x .github/workflows/ci.yml; next look in 90 s"]


@pytest.mark.parametrize("where", ["poll", "prepare"])
def test_any_other_refusal_is_raised_unchanged_and_not_retried(where):
    error = a.AdmissionError("packet base is stale relative to current main")
    script = Script([error] if where == "poll" else [[]], [error])
    with pytest.raises(a.AdmissionError, match="packet base is stale"):
        script.go()
    assert script.clock.slept == [] and script.calls[-1] == where


def test_the_wait_ends_after_the_limit_and_says_what_it_waits_on():
    script = Script([[run(row_id=n) for n in range(1, 8)]] * 10, [])
    with pytest.raises(wait.StillWaiting) as stopped:
        script.go(max_minutes=4, poll_seconds=90)
    assert script.clock.slept == [90, 90, 90]  # 0, 1.5 and 3 minutes waited; at 4.5 the limit is passed
    assert stopped.value.waiting_on == "uncovered active run 1 in_progress main (push), 2 in_progress main (push), 3 in_progress main (push), " \
                                       "4 in_progress main (push), 5 in_progress main (push), and 2 more"
    assert stopped.value.message.startswith("prepare did not get a clear window in 4 min: uncovered active run 1 ")


def test_transient_is_exactly_truncation_and_membership_drift():
    assert wait.transient(a.AdmissionError("truncated inventory")) and wait.transient(a.AdmissionError("active run membership drift; reprepare when stable"))
    for text in ("inventory count mismatch", "main changed", "registry snapshot changed; reprepare and review, do not overwrite"):
        assert not wait.transient(a.AdmissionError(text))
