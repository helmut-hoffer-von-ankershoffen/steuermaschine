"""admission_lib.focused_admit, part one: file and JSON guards, process helpers, the packet loader, the registry merge,
the writer lock, the receipt and apply_registry. Real files in tmp_path; processes and GitHub are scripted."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native.admission_lib import focused_admit as fa

SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40


def digest(value):
    return hashlib.sha256(fa.canonical(value)).hexdigest()


# ---------- file and JSON guards ----------

def test_stable_file_reads_and_refuses_unsafe_files(tmp_path):
    good = tmp_path / "f"
    good.write_bytes(b"hello")
    assert fa._stable_file(good, 10) == b"hello"
    with pytest.raises(fa.AdmissionError, match=r"exceeds|unsafe file type, owner, or size"):
        fa._stable_file(good, 3)
    with pytest.raises(fa.AdmissionError, match="unsafe file type"):
        fa._stable_file(tmp_path, 10)
    link = tmp_path / "link"
    link.symlink_to(good)
    with pytest.raises(fa.AdmissionError, match="unsafe file type"):
        fa._stable_file(link, 10)
    with pytest.raises(fa.AdmissionError, match="cannot safely read"):
        fa._stable_file(tmp_path / "missing", 10)


def test_stable_file_detects_a_file_that_changes_under_it(tmp_path, monkeypatch):
    path = tmp_path / "f"
    path.write_bytes(b"data")
    real = fa._identity
    calls = []

    def shifting(info):
        calls.append(1)
        return real(info) if len(calls) < 2 else (*real(info)[:-1], len(calls))

    monkeypatch.setattr(fa, "_identity", shifting)
    with pytest.raises(fa.AdmissionError, match="changed while"):
        fa._stable_file(path, 100)
    calls.clear()

    def shifting_late(info):
        calls.append(1)
        return real(info) if len(calls) < 3 else (*real(info)[:-1], len(calls))

    monkeypatch.setattr(fa, "_identity", shifting_late)
    with pytest.raises(fa.AdmissionError, match="changed while"):
        fa._stable_file(path, 100)


def test_stable_file_refuses_a_foreign_owner(tmp_path, monkeypatch):
    path = tmp_path / "f"
    path.write_bytes(b"x")
    monkeypatch.setattr(fa.os, "getuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(fa.AdmissionError, match="unsafe file type, owner"):
        fa._stable_file(path, 10)


def test_json_bytes_and_unique_pairs_and_sha():
    assert fa._json_bytes(b'{"a": 1}', "x") == {"a": 1}
    for raw, message in ((b'{"a": 1, "a": 2}', "duplicate JSON key"), (b"nope", "invalid x JSON"), (b"\xff", "invalid x JSON"), (b"[1]", "must be a JSON object")):
        with pytest.raises(fa.AdmissionError, match=message):
            fa._json_bytes(raw, "x")
    assert fa._sha(SHA_A, "x") == SHA_A
    for bad in (5, "A" * 40, "a" * 39, SHA_A + "\n"):
        with pytest.raises(fa.AdmissionError, match="full lowercase SHA"):
            fa._sha(bad, "x")
    assert fa._sha("f" * 64, "x", fa.HEX)


def test_packet_approval_rules():
    fa._require_packet_approval(True, "d", "d")
    fa._require_packet_approval(False, None, "d")
    with pytest.raises(fa.AdmissionError, match="requires the exact"):
        fa._require_packet_approval(True, "x", "d")
    with pytest.raises(fa.AdmissionError, match="applies only with --apply"):
        fa._require_packet_approval(False, "x", "d")


# ---------- processes ----------

def test_run_scrubs_git_env_and_reports_failures(monkeypatch):
    seen = []
    monkeypatch.setenv("GIT_DIR", "/x")
    monkeypatch.setattr(fa.subprocess, "run", lambda argv, **kw: seen.append(kw["env"]) or SimpleNamespace(returncode=0, stdout=b"out", stderr=b""))
    assert fa._run(["git", "x"]) == b"out" and "GIT_DIR" not in seen[0]
    monkeypatch.setattr(fa.subprocess, "run", lambda argv, **kw: SimpleNamespace(returncode=2, stdout=b"", stderr=b" boom \n"))
    with pytest.raises(fa.AdmissionError, match=r"command failed \(git\): boom"):
        fa._run(["/usr/bin/git", "x"])
    monkeypatch.setattr(fa.subprocess, "run", lambda argv, **kw: SimpleNamespace(returncode=2, stdout=b"", stderr=b""))
    with pytest.raises(fa.AdmissionError, match="command failed \\(git\\): 2"):
        fa._run(["git", "x"])


def test_read_main_and_variables(monkeypatch):
    monkeypatch.setattr(fa, "_run", lambda argv, **kw: SHA_A.encode() + b"\n")
    assert fa._read_main("o/r") == SHA_A
    monkeypatch.setattr(fa, "_run", lambda argv, **kw: b"junk")
    with pytest.raises(fa.AdmissionError, match="live main SHA"):
        fa._read_main("o/r")
    calls = []
    monkeypatch.setattr(fa, "_run", lambda argv, **kw: calls.append(argv) or b"")
    fa._gh_variable_set("o/r", "V", "val")
    assert calls == [["gh", "variable", "set", "V", "--repo", "o/r", "--body", "val"]]


def completed(code=0, out=b"", err=b""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


def test_gh_variable_get_and_slot_get(monkeypatch):
    monkeypatch.setattr(fa.subprocess, "run", lambda argv, **kw: completed(out=b'{"a":1}\n'))
    assert fa._gh_variable_get("o/r", "V") == '{"a":1}' and fa._gh_slot_get("o/r", "V") == '{"a":1}'
    monkeypatch.setattr(fa.subprocess, "run", lambda argv, **kw: completed(1, err=b"gh: HTTP 404"))
    assert fa._gh_variable_get("o/r", "V") == "{}" and fa._gh_slot_get("o/r", "V") == ""
    monkeypatch.setattr(fa.subprocess, "run", lambda argv, **kw: completed(1, err=b"Not Found"))
    assert fa._gh_variable_get("o/r", "V") == "{}"
    monkeypatch.setattr(fa.subprocess, "run", lambda argv, **kw: completed(1, err=b"HTTP 500"))
    with pytest.raises(fa.AdmissionError, match="could not read focused plan registry"):
        fa._gh_variable_get("o/r", "V")
    with pytest.raises(fa.AdmissionError, match="could not read optional slot"):
        fa._gh_slot_get("o/r", "V")


@pytest.mark.parametrize("remote,message", [
    ("https://github.com/{core}.git", None), ("ssh://git@github.com/{core}", None), ("git@github.com:{core}.git", None),
    ("https://example.com/x/y", "not a canonical GitHub URL"), ("https://github.com/only", "lacks an owner/repository"),
    ("https://github.com/a//", "lacks an owner/repository"), ("https://github.com/other/repo", "differs from the reviewed"),
])
def test_repository_from_remote(monkeypatch, remote, message):
    from steuermaschine_heinzel.native.admission_lib import identity

    monkeypatch.setattr(fa, "_run", lambda argv, **kw: remote.format(core=identity.CORE).encode() + b"\n")
    if message is None:
        assert fa._repository_from_remote(Path("/core")) == identity.CORE
    else:
        with pytest.raises(fa.AdmissionError, match=message):
            fa._repository_from_remote(Path("/core"))


def test_require_core_remote(monkeypatch):
    monkeypatch.setattr(fa, "_run", lambda argv, **kw: b"https://github.com/o/r/\n")
    fa._require_core_remote(Path("/core"), "o/r")
    with pytest.raises(fa.AdmissionError, match="not the canonical repository"):
        fa._require_core_remote(Path("/core"), "o/other")


def test_management_core_refuses_a_malformed_identity(monkeypatch):
    from steuermaschine_heinzel.native.admission_lib import identity

    monkeypatch.setattr(identity, "CORE", "no-slash")
    with pytest.raises(fa.AdmissionError, match="malformed"):
        fa._management_core()


def test_literal(tmp_path):
    source = b"A = 1\nB = ('x', 'y')\nC: int = 3\n"
    assert fa._literal(source, "B") == ("x", "y")
    with pytest.raises(fa.AdmissionError, match="lacks literal"):
        fa._literal(source, "C")
    with pytest.raises(fa.AdmissionError, match="lacks literal"):
        fa._literal(source, "Z")


# ---------- the packet loader ----------

def make_packet(tmp_path, source_pair=False):
    repo_root = tmp_path / "management"
    (repo_root / "research").mkdir(parents=True)
    review = repo_root / "research/review.md"
    review.write_text("approved")
    plan = {"event": "pull_request", "ref": "refs/pull/*/merge", "head": SHA_A, "base": SHA_B, "tree": SHA_C, "scope": ["x"]}
    packet = {"schema": 1, "manifest": plan, "review": {"path": "research/review.md", "sha256": hashlib.sha256(b"approved").hexdigest(), "head": SHA_A, "base": SHA_B, "tree": SHA_C,
                                                            "manifest_sha256": digest(plan)}}
    if source_pair:
        main_plan = {"event": "push", "ref": "refs/heads/main", "base": SHA_B, "tree": SHA_C, "scope": ["x"]}
        packet["main_manifest"] = main_plan
        packet["review"]["main_manifest_sha256"] = digest(main_plan)
    return packet, repo_root


def write_packet(tmp_path, packet):
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(packet))
    return path


def test_load_packet_accepts_the_reviewed_shapes(tmp_path):
    packet, root = make_packet(tmp_path)
    loaded, raw, review = fa.load_packet(write_packet(tmp_path, packet), root, SHA_A)
    assert loaded == packet and review == b"approved" and raw == (tmp_path / "packet.json").read_bytes()
    pair, root = make_packet(tmp_path / "p", source_pair=True)
    (tmp_path / "p").mkdir(exist_ok=True)
    loaded, _, _ = fa.load_packet(write_packet(tmp_path / "p", pair), root, SHA_A, source_pair=True)
    assert "main_manifest" in loaded


def mutate(packet, path, value):
    target = packet
    for key in path[:-1]:
        target = target[key]
    if value is KeyError:
        del target[path[-1]]
    else:
        target[path[-1]] = value


@pytest.mark.parametrize("path,value,message", [
    (("schema",), 2, "requires schema 1"), (("schema",), True, "requires schema 1"),
    (("manifest",), "x", "requires manifest and review"), (("review",), None, "requires manifest and review"),
    (("manifest", "head"), "zz", "manifest head"), (("manifest", "event"), "push", "only exact pull_request plans"),
    (("manifest", "ref"), "refs/heads/main", "only exact pull_request plans"),
    (("main_manifest",), {}, "requires explicit reviewed source pair mode"),
    (("review", "head"), SHA_B, "stale head"), (("review", "base"), SHA_C, "differs from manifest base/tree"),
    (("review", "tree"), SHA_A, "differs from manifest base/tree"), (("review", "manifest_sha256"), "0" * 64, "stale manifest digest"),
    (("review", "path"), "", "repository-relative"), (("review", "path"), "/etc/passwd", "repository-relative"), (("review", "path"), "../x", "repository-relative"),
    (("review", "path"), "research/missing.md", "must remain inside"), (("review", "sha256"), "0" * 64, "do not match the packet digest"),
])
def test_load_packet_refusals(tmp_path, path, value, message):
    packet, root = make_packet(tmp_path)
    if path == ("manifest", "head"):
        mutate(packet, ("review", "head"), "zz")
    mutate(packet, path, value)
    with pytest.raises(fa.AdmissionError, match=message):
        fa.load_packet(write_packet(tmp_path, packet), root, SHA_A)


def test_load_packet_refuses_a_different_reviewed_head(tmp_path):
    packet, root = make_packet(tmp_path)
    with pytest.raises(fa.AdmissionError, match="reviewed head differs"):
        fa.load_packet(write_packet(tmp_path, packet), root, SHA_B)


@pytest.mark.parametrize("change,message", [
    (lambda p: p.pop("main_manifest"), "headless push-tree"), (lambda p: p["main_manifest"].update(event="pull_request"), "headless push-tree"),
    (lambda p: p["main_manifest"].update(head=SHA_A), "headless push-tree"), (lambda p: p["main_manifest"].update(scope=["other"]), "differ in reviewed scope"),
    (lambda p: p["review"].update(main_manifest_sha256="0" * 64), "stale main manifest digest"),
])
def test_load_packet_pair_refusals(tmp_path, change, message):
    packet, root = make_packet(tmp_path, source_pair=True)
    change(packet)
    with pytest.raises(fa.AdmissionError, match=message):
        fa.load_packet(write_packet(tmp_path, packet), root, SHA_A, source_pair=True)


def test_load_packet_refuses_a_symlinked_review_outside_the_repository(tmp_path):
    packet, root = make_packet(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("approved")
    (root / "research/review.md").unlink()
    (root / "research/review.md").symlink_to(outside)
    with pytest.raises(fa.AdmissionError, match="must remain inside"):
        fa.load_packet(write_packet(tmp_path, packet), root, SHA_A)


def test_validate_live_binding():
    packet = {"manifest": {"head": SHA_A, "base": SHA_B}, "review": {"head": SHA_A}}
    fa.validate_live_binding(packet, SHA_A, SHA_B)
    with pytest.raises(fa.AdmissionError, match="stale relative"):
        fa.validate_live_binding(packet, SHA_A, SHA_C)
    with pytest.raises(fa.AdmissionError, match="reviewed head differs"):
        fa.validate_live_binding(packet, SHA_C, SHA_B)
    packet["review"]["head"] = SHA_C
    with pytest.raises(fa.AdmissionError, match="reviewed head differs"):
        fa.validate_live_binding(packet, SHA_A, SHA_B)
    fa.validate_live_binding({"manifest": packet["manifest"]}, SHA_A, SHA_B)


# ---------- the registry ----------

def test_parse_registry_and_keys_and_merge():
    assert fa.parse_registry("") == {} and fa.parse_registry(b'{"a": 1}') == {"a": 1}
    with pytest.raises(fa.AdmissionError, match="exceeds the canonical 48 KiB"):
        fa.parse_registry("x" * (fa.MAX_REGISTRY_BYTES + 1))
    pr = {"event": "pull_request", "head": SHA_A}
    main = {"event": "push", "ref": "refs/heads/main", "tree": SHA_C}
    assert fa.registry_key(pr) == f"pull_request:{SHA_A}" and fa.registry_key(main) == f"push-tree:{SHA_C}"
    for bad in ({"event": "push", "ref": "refs/heads/main", "tree": SHA_C, "head": SHA_A}, {"event": "other"}, {}):
        with pytest.raises(fa.AdmissionError, match="unsupported focused registry key"):
            fa.registry_key(bad)
    merged, changed = fa.merge_registry("", pr)
    assert changed and json.loads(merged) == {f"pull_request:{SHA_A}": pr}
    again, changed = fa.merge_registry(merged.encode(), pr)
    assert not changed and again == merged
    assert fa.merge_registry(" {}", {"event": "pull_request", "head": SHA_B})[1] is True
    assert fa.merge_registry(json.dumps({f"pull_request:{SHA_A}": pr}), pr) == (json.dumps({f"pull_request:{SHA_A}": pr}), False)
    with pytest.raises(fa.AdmissionError, match="key conflict"):
        fa.merge_registry(merged, {**pr, "extra": 1})
    big = {"event": "pull_request", "head": SHA_B, "pad": "x" * fa.MAX_REGISTRY_BYTES}
    with pytest.raises(fa.AdmissionError, match="merged focused plan registry exceeds"):
        fa.merge_registry("{}", big)


# ---------- the writer lock ----------

def test_lock_serializes_and_refuses_unsafe_places(tmp_path, monkeypatch):
    path = tmp_path / "locks" / "registry.lock"
    with fa._lock(path) as handle:
        assert not handle.closed
    assert path.exists()
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "linkdir").symlink_to(real)
    with pytest.raises(fa.AdmissionError, match="directory is a symlink"), fa._lock(tmp_path / "linkdir" / "x.lock"):
        pass
    target = tmp_path / "locks" / "target"
    target.write_text("")
    (tmp_path / "locks" / "link.lock").symlink_to(target)
    with pytest.raises(fa.AdmissionError, match="cannot open focused registry lock safely"), fa._lock(tmp_path / "locks" / "link.lock"):
        pass
    monkeypatch.setattr(fa.os, "getuid", lambda: 1)
    with pytest.raises(fa.AdmissionError, match="unsafe owner/type"), fa._lock(path):
        pass


def test_lock_refuses_a_non_regular_file_and_times_out_when_busy(tmp_path, monkeypatch):
    import fcntl
    import stat

    path = tmp_path / "x.lock"
    real_fstat = os.fstat
    monkeypatch.setattr(fa.os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFIFO, st_uid=os.getuid()))
    with pytest.raises(fa.AdmissionError, match="lock has unsafe owner/type"), fa._lock(path):
        pass
    monkeypatch.setattr(fa.os, "fstat", real_fstat)
    clock = SimpleNamespace(t=0.0)
    monkeypatch.setattr(fa.time, "monotonic", lambda: clock.t)
    monkeypatch.setattr(fa.time, "sleep", lambda s: setattr(clock, "t", clock.t + 10))
    flocks = []

    def flock(handle, flag):
        flocks.append(flag)
        if flag & fcntl.LOCK_NB:
            raise BlockingIOError

    monkeypatch.setattr(fa.fcntl, "flock", flock)
    with pytest.raises(fa.AdmissionError, match="lock is busy"), fa._lock(path):
        pass


# ---------- receipts and apply_registry ----------

def receipt(**more):
    return {"schema": 1, "admission_id": "0" * 32, "head": SHA_A, **more}


def test_write_receipt_is_idempotent_and_refuses_changes(tmp_path):
    path = fa._write_receipt(receipt(), tmp_path)
    assert path.name == f"head-{SHA_A}-{'0' * 32}.json" and json.loads(path.read_text())["head"] == SHA_A
    assert fa._write_receipt(receipt(), tmp_path) == path
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]
    with pytest.raises(fa.AdmissionError, match="different receipt already exists"):
        fa._write_receipt(receipt(extra=1), tmp_path)
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


@pytest.mark.parametrize("bad", [None, "short", "G" * 32, 5])
def test_write_receipt_needs_an_identifier(tmp_path, bad):
    with pytest.raises(fa.AdmissionError, match="immutable per-admission identifier"):
        fa._write_receipt({**receipt(), "admission_id": bad}, tmp_path)


def test_write_receipt_refuses_symlinked_directories(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    home = tmp_path / "h1"
    home.mkdir()
    (home / ".cache").symlink_to(real)
    monkeypatch.setattr(statedir, "root", lambda: home / ".cache")  # the state directory itself is a symlink
    with pytest.raises(fa.AdmissionError, match="cache is a symlink"):
        fa._write_receipt(receipt(), home)
    home = tmp_path / "h2"
    (home / ".cache").mkdir(parents=True)
    monkeypatch.setattr(statedir, "root", lambda: home / ".cache")
    (statedir.root() / "focused-admission-receipts").symlink_to(real)
    with pytest.raises(fa.AdmissionError, match="receipt directory is a symlink"):
        fa._write_receipt(receipt(), home)


def test_write_receipt_refuses_a_foreign_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(fa.os, "getuid", lambda: 1)
    with pytest.raises(fa.AdmissionError, match="unsafe owner/type"):
        fa._write_receipt(receipt(), tmp_path)


def test_apply_registry_merges_reads_back_and_records(tmp_path):
    plan = {"event": "pull_request", "head": SHA_A}
    store = {"value": ""}
    sets = []

    def put(value):
        sets.append(value)
        store["value"] = value

    fresh = []
    path = fa.apply_registry([plan], lambda: store["value"], put, receipt(), tmp_path / "lock", tmp_path, check_fresh=lambda: fresh.append(1))
    data = json.loads(path.read_text())
    assert data["registry_changed"] is True and len(sets) == 1 and len(fresh) == 2 and data["registry_sha256"] == data["readback_sha256"]
    again = fa.apply_registry(plan, lambda: store["value"], put, receipt(admission_id="1" * 32), tmp_path / "lock", tmp_path)
    assert json.loads(again.read_text())["registry_changed"] is False and len(sets) == 1
    with pytest.raises(fa.AdmissionError, match="different receipt already exists"):
        fa.apply_registry(plan, lambda: store["value"], put, receipt(), tmp_path / "lock", tmp_path)


def test_apply_registry_refuses_a_readback_mismatch(tmp_path):
    plan = {"event": "pull_request", "head": SHA_A}
    with pytest.raises(fa.AdmissionError, match="readback mismatch"):
        fa.apply_registry([plan], lambda: "{}", lambda value: None, receipt(), tmp_path / "lock", tmp_path)
