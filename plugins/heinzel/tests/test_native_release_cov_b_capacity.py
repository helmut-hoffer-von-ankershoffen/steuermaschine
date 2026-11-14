"""candidate_capacity collection helpers: bounded API reads, pagination, annotations, artifact download."""

import json
from datetime import UTC
from types import SimpleNamespace

import policy_kit as kit
import pytest

from steuermaschine_heinzel.native.queue_lib import hzlib
from steuermaschine_heinzel.native.release_lib import candidate_capacity as cap


@pytest.fixture(autouse=True)
def reviewed_policy(monkeypatch):
    """Core's release policy at the release head: the old CI shape, describing the fixture workflow and recorder."""
    monkeypatch.setattr(cap, "reviewed", lambda head: kit.policy(commit=head))


def fake_get(monkeypatch, table):
    """table: path -> value or callable."""
    def ghj(*args, default=None):
        path = args[1].removeprefix(f"repos/{hzlib.CORE}/")
        value = table.get(path, default)
        return value(path) if callable(value) else value
    monkeypatch.setattr(hzlib, "ghj", ghj)


def test_encoded_digest_and_bound():
    assert cap.digest({"b": 1, "a": 2}) == cap.digest({"a": 2, "b": 1})
    with pytest.raises(cap.InvalidCapacity):
        cap.encoded("x" * (cap.LIMIT + 1))
    with pytest.raises(ValueError):
        cap.encoded(float("nan"))


def test_get_requires_evidence(monkeypatch):
    fake_get(monkeypatch, {"a": {"k": 1}})
    assert cap.get("a") == {"k": 1}
    with pytest.raises(cap.InvalidCapacity, match="unavailable"):
        cap.get("missing")


def test_download(monkeypatch):
    monkeypatch.setattr(cap.edge, "run", lambda cmd, **k: SimpleNamespace(returncode=0, stdout=b"zip"))
    assert cap.download(5) == b"zip"
    monkeypatch.setattr(cap.edge, "run", lambda cmd, **k: SimpleNamespace(returncode=1, stdout=b""))
    with pytest.raises(cap.InvalidCapacity):
        cap.download(5)
    monkeypatch.setattr(cap.edge, "run", lambda cmd, **k: SimpleNamespace(returncode=0, stdout=b"x" * (1024 * 1024 + 1)))
    with pytest.raises(cap.InvalidCapacity):
        cap.download(5)


def pages(rows_per_page, total, key="jobs"):
    def get(path):
        page = int(path.rsplit("page=", 1)[1])
        return {"total_count": total, key: rows_per_page(page)}
    return get


def test_paged_single_and_multi(monkeypatch):
    def rows(page):
        return [{"id": 1}, {"id": 2}]
    fake_get(monkeypatch, {"p?per_page=100&page=1": pages(rows, 2)})
    assert cap.paged("p", "jobs") == [{"id": 1}, {"id": 2}]
    def two(page):
        return [{"id": page * 10}]
    fake_get(monkeypatch, {f"p?x=1&per_page=100&page={n}": pages(two, 2) for n in (1, 2)})
    assert [r["id"] for r in cap.paged("p?x=1", "jobs")] == [10, 20]


@pytest.mark.parametrize("data,msg", [
    ([], "malformed capacity inventory"),
    ({"total_count": "2", "jobs": []}, "malformed capacity inventory"),
    ({"total_count": 0, "jobs": []}, "total changed"),
    ({"total_count": 1, "jobs": [{"id": 1}, {"id": 2}]}, "extra capacity"),
    ({"total_count": 1, "jobs": [{"id": 0}]}, "invalid capacity inventory IDs"),
    ({"total_count": 2, "jobs": [{"id": 1}, {"id": 1}]}, "duplicate"),
    ({"total_count": 3, "jobs": []}, "incomplete"),
])
def test_paged_rejects(monkeypatch, data, msg):
    fake_get(monkeypatch, {"p?per_page=100&page=1": data})
    with pytest.raises(cap.InvalidCapacity, match=msg):
        cap.paged("p", "jobs")


def test_paged_changed_total_and_overflow(monkeypatch):
    def get(path):
        page = int(path.rsplit("page=", 1)[1])
        return {"total_count": 3 if page == 1 else 4, "jobs": [{"id": page}]}
    fake_get(monkeypatch, {f"p?per_page=100&page={n}": get for n in (1, 2)})
    with pytest.raises(cap.InvalidCapacity, match="total changed"):
        cap.paged("p", "jobs")
    def never(path):
        return {"total_count": 1000, "jobs": [{"id": int(path.rsplit("page=", 1)[1])}]}
    fake_get(monkeypatch, {f"p?per_page=100&page={n}": never for n in range(1, 11)})
    with pytest.raises(cap.InvalidCapacity, match="pagination exceeded"):
        cap.paged("p", "jobs")


def job(i=7):
    return {"id": i, "check_run_url": f"https://api.github.com/repos/{hzlib.CORE}/check-runs/{i}"}


def test_annotations(monkeypatch):
    fake_get(monkeypatch, {"check-runs/7/annotations?per_page=100&page=1": [{"m": 1}]})
    assert cap.annotations(job()) == [{"m": 1}]
    with pytest.raises(cap.InvalidCapacity, match="not bound"):
        cap.annotations({"id": 7, "check_run_url": "https://elsewhere"})
    fake_get(monkeypatch, {"check-runs/7/annotations?per_page=100&page=1": {"x": 1}})
    with pytest.raises(cap.InvalidCapacity, match="malformed annotation"):
        cap.annotations(job())
    full = [{"m": i} for i in range(100)]
    fake_get(monkeypatch, {f"check-runs/7/annotations?per_page=100&page={n}": full for n in range(1, 11)})
    with pytest.raises(cap.InvalidCapacity, match="exceeds bound"):
        cap.annotations(job())
    fake_get(monkeypatch, {"check-runs/7/annotations?per_page=100&page=1": full,
                           "check-runs/7/annotations?per_page=100&page=2": []})
    assert len(cap.annotations(job())) == 100


def test_require():
    cap.require(True, "x")
    with pytest.raises(cap.InvalidCapacity, match="boom"):
        cap.require(False, "boom")
    assert json.loads(cap.encoded({"a": 1})) == {"a": 1}


# ---- job-level checks
def good_job(**kw):
    base = {"id": 3, "run_id": 9, "head_sha": "h", "status": "completed", "run_attempt": 1, "runner_id": 4,
            "runner_name": "r", "started_at": "2026-01-01T00:00:00+00:00", "completed_at": "2026-01-01T00:01:00+00:00",
            "steps": [{"status": "completed", "conclusion": "success"}]}
    return base | kw


def test_job_shape():
    cap.job_shape(good_job(), 9, "h")
    for bad in (good_job(id=0), good_job(run_id=1), good_job(head_sha="x"), good_job(status="queued")):
        with pytest.raises(cap.InvalidCapacity, match="malformed"):
            cap.job_shape(bad, 9, "h")
    with pytest.raises(cap.InvalidCapacity, match="attempt"):
        cap.job_shape(good_job(run_attempt=3), 9, "h")


def test_executed():
    cap.executed(good_job())
    for bad in (good_job(runner_id=0), good_job(runner_name=""), good_job(steps=[]), good_job(started_at=None)):
        with pytest.raises(cap.InvalidCapacity, match="never executed"):
            cap.executed(bad)
    with pytest.raises(cap.InvalidCapacity, match="clocks reversed"):
        cap.executed(good_job(completed_at="2025-01-01T00:00:00+00:00"))
    with pytest.raises(cap.InvalidCapacity, match="unsuccessful"):
        cap.executed(good_job(steps=[{"status": "completed", "conclusion": "failure"}]))


def test_clock():
    with pytest.raises(cap.InvalidCapacity, match="missing"):
        cap.clock(None)
    with pytest.raises(cap.InvalidCapacity, match="timezone"):
        cap.clock("2026-01-01T00:00:00")
    assert cap.clock("2026-01-01T00:00:00+00:00").year == 2026


def steps(*names):
    return [{"name": n, "conclusion": "success"} for n in names]


def test_replacement_steps():
    cap.replacement_steps({"name": "CI OK", "steps": steps("Fail if any required job failed")})
    cap.replacement_steps({"name": "Build", "steps": steps(*(cap.BUILD_STEPS | {
        "Run make build", "Run actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9"}))})
    cap.replacement_steps({"name": "Heinzel Notifier (macOS app)", "steps": steps(
        "Run make notifier", "Real launches leave no process behind (ADR 0043)",
        "Pack the app, keeping its executable bit and signature",
        "Run actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9")})
    unit = steps("Unit and integration tests (unit first, 4 workers, stop at the first failure)",
                 "Install Python 3.15 dependencies with the verified PyYAML compatibility build",
                 "Select the locked wheel Node without executing it")
    cap.replacement_steps({"name": "Test (macos-latest, Python 3.15, unit)", "steps": unit})
    integ = steps("Unit and integration tests (unit first, 4 workers, stop at the first failure)", "Install wheel dependencies")
    cap.replacement_steps({"name": "Test (macos-latest, Python 3.12, integration)", "steps": integ})
    with pytest.raises(cap.InvalidCapacity, match="replacement lacks"):
        cap.replacement_steps({"name": "Test (macos-latest, Python 3.12, unit)", "steps": integ})
    with pytest.raises(cap.InvalidCapacity, match="replacement lacks"):
        cap.replacement_steps({"name": "Other", "steps": []})


def annotation(msg, level):
    return {"path": ".github", "blob_href": f"https://github.com/{hzlib.CORE}/blob/h/.github",
            "annotation_level": level, "message": msg}


def test_never_acquired():
    job = {"conclusion": "cancelled", "runner_id": 0, "runner_name": "", "steps": [], "labels": ["macos-latest"]}
    rows = [annotation(cap.NOT_ACQUIRED, "failure"), annotation(cap.CAPACITY, "notice")]
    cap.never_acquired(job, rows, "h")
    with pytest.raises(cap.InvalidCapacity, match="never-acquired"):
        cap.never_acquired(dict(job, runner_id=5), rows, "h")
    with pytest.raises(cap.InvalidCapacity, match="unknown capacity annotation"):
        cap.never_acquired(job, [], "h")
    with pytest.raises(cap.InvalidCapacity, match="unknown capacity annotation"):
        cap.never_acquired(job, [annotation("other", "failure")], "h")
    with pytest.raises(cap.InvalidCapacity, match="specific"):
        cap.never_acquired(job, [annotation(cap.NOT_ACQUIRED, "failure")], "h")


def test_stable_job_and_digest_of_job():
    assert set(cap.stable_job({"id": 1, "extra": 2})) == set(cap.REUSED)


def workflow(needs_test="", needs_ci="[build, test, notifier]"):
    lines = ["jobs:", "  notifier:", "    runs-on: x", "  build:", "    needs: notifier", "  test:"]
    if needs_test:
        lines.append(f"    needs: {needs_test}")
    return "\n".join([*lines, "  ci-ok:", f"    needs: {needs_ci}"])


def closure_data(text, names):
    import base64

    from steuermaschine_heinzel.native.queue_lib import release_candidate as strict
    return {"blobs": {strict.WORKFLOW: base64.b64encode(text.encode()).decode()}, "proof": {"head": "1" * 40},
            "jobs": [{"id": i, "name": n} for i, n in names.items()]}


def test_dependency_closure():
    names = {1: "Heinzel Notifier (macOS app)", 2: "Test (macos-latest, Python 3.12, unit)"}
    assert cap.dependency_closure(closure_data(workflow(), names), [1]) == {"Build", "CI OK"}
    data = closure_data(workflow(), names)
    assert cap.dependency_closure(data, [2]) == {"CI OK"}


def test_dependency_closure_refusals():
    names = {2: "Test (macos-latest, Python 3.12, unit)"}
    with pytest.raises(cap.InvalidCapacity, match="duplicate workflow job"):
        cap.dependency_closure(closure_data("  a:\n  a:\n", names), [2])
    with pytest.raises(cap.InvalidCapacity, match="duplicate workflow needs"):
        cap.dependency_closure(closure_data("  a:\n    needs: b\n    needs: c\n  b:\n  c:\n", names), [2])
    with pytest.raises(cap.InvalidCapacity, match="unreviewed"):
        cap.dependency_closure(closure_data("  a:\n    needs: ${{ x }}\n", names), [2])
    with pytest.raises(cap.InvalidCapacity, match="incomplete workflow"):
        cap.dependency_closure(closure_data("  a:\n    needs: zzz\n", names), [2])
    with pytest.raises(cap.InvalidCapacity, match="unapproved descendant"):
        cap.dependency_closure(closure_data(workflow("notifier").replace("  ci-ok:", "  extra:\n    needs: test\n  ci-ok:"), names), [2])


# ---- refusals, identity, clocks
def test_safe_refusal():
    assert cap.safe_refusal(cap.InvalidCapacity("skipped control acquired execution")) == "skipped control acquired execution"
    assert cap.safe_refusal(ValueError("secret detail")) == "capacity evidence failed validation"


def test_same_pr_identity():
    cap.same_pr_identity({"a": 1, "mergeable_state": "blocked"}, {"a": 1, "mergeable_state": "clean"})
    cap.same_pr_identity({"a": 1}, {"a": 1})
    with pytest.raises(cap.InvalidCapacity, match="unapproved metadata"):
        cap.same_pr_identity({"a": 1}, {"a": 2})
    with pytest.raises(cap.InvalidCapacity, match="unapproved metadata"):
        cap.same_pr_identity({"a": 1, "mergeable_state": "x"}, {"a": 1})
    with pytest.raises(cap.InvalidCapacity, match="unapproved metadata"):
        cap.same_pr_identity([], {})
    with pytest.raises(cap.InvalidCapacity, match="not reviewed"):
        cap.same_pr_identity({"mergeable_state": "clean"}, {"mergeable_state": "blocked"})


def evidence():
    run = {k: k + "v" for k in cap.RUN_KEYS} | {"conclusion": "failure"}
    return {"pr": {"n": 1}, "proof": {"p": 1}, "head": {"h": 1}, "source": {"s": 1}, "artifact": {"a": 1},
            "artifact_zip": "z", "blobs": {"x": "y"}, "annotations": {"1": []}, "run": run,
            "jobs": [good_job(id=1, name="J")]}


def test_unchanged_original():
    import copy
    saved = evidence()
    cap.unchanged_original(saved, copy.deepcopy(saved))
    for key, msg in (("proof", "source or annotation"), ("annotations", "source or annotation")):
        fresh = copy.deepcopy(saved)
        fresh[key] = {"changed": 1}
        with pytest.raises(cap.InvalidCapacity, match=msg):
            cap.unchanged_original(saved, fresh)
    fresh = copy.deepcopy(saved)
    fresh["run"]["conclusion"] = "cancelled"
    with pytest.raises(cap.InvalidCapacity, match="run identity"):
        cap.unchanged_original(saved, fresh)
    fresh = copy.deepcopy(saved)
    fresh["jobs"][0]["conclusion"] = "failure"
    fresh["jobs"][0]["runner_name"] = "other"
    with pytest.raises(cap.InvalidCapacity, match="jobs/results"):
        cap.unchanged_original(saved, fresh)


def test_execution_tuple_and_boundary():
    from datetime import datetime
    assert "id" not in cap.execution_tuple({"id": 1, "name": "n"})
    intent = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
    job = good_job(steps=[{"conclusion": "success", "started_at": "2026-01-01T00:00:10+00:00",
                           "completed_at": "2026-01-01T00:00:20+00:00"}])
    cap.execution_boundary(job, intent, reused=True)
    with pytest.raises(cap.InvalidCapacity, match="predates intent"):
        cap.execution_boundary(job, intent, reused=False)
    with pytest.raises(cap.InvalidCapacity, match="lacks execution clocks"):
        cap.execution_boundary(good_job(steps=[{"conclusion": "success"}]), intent, True)
    with pytest.raises(cap.InvalidCapacity, match="step clocks reversed"):
        cap.execution_boundary(good_job(steps=[{"conclusion": "skipped", "started_at": "2026-01-01T00:00:30+00:00",
                                                "completed_at": "2026-01-01T00:00:20+00:00"}]), intent, True)
    with pytest.raises(cap.InvalidCapacity, match="job clocks reversed"):
        cap.execution_boundary(good_job(started_at="2026-01-02T00:00:00+00:00"), intent, True)
    cap.execution_boundary(good_job(steps=[{"conclusion": "skipped"}]), intent, True)


def test_skipped_carrier():
    base = {"runner_id": None, "runner_name": None, "conclusion": "skipped", "steps": []}
    cap.skipped_carrier(dict(base), dict(base))
    zero = dict(base, runner_id=0, runner_name="")
    cap.skipped_carrier(dict(zero), dict(zero))
    with pytest.raises(cap.InvalidCapacity, match="acquired execution"):
        cap.skipped_carrier(dict(base, runner_id=3, runner_name="r"), dict(base))
    with pytest.raises(cap.InvalidCapacity, match="acquired execution"):
        cap.skipped_carrier({"conclusion": "skipped", "steps": []}, dict(base))


class Rel:
    def __init__(self, tmp_path, data, raw=b"receipt", hold=True, pretag=None):
        import hashlib
        self.data, self.hold, self.saved = data, hold, []
        self.receipt = tmp_path / "receipt"
        self.receipt.write_bytes(raw)
        self.sha = hashlib.sha256(raw).hexdigest()
        self._pretag = pretag or {"draft": False, "pr": 1, "head": "h"}

    def paths(self):
        return (None, self.receipt)

    def state(self):
        return self.data

    def owned_hold_exists(self, version):
        return self.hold

    def pretag_identity(self, version):
        return self._pretag

    def remember(self, **fields):
        self.saved.append(fields)
        self.data = self.data | fields

    def now_iso(self):
        return "2026-01-01T00:00:00+00:00"


def test_ownership(tmp_path):
    data = {"version": "1.0.0", "freeze_token": "t"}
    rel = Rel(tmp_path, data)
    assert cap.ownership(rel, "1.0.0", rel.sha, "t") is data
    with pytest.raises(cap.InvalidCapacity, match="receipt CAS"):
        cap.ownership(rel, "1.0.0", "bad", "t")
    with pytest.raises(cap.InvalidCapacity, match="owned release hold"):
        cap.ownership(rel, "1.0.0", rel.sha, "other")
    rel.hold = False
    with pytest.raises(cap.InvalidCapacity, match="owned release hold"):
        cap.ownership(rel, "1.0.0", rel.sha, "t")
    rel2 = Rel(tmp_path, dict(data, snapshot={"x": 1}))
    with pytest.raises(cap.InvalidCapacity, match="pre-candidate"):
        cap.ownership(rel2, "1.0.0", rel2.sha, "t")


def args(**kw):
    base = dict(version="1.0.0", expected_receipt_sha256="", expected_freeze_token="t", candidate_run=5,
                candidate_capacity="prepare", expected_audit_sha256=None)
    return SimpleNamespace(**(base | kw))


def test_action_preconditions(tmp_path):
    rel = Rel(tmp_path, {"version": "1.0.0", "freeze_token": "t"})
    ns = args(expected_receipt_sha256=rel.sha, candidate_run="x")
    with pytest.raises(cap.InvalidCapacity, match="exact candidate run"):
        cap.action(rel, ns)
    rel._pretag = {"draft": True}
    with pytest.raises(cap.InvalidCapacity, match="ready pre-publication"):
        cap.action(rel, args(expected_receipt_sha256=rel.sha))
    rel._pretag = {"draft": False, "pr": 1, "head": "h"}
    rel.data["candidate_reacquisition"] = {"audit": {}, "request": None}
    with pytest.raises(cap.InvalidCapacity, match="already exists"):
        cap.action(rel, args(expected_receipt_sha256=rel.sha))
    del rel.data["candidate_reacquisition"]
    with pytest.raises(cap.InvalidCapacity, match="does not consume"):
        cap.action(rel, args(expected_receipt_sha256=rel.sha, expected_audit_sha256="x"))
    with pytest.raises(cap.InvalidCapacity, match="already intended"):
        cap.action(rel, args(expected_receipt_sha256=rel.sha, candidate_capacity="request"))


def test_consume_refusals(tmp_path):
    rel = Rel(tmp_path, {"version": "1.0.0", "freeze_token": "t"})
    with pytest.raises(cap.InvalidCapacity, match="no explicit capacity request"):
        cap.consume(rel, 5, 1)
    request = {"at": "2026-01-01T00:00:00+00:00", "operation": "rerun-failed-jobs", "run_id": 5, "expected_attempt": 2,
               "eligible_ids": [1], "closure": ["CI OK"], "status": "submitted"}
    audit = {"eligible_ids": [1], "closure": ["CI OK"], "original": {"proof": {"pr": 1}}}
    rel.data["candidate_reacquisition"] = {"audit": audit, "request": request}
    with pytest.raises(cap.InvalidCapacity, match="run/cell identity"):
        cap.consume(rel, 6, 1)
    with pytest.raises(cap.InvalidCapacity, match="differs from merge target"):
        cap.consume(rel, 5, 2)
    rel.data["candidate_reacquisition"]["request"] = dict(request, at="2026-01-01T00:00:00")
    with pytest.raises(cap.InvalidCapacity, match="timestamp"):
        cap.consume(rel, 5, 1)
    rel.data["candidate_reacquisition"]["request"] = dict(request, extra=1)
    with pytest.raises(cap.InvalidCapacity, match="unknown request protocol"):
        cap.consume(rel, 5, 1)
    rel.hold = False
    with pytest.raises(cap.InvalidCapacity, match="hold changed"):
        cap.consume(rel, 5, 1)


# ---- a complete authenticated candidate: identity, original inventory, audit validation
H, T, B, S = "1" * 40, "2" * 40, "3" * 40, "4" * 40
VERSION = "1.2.3"


def candidate_data(run_id=9):
    import base64

    from steuermaschine_heinzel.native.queue_lib import release_candidate as strict
    pins = kit.manifest()["sources"]
    proof = {"schema": 1, "repository": strict.REPOSITORY, "event": "pull_request", "version": VERSION, "run_id": run_id,
             "run_attempt": 1, "sources": pins, "pr": 5, "head": H, "tree": T, "base": B, "source_commit": S}
    run = {"id": run_id, "run_attempt": 1, "status": "completed", "conclusion": "failure", "event": "pull_request",
           "path": strict.WORKFLOW, "head_branch": f"release/v{VERSION}", "head_sha": H,
           "repository": {"full_name": strict.REPOSITORY}, "head_repository": {"full_name": strict.REPOSITORY}}
    pr = {"state": "open", "draft": False, "number": 5, "title": f"chore(release): v{VERSION}",
          "head": {"sha": H, "ref": f"release/v{VERSION}", "repo": {"full_name": strict.REPOSITORY}},
          "base": {"sha": B, "ref": "main", "repo": {"full_name": strict.REPOSITORY}}}
    head = {"sha": H, "commit": {"tree": {"sha": T}}}
    source = {"sha": S, "commit": {"tree": {"sha": T}}, "parents": [{"sha": B}, {"sha": H}]}
    blobs = {path: base64.b64encode(kit.files()[path]).decode() for path in pins}
    inventory = kit.expected_jobs("pull_request")
    jobs, annots, n = [], {}, 100
    for name, expected in inventory.items():
        n += 1
        base = {"id": n, "name": name, "run_id": run_id, "run_attempt": 1, "head_sha": H, "status": "completed",
                "runner_id": 7, "runner_name": "r", "started_at": "2026-01-01T00:00:00+00:00",
                "completed_at": "2026-01-01T00:01:00+00:00", "steps": [{"name": "s", "status": "completed", "conclusion": "success"}]}
        if name == "CI OK":
            base |= {"conclusion": "failure", "steps": [{"name": "Fail if any required job failed", "status": "completed", "conclusion": "failure"}]}
        elif expected == "skipped":
            base |= {"conclusion": "skipped", "steps": [], "runner_id": 0, "runner_name": ""}
        elif name == "Test (macos-latest, Python 3.12, unit)":
            base |= {"conclusion": "cancelled", "runner_id": 0, "runner_name": "", "steps": [], "labels": ["macos-latest"],
                     "check_run_url": f"https://api.github.com/repos/{hzlib.CORE}/check-runs/{n}"}
            annots[str(n)] = [annotation(cap.NOT_ACQUIRED, "failure") | {"blob_href": f"https://github.com/{hzlib.CORE}/blob/{H}/.github"},
                              annotation(cap.CAPACITY, "notice") | {"blob_href": f"https://github.com/{hzlib.CORE}/blob/{H}/.github"}]
        else:
            base |= {"conclusion": "success"}
        jobs.append(base)
    return {"run": run, "proof": proof, "pr": pr, "head": head, "source": source, "blobs": blobs, "jobs": jobs, "annotations": annots}


def test_identity_accepts_and_refuses():
    data = candidate_data()
    assert cap.identity(data, VERSION, 9, 1)["pr"] == 5

    def broken(mutate, msg):
        d = candidate_data()
        mutate(d)
        with pytest.raises(cap.InvalidCapacity, match=msg):
            cap.identity(d, VERSION, 9, 1)
    broken(lambda d: d["run"].update(id=3), "wrong capacity run/attempt")
    broken(lambda d: d["run"].update(event="push"), "wrong repository")
    broken(lambda d: d["proof"].update(tree="x"), "invalid proof identities")
    broken(lambda d: d["pr"].update(state="closed"), "release PR/base changed")
    broken(lambda d: d["run"].update(head_branch="release/v9.9.9"), "noncanonical release identity")
    broken(lambda d: d["proof"].update(schema=2), "source proof protocol")
    broken(lambda d: d["head"].update(sha="x"), "source tree/parents")
    broken(lambda d: d["blobs"].pop(next(iter(d["blobs"]))), "fingerprint inventory")
    import base64
    broken(lambda d: d["blobs"].update({next(iter(d["blobs"])): base64.b64encode(b"tampered").decode()}), "differs from reviewed pins")


def test_original_accepts_and_refuses():
    data = candidate_data()
    eligible = cap.original(data, VERSION, 9)
    assert len(eligible) == 1

    def broken(mutate, msg):
        d = candidate_data()
        mutate(d)
        with pytest.raises(cap.InvalidCapacity, match=msg):
            cap.original(d, VERSION, 9)
    broken(lambda d: d["run"].update(conclusion="success"), "terminal failure")
    broken(lambda d: d["jobs"].pop(), "inventory differs")
    broken(lambda d: d["jobs"][0].update(run_attempt=2), "rerun")
    def ci_ok(d):
        return next(j for j in d["jobs"] if j["name"] == "CI OK")
    broken(lambda d: ci_ok(d).update(steps=[]), "aggregate")
    def skipped(d):
        return next(j for j in d["jobs"] if j["conclusion"] == "skipped")
    broken(lambda d: skipped(d).update(steps=[{"x": 1}]), "skipped control")
    def non_mac(d):  # the job order follows a set (hash seed), so pick a non-Mac success job by name, not by index
        return next(j for j in d["jobs"] if j["conclusion"] == "success" and j["name"] not in ("CI OK", "Build")
                    and "macos" not in j["name"].lower() and "macOS" not in j["name"])
    broken(lambda d: non_mac(d).update(conclusion="failure"), "non-Mac workload failed")
    broken(lambda d: d["annotations"].update({"999": []}), "extra or missing annotated cells")
    broken(lambda d: d["annotations"].clear(), "unknown capacity annotation")
    nothing = candidate_data()
    for j in nothing["jobs"]:
        if j["conclusion"] == "cancelled":
            j.update(conclusion="success", runner_id=7, runner_name="r", steps=[{"status": "completed", "conclusion": "success"}])
    nothing["annotations"] = {}
    with pytest.raises(cap.InvalidCapacity, match="no missing execution"):
        cap.original(nothing, VERSION, 9)


def test_original_notifier_closure():
    d = candidate_data()
    notifier = next(j for j in d["jobs"] if j["name"] == "Heinzel Notifier (macOS app)")
    notifier.update(conclusion="cancelled", runner_id=0, runner_name="", steps=[], labels=["macos-latest"])
    d["annotations"][str(notifier["id"])] = list(next(iter(d["annotations"].values())))
    build = next(j for j in d["jobs"] if j["name"] == "Build")
    with pytest.raises(cap.InvalidCapacity, match="missing-notifier closure"):
        cap.original(d, VERSION, 9)
    build["steps"] = [{"name": n, "status": "completed", "conclusion": "skipped"} for n in cap.BUILD_STEPS]
    assert len(cap.original(d, VERSION, 9)) == 2
    assert cap.dependency_closure(d, cap.original(d, VERSION, 9)) == {"Build", "CI OK"}


def test_validate_audit():
    d = candidate_data()
    eligible = cap.original(d, VERSION, 9)
    audit = {"schema": cap.SCHEMA, "version": VERSION, "freeze_token": "t", "run_id": 9, "original": d,
             "original_sha256": cap.digest(d), "eligible_ids": eligible, "closure": sorted(cap.dependency_closure(d, eligible))}
    audit["audit_sha256"] = cap.digest(audit)
    assert cap.validate_audit(audit, VERSION, "t", 9) == eligible
    with pytest.raises(cap.InvalidCapacity, match="ownership identity"):
        cap.validate_audit(audit, VERSION, "other", 9)
    with pytest.raises(cap.InvalidCapacity, match="audit digest differs"):
        cap.validate_audit(dict(audit, original_sha256="x"), VERSION, "t", 9)
    with pytest.raises(cap.InvalidCapacity, match="replay inventory"):
        cap.validate_audit(dict(audit, eligible_ids=[1]), VERSION, "t", 9)
    with pytest.raises(cap.InvalidCapacity, match="fingerprint differs"):
        cap.validate_audit(dict(audit, audit_sha256="x"), VERSION, "t", 9)


def test_collect_original_with_fake_api(monkeypatch):
    d = candidate_data()
    run = dict(d["run"])
    monkeypatch.setattr(cap, "get", lambda path: run if path == "actions/runs/9" else {})
    monkeypatch.setattr(cap, "source_evidence", lambda rid, att: {"jobs": d["jobs"], **{k: d[k] for k in ("run", "proof", "pr", "head", "source", "blobs")}})
    monkeypatch.setattr(cap, "annotations", lambda job: d["annotations"][str(job["id"])])
    run.update(status="queued")
    with pytest.raises(cap.InvalidCapacity, match="must finish"):
        cap.collect_original(VERSION, 9)
    run.update(status="completed", run_attempt=1)
    out = cap.collect_original(VERSION, 9)
    assert set(out["annotations"]) == set(d["annotations"])


# ---- source_evidence on a fake API and artifact download
def evidence_world(monkeypatch, **over):
    import io
    import json as _json
    import zipfile

    from steuermaschine_heinzel.native.queue_lib import release_candidate as strict
    policy = kit.policy(commit=H)
    proof = {"pr": 5, "head": H, "source_commit": S, "schema": 1}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(strict.PROOF_FILE, _json.dumps(over.pop("proof", proof)))
    raw = buf.getvalue()
    run = {"id": 9, "head_sha": H, "status": "completed", "run_attempt": 1}
    artifact = {"id": 3, "name": strict.ARTIFACT, "expired": False, "workflow_run": {"id": 9, "head_sha": H}}
    table = {"actions/runs/9/attempts/1": over.get("run", run), "pulls/5": {"n": 5}, f"commits/{H}": {"h": 1}, f"commits/{S}": {"s": 1}}
    paged_table = {"actions/workflows/ci.yml/runs": over.get("candidates", [{"id": 9}]), "actions/runs/9/attempts/1/jobs": [{"id": 1}],
                   "actions/runs/9/artifacts": over.get("artifacts", [artifact])}
    monkeypatch.setattr(cap, "get", lambda path: table[path])
    monkeypatch.setattr(cap, "paged", lambda path, key: next(v for k, v in paged_table.items() if path.startswith(k)))
    monkeypatch.setattr(cap, "download", lambda artifact_id: raw)
    monkeypatch.setattr(cap.strict, "source_bytes", lambda get, path, source, pinned: path.encode())
    return table, policy


def test_source_evidence_collects(monkeypatch):
    _table, policy = evidence_world(monkeypatch)
    out = cap.source_evidence(9, 1)
    assert out["pr"] == {"n": 5} and set(out["blobs"]) == set(policy.sources) and out["jobs"] == [{"id": 1}]


def test_source_evidence_refusals(monkeypatch):
    evidence_world(monkeypatch, candidates=[{"id": 9}, {"id": 10}])
    with pytest.raises(cap.InvalidCapacity, match="competing"):
        cap.source_evidence(9, 1)
    evidence_world(monkeypatch, run={"id": 9, "head_sha": H, "status": "queued", "run_attempt": 1})
    with pytest.raises(cap.InvalidCapacity, match="not complete"):
        cap.source_evidence(9, 1)
    evidence_world(monkeypatch, artifacts=[])
    with pytest.raises(cap.InvalidCapacity, match="absent or duplicated"):
        cap.source_evidence(9, 1)
    from steuermaschine_heinzel.native.queue_lib import release_candidate as strict
    evidence_world(monkeypatch, artifacts=[{"id": 3, "name": strict.ARTIFACT, "expired": True, "workflow_run": {"id": 9, "head_sha": H}}])
    with pytest.raises(cap.InvalidCapacity, match="identity differs"):
        cap.source_evidence(9, 1)
    evidence_world(monkeypatch, proof={"pr": 0})
    with pytest.raises(cap.InvalidCapacity, match="canonical PR"):
        cap.source_evidence(9, 1)
    evidence_world(monkeypatch, proof={"pr": 5, "source_commit": "x"})
    with pytest.raises(cap.InvalidCapacity, match="tested source identity"):
        cap.source_evidence(9, 1)
    table, _ = evidence_world(monkeypatch)
    state = {"n": 0}
    first = table["actions/runs/9/attempts/1"]

    def changing(path):
        if path == "actions/runs/9/attempts/1":
            state["n"] += 1
            return first if state["n"] == 1 else dict(first, extra=1)
        return table[path]
    monkeypatch.setattr(cap, "get", changing)
    with pytest.raises(cap.InvalidCapacity, match="changed during collection"):
        cap.source_evidence(9, 1)
