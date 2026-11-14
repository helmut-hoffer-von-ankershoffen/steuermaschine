"""Release rejection history and evidence helpers on a private tmp HOME: private reads, the pinned history directory,
atomic persistence, alias reconciliation, paginated GitHub inventories, the quiet check, compose log prelude."""

import base64
import json
import os

import pytest

from steuermaschine_heinzel.native.queue_lib import hzlib
from steuermaschine_heinzel.native.release_lib import rejection as rej


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    (home / ".cache").mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    return home


def record(version="1.2.3", phase="archived", token="tok", extra=None):
    receipt = json.dumps({"version": version, "freeze_token": token}).encode()
    return {"schema": 1, "phase": phase, "receipt_base64": base64.b64encode(receipt).decode(),
            "decision": {"version": version, "token": token, "receipt_sha256": rej.digest(receipt)}} | (extra or {})


def test_decode():
    assert rej.decode(b'{"a": 1}') == {"a": 1}
    for raw in (b'{"a": 1, "a": 2}', b"[1]", b"{bad", b"\xff"):
        with pytest.raises(rej.InvalidRejection):
            rej.decode(raw)


def test_private_read(tmp_path, home):
    path = home / "f"
    path.write_bytes(b"x")
    path.chmod(0o600)
    assert rej.private_read(path) == b"x"
    path.chmod(0o644)
    with pytest.raises(rej.InvalidRejection, match="unsafe private"):
        rej.private_read(path)
    path.chmod(0o600)
    (home / "link").symlink_to(path)
    with pytest.raises(OSError):
        rej.private_read(home / "link")
    path.write_bytes(b"x" * (rej.LIMIT + 1))
    with pytest.raises(rej.InvalidRejection):
        rej.private_read(path)
    home.chmod(0o777)
    try:
        with pytest.raises(rej.InvalidRejection, match="parent directory"):
            rej.private_read(path)
    finally:
        home.chmod(0o700)
    other = home / "g"
    other.write_bytes(b"y")
    other.chmod(0o600)
    os.link(other, home / "h")
    with pytest.raises(rej.InvalidRejection, match="unsafe private"):
        rej.private_read(other)


def test_open_history_and_check_directory(home):
    with pytest.raises(FileNotFoundError):
        rej.open_history()
    fd = rej.open_history(create=True)
    try:
        assert rej.history().is_dir()
        rej.check_directory(fd)
        again = rej.open_history(create=True)
        os.close(again)
        os.rename(rej.history(), home / "moved")
        rej.history().mkdir(mode=0o700)
        with pytest.raises(rej.InvalidRejection, match="replaced"):
            rej.check_directory(fd)
    finally:
        os.close(fd)
    rej.history().chmod(0o755)
    with pytest.raises(rej.InvalidRejection, match="unsafe rejection history"):
        rej.open_history()


def test_persist_and_validate():
    fd = rej.open_history(create=True)
    try:
        data = record()
        rej.persist(fd, "1.2.3.json", data, initial=True)
        assert rej.decode(rej.private_read("1.2.3.json", fd))["phase"] == "archived"
        rej.persist(fd, "1.2.3.json", record(phase="complete"))
        with pytest.raises(rej.InvalidRejection, match="collision"):
            rej.persist(fd, "1.2.3.json", record(phase="archived"))
        with pytest.raises(rej.InvalidRejection, match="collision"):
            rej.persist(fd, "1.2.3.json", record(token="other"))
        assert not [n for n in os.listdir(fd) if n.startswith(".pending-")]
        big = record(extra={"pad": "x" * (rej.LIMIT + 1)})
        with pytest.raises(rej.InvalidRejection, match="exceeds limit"):
            rej.persist(fd, "9.9.9.json", big, initial=True)
    finally:
        os.close(fd)


def test_validate_record_refusals():
    ok = record()
    assert rej.validate_record(ok, "1.2.3.json")["version"] == "1.2.3"
    for bad in (dict(ok, schema=2), dict(ok, phase="weird"), ok):
        with pytest.raises(rej.InvalidRejection, match="unrecognized"):
            rej.validate_record(bad, "9.9.9.json" if bad is ok else "1.2.3.json")
    bad = record()
    bad["decision"]["receipt_sha256"] = "0" * 64
    with pytest.raises(rej.InvalidRejection, match="digest mismatch"):
        rej.validate_record(bad, "1.2.3.json")
    bad = record()
    bad["decision"]["token"] = "other"
    with pytest.raises(rej.InvalidRejection, match="identity mismatch"):
        rej.validate_record(bad, "1.2.3.json")


def test_records_guard_and_alias():
    assert rej.records() == []
    fd = rej.open_history(create=True)
    rej.persist(fd, "1.2.3.json", record(phase="complete"), initial=True)
    rej.persist(fd, "2.0.0.json", record("2.0.0", phase="archived"), initial=True)
    os.close(fd)
    (rej.history() / (".pending-" + "a" * 32)).write_bytes(b"")
    assert len(rej.records()) == 2
    with pytest.raises(rej.InvalidRejection, match="pending release rejection"):
        rej.guard("5.0.0")
    os.unlink(rej.history() / "2.0.0.json")
    with pytest.raises(rej.InvalidRejection, match="cannot be resumed"):
        rej.guard("1.2.3")
    rej.guard("5.0.0")
    (rej.history() / "junk").write_text("")
    with pytest.raises(rej.InvalidRejection, match="unknown rejection history entry"):
        rej.records()


def test_reconcile_archive_alias():
    fd = rej.open_history(create=True)
    try:
        rej.persist(fd, "1.2.3.json", record(), initial=True)
        rej.reconcile_archive_alias(fd, "1.2.3.json")  # one link: nothing to do
        alias = ".pending-" + "b" * 32
        os.link(rej.history() / "1.2.3.json", rej.history() / alias)
        rej.reconcile_archive_alias(fd, "1.2.3.json")
        assert not (rej.history() / alias).exists()
        os.link(rej.history() / "1.2.3.json", rej.history() / "foreign")
        with pytest.raises(rej.InvalidRejection, match="unknown hard-link alias"):
            rej.reconcile_archive_alias(fd, "1.2.3.json")
        os.link(rej.history() / "1.2.3.json", rej.history() / "foreign2")
        with pytest.raises(rej.InvalidRejection, match="unsafe archive hard-link"):
            rej.reconcile_archive_alias(fd, "1.2.3.json")
    finally:
        os.close(fd)


def test_api_and_paginated(monkeypatch):
    monkeypatch.setattr(hzlib, "gh", lambda *a: (1, ""))
    with pytest.raises(rej.InvalidRejection, match="unavailable"):
        rej.api("x")
    monkeypatch.setattr(hzlib, "gh", lambda *a: (0, "x" * (rej.LIMIT + 1)))
    with pytest.raises(rej.InvalidRejection, match="oversized"):
        rej.api("x")
    monkeypatch.setattr(hzlib, "gh", lambda *a: (0, '{"a": 1}'))
    assert rej.api("x") == {"a": 1}


def serve(monkeypatch, pages):
    monkeypatch.setattr(rej, "api", lambda endpoint: pages(int(endpoint.rsplit("page=", 1)[1])))


def test_paginated(monkeypatch):
    serve(monkeypatch, lambda p: {"total_count": 2, "w": [{"id": p}]})
    assert [r["id"] for r in rej.paginated("e?x=1", "w")] == [1, 2]
    for data, msg in (({"total_count": "x", "w": []}, "unknown or incomplete"),
                      ({"total_count": 1, "w": "no"}, "unknown or incomplete"),
                      ({"total_count": 1, "w": [{"id": 0}]}, "invalid or repeated"),
                      ({"total_count": 1, "w": [5]}, "invalid or repeated"),
                      ({"total_count": 5, "w": []}, "pagination incomplete"),
                      ({"total_count": 1, "w": [{"id": i} for i in range(101)]}, "changed during pagination")):
        serve(monkeypatch, lambda p, d=data: d)
        with pytest.raises(rej.InvalidRejection, match=msg):
            rej.paginated("e", "w")
    serve(monkeypatch, lambda p: {"total_count": 3 if p == 1 else 4, "w": [{"id": p}]})
    with pytest.raises(rej.InvalidRejection, match="changed during"):
        rej.paginated("e", "w")
    serve(monkeypatch, lambda p: {"total_count": 1, "w": [{"id": 1}, {"id": 2}]})
    with pytest.raises(rej.InvalidRejection, match="pagination incomplete"):
        rej.paginated("e", "w")


def inventory(monkeypatch, running=None, missing=False, extra_path=None, release="absent"):
    def paginated(endpoint, key):
        repo = hzlib.PLUGINS if hzlib.PLUGINS in endpoint else hzlib.CORE
        if key == "workflows":
            names = rej.PUBLICATION_WORKFLOWS[repo] | rej.ORDINARY_WORKFLOWS[repo]
            if missing:
                names = names - {sorted(rej.PUBLICATION_WORKFLOWS[repo])[0]}
            rows = [{"id": i + 1, "path": ".github/workflows/" + n} for i, n in enumerate(sorted(names))]
            return rows + ([{"id": 999, "path": extra_path}] if extra_path else [])
        if running and "status=queued" in endpoint:
            return [running(repo)]
        return []
    monkeypatch.setattr(rej, "paginated", paginated)
    payload = {"data": {"repository": {"nameWithOwner": hzlib.PLUGINS, "release": None if release == "absent" else {"tagName": "x"}}}}
    monkeypatch.setattr(hzlib, "gh", lambda *a: (0, json.dumps(payload)))


def test_quiet(monkeypatch):
    inventory(monkeypatch)
    rej.quiet()
    inventory(monkeypatch, missing=True)
    with pytest.raises(rej.InvalidRejection, match="incomplete"):
        rej.quiet()
    inventory(monkeypatch, extra_path=".github/workflows/ci.yml")
    with pytest.raises(rej.InvalidRejection, match="ambiguous workflow"):
        rej.quiet()
    inventory(monkeypatch, release="present")
    with pytest.raises(rej.InvalidRejection, match="combined app absent"):
        rej.quiet()

    def run_of(path_name):
        def make(repo):
            return {"workflow_id": 1, "path": path_name}
        return make
    inventory(monkeypatch, running=lambda repo: {"workflow_id": 12345, "path": "x"})
    with pytest.raises(rej.InvalidRejection, match="unknown active workflow"):
        rej.quiet()


def log_of(env_lines=None, prelude=True, extra=()):
    ts = "2026-01-01T00:00:00.0000000Z "
    env = env_lines if env_lines is not None else [
        f"  GH_REPO: {hzlib.PLUGINS}", f"  CORE_URL: {rej.URL}", f"  VERSION: {rej.VERSION}", "  REF: refs/heads/main"]
    lines = ['##[group]Run test "$REF" = refs/heads/main', "env:", *env, "##[endgroup]", *extra] if prelude else ["nothing"]
    return "\n".join(ts + line for line in lines)


def test_log_inputs():
    rej.log_inputs(log_of())
    with pytest.raises(rej.InvalidRejection, match="too large"):
        rej.log_inputs("x" * (32 * 1024 * 1024 + 1))
    with pytest.raises(rej.InvalidRejection, match="missing or ambiguous"):
        rej.log_inputs(log_of(prelude=False))
    ts = "2026-01-01T00:00:00.0000000Z "
    start = ts + '##[group]Run test "$REF" = refs/heads/main'
    with pytest.raises(rej.InvalidRejection, match="incomplete trusted"):
        rej.log_inputs(start + "\n" + ts + "env:")
    with pytest.raises(rej.InvalidRejection, match="unframed"):
        rej.log_inputs(start + "\nunframed\n" + ts + "##[endgroup]")
    with pytest.raises(rej.InvalidRejection, match="missing runner environment"):
        rej.log_inputs(start + "\n" + ts + "##[endgroup]")
    with pytest.raises(rej.InvalidRejection, match="inputs differ"):
        rej.log_inputs(log_of(env_lines=["  GH_REPO: evil"]))


def test_workflow_bytes():
    assert rej.workflow_bytes({"encoding": "base64", "content": base64.b64encode(b"abc").decode() + "\n"}) == b"abc"
    with pytest.raises(rej.InvalidRejection):
        rej.workflow_bytes({"encoding": "utf-8", "content": "x"})
    with pytest.raises(rej.InvalidRejection):
        rej.workflow_bytes({"encoding": "base64", "content": 5})


# ---- PyPI identity on a fake urlopen
class Response:
    def __init__(self, url, body):
        self.url, self.body = url, body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self.body[:n] if n >= 0 else self.body


WHEEL_URL = f"https://files.pythonhosted.org/packages/ab/{rej.WHEEL}"


def metadata(**over):
    wheel = {"filename": rej.WHEEL, "url": WHEEL_URL, "packagetype": "bdist_wheel", "digests": {"sha256": rej.WHEEL_SHA}, "size": 3}
    doc = {"info": {"name": "heinzel", "version": rej.VERSION}, "urls": [wheel]}
    return doc | over


def fake_urlopen(monkeypatch, meta_url="https://pypi.org/x", doc=None, wheel_url=WHEEL_URL, wheel=b"abc", meta_body=None):
    body = meta_body if meta_body is not None else json.dumps(doc or metadata()).encode()

    def urlopen(url, timeout=None):
        if "pypi.org/pypi" in url:
            return Response(meta_url, body)
        return Response(wheel_url, wheel)
    monkeypatch.setattr(rej.urllib.request, "urlopen", urlopen)


def test_pypi_identity(monkeypatch):
    monkeypatch.setattr(rej, "WHEEL_SHA", rej.digest(b"abc"))
    fake_urlopen(monkeypatch, doc=metadata())
    # the pinned digest in the fake metadata must follow the patched constant
    doc = metadata()
    doc["urls"][0]["digests"]["sha256"] = rej.digest(b"abc")
    fake_urlopen(monkeypatch, doc=doc)
    out = rej.pypi_identity(b"abc")
    assert out["pypi_size"] == 3 and out["pypi_sha256"] == rej.digest(b"abc")
    with pytest.raises(rej.InvalidRejection, match="bytes differ"):
        rej.pypi_identity(b"other")

    def refuses(msg, **kw):
        fake_urlopen(monkeypatch, **kw)
        with pytest.raises(rej.InvalidRejection, match=msg):
            rej.pypi_identity(b"abc")
    refuses("unsafe PyPI metadata redirect", meta_url="https://evil.example/x", doc=doc)
    refuses("exceeds limit", meta_body=b"x" * (rej.LIMIT + 1))
    refuses("identity mismatch", doc=dict(doc, info={"name": "other", "version": rej.VERSION}))
    refuses("unknown PyPI file inventory", doc=dict(doc, urls="x"))
    refuses("missing or ambiguous", doc=dict(doc, urls=[]))
    bad = json.loads(json.dumps(doc))
    bad["urls"][0]["size"] = 0
    refuses("digest/URL mismatch", doc=bad)
    refuses("unsafe PyPI wheel redirect", doc=doc, wheel_url="https://evil.example/w")


# ---- reject() argument validation and the evidence run identity
class Args:
    def __init__(self, **kw):
        base = dict(version=rej.VERSION, expected_receipt_sha256="a" * 64, expected_freeze_token="b" * 32, failed_compose_run=5,
                    failed_compose_attempt=1, expected_compose_head=rej.HEAD, expected_core_source=rej.SOURCE, reason="why")
        self.__dict__.update(base | kw)


@pytest.mark.parametrize("over", [
    {"version": "9.9.9"}, {"expected_compose_head": "x"}, {"expected_core_source": "x"}, {"failed_compose_run": 0},
    {"failed_compose_attempt": "1"}, {"expected_receipt_sha256": "zz"}, {"expected_freeze_token": None}, {"reason": "  "},
    {"reason": "x" * 4001},
])
def test_reject_requires_exact_identities(over):
    with pytest.raises(rej.InvalidRejection, match="exact reviewed"):
        rej.reject(None, Args(**over))


def test_reject_refuses_other_pending_record():
    fd = rej.open_history(create=True)
    rej.persist(fd, "0.1.0.json", record("0.1.0", phase="archived"), initial=True)
    os.close(fd)
    with pytest.raises(rej.InvalidRejection, match="another rejection is pending"):
        rej.reject(None, Args())


def test_evidence_run_identity_refusals(monkeypatch):
    run = {"id": 5, "run_attempt": 1, "repository": {"full_name": hzlib.PLUGINS}, "head_repository": {"full_name": hzlib.PLUGINS},
           "head_sha": rej.HEAD, "head_branch": "main", "path": rej.WORKFLOW, "event": "workflow_dispatch", "status": "completed",
           "conclusion": "failure", "workflow_id": 7}
    answers = {}

    def api(endpoint):
        return answers[endpoint.split(hzlib.PLUGINS + "/")[1].split("?")[0]]
    monkeypatch.setattr(rej, "api", api)
    decision = {"run": 5, "attempt": 1}
    answers["actions/runs/5/attempts/1"] = dict(run, conclusion="success")
    with pytest.raises(rej.InvalidRejection, match="run identity mismatch"):
        rej.evidence(decision)
    answers["actions/runs/5/attempts/1"] = run
    answers["actions/runs/5"] = {"run_attempt": 2, "head_sha": rej.HEAD}
    with pytest.raises(rej.InvalidRejection, match="rerun or changed"):
        rej.evidence(decision)
    answers["actions/runs/5"] = {"run_attempt": 1, "head_sha": rej.HEAD}
    answers["actions/workflows/7"] = {"id": 8, "path": rej.WORKFLOW}
    with pytest.raises(rej.InvalidRejection, match="workflow identity mismatch"):
        rej.evidence(decision)
    answers["actions/workflows/7"] = {"id": 7, "path": rej.WORKFLOW}
    answers[f"contents/{rej.WORKFLOW}"] = {"encoding": "base64", "content": base64.b64encode(b"changed").decode()}
    with pytest.raises(rej.InvalidRejection, match="reviewed fingerprint"):
        rej.evidence(decision)
