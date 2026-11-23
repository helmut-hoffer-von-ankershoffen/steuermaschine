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


def test_exact_candidate_refuses_moved_head_before_arming(evidence):
    _, remote, _ = evidence
    exact.prepare(CANDIDATE.version, CANDIDATE.token, CANDIDATE.pr)
    remote[f"pulls/{CANDIDATE.pr}"] = pr(head={**pr()["head"], "sha": "e" * 40})
    with pytest.raises(exact.ExactReleaseError, match="moved"):
        exact.verify_open(CANDIDATE)


@pytest.mark.parametrize("bad", [
    {"parents": ["e" * 40]}, {"parents": [CANDIDATE.base, CANDIDATE.head]},
    {"tree": "e" * 40}, {"message": "chore(release): v0.9.55"},
])
def test_merge_race_never_tags_the_wrong_base_or_tree(evidence, monkeypatch, tmp_path, bad):
    records, remote, _ = evidence
    exact.prepare(CANDIDATE.version, CANDIDATE.token, CANDIDATE.pr)
    approve(records)
    remote[f"pulls/{CANDIDATE.pr}"] = pr(state="closed", merged=True, merge_commit_sha=MERGE)
    remote[f"commits/{MERGE}"] = commit(MERGE, **bad)
    monkeypatch.setattr(exact, "_git", lambda *_: pytest.fail("never read or write a tag for an unverified merge"))
    with pytest.raises(exact.ExactReleaseError, match="no tag"):
        exact.ensure_tag(CANDIDATE, tmp_path, create=True)


def test_tag_created_once_and_resumed_only_when_exact(evidence, monkeypatch, tmp_path):
    records, remote, receipt = evidence
    exact.prepare(CANDIDATE.version, CANDIDATE.token, CANDIDATE.pr)
    approve(records)
    remote[f"pulls/{CANDIDATE.pr}"] = pr(state="closed", merged=True, merge_commit_sha=MERGE)
    ref = f"refs/tags/{CANDIDATE.tag}"
    commands, tag = [], []

    def git(_clone, *argv):
        commands.append(argv)
        if argv[0] == "push":
            assert argv[-1] == f"{MERGE}:{ref}" and "--force" not in argv
            tag.append(f"{MERGE}\t{ref}")
        return "\n".join(tag) if argv[0] == "ls-remote" else ""

    monkeypatch.setattr(exact, "_git", git)
    assert not exact.ensure_tag(CANDIDATE, tmp_path)
    assert exact.ensure_tag(CANDIDATE, tmp_path, create=True)
    receipt["finished"] = True  # an already verified tag remains resumable after dropping the hold
    assert exact.ensure_tag(CANDIDATE, tmp_path, create=True)
    assert [argv[0] for argv in commands].count("push") == 1
    tag[:] = [f"{'e' * 40}\t{ref}"]
    with pytest.raises(exact.ExactReleaseError, match="existing tag"):
        exact.ensure_tag(CANDIDATE, tmp_path, create=True)
    assert [argv[0] for argv in commands].count("push") == 1
