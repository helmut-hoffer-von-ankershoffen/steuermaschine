"""release_candidate: version selection, dispatch lookup, job and run inventories, validation of a complete candidate,
the verify collection on a fake API, the recorder and the command line. No network, no git, no gh."""

import base64
import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel.native.queue_lib import release_candidate as rc

H, T, B, S = "1" * 40, "2" * 40, "3" * 40, "4" * 40
V = "1.2.3"
PINS = {rc.WORKFLOW: "a" * 64, rc.RECORDER: "b" * 64}


def make_pr(**over):
    pr = {"number": 5, "state": "open", "draft": False, "title": f"chore(release): v{V}",
          "head": {"sha": H, "ref": f"release/v{V}", "repo": {"full_name": rc.REPOSITORY}},
          "base": {"sha": B, "ref": "main", "repo": {"full_name": rc.REPOSITORY}}}
    return pr | over


def jobs_for(event="pull_request", run_id=9):
    out = []
    for i, (name, concl) in enumerate(rc.expected_jobs(event).items(), start=1):
        job = {"id": i, "name": name, "run_id": run_id, "run_attempt": 1, "head_sha": H, "status": "completed", "conclusion": concl}
        if concl == "success":
            job |= {"runner_id": 4, "steps": [{"n": 1}]}
        out.append(job)
    return out


def evidence(event="pull_request", run_id=9):
    run = {"id": run_id, "run_attempt": 1, "status": "completed", "conclusion": "success", "event": event, "path": rc.WORKFLOW,
           "head_sha": H, "head_branch": f"release/v{V}", "repository": {"full_name": rc.REPOSITORY},
           "head_repository": {"full_name": rc.REPOSITORY}, "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T01:00:00+00:00"}
    pr_proof = event == "pull_request"
    proof = {"schema": 1, "repository": rc.REPOSITORY, "event": event, "run_id": run_id, "run_attempt": 1, "head": H, "tree": T,
             "version": V, "sources": PINS, "base": B, "source_commit": S if pr_proof else H, "pr": 5 if pr_proof else None}
    source = {"sha": proof["source_commit"], "commit": {"tree": {"sha": T}}, "parents": [{"sha": B}, {"sha": H}]}
    return {"run": run, "jobs": jobs_for(event, run_id), "proof": proof, "pr": make_pr(), "source": source,
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


def test_command(monkeypatch):
    with pytest.raises(rc.CandidateError, match="unsupported"):
        rc.command(["rm", "x"])
    monkeypatch.setattr(rc.shutil, "which", lambda name: None)
    with pytest.raises(rc.CandidateError, match="not installed"):
        rc.command(["git", "x"])
    seen = []
    monkeypatch.setattr(rc.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(rc.edge, "run", lambda argv, **k: seen.append(argv) or SimpleNamespace(stdout=b"out"))
    assert rc.command(["gh", "api", "x"]) == b"out" and seen == [["/bin/gh", "api", "x"]]
    monkeypatch.setattr(rc, "command", lambda argv: b" abc\n")
    assert rc.git("rev-parse") == "abc"


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


def test_dispatch_pull():
    pr = make_pr()

    def get(path):
        return [pr]
    assert rc.dispatch_pull(H, f"release/v{V}", get)["number"] == 5
    with pytest.raises(rc.CandidateError, match="exact source head"):
        rc.dispatch_pull("x", "r", get)
    with pytest.raises(rc.CandidateError, match="may be truncated"):
        rc.dispatch_pull(H, f"release/v{V}", lambda p: [pr] * 100)
    with pytest.raises(rc.CandidateError, match="no unique"):
        rc.dispatch_pull(H, "release/v9.9.9", get)


def test_context_select_and_git(monkeypatch, tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": make_pr()}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REPOSITORY", rc.REPOSITORY)
    monkeypatch.setenv("GITHUB_REF_NAME", "x")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    assert rc.context()[0] == "pull_request"
    assert rc.select_release() is True
    event.write_text(json.dumps({"pull_request": make_pr(draft=True)}))
    with pytest.raises(rc.CandidateError, match="open non-draft"):
        rc.select_release()
    event.write_text(json.dumps({"pull_request": make_pr(title="other")}))
    assert rc.select_release() is False
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_REF_NAME", f"release/v{V}")
    monkeypatch.setenv("GITHUB_SHA", H)
    monkeypatch.setattr(rc, "command", lambda argv: json.dumps([make_pr()]).encode())
    assert rc.select_release() is True


def test_record(monkeypatch, tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": make_pr()}))
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / ".github/workflows").mkdir(parents=True)
    (root / "pyproject.toml").write_text(f'[project]\nversion = "{V}"\n')
    (root / rc.WORKFLOW).write_text("w")
    (root / rc.RECORDER).write_text("r")
    for k, v in {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_REPOSITORY": rc.REPOSITORY, "GITHUB_REF_NAME": "x",
                 "GITHUB_EVENT_PATH": str(event), "GITHUB_SHA": S, "GITHUB_RUN_ID": "9", "GITHUB_RUN_ATTEMPT": "1"}.items():
        monkeypatch.setenv(k, v)
    answers = {("rev-parse", "HEAD"): S, ("rev-parse", "HEAD^{tree}"): T, ("rev-parse", f"{H}^{{tree}}"): T}
    monkeypatch.setattr(rc, "git", lambda *args: answers.get(args, B))
    proof = rc.record(root)
    assert proof["pr"] == 5 and proof["sources"][rc.WORKFLOW] == hashlib.sha256(b"w").hexdigest() and proof["run_id"] == 9
    answers[("rev-parse", f"{H}^{{tree}}")] = "5" * 40
    with pytest.raises(rc.CandidateError, match="tested merge tree"):
        rc.record(root)
    answers[("rev-parse", f"{H}^{{tree}}")] = T
    monkeypatch.setenv("GITHUB_SHA", "other")
    with pytest.raises(rc.CandidateError, match="differs from the Actions event SHA"):
        rc.record(root)
    monkeypatch.setenv("GITHUB_SHA", S)
    (root / "pyproject.toml").write_text('[project]\nversion = "9.9.9"\n')
    with pytest.raises(rc.CandidateError, match="project version differs"):
        rc.record(root)
    (root / "pyproject.toml").write_text(f'[project]\nversion = "{V}"\n')
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_REF_NAME", f"release/v{V}")
    monkeypatch.setenv("GITHUB_SHA", H)
    answers[("rev-parse", "HEAD")] = H
    out = rc.record(root)
    assert out["pr"] is None and out["head"] == H
    monkeypatch.setenv("GITHUB_REPOSITORY", "other/x")
    with pytest.raises(rc.CandidateError, match="not an owner-repository"):
        rc.record(root)


def test_quarantine_lane():
    def lane(*names, concl="success", attempt=1):
        return [{"name": n, "run_id": 9, "run_attempt": attempt, "head_sha": H, "status": "completed", "conclusion": concl} for n in names]
    base = [{"name": "x"}]
    assert rc.quarantine_lane(base, 9, H) == base
    good = lane(rc.QUARANTINE_REGISTRY, rc.QUARANTINE_PLAN) + lane(*rc.QUARANTINE_RUNS, concl="failure")
    assert rc.quarantine_lane(base + good, 9, H) == base
    idle = lane(rc.QUARANTINE_REGISTRY, rc.QUARANTINE_PLAN) + lane(rc.QUARANTINE_IDLE, concl="skipped")
    assert rc.quarantine_lane(base + idle, 9, H) == base
    with pytest.raises(rc.CandidateError, match="missing, extra or duplicated"):
        rc.quarantine_lane(lane(rc.QUARANTINE_REGISTRY), 9, H)
    with pytest.raises(rc.CandidateError, match="failed its registry check"):
        rc.quarantine_lane(lane(rc.QUARANTINE_REGISTRY, concl="failure") + lane(rc.QUARANTINE_PLAN) + lane(*rc.QUARANTINE_RUNS), 9, H)
    with pytest.raises(rc.CandidateError, match="failed its registry check"):
        rc.quarantine_lane([*good], 8, H)


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
    assert rc.source_bytes(lambda p: blob, rc.WORKFLOW, S) == b"hello world"
    with pytest.raises(rc.CandidateError, match="invalid reviewed source request"):
        rc.source_bytes(lambda p: blob, "other", S)
    with pytest.raises(rc.CandidateError, match="source blob missing"):
        rc.source_bytes(lambda p: {"encoding": "x"}, rc.WORKFLOW, S)


def broken(mutate, msg, event="pull_request", expected=EXPECTED, pins=PINS):
    data = evidence(event)
    mutate(data)
    with pytest.raises(rc.CandidateError, match=msg):
        rc.validate(data, expected, pins)


def test_validate_accepts_both_events():
    out = rc.validate(evidence(), EXPECTED, PINS)
    assert out["job_count"] == len(rc.expected_jobs("pull_request")) and out["workflow_wall_seconds"] == 3600
    out = rc.validate(evidence("workflow_dispatch"), EXPECTED, PINS)
    assert out["event"] == "workflow_dispatch"


def test_validate_refusals():
    broken(lambda d: None, "invalid expected head", expected=rc.ExpectedCandidate(9, 5, "x", T))
    broken(lambda d: None, "missing reviewed CI source pins", pins={rc.WORKFLOW: "a" * 64})
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
    sources = {rc.WORKFLOW: b"wf", rc.RECORDER: b"rec"}
    pins = {k: hashlib.sha256(v).hexdigest() for k, v in sources.items()}
    proof["sources"] = pins
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(rc.PROOF_FILE, json.dumps(proof))
    archive = buf.getvalue()
    table = {"actions/runs/9": ev["run"], "pulls/5": ev["pr"], f"commits/{H}": ev["head"], f"commits/{S}": ev["source"],
             "actions/runs/9/artifacts?per_page=100": {"total_count": 1, "artifacts": [
                 {"name": rc.ARTIFACT, "expired": False, "id": 3, "workflow_run": {"id": 9, "head_sha": H}}]}}
    jobs = ev["jobs"]
    table["actions/runs/9/attempts/1/jobs?per_page=100&page=1"] = {"total_count": len(jobs), "jobs": jobs}
    for path, content in sources.items():
        table[f"contents/{path}?ref={S}"] = {"encoding": "base64", "type": "file", "content": base64.b64encode(content).decode()}
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
    with pytest.raises(rc.CandidateError, match="differs from independent review"):
        rc.verify(get, download, EXPECTED, {rc.WORKFLOW: "0" * 64, rc.RECORDER: "1" * 64})
    flips = iter([0, 1])
    real = dict(table)
    def changing(path):
        if path == "actions/runs/9" and next(flips, 1):
            return dict(real[path], updated_at="later")
        return real[path]
    with pytest.raises(rc.CandidateError, match="changed during evidence"):
        rc.verify(changing, download, EXPECTED, pins)


def test_main_cli(monkeypatch, tmp_path, capsys):
    out = tmp_path / "out" / "o.txt"
    out.parent.mkdir()
    monkeypatch.setattr(rc, "select_release", lambda: True)
    monkeypatch.setattr(rc.sys, "argv", ["rc", "select", "--output", str(out)])
    assert rc.main() == 0 and out.read_text() == "full_release=true\n"
    monkeypatch.setattr(rc, "verify", lambda get, dl, exp, pins: {"ok": exp.run_id})
    monkeypatch.setattr(rc, "command", lambda argv: b'{"a": 1}')
    monkeypatch.setattr(rc.sys, "argv", ["rc", "verify", "--run-id", "9", "--pr", "5", "--head", H, "--tree", T,
                                          "--workflow-sha256", "a" * 64, "--recorder-sha256", "b" * 64])
    assert rc.main() == 0 and '"ok": 9' in capsys.readouterr().out
    seen = {}
    def fake_verify(get, dl, exp, pins):
        seen["get"], seen["dl"] = get("x"), dl(3)
        return {}
    monkeypatch.setattr(rc, "verify", fake_verify)
    rc.main()
    assert seen == {"get": {"a": 1}, "dl": b'{"a": 1}'}
    monkeypatch.setattr(rc, "record", lambda root: {"p": 1})
    target = tmp_path / "rec" / "p.json"
    monkeypatch.setattr(rc.sys, "argv", ["rc", "record", "--output", str(target)])
    assert rc.main() == 0 and json.loads(target.read_text()) == {"p": 1}
