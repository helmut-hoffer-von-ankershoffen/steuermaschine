"""The release-window lock: `sm release admit prepare` takes it and leaves it for the review and the apply, a successful
`sm release admit apply` gives it back, and `sm head validate` waits (exit 75) while it holds, pushing nothing."""

import json

from test_native_dispatch import lanes, remote, rt  # noqa: F401 - fixtures

from steuermaschine.testing import invoke
from steuermaschine_heinzel.native import release_admit, release_window, window
from steuermaschine_heinzel.native.release_admit_lib import release_ops


def test_validate_waits_while_a_release_holds_the_window_and_runs_when_it_is_given_back(rt, lanes):  # noqa: F811
    runtime = rt(commands=remote())
    hold = release_window.take(runtime, "prepare", "/r/packet.json")
    held = invoke(runtime, ["head", "validate", "."])
    assert held.exit_code == 75 and "a release prepare holds the release window (/r/packet.json" in held.stderr and "Nothing was pushed" in held.stderr
    assert runtime.providers.commands.git == [] and lanes["started"] == [] and window.the_window(runtime).status().waiting == []
    release_window.give_back(runtime, hold)
    assert invoke(runtime, ["head", "validate", "."]).exit_code == 0


def test_the_hold_ends_by_itself_after_its_minutes(rt):  # noqa: F811
    runtime = rt()
    hold = release_window.take(runtime, "apply", "p")
    assert hold.until - hold.since == 60 * 60 and release_window.holder(runtime) == hold
    runtime.providers.clock.sleep(60 * 60)
    assert release_window.holder(runtime) is None
    assert release_window.holder(rt()) is None  # nothing recorded


def test_prepare_keeps_the_hold_and_a_successful_apply_gives_it_back(rt, monkeypatch, tmp_path):  # noqa: F811
    runtime = rt()
    runtime.providers.store.put_receipt("verdicts", "a" * 64, {"verdict": "approve", "reviewer": "reviewer-1"})  # apply needs a recorded verdict

    def prepared(*args, **kwargs):
        assert release_window.holder(runtime).step == "prepare"
        raise release_ops.ERRORS[0]("uncovered active run 7")

    monkeypatch.setattr(release_ops, "prepare", prepared)
    refused = invoke(runtime, ["release", "admit", "prepare", str(tmp_path / "p.json"), "--core-repo", ".", "--review-root", "."])
    assert refused.exit_code == 1 and release_window.holder(runtime).step == "prepare"  # a refused prepare keeps the hold: runs drain

    def applied(packet, approved):
        assert release_window.holder(runtime).step == "apply"
        return release_admit.admission.ApplyResult(receipt_path="r.json", sha256=approved, head="h")

    monkeypatch.setattr(release_ops, "apply", applied)
    done = invoke(runtime, ["--format", "json", "release", "admit", "apply", str(tmp_path / "p.json"), "--approved", "a" * 64])
    assert done.exit_code == 0 and json.loads(done.stdout)["receipt_path"] == "r.json" and release_window.holder(runtime) is None


def test_a_failed_apply_keeps_the_hold(rt, monkeypatch):  # noqa: F811
    runtime = rt()
    runtime.providers.store.put_receipt("verdicts", "a" * 64, {"verdict": "approve", "reviewer": "reviewer-1"})  # apply needs a recorded verdict

    def failed(packet, approved):
        raise release_ops.ERRORS[0]("prepared transaction changed")

    monkeypatch.setattr(release_ops, "apply", failed)
    assert invoke(runtime, ["release", "admit", "apply", "p.json", "--approved", "a" * 64]).exit_code == 1
    assert release_window.holder(runtime).step == "apply"


def test_the_minutes_come_from_the_setting(rt):  # noqa: F811
    runtime = rt()
    runtime.settings["release_window_minutes"] = 5
    hold = release_window.take(runtime, "prepare", "p")
    assert hold.until - hold.since == 300 and "until" in hold.describe()


def test_apply_gives_back_only_its_own_hold(rt, monkeypatch, tmp_path):  # noqa: F811
    """S13: the apply of packet A neither clears a later prepare's hold of packet B nor one written while it ran."""
    runtime = rt()
    runtime.providers.store.put_receipt("verdicts", "a" * 64, {"verdict": "approve", "reviewer": "reviewer-1"})  # apply needs a recorded verdict
    b = str(tmp_path / "b.json")
    release_window.take(runtime, "prepare", b)  # B's prepare holds the window for its review
    monkeypatch.setattr(release_ops, "apply", lambda packet, approved: release_admit.admission.ApplyResult(receipt_path="r.json", sha256=approved, head="h"))
    assert invoke(runtime, ["release", "admit", "apply", str(tmp_path / "a.json"), "--approved", "a" * 64]).exit_code == 0
    after = release_window.holder(runtime)
    assert after is not None and after.step == "prepare" and after.packet == b  # B's hold is back, not cleared

    def overtaken(packet, approved):  # another release step takes the window while this apply runs
        release_window.take(runtime, "prepare", str(tmp_path / "c.json"))
        return release_admit.admission.ApplyResult(receipt_path="r.json", sha256=approved, head="h")

    monkeypatch.setattr(release_ops, "apply", overtaken)
    assert invoke(runtime, ["release", "admit", "apply", b, "--approved", "a" * 64]).exit_code == 0
    assert release_window.holder(runtime).packet == str(tmp_path / "c.json")
    mine = release_window.take(runtime, "apply", "c")
    release_window.give_back(runtime, mine, release_window.ReleaseWindowHold(step="prepare", packet="c", pid=1, host="h", since=0, until=1))
    assert release_window.holder(runtime) is None  # the same packet's own earlier hold: given back with it
