"""The owned release queue never publishes a moved or unreviewed candidate."""

from dataclasses import asdict

import pytest

from steuermaschine import verdicts
from steuermaschine.engine import Refuse
from steuermaschine.testing import fake_providers
from steuermaschine_heinzel.native.release_lib import exact_queue as exact

CANDIDATE = exact.Candidate("0.9.55", "1" * 32, 801, "a" * 40, "b" * 40, "c" * 40, 1234)
MERGE = "d" * 40
APPROVED = {"head": CANDIDATE.head, "base": CANDIDATE.base, "tree": CANDIDATE.tree, "run_id": CANDIDATE.run, "run_attempt": 1}


def pr(**fields):
    repo = {"full_name": exact.hzlib.CORE}
    return {"number": CANDIDATE.pr, "head": {"sha": CANDIDATE.head, "ref": f"release/{CANDIDATE.tag}", "repo": repo},
            "base": {"ref": "main", "repo": repo}, "title": f"chore(release): {CANDIDATE.tag}", "draft": False, "state": "open", **fields}


def commit(sha, *, parents=None, tree=None, message=None):
    return {"sha": sha, "parents": [{"sha": p} for p in (parents if parents is not None else [CANDIDATE.base])],
            "commit": {"tree": {"sha": tree or CANDIDATE.tree}, "message": message or CANDIDATE.subject}}


@pytest.fixture
def evidence(monkeypatch, tmp_path):
    records = fake_providers().store
    hold = tmp_path / CANDIDATE.token
    hold.write_text(CANDIDATE.version)
    receipt = {"version": CANDIDATE.version, "freeze_token": CANDIDATE.token, "merge_route": exact.MODE}
    monkeypatch.setattr(exact, "store", lambda: records)
    monkeypatch.setattr(exact, "active_receipt", lambda: receipt)
    monkeypatch.setattr(exact.hzlib, "release_holds", lambda: tmp_path)
    monkeypatch.setattr(exact.hzlib, "incident_frozen", lambda: False)
    monkeypatch.setattr(exact, "base_ready", lambda expected=None: expected == CANDIDATE.base)
    remote = {f"pulls/{CANDIDATE.pr}": pr(), f"commits/{CANDIDATE.head}": commit(CANDIDATE.head),
              f"commits/{MERGE}": commit(MERGE), "git/ref/heads/main": {"object": {"sha": CANDIDATE.base, "type": "commit"}},
              f"compare/{MERGE}...main": {"status": "ahead"}}
    monkeypatch.setattr(exact, "_api", remote.__getitem__)
    monkeypatch.setattr(exact.candidate_policy, "verify_pr", lambda number: APPROVED if number == CANDIDATE.pr else {})
    return records, remote, receipt


def approve(records):
    verdicts.record_head_verdict(records, CANDIDATE.digest, CANDIDATE.head,
                                 {"digest": CANDIDATE.digest, "reviewer": "other-model", "model": "Claude Opus", "verdict": "approve"})


def test_release_review_survives_restart_but_belongs_to_one_candidate_and_hold(evidence, tmp_path):
    records, _, receipt = evidence
    item = {"repo": exact.hzlib.CORE, "pr": str(CANDIDATE.pr), "reviewed_head": CANDIDATE.head}
    assert exact.prepare(CANDIDATE.version, CANDIDATE.token, CANDIDATE.pr) == CANDIDATE
    with pytest.raises(Refuse, match="no verdict"):
        exact.queue_candidate(item)
    approve(records)
    assert exact.prepare(CANDIDATE.version, CANDIDATE.token, CANDIDATE.pr) == CANDIDATE
    assert records.get_receipt(exact.KIND, CANDIDATE.version) == asdict(CANDIDATE)
    assert exact.queue_candidate(item) == CANDIDATE
    assert exact.queue_candidate({**item, "pr": "802"}) is None
    assert exact.queue_candidate({**item, "reviewed_head": "e" * 40}) is None
    with pytest.raises(exact.ExactReleaseError, match="preserved review"):
        exact.prepare(CANDIDATE.version, "2" * 32, CANDIDATE.pr)
    (tmp_path / ("3" * 32)).write_text("unrelated release")
    with pytest.raises(exact.ExactReleaseError, match="sole release hold"):
        exact.queue_candidate(item)
    (tmp_path / ("3" * 32)).unlink()
    receipt["hold_events"] = [{"action": "pause-request"}]
    with pytest.raises(exact.ExactReleaseError, match="paused"):
        exact.queue_candidate(item)


def test_exact_candidate_refuses_moved_head_before_merge(evidence):
    _, remote, _ = evidence
    exact.prepare(CANDIDATE.version, CANDIDATE.token, CANDIDATE.pr)
    remote[f"pulls/{CANDIDATE.pr}"] = pr(head={**pr()["head"], "sha": "e" * 40})
    with pytest.raises(exact.ExactReleaseError, match="moved"):
        exact.verify_open(CANDIDATE)


@pytest.mark.parametrize("bad", [
    {"parents": ["e" * 40]}, {"parents": [CANDIDATE.base, CANDIDATE.head]},
    {"tree": "e" * 40}, {"message": "chore(release-merge): v0.9.55"},
])
def test_post_merge_check_rejects_the_wrong_base_or_tree(evidence, bad):
    records, remote, _ = evidence
    exact.prepare(CANDIDATE.version, CANDIDATE.token, CANDIDATE.pr)
    approve(records)
    remote[f"pulls/{CANDIDATE.pr}"] = pr(state="closed", merged=True, merge_commit_sha=MERGE)
    remote[f"commits/{MERGE}"] = commit(MERGE, **bad)
    with pytest.raises(exact.ExactReleaseError, match="merged release source differs"):
        exact.merged_commit(CANDIDATE)


@pytest.mark.parametrize("queued, denied", [(True, False), (False, False), (False, True)])
def test_release_delegates_to_queue_commands_once(monkeypatch, queued, denied):
    from types import SimpleNamespace

    import steuermaschine.native
    from steuermaschine_heinzel.native.release_lib import runner

    monkeypatch.setattr(runner, "release_pr", lambda tag: {"state": "OPEN", "number": CANDIDATE.pr})
    monkeypatch.setattr(runner, "state", lambda: {"freeze_token": CANDIDATE.token})
    monkeypatch.setattr(exact, "merged_commit", lambda _: MERGE)
    monkeypatch.setattr(exact, "prepare", lambda *_: CANDIDATE)
    monkeypatch.setattr(exact, "approve_required", lambda _: None)
    steps, calls = [], []
    monkeypatch.setattr(runner, "step", lambda name, *_: steps.append(name))
    row = SimpleNamespace(repo=runner.CORE, pr=str(CANDIDATE.pr), status="queued", reviewed_head=CANDIDATE.head)

    def call(command, **arguments):
        calls.append(command.path)
        if command.path == "queue list":
            assert arguments == {"active": True}
            return SimpleNamespace(entries=[row] if queued else [])
        assert command.path == "queue add"
        assert arguments["pr"] == [str(CANDIDATE.pr)] and arguments["reviewed_head"] == CANDIDATE.head
        return SimpleNamespace(exit=1 if denied else 0, errors=["refused"], lines=[])

    monkeypatch.setattr(steuermaschine.native, "call", call)
    if denied:
        with pytest.raises(runner.Stop, match="active queue refused"):
            runner.exact_queue_release(CANDIDATE.version, CANDIDATE.tag, 1)
        assert steps == []
    else:
        runner.exact_queue_release(CANDIDATE.version, CANDIDATE.tag, 1)
        assert steps == ["release PR merged", "tag"]
    assert calls == (["queue list"] if queued else ["queue list", "queue add"])


def test_runner_waits_for_the_hosted_tag_and_rejects_a_different_commit(monkeypatch):
    from steuermaschine_heinzel.native.release_lib import runner

    monkeypatch.setattr(runner, "release_pr", lambda tag: {"state": "MERGED", "number": CANDIDATE.pr})
    monkeypatch.setattr(exact, "read_candidate", lambda _: CANDIDATE)
    monkeypatch.setattr(exact, "approve_required", lambda _: None)
    monkeypatch.setattr(exact, "merged_commit", lambda _: MERGE)
    hosted_tag = []
    monkeypatch.setattr(runner, "has_tag", lambda repo, tag: bool(hosted_tag))
    monkeypatch.setattr(runner, "tag_commit", lambda tag: hosted_tag[0])

    def step(name, done, act, timeout):
        assert name == "tag" and act is None and timeout == 1
        if not hosted_tag:
            assert not done()
            hosted_tag.append(MERGE)
        assert done()

    monkeypatch.setattr(runner, "step", step)
    runner.exact_queue_release(CANDIDATE.version, CANDIDATE.tag, 1)
    hosted_tag[0] = "e" * 40
    with pytest.raises(runner.Stop, match="does not point at the reviewed merge"):
        runner.exact_queue_release(CANDIDATE.version, CANDIDATE.tag, 1)
