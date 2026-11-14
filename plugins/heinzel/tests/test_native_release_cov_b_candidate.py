"""release_candidate: version selection, dispatch lookup, job and run inventories, validation of a complete candidate,
the verify collection on a fake API, the recorder and the command line. No network, no git, no gh."""

import base64
import hashlib
import io
import json
import zipfile

import policy_kit as kit
import pytest

from steuermaschine_heinzel.native.queue_lib import release_candidate as rc

H, T, B, S = "1" * 40, "2" * 40, "3" * 40, "4" * 40
V = "1.2.3"
PINS = {kit.WORKFLOW: "a" * 64, kit.RECORDER: "b" * 64}
POLICY = kit.policy(commit=H)


def make_pr(**over):
    pr = {"number": 5, "state": "open", "draft": False, "title": f"chore(release): v{V}",
          "head": {"sha": H, "ref": f"release/v{V}", "repo": {"full_name": rc.REPOSITORY}},
          "base": {"sha": B, "ref": "main", "repo": {"full_name": rc.REPOSITORY}}}
    return pr | over


def jobs_for(event="pull_request", run_id=9, policy=POLICY):
    out = []
    for i, (name, concl) in enumerate(policy.expected_jobs(event).items(), start=1):
        job = {"id": i, "name": name, "run_id": run_id, "run_attempt": 1, "head_sha": H, "status": "completed", "conclusion": concl}
        if concl == "success":
            job |= {"runner_id": 4, "steps": [{"n": 1}]}
        out.append(job)
    return out


def evidence(event="pull_request", run_id=9, policy=POLICY):
    run = {"id": run_id, "run_attempt": 1, "status": "completed", "conclusion": "success", "event": event, "path": rc.WORKFLOW,
           "head_sha": H, "head_branch": f"release/v{V}", "repository": {"full_name": rc.REPOSITORY},
           "head_repository": {"full_name": rc.REPOSITORY}, "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T01:00:00+00:00"}
    pr_proof = event == "pull_request"
    proof = {"schema": 1, "repository": rc.REPOSITORY, "event": event, "run_id": run_id, "run_attempt": 1, "head": H, "tree": T,
             "version": V, "sources": PINS, "base": B, "source_commit": S if pr_proof else H, "pr": 5 if pr_proof else None}
    source = {"sha": proof["source_commit"], "commit": {"tree": {"sha": T}}, "parents": [{"sha": B}, {"sha": H}]}
    return {"run": run, "jobs": jobs_for(event, run_id, policy), "proof": proof, "pr": make_pr(), "source": source,
            "head": {"sha": H, "commit": {"tree": {"sha": T}}}}


EXPECTED = rc.ExpectedCandidate(9, 5, H, T)


def test_require_and_valid_sha_and_unique():
    rc.require(True, "x")
    with pytest.raises(rc.CandidateError, match="x"):
        rc.require(0, "x")
    assert rc.valid_sha(H) and not rc.valid_sha("zz") and not rc.valid_sha(None)
    assert rc.unique_object([("a", 1)]) == {"a": 1}
    with pytest.raises(rc.CandidateError):
        rc.unique_object([("a", 1), ("a", 2)])


def test_release_version():
    f = rc.release_version
    assert f("pull_request", "other/repo", "", {}) is None
    assert f("pull_request", rc.REPOSITORY, "", {"pull_request": make_pr()}) == V
    foreign = make_pr(head={"sha": H, "ref": f"release/v{V}", "repo": {"full_name": "x/y"}})
    assert f("pull_request", rc.REPOSITORY, "", {"pull_request": foreign}) is None
    assert f("pull_request", rc.REPOSITORY, "", {"pull_request": make_pr(title="other")}) is None
    assert f("pull_request", rc.REPOSITORY, "", {"pull_request": make_pr(head={"sha": H, "ref": "fix/x", "repo": {"full_name": rc.REPOSITORY}})}) is None
    assert f("workflow_dispatch", rc.REPOSITORY, f"release/v{V}", {}) == V
    assert f("workflow_dispatch", rc.REPOSITORY, "main", {}) is None
    assert f("push", rc.REPOSITORY, f"release/v{V}", {}) is None


def test_quarantine_lane():
    def lane(*names, concl="success", attempt=1):
        return [{"name": n, "run_id": 9, "run_attempt": attempt, "head_sha": H, "status": "completed", "conclusion": concl} for n in names]
    base = [{"name": "x"}]
    q = POLICY.quarantine  # the lane's job names are the policy's
    assert rc.quarantine_lane(base, 9, H, POLICY) == base
    good = lane(q.registry, q.plan) + lane(*q.runs, concl="failure")
    assert rc.quarantine_lane(base + good, 9, H, POLICY) == base
    idle = lane(q.registry, q.plan) + lane(q.idle, concl="skipped")
    assert rc.quarantine_lane(base + idle, 9, H, POLICY) == base
    with pytest.raises(rc.CandidateError, match="missing, extra or duplicated"):
        rc.quarantine_lane(lane(q.registry), 9, H, POLICY)
    with pytest.raises(rc.CandidateError, match="failed its registry check"):
        rc.quarantine_lane(lane(q.registry, concl="failure") + lane(q.plan) + lane(*q.runs), 9, H, POLICY)
    with pytest.raises(rc.CandidateError, match="failed its registry check"):
        rc.quarantine_lane([*good], 8, H, POLICY)
    renamed = kit.policy(commit=H, quarantine=POLICY.quarantine.__class__("R", "P", frozenset({"Q1"}), "I"))
    assert rc.quarantine_lane(base + lane("R", "P", "Q1"), 9, H, renamed) == base
    assert rc.quarantine_lane(base + good, 9, H, renamed) == base + good  # names of another policy are workload


def test_job_inventory():
    def pages(total, per_page, ids=None):
        def get(path):
            page = int(path.rsplit("page=", 1)[1])
            rows = [{"id": i} for i in range((page - 1) * per_page + 1, min(page * per_page, total) + 1)]
            return {"total_count": total, "jobs": rows}
        return get
    assert len(rc.job_inventory(pages(3, 2), 9)) == 3
    for data, msg in (([], "malformed"), ({"total_count": 0, "jobs": []}, "changed or exceeds"),
                      ({"total_count": 1, "jobs": [{"id": 1}, {"id": 2}]}, "extra jobs"),
                      ({"total_count": 1, "jobs": [{"id": "x"}]}, "malformed job identity"),
                      ({"total_count": 2, "jobs": [{"id": 1}, {"id": 1}]}, "duplicate"),
                      ({"total_count": 3, "jobs": []}, "truncated")):
        with pytest.raises(rc.CandidateError, match=msg):
            rc.job_inventory(lambda p, d=data: d, 9)
    with pytest.raises(rc.CandidateError, match="changed or exceeds"):
        rc.job_inventory(lambda p: {"total_count": 3 if p.endswith("page=1") else 4, "jobs": [{"id": int(p[-1])}]}, 9)
    with pytest.raises(rc.CandidateError, match="exceeded bound"):
        rc.job_inventory(lambda p: {"total_count": 1000, "jobs": [{"id": int(p.rsplit("=", 1)[1])}]}, 9)


def test_discover():
    run = {"id": 9, "head_sha": H, "run_attempt": 1}
    assert rc.discover(lambda p: {"total_count": 1, "workflow_runs": [run]}, H) == 9
    with pytest.raises(rc.CandidateError, match="invalid release head"):
        rc.discover(lambda p: {}, "x")
    for data, msg in (([], "malformed"), ({"total_count": 0, "workflow_runs": []}, "changed or is empty"),
                      ({"total_count": 1, "workflow_runs": [run, run]}, "extra candidate"),
                      ({"total_count": 1, "workflow_runs": [dict(run, run_attempt=2)]}, "ambiguous"),
                      ({"total_count": 2, "workflow_runs": []}, "truncated")):
        with pytest.raises(rc.CandidateError, match=msg):
            rc.discover(lambda p, d=data: d, H)
    def two(p):
        return {"total_count": 2, "workflow_runs": [run]}
    with pytest.raises(rc.CandidateError, match="ambiguous"):
        rc.discover(two, H)
    with pytest.raises(rc.CandidateError, match="exceeded bound"):
        rc.discover(lambda p: {"total_count": 1000, "workflow_runs": [run]}, H)


def test_source_bytes():
    blob = {"encoding": "base64", "type": "file", "content": base64.b64encode(b"hello world").decode()[:8] + "\n" + base64.b64encode(b"hello world").decode()[8:]}
    assert rc.source_bytes(lambda p: blob, kit.WORKFLOW, S, PINS) == b"hello world"
    with pytest.raises(rc.CandidateError, match="invalid reviewed source request"):
        rc.source_bytes(lambda p: blob, "other", S, PINS)
    with pytest.raises(rc.CandidateError, match="source blob missing"):
        rc.source_bytes(lambda p: {"encoding": "x"}, kit.WORKFLOW, S, PINS)


def broken(mutate, msg, event="pull_request", expected=EXPECTED, policy=POLICY):
    data = evidence(event)
    mutate(data)
    with pytest.raises(rc.CandidateError, match=msg):
        rc.validate(data, expected, policy)


@pytest.mark.parametrize(("shape", "tests", "jobs"), [("old", 16, 69), ("new", 4, 57)])
def test_validate_accepts_both_events_in_both_ci_shapes(shape, tests, jobs):
    policy = kit.policy(commit=H, shape=shape)
    out = rc.validate(evidence(policy=policy), EXPECTED, policy)
    assert out["job_count"] == len(policy.expected_jobs("pull_request")) == jobs and out["workflow_wall_seconds"] == 3600
    assert len(policy.test_jobs) == tests and out["policy_commit"] == H
    out = rc.validate(evidence("workflow_dispatch", policy=policy), EXPECTED, policy)
    assert out["event"] == "workflow_dispatch" and out["job_count"] == jobs


def test_the_commit_binds_the_sources_no_checksum_is_compared():
    """The proof's recorded digests are not compared with anything: the reviewed head and its tree bind the sources."""
    assert rc.validate(evidence(), EXPECTED, POLICY)["policy_commit"] == H
    data = evidence()
    data["proof"]["sources"] = {"anything": "else"}
    assert rc.validate(data, EXPECTED, POLICY)["run_id"] == 9
    broken(lambda d: d["source"]["commit"]["tree"].update(sha="9" * 40), "tested checkout tree differs")
    broken(lambda d: None, "not the release head's", policy=kit.policy(commit="9" * 40))
    broken(lambda d: None, "not the release head's", policy=kit.policy(commit=H, repository="x/y"))


def test_validate_refuses_a_candidate_of_another_ci_shape():
    """The inventory is the policy's: a run of the old shape does not pass the new shape's policy, nor the reverse."""
    old, new = (kit.policy(commit=H, shape=shape) for shape in ("old", "new"))
    with pytest.raises(rc.CandidateError, match="short-matrix"):
        rc.validate(evidence(policy=old), EXPECTED, new)
    with pytest.raises(rc.CandidateError, match="short-matrix"):
        rc.validate(evidence(policy=new), EXPECTED, old)


def test_validate_refusals():
    broken(lambda d: None, "invalid expected head", expected=rc.ExpectedCandidate(9, 5, "x", T))
    broken(lambda d: None, "wrong candidate repository", policy=kit.policy(
        commit=H, workflow=".github/workflows/other.yml"))
    broken(lambda d: d["run"].update(run_attempt=2), "original attempt")
    broken(lambda d: d["run"].update(path="x"), "wrong candidate repository")
    broken(lambda d: d["run"].update(conclusion="failure"), "not completed successfully")
    broken(lambda d: d["run"].update(event="push"), "event is not")
    broken(lambda d: d["run"].update(head_sha="x"), "exact expected head")
    broken(lambda d: d["pr"].update(state="closed"), "no longer the expected")
    broken(lambda d: d["run"].update(head_branch="release/v9.9.9"), "branch/title")
    broken(lambda d: d["proof"].update(tree="x"), "source artifact differs")
    broken(lambda d: d["proof"].update(base="x"), "release base changed")
    broken(lambda d: d["head"].update(sha="x"), "live release head tree")
    broken(lambda d: d["source"].update(sha="x"), "tested checkout tree")
    broken(lambda d: d["source"].update(parents=[]), "PR merge source")
    broken(lambda d: d["proof"].update(pr=7, source_commit=H), "dispatch source", event="workflow_dispatch")
    broken(lambda d: d["jobs"].pop(), "short-matrix")
    broken(lambda d: d["jobs"][1].update(id=d["jobs"][0]["id"]), "duplicate candidate job IDs")
    broken(lambda d: d["jobs"][0].update(conclusion="failure"), "failed, changed")
    broken(lambda d: d["jobs"][0].update(runner_id=0), "never acquired a runner")
    broken(lambda d: d["run"].update(created_at=None), "timestamps missing")
    broken(lambda d: d["run"].update(updated_at="2025-01-01T00:00:00+00:00"), "timestamps reversed")


def verify_world(mutate=None):
    ev = evidence()
    proof = ev["proof"]
    pins = kit.policy(commit=H)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(rc.PROOF_FILE, json.dumps(proof))
    archive = buf.getvalue()
    table = {"actions/runs/9": ev["run"], "pulls/5": ev["pr"], f"commits/{H}": ev["head"], f"commits/{S}": ev["source"],
             "actions/runs/9/artifacts?per_page=100": {"total_count": 1, "artifacts": [
                 {"name": rc.ARTIFACT, "expired": False, "id": 3, "workflow_run": {"id": 9, "head_sha": H}}]}}
    jobs = ev["jobs"]
    table["actions/runs/9/attempts/1/jobs?per_page=100&page=1"] = {"total_count": len(jobs), "jobs": jobs}
    if mutate:
        mutate(table)
    return table, (lambda p: table[p]), (lambda i: archive), pins


def test_verify_collects_and_validates():
    _table, get, download, pins = verify_world()
    assert rc.verify(get, download, EXPECTED, pins)["run_id"] == 9


def test_verify_refusals():
    table, get, download, pins = verify_world()
    with pytest.raises(rc.CandidateError, match="invalid expected candidate identity"):
        rc.verify(get, download, rc.ExpectedCandidate(0, 5, H, T), pins)
    table["actions/runs/9"] = dict(table["actions/runs/9"], status="in_progress")
    with pytest.raises(rc.CandidateError, match="not terminal"):
        rc.verify(get, download, EXPECTED, pins)
    for artifacts, msg in (({"total_count": 2, "artifacts": []}, "truncated artifact"),
                           ({"total_count": 0, "artifacts": []}, "absent or duplicated"),
                           ({"total_count": 1, "artifacts": [{"name": rc.ARTIFACT, "expired": False, "id": 3, "workflow_run": {"id": 8, "head_sha": H}}]}, "another run"),
                           ({"total_count": 1, "artifacts": [{"name": rc.ARTIFACT, "expired": False, "id": 0, "workflow_run": {"id": 9, "head_sha": H}}]}, "malformed source artifact ID")):
        table, get, download, pins = verify_world(lambda t, a=artifacts: t.update({"actions/runs/9/artifacts?per_page=100": a}))
        with pytest.raises(rc.CandidateError, match=msg):
            rc.verify(get, download, EXPECTED, pins)
    table, get, download, pins = verify_world()
    with pytest.raises(rc.CandidateError, match="exceeds bound"):
        rc.verify(get, lambda i: b"x" * (1024 * 1024 + 1), EXPECTED, pins)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("other.json", "{}")
    with pytest.raises(rc.CandidateError, match="unexpected candidate source archive"):
        rc.verify(get, lambda i: buf.getvalue(), EXPECTED, pins)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(rc.PROOF_FILE, json.dumps({"source_commit": "x"}))
    with pytest.raises(rc.CandidateError, match="malformed candidate source identities"):
        rc.verify(get, lambda i: buf.getvalue(), EXPECTED, pins)
    flips = iter([0, 1])
    real = dict(table)
    def changing(path):
        if path == "actions/runs/9" and next(flips, 1):
            return dict(real[path], updated_at="later")
        return real[path]
    with pytest.raises(rc.CandidateError, match="changed during evidence"):
        rc.verify(changing, download, EXPECTED, pins)
