"""Pipelined release train step 2 (research/pipelined-release-train-2026-11-08.md), hz-release part: at the tag the
receipt is handed off to ~/.cache/hz-release/vN.json and the global lock is freed; N+1 merges (= publishes) only after
N is ACCEPTED or explicitly absorbed."""

import fcntl
import json

import pytest


@pytest.fixture
def rel(tool, home):
    return tool("hz-release")


def lock_free(path) -> bool:
    with path.open("a") as other:
        try:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(other, fcntl.LOCK_UN)
        return True


def write_tail(rel, version, **fields):
    rel.tail_dir().mkdir(parents=True, exist_ok=True)
    rel.tail_receipt(version).write_text(json.dumps({"version": version} | fields))


def test_1_handoff_moves_the_receipt_and_frees_the_global_lock(rel, home):
    rel.acquire_global_lock()
    rel.remember(version="1.2.4")
    global_lock = rel.global_receipt().with_suffix(".lock")
    assert not lock_free(global_lock)
    rel.lift_freeze_at_tag(False, rel.paths()[0])
    assert not rel.global_receipt().exists(), "an absent global receipt starts the next release"
    tail = json.loads(rel.tail_receipt("1.2.4").read_text())
    assert tail["version"] == "1.2.4" and tail["freeze_lifted_at"]
    assert lock_free(global_lock), "a second sm release resume N+1 acquires the global lock"
    assert not lock_free(rel.tail_lock("1.2.4")), "the tail keeps its own lock"
    rel.lift_freeze_at_tag(False, rel.paths()[0])  # idempotent on resume
    assert rel.paths()[1] == rel.tail_receipt("1.2.4")


def test_1b_a_tail_resume_never_touches_the_shared_sentinel(rel, home):
    write_tail(rel, "1.2.4", freeze_lifted_at="x")
    rel.route_to_tail("1.2.4")
    sentinel = rel.paths()[0]
    sentinel.touch()  # N+1's freeze
    rel.lift_freeze_at_tag(False, sentinel)
    assert sentinel.exists()


def test_2_resume_with_a_tail_receipt_routes_to_it(rel, home, monkeypatch):
    write_tail(rel, "1.2.4", freeze_lifted_at="x")
    seen = {}

    def fake_core(request, *a, **kw):
        seen["receipt"] = rel.paths()[1]
        seen["tail_locked"] = not lock_free(rel.tail_lock("1.2.4"))
        seen["global_free"] = lock_free(rel.global_receipt().with_suffix(".lock"))
        return 0
    monkeypatch.setattr(rel, "release_core", fake_core)
    monkeypatch.setattr(rel, "rejection_guard", lambda v: None)
    assert rel.main(["1.2.4", "--snapshot-acceptance", "--candidate-gate"]) == 0
    assert seen == {"receipt": rel.tail_receipt("1.2.4"), "tail_locked": True, "global_free": True}
    assert lock_free(rel.tail_lock("1.2.4")), "released on exit"


def test_2b_a_second_tail_command_stops(rel, home, monkeypatch):
    write_tail(rel, "1.2.4")
    monkeypatch.setattr(rel, "rejection_guard", lambda v: None)
    with rel.tail_lock("1.2.4").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert rel.main(["1.2.4"]) == 1
    assert lock_free(rel.global_receipt().with_suffix(".lock"))


def test_3_remember_after_handoff_writes_the_tail_only(rel, home):
    rel.remember(version="1.2.4")
    rel.hand_off_tail()
    rel.remember(pypi="2026-11-08T07:00:00Z")
    assert not rel.global_receipt().exists()
    assert json.loads(rel.tail_receipt("1.2.4").read_text())["pypi"] == "2026-11-08T07:00:00Z"


def _through_tail(rel, monkeypatch, good: bool):
    tagged = {"v": False}
    monkeypatch.setattr(rel, "next_version", lambda request: ("1.2.4", "patch"))
    monkeypatch.setattr(rel, "rejection_guard", lambda v: None)
    monkeypatch.setattr(rel, "has_tag", lambda repo, tag: tagged["v"])
    monkeypatch.setattr(rel, "main_candidate_gate", lambda v: True)
    monkeypatch.setattr(rel, "release_pr", lambda tag: {"state": "MERGED", "number": 5})
    names = []

    def fake_step(name, done, act, timeout, check=None):
        names.append(name)
        if name == "tag":
            tagged["v"] = True
    monkeypatch.setattr(rel, "step", fake_step)
    for name in ("pypi_step", "compose_step"):
        monkeypatch.setattr(rel, name, lambda *a: None)
    monkeypatch.setattr(rel, "acceptance_step", lambda *a: 7)
    monkeypatch.setattr(rel, "verdict", lambda run: (good, []))
    monkeypatch.setattr(rel, "publish_acceptance", lambda v, run, ok: names.append(f"marker {v} {run} {ok}"))
    return names


@pytest.mark.parametrize("good", [True, False])
def test_4_the_verdict_writes_accepted(rel, home, monkeypatch, good):
    names = _through_tail(rel, monkeypatch, good)
    rel.paths()[0].touch()
    assert rel.release_core("1.2.4", "", 1, False, candidate_gate=True) == (0 if good else 1)
    tail = json.loads(rel.tail_receipt("1.2.4").read_text())
    assert tail["finished"] is True and tail["accepted"] is good and tail["acceptance_run"] == 7
    assert names[-1] == f"marker 1.2.4 7 {good}", "the public marker auto-update reads carries the verdict"
    assert not rel.global_receipt().exists()


def test_5_absorb_refuses_unfinished_and_accepted_and_records_fields(rel, home, monkeypatch):
    monkeypatch.setattr(rel.hzlib, "ghj", lambda *a, **kw: [{"headRefName": "release/v1.2.5"}])
    monkeypatch.setattr(rel, "rejection_guard", lambda v: None)
    write_tail(rel, "1.2.4")
    assert rel.main(["--absorb", "1.2.4", "--reason", "fixed in 1.2.5 by #640"]) == 1, "unfinished"
    write_tail(rel, "1.2.4", finished=True, accepted=True)
    assert rel.main(["--absorb", "1.2.4", "--reason", "fixed in 1.2.5 by #640"]) == 1, "accepted"
    write_tail(rel, "1.2.4", finished=True, accepted=False, acceptance_run=9)
    assert rel.main(["--absorb", "1.2.4", "--reason", "it was flaky anyway"]) == 1, "needs a fix PR or review line"
    assert rel.main(["--absorb", "1.2.4", "--reason", "fixed in 1.2.5 by #640"]) == 0
    tail = json.loads(rel.tail_receipt("1.2.4").read_text())
    assert tail["absorbed_by"] == "1.2.5" and tail["absorb_reason"] == "fixed in 1.2.5 by #640" and tail["absorbed_at"]
    assert rel.main(["--absorb", "1.2.4", "--reason", "fixed in 1.2.5 by #640"]) == 0, "idempotent"
    assert rel.main(["--absorb", "1.2.9", "--reason", "fixed in 1.2.5 by #640"]) == 1, "no tail receipt"
    with pytest.raises(SystemExit):
        rel.main(["--absorb", "1.2.4", "--reason", "x by #1", "--intro", "y"])


def test_6_previous_release_accepted(rel, home):
    assert rel.previous_accepted("1.2.5"), "no previous tail"
    write_tail(rel, "1.2.3", finished=True, accepted=False)  # older, absorbed or not: only the newest below counts
    write_tail(rel, "1.2.4")
    assert rel.previous_accepted("1.2.5") is False, "unfinished waits"
    write_tail(rel, "1.2.4", finished=True, accepted=False, acceptance_run=42)
    with pytest.raises(rel.Stop, match=r"previous release 1\.2\.4 NOT ACCEPTED \(run 42\).*sm release resume 1\.2\.4 --absorb --reason"):
        rel.previous_accepted("1.2.5")
    write_tail(rel, "1.2.4", finished=True, accepted=False, absorbed_by="1.2.5")
    assert rel.previous_accepted("1.2.5")
    write_tail(rel, "1.2.4", finished=True, accepted=True)
    assert rel.previous_accepted("1.2.5")
    write_tail(rel, "1.2.6", finished=False)
    assert rel.previous_accepted("1.2.5"), "a newer tail is not the previous release"
    rel.tail_receipt("1.2.4").write_text("{")
    with pytest.raises(rel.Stop, match="corrupt"):
        rel.previous_accepted("1.2.5")


def test_7_release_pr_opens_before_the_wait_and_merges_after(rel, home, monkeypatch):
    names = _through_tail(rel, monkeypatch, True)
    rel.paths()[0].touch()
    rel.release_core("1.2.4", "", 1, False, candidate_gate=True)
    assert names.index("release PR open") < names.index("previous release accepted") < names.index("release PR merged") < names.index("tag")


def test_7b_the_wait_blocks_the_merge_until_the_verdict(rel, home, monkeypatch):
    write_tail(rel, "1.2.3", finished=True, accepted=False, acceptance_run=42)
    monkeypatch.setattr(rel, "next_version", lambda request: ("1.2.4", "patch"))
    monkeypatch.setattr(rel, "rejection_guard", lambda v: None)
    monkeypatch.setattr(rel, "has_tag", lambda repo, tag: False)
    monkeypatch.setattr(rel, "main_candidate_gate", lambda v: True)
    monkeypatch.setattr(rel, "release_pr", lambda tag: {"state": "OPEN", "number": 5})
    monkeypatch.setattr(rel, "merge_release_pr", lambda *a: pytest.fail("N NOT ACCEPTED must block N+1's merge"))
    rel.paths()[0].touch()
    with pytest.raises(rel.Stop, match="previous release 1.2.3 NOT ACCEPTED"):
        rel.release_core("1.2.4", "", 1, False, candidate_gate=True)


def test_8_never_cut_at_or_below_a_running_tail(rel, home, monkeypatch):
    monkeypatch.setattr(rel, "rejection_guard", lambda v: None)
    write_tail(rel, "1.2.5")
    monkeypatch.setattr(rel, "next_version", lambda request: ("1.2.4", "patch"))
    monkeypatch.setattr(rel, "has_tag", lambda repo, tag: False)  # no tag: a clear Not Found
    with pytest.raises(rel.Stop, match="1.2.5 is past its tag and unfinished; never cut 1.2.4"):
        rel.release_core("1.2.4", "", 1)
    monkeypatch.setattr(rel, "next_version", lambda request: ("1.2.5", "patch"))
    with pytest.raises(rel.Stop, match="run sm release resume 1.2.5"):
        rel.release_core("patch", "", 1)
    assert not rel.global_receipt().exists()


# ---------------------------------------------------------------- review fixes (research/review-pipelined-train.md)

def test_r1_a_tail_command_routes_before_the_global_lock(rel, home, monkeypatch):
    """N+1 holds the global lock pre-tag (incl. its wait for N): N's tail resume must not wait on it."""
    write_tail(rel, "1.2.4", freeze_lifted_at="x")
    seen = {}
    monkeypatch.setattr(rel, "release_core", lambda *a, **kw: seen.setdefault("receipt", rel.paths()[1]) and 0)
    monkeypatch.setattr(rel, "rejection_guard", lambda v: None)
    path = rel.global_receipt().with_suffix(".lock")
    with path.open("a") as n_plus_1:
        fcntl.flock(n_plus_1, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert rel.main(["1.2.4", "--snapshot-acceptance", "--candidate-gate"]) == 0
        assert seen["receipt"] == rel.tail_receipt("1.2.4")
        assert rel.main(["1.2.5"]) == 1, "a pre-tag command still needs the global lock"


LEGACY = {"version": "0.9.31", "freeze_token": "f" * 32, "finished": True, "acceptance_run": 42451836720,
          "freeze_lifted_at": "2026-11-08T05:31:00Z"}


def test_r3_migrate_legacy_moves_a_finished_global_receipt_as_not_accepted(rel, home):
    rel.global_receipt().write_text(json.dumps(LEGACY))
    assert rel.main(["migrate-legacy"]) == 0
    tail = json.loads(rel.tail_receipt("0.9.31").read_text())
    assert tail["accepted"] is False and tail["acceptance_run"] == 42451836720 and tail["finished"] is True
    assert not rel.global_receipt().exists()
    assert rel.main(["migrate-legacy"]) == 0, "idempotent: nothing to migrate"
    rel.global_receipt().write_text(json.dumps({"version": "0.9.32", "freeze_token": "a" * 32}))
    assert rel.main(["migrate-legacy"]) == 0 and json.loads(rel.global_receipt().read_text())["version"] == "0.9.32", \
        "an unfinished (pre-tag) receipt is never moved"
    with pytest.raises(SystemExit):
        rel.main(["migrate-legacy", "--intro", "x"])
    with pytest.raises(rel.Stop, match="previous release 0.9.31 NOT ACCEPTED"):
        rel.previous_accepted("0.9.32")


def test_r3_a_finished_legacy_receipt_blocks_the_next_merge_until_absorbed(rel, home, monkeypatch):
    """The reviewer's simulation C: 0.9.32 must not publish past a NOT ACCEPTED legacy 0.9.31."""
    rel.global_receipt().write_text(json.dumps(LEGACY))
    monkeypatch.setattr(rel, "next_version", lambda request: ("0.9.32", "patch"))
    monkeypatch.setattr(rel, "rejection_guard", lambda v: None)
    monkeypatch.setattr(rel, "has_tag", lambda repo, tag: False)
    monkeypatch.setattr(rel, "main_candidate_gate", lambda v: True)
    monkeypatch.setattr(rel, "release_pr", lambda tag: {"state": "OPEN", "number": 5})
    monkeypatch.setattr(rel, "merge_release_pr", lambda *a: pytest.fail("0.9.31 NOT ACCEPTED must block 0.9.32's merge"))
    rel.paths()[0].touch()
    with pytest.raises(rel.Stop, match=r"previous release 0\.9\.31 NOT ACCEPTED \(run 42451836720\)"):
        rel.release_core("0.9.32", "", 1, False, candidate_gate=True)
    assert json.loads(rel.tail_receipt("0.9.31").read_text())["accepted"] is False, "N's verdict is preserved"
    assert json.loads(rel.global_receipt().read_text())["version"] == "0.9.32"


def test_r3_absorb_accepts_a_finished_legacy_receipt(rel, home, monkeypatch):
    rel.global_receipt().write_text(json.dumps(LEGACY))
    monkeypatch.setattr(rel.hzlib, "ghj", lambda *a, **kw: [{"headRefName": "release/v0.9.32"}])
    monkeypatch.setattr(rel, "rejection_guard", lambda v: None)
    assert rel.main(["--absorb", "0.9.31", "--reason", "no fix named"]) == 1
    assert rel.main(["--absorb", "0.9.31", "--reason", "fixed in 0.9.32 by #650"]) == 0
    tail = json.loads(rel.tail_receipt("0.9.31").read_text())
    assert tail["accepted"] is False and tail["absorbed_by"] == "0.9.32"
    assert not rel.global_receipt().exists() and rel.previous_accepted("0.9.32")


def test_r3_previous_accepted_reads_a_finished_legacy_global_receipt(rel, home):
    rel.global_receipt().write_text(json.dumps(LEGACY))  # no `accepted` key, no tail dir
    with pytest.raises(rel.Stop, match="previous release 0.9.31 NOT ACCEPTED"):
        rel.previous_accepted("0.9.32")
    assert rel.previous_accepted("0.9.31") is True, "only an older release counts as previous"
    rel.global_receipt().write_text(json.dumps(LEGACY | {"accepted": True}))
    assert rel.previous_accepted("0.9.32")


def test_tagged_snapshot_adoption_without_hold_hands_off_and_preserves_incident(rel, home, monkeypatch):
    _through_tail(rel, monkeypatch, True)
    rel.remember(version="1.2.4")
    incident = rel.paths()[0]
    incident.write_text("foreign incident")
    monkeypatch.setattr(rel, "has_tag", lambda repo, tag: tag == "v1.2.4")
    monkeypatch.setattr(rel, "pinned_snapshot", lambda version: {"version": version})
    assert rel.release_core("1.2.4", "", 1, True, candidate_gate=True) == 0
    tail = json.loads(rel.tail_receipt("1.2.4").read_text())
    assert tail["finished"] is True and tail["accepted"] is True
    assert "freeze_token" not in tail and incident.read_text() == "foreign incident"
