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


BASE = {"headSha": CANDIDATE.base, "status": "completed", "conclusion": "success", "attempt": 1}
PUSH = {**BASE, "event": "push"}
MAC = {**BASE, "event": "workflow_dispatch", "displayTitle": exact.main_lanes.MACOS_TITLE + " " + CANDIDATE.base}


@pytest.mark.parametrize(("runs", "ready"), [
    ([PUSH, MAC], True),
    ([PUSH, {**MAC, "attempt": 2}], False),
    ([{**PUSH, "conclusion": "failure"}, MAC, PUSH], False),
    ([PUSH, {**MAC, "headSha": "f" * 40}], False),
    ([PUSH, {**MAC, "displayTitle": "other workflow dispatch"}], False),
    ([MAC, {**PUSH, "event": "schedule"}], False),
    ([PUSH, None], False),
    ({}, False),
])
def test_current_base_requires_both_original_main_ci_lanes(monkeypatch, runs, ready):
    ref = {"object": {"sha": CANDIDATE.base, "type": "commit"}}
    monkeypatch.setattr(exact.core_facts, "ci_file", lambda: "ci.yml")

    def gh(*argv, default=None):
        if argv[0] == "api":
            assert argv[1].endswith("/git/ref/heads/main")
            return ref
        assert argv[:2] == ("run", "list") and ("--commit", CANDIDATE.base) == argv[8:10]
        return runs

    monkeypatch.setattr(exact.hzlib, "ghj", gh)
    assert exact.base_ready(CANDIDATE.base) == ready
    assert not exact.base_ready("0" * 40)
    ref["object"]["type"] = "tag"
    assert not exact.base_ready()


@pytest.mark.parametrize("contents", [None, "{", "[]", '{"merge_route":"another-route"}'])
def test_bad_or_unrelated_release_state_never_opens_the_freeze(monkeypatch, tmp_path, contents):
    release = tmp_path / "release.json"
    if contents is not None:
        release.write_text(contents)
    monkeypatch.setattr(exact.statedir, "path", lambda _: release)
    monkeypatch.setattr(exact.hzlib, "release_holds_present", lambda: True)
    item = {"repo": exact.hzlib.CORE, "pr": str(CANDIDATE.pr), "reviewed_head": CANDIDATE.head}
    assert exact.queue_candidate(item) is None
    assert exact.reviewed_freeze_active() == (contents != '{"merge_route":"another-route"}')
    monkeypatch.setattr(exact.hzlib, "release_holds_present", lambda: False)
    assert not exact.reviewed_freeze_active()
    assert exact.queue_candidate(item) is None


def test_release_review_requires_a_registered_author_and_reviewer_model(evidence):
    records, _, _ = evidence
    with pytest.raises(exact.ExactReleaseError, match="authorship"):
        exact.approve_required(CANDIDATE)
    exact.prepare(CANDIDATE.version, CANDIDATE.token, CANDIDATE.pr)
    verdicts.record_head_verdict(records, CANDIDATE.digest, CANDIDATE.head,
                                 {"digest": CANDIDATE.digest, "reviewer": "other-model", "verdict": "approve"})
    with pytest.raises(exact.ExactReleaseError, match="record its model"):
        exact.approve_required(CANDIDATE)


def test_open_candidate_refuses_replaced_attempt_or_moved_main(evidence):
    _, remote, _ = evidence
    with pytest.raises(exact.ExactReleaseError, match="first-attempt"):
        exact.verify_open(CANDIDATE, {**APPROVED, "run_attempt": 2})
    remote["git/ref/heads/main"]["object"]["sha"] = "e" * 40
    with pytest.raises(exact.ExactReleaseError, match="current main moved"):
        exact.verify_open(CANDIDATE)


def test_release_merge_must_be_linked_to_the_pr_and_still_on_main(evidence):
    _, remote, _ = evidence
    with pytest.raises(exact.ExactReleaseError, match="has not merged"):
        exact.merged_commit(CANDIDATE)
    remote[f"pulls/{CANDIDATE.pr}"] = pr(state="closed", merged=True, merge_commit_sha=MERGE)
    assert exact.merged_commit(CANDIDATE) == MERGE
    remote[f"compare/{MERGE}...main"]["status"] = "diverged"
    with pytest.raises(exact.ExactReleaseError, match="cannot be proven to be on main"):
        exact.merged_commit(CANDIDATE)


@pytest.mark.parametrize(("initial", "requested", "ready", "tagged", "error"), [
    ("", True, True, False, "queued"),
    (exact.MODE, False, True, False, "queued"),
    (exact.MODE, False, False, False, "both original main CI lanes"),
    ("legacy", True, True, False, "unrecognized merge route"),
    ("", True, True, True, "no reviewed queue authorization"),
    (exact.MODE, False, True, False, "refuses a candidate waiver"),
    (exact.MODE, False, True, False, "needs the owned release-PR freeze"),
    (exact.MODE, False, True, False, "candidate receipt unavailable"),
])
def test_resume_adopts_only_the_current_open_release_without_legacy_merge(monkeypatch, tmp_path, initial, requested, ready, tagged, error):
    from steuermaschine_heinzel.native.release_lib import runner

    receipt = {"version": CANDIDATE.version}
    if initial:
        receipt["merge_route"] = initial
    monkeypatch.setattr(runner, "state", lambda: receipt)
    monkeypatch.setattr(runner, "paths", lambda: (tmp_path / "freeze", tmp_path / "receipt.json"))
    monkeypatch.setattr(runner, "remember", lambda **fields: receipt.update(fields))
    monkeypatch.setattr(runner, "pick_flow", lambda *_: runner.release_flow.RELEASE_PR)
    monkeypatch.setattr(runner, "next_version", lambda _: (CANDIDATE.version, "patch"))
    monkeypatch.setattr(runner, "pending_waiver", lambda _: {"job": "CI OK"} if "waiver" in error else None)
    monkeypatch.setattr(runner, "has_tag", lambda *_: tagged)
    monkeypatch.setattr(runner, "release_pr", lambda _: {"number": CANDIDATE.pr, "state": "OPEN"})
    monkeypatch.setattr(runner, "previous_accepted", lambda _: True)
    for name in ("resume_paused_hold", "guard_tails", "rejection_guard"):
        monkeypatch.setattr(runner, name, lambda *_: None)
    sequence = []
    monkeypatch.setattr(runner, "acquire_snapshot_freeze", lambda version: sequence.append(("freeze", version)))
    monkeypatch.setattr(runner, "disarm_core_auto_merge", lambda: sequence.append("disarm"))
    def read_candidate(_):
        if error == "candidate receipt unavailable":
            raise exact.ExactReleaseError(error)
        return None

    monkeypatch.setattr(exact, "read_candidate", read_candidate)
    monkeypatch.setattr(exact, "base_ready", lambda expected: sequence.append(("base", expected)) or ready)
    monkeypatch.setattr(runner, "merge_release_pr", lambda *_: pytest.fail("must never use legacy merge"))
    monkeypatch.setattr(runner, "open_release_pr", lambda *_: pytest.fail("must adopt the already-open PR"))

    def step(name, done, action, timeout):
        sequence.append(name)
        assert done()

    def queued(version, tag, timeout):
        assert (version, tag, timeout) == (CANDIDATE.version, CANDIDATE.tag, 1)
        assert receipt["merge_route"] == exact.MODE
        raise runner.Stop("queued")

    monkeypatch.setattr(runner, "step", step)
    monkeypatch.setattr(runner, "exact_queue_release", queued)
    with pytest.raises(runner.Stop, match=error):
        runner.release_core(CANDIDATE.version, "", 1, snapshot_acceptance=requested, reviewed_queue=requested,
                            refresh=error == "needs the owned release-PR freeze")
    if error == "queued":
        assert sequence == [("freeze", CANDIDATE.version), "disarm", ("base", None), "release PR open", "previous release accepted"]


@pytest.mark.parametrize("patch", [{"version": "01.9.55"}, {"token": "x"}, {"pr": 0}, {"head": "ab"}, {"run": 0}])
def test_candidate_receipt_rejects_invalid_identities(patch):
    with pytest.raises(exact.ExactReleaseError, match="exact release identities"):
        exact.Candidate(**(asdict(CANDIDATE) | patch))


@pytest.mark.parametrize(("data", "reason"), [
    ({"unexpected": "field"}, "malformed"),
    (asdict(CANDIDATE) | {"version": "0.9.56"}, "different version"),
])
def test_persisted_candidate_receipt_cannot_be_rebound(evidence, data, reason):
    records, _, _ = evidence
    records.put_receipt(exact.KIND, CANDIDATE.version, data)
    with pytest.raises(exact.ExactReleaseError, match=reason):
        exact.read_candidate(CANDIDATE.version)


def test_freeze_token_must_still_belong_to_candidate(evidence, monkeypatch, tmp_path):
    _, _, receipt = evidence
    receipt["freeze_token"] = "2" * 32
    with pytest.raises(exact.ExactReleaseError, match="does not own"):
        exact.require_hold(CANDIDATE)
    receipt["freeze_token"] = CANDIDATE.token
    monkeypatch.setattr(exact.hzlib, "release_holds", lambda: tmp_path / "missing")
    with pytest.raises(exact.ExactReleaseError, match="cannot verify the ownership"):
        exact.require_hold(CANDIDATE)


def test_candidate_proof_rejects_unavailable_main_ci(evidence, monkeypatch):
    monkeypatch.setattr(exact, "base_ready", lambda _: False)
    with pytest.raises(exact.ExactReleaseError, match="both original main CI lanes"):
        exact.verify_open(CANDIDATE)


@pytest.mark.parametrize(("state", "number", "reason"), [
    (None, None, "no release PR"),
    ("OPEN", CANDIDATE.pr, "first-attempt CI run"),
    ("MERGED", CANDIDATE.pr + 1, "no exact candidate authorization"),
])
def test_release_queue_refusal_occurs_before_tag_readback(evidence, monkeypatch, state, number, reason):
    from steuermaschine_heinzel.native.release_lib import runner

    exact.prepare(CANDIDATE.version, CANDIDATE.token, CANDIDATE.pr)
    monkeypatch.setattr(runner, "state", lambda: {"freeze_token": CANDIDATE.token})
    monkeypatch.setattr(runner, "release_pr", lambda _: {"state": state, "number": number} if state else None)
    monkeypatch.setattr(runner, "has_tag", lambda *_: pytest.fail("an unauthorized merge cannot advance to tag readback"))
    with pytest.raises(runner.Stop, match=reason):
        runner.exact_queue_release(CANDIDATE.version, CANDIDATE.tag, 1)


def test_unreadable_merged_source_stops_before_matching_a_tag(monkeypatch):
    from steuermaschine_heinzel.native.release_lib import runner

    def fail(_):
        raise exact.ExactReleaseError("cannot prove the resulting tree")

    monkeypatch.setattr(runner, "release_pr", lambda _: {"state": "MERGED", "number": CANDIDATE.pr})
    monkeypatch.setattr(exact, "read_candidate", lambda _: CANDIDATE)
    monkeypatch.setattr(exact, "approve_required", lambda _: None)
    monkeypatch.setattr(exact, "merged_commit", fail)
    monkeypatch.setattr(runner, "has_tag", lambda *_: pytest.fail("the tree must be proven before reading the tag"))
    with pytest.raises(runner.Stop, match="cannot prove the resulting tree"):
        runner.exact_queue_release(CANDIDATE.version, CANDIDATE.tag, 1)


@pytest.mark.parametrize("extra", [[], ["--snapshot-acceptance", "--waive-job", "CI OK"],
                                  ["--snapshot-acceptance", "--use-candidate-composite", "123"]])
def test_reviewed_queue_cli_refuses_weaker_release_paths(monkeypatch, extra):
    from steuermaschine_heinzel.native.release_lib import runner

    monkeypatch.setattr(runner, "acquire_global_lock", lambda: pytest.fail("invalid options must never take the lock"))
    with pytest.raises(SystemExit) as exc:
        runner.main([CANDIDATE.version, "--reviewed-queue", *extra])
    assert exc.value.code == 2
