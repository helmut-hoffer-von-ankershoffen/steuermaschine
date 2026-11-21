"""release_admit_lib.release_slots: the pinned-host Runtime, the PR hold check, the release guard and the argv refusals, on fakes."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native.release_admit_lib import release_slots as s

a = s.a
REPO = "o/heinzel"


def make_runtime(tmp_path, host=None):
    rt = s.Runtime.__new__(s.Runtime)
    rt.core, rt.home, rt.cache, rt.management = tmp_path / "core", tmp_path, tmp_path / ".cache", tmp_path / "Code/heinzel-management"
    rt.github_host, rt.repo, rt.held_version_locks, rt.queue_baseline = host, s.hzlib.CORE, None, None
    return rt


def test_runtime_init_pins_paths_and_host(tmp_path, monkeypatch):
    monkeypatch.setattr(a, "_repository_from_remote", lambda core: REPO)
    rt = s.Runtime(tmp_path / "core", home=tmp_path, github_host="github.com")
    assert rt.repo == REPO and rt.cache == statedir.root() and rt.github_host == "github.com"
    assert rt.service is None and rt.plist is None  # outside a native command no label is configured
    with pytest.raises(a.AdmissionError, match="queue_service_label"):
        rt.queue_state()  # refuses before any launchctl call
    monkeypatch.setattr(s._slots, "queue_label", lambda: "x.queue")
    rt = s.Runtime(tmp_path / "core", home=tmp_path, github_host="github.com")
    assert rt.service.endswith("/x.queue") and rt.plist == tmp_path / "Library/LaunchAgents/x.queue.plist"
    monkeypatch.setattr(s.Path, "home", classmethod(lambda cls: tmp_path / "elsewhere"))
    assert s.Runtime(tmp_path / "core").home == tmp_path / "elsewhere"


def test_runtime_run_pins_the_host_for_gh_only(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(s.subprocess, "run", lambda args, **kw: seen.append((args, kw)) or SimpleNamespace(returncode=0))
    monkeypatch.setenv("GH_REPO", "x/y")
    rt = make_runtime(tmp_path, "github.com")
    rt.run("gh", "api", "repos/x")
    args, kw = seen[0]
    assert args[-2:] == ("--hostname", "github.com") and kw["env"]["GH_HOST"] == "github.com" and "GH_REPO" not in kw["env"]
    rt.run("gh", "variable", "list")
    assert seen[1][0] == ("gh", "variable", "list")
    rt.run("git", "status")
    assert "env" not in seen[2][1]
    make_runtime(tmp_path).run("gh", "api", "x")
    assert "env" not in seen[3][1]


def test_runtime_run_refuses_failures_unless_unchecked(tmp_path, monkeypatch):
    monkeypatch.setattr(s.subprocess, "run", lambda args, **kw: SimpleNamespace(returncode=1))
    rt = make_runtime(tmp_path)
    with pytest.raises(a.AdmissionError, match="git operation failed"):
        rt.run("git", "x")
    assert rt.run("git", "x", check=False).returncode == 1


def test_set_variable(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path, "github.com")
    calls = []
    monkeypatch.setattr(rt, "run", lambda *args, **kw: calls.append((args, kw)))
    rt.set_variable(s.NAMES[1], "value")
    assert calls == [(("gh", "variable", "set", s.NAMES[1], "-R", f"github.com/{s.hzlib.CORE}"), {"data": b"value"})]
    rt.github_host = None
    rt.set_variable(s.NAMES[2], "v")
    assert calls[1][0][-1] == s.hzlib.CORE
    with pytest.raises(a.AdmissionError):
        rt.set_variable(s.NAMES[0], "v")


def write_actions(rt, text):
    path = rt.management / "tools/pr-actions.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_check_pr_holds(tmp_path):
    rt = make_runtime(tmp_path)
    write_actions(rt, '["801"]\nhold = false\n["plugins #5"]\nhold = true\n')
    s.check_pr_holds(rt, 801)
    s.check_pr_holds(rt, 5, require_owner=False)  # another repo's hold is not ours
    with pytest.raises(a.AdmissionError, match="ownership"):
        s.check_pr_holds(rt, 99)
    s.check_pr_holds(rt, 99, require_owner=False)
    write_actions(rt, '["801"]\nhold = true\n')
    with pytest.raises(a.AdmissionError, match="held"):
        s.check_pr_holds(rt, 801, require_owner=False)
    write_actions(rt, '["801"]\nhold = "yes"\n')
    with pytest.raises(a.AdmissionError, match="held"):
        s.check_pr_holds(rt, 801)
    write_actions(rt, 'top = 1\n["weird key here now"]\nhold = false\n["801"]\nhold = false\n')
    with pytest.raises(a.AdmissionError, match="unrecognized potential hold"):
        s.check_pr_holds(rt, 801)
    write_actions(rt, '["weird key here now"]\nhold = false\n["801"]\nhold = false\n')
    s.check_pr_holds(rt, 801)


def test_read_release_nonconsumers(monkeypatch):
    monkeypatch.setattr(s, "read_stuck_queued", lambda rt, receipt: {"ok": receipt["run"]})
    good = {"kind": s.STUCK_KIND, "run": 5}
    assert s.read_release_nonconsumers(None, [good, {**good, "run": 6}]) == {5: {"ok": 5}, 6: {"ok": 6}}
    assert s.read_release_nonconsumers(None, []) == {}
    for bad in ("x", [1], [{"kind": "other", "run": 1}], [{**good, "run": 0}], [{**good, "run": True}], [good, good]):
        with pytest.raises(a.AdmissionError):
            s.read_release_nonconsumers(None, bad)


def test_release_snapshot(monkeypatch):
    rt = SimpleNamespace(variables=lambda: ({s.NAMES[0]: "x", "OTHER": "y"}, {}))
    values, present = s.release_snapshot(rt)
    assert values[s.NAMES[0]] == "x" and set(values) == set(s.NAMES) and present == [s.NAMES[0]]
    with pytest.raises(a.AdmissionError):
        s.release_snapshot(SimpleNamespace(variables=lambda: ({}, {s.NAMES[0]: "org"})))


def guard_world(monkeypatch, *, ci=None, runs=(), recovery=None, frozen=False, state="state = running", locks=None):
    calls = []
    ci = ci or {"id": 7, "path": ".github/workflows/ci.yml", "state": "active"}
    rt = SimpleNamespace(repo=REPO, cache=Path("/c"), held_version_locks=locks, api=lambda path: ci, pages=lambda path, key: list(runs), queue_state=lambda: state)
    packet = {"release": {"head": "h" * 40, "recovery": recovery}}
    monkeypatch.setattr(s.release, "source_proof", lambda rt, packet, root: ("src", "diff"))
    monkeypatch.setattr(s.release, "remote_identity", lambda rt, packet: 42)
    monkeypatch.setattr(s.release, "recovery_proof", lambda rt, packet, root, wf: {"required_entry": "entry"})
    monkeypatch.setattr(s, "check_preopen", lambda rt, info: calls.append("preopen"))
    monkeypatch.setattr(s, "check_pr_holds", lambda rt, pr, require_owner: calls.append(("holds", pr, require_owner)))
    monkeypatch.setattr(s.hzlib, "merge_frozen", lambda: frozen)
    monkeypatch.setattr(s, "version_locks", lambda cache: "inv")
    monkeypatch.setattr(s, "check_consumers", lambda rt, ex, desired, full: calls.append(("consumers", full)))
    monkeypatch.setattr(s, "check_budget", lambda rt, desired: 123)
    return rt, packet, calls


def test_release_guard_fresh_and_recovery(monkeypatch):
    rt, packet, calls = guard_world(monkeypatch)
    out = s.release_guard(rt, packet, Path("/r"), 7, {}, paused=False)
    assert out == dict(source="src", repository_id=42, recovery=None, budget_bytes=123) and calls == ["preopen", ("consumers", True)]
    rt, packet, calls = guard_world(monkeypatch, recovery={"pr": 9}, state=None, locks="inv")
    out = s.release_guard(rt, packet, Path("/r"), 7, {}, paused=True, exceptions={1: 2}, full=False)
    assert out["recovery"] == {"required_entry": "entry"} and calls == [("holds", 9, False), ("consumers", False)]


@pytest.mark.parametrize("kwargs,paused,message", [
    (dict(ci={"id": 8, "path": ".github/workflows/ci.yml", "state": "active"}), False, "canonical release CI"),
    (dict(ci={"id": 7, "path": "x.yml", "state": "active"}), False, "canonical release CI"),
    (dict(ci={"id": 7, "path": ".github/workflows/ci.yml", "state": "disabled_manually"}), False, "canonical release CI"),
    (dict(runs=[1]), False, "CI already exists"),
    (dict(frozen=True), False, "merge freeze"),
    (dict(locks=None), True, "version lock inventory"),
    (dict(locks="other", state=None), True, "version lock inventory"),
    (dict(state=None), False, "queue exclusion"),
    (dict(state="state = stopped"), False, "queue exclusion"),
    (dict(state="state = running", locks="inv"), True, "queue exclusion"),
])
def test_release_guard_refusals(monkeypatch, kwargs, paused, message):
    rt, packet, _calls = guard_world(monkeypatch, **kwargs)
    with pytest.raises(a.AdmissionError, match=message):
        s.release_guard(rt, packet, Path("/r"), 7, {}, paused=paused)


def run_main(monkeypatch, capsys, argv, isolated=True, runtime_error=None):
    import sys

    class FakeSys:
        flags = SimpleNamespace(isolated=isolated)

        def __getattr__(self, name):
            return getattr(sys, name)

    monkeypatch.setattr(s, "sys", FakeSys())
    return s.main(argv), capsys.readouterr().err


def base_argv(tmp_path, *more):
    return ["prepare", str(tmp_path / "packet.json"), "--core-repo", str(tmp_path), "--review-root", str(tmp_path), *more]


@pytest.mark.parametrize("extra,message", [
    ([], "this entry admits release-main-only"),
    (["--release-main-only", "--pr", "5"], "excludes PR"),
    (["--release-main-only", "--reviewed-protected-producer"], "excludes PR"),
    (["--release-main-only", "--stuck-queued"], "excludes batch"),
    (["--release-main-only", "--packets", "p"], "excludes batch"),
])
def test_main_refusals(tmp_path, monkeypatch, capsys, extra, message):
    code, err = run_main(monkeypatch, capsys, base_argv(tmp_path, *extra))
    assert code == 1 and message in err and '"result": "refused"' in err


def test_main_requires_isolation_and_apply_pair(tmp_path, monkeypatch, capsys):
    code, err = run_main(monkeypatch, capsys, base_argv(tmp_path, "--release-main-only"), isolated=False)
    assert code == 1 and "python3 -I" in err
    argv = ["apply", str(tmp_path / "p"), "--core-repo", str(tmp_path), "--review-root", str(tmp_path), "--release-main-only"]
    code, err = run_main(monkeypatch, capsys, argv)
    assert code == 1 and "apply needs both" in err


def test_main_prepare_prints_and_cleans_up(tmp_path, monkeypatch, capsys):
    source, workspace = tmp_path / "src", tmp_path / "ws"
    source.mkdir(), workspace.mkdir()
    seen = {}
    monkeypatch.setattr(s, "Runtime", lambda core, github_host: SimpleNamespace(core=core))
    monkeypatch.setattr(s, "prepare_release", lambda rt, packet, root, nonconsumers: seen.update(nc=nonconsumers) or ({"x": 1}, {"p": 1}, source, workspace))
    code, err = run_main(monkeypatch, capsys, base_argv(tmp_path, "--release-main-only"))
    assert code == 0 and seen["nc"] == [] and not source.exists() and not workspace.exists() and '"result": "success"' in err


def test_main_reads_nonconsumers_and_applies_the_approved_transaction(tmp_path, monkeypatch, capsys):
    source, workspace = tmp_path / "src", tmp_path / "ws"
    source.mkdir(), workspace.mkdir()
    prepared = {"x": 1}
    nonconsumers = tmp_path / "nc.json"
    nonconsumers.write_text(json.dumps({"receipts": [{"run": 1}]}))
    approved = tmp_path / "prepared.json"
    approved.write_text(a.canonical(prepared).decode())
    applied = []
    monkeypatch.setattr(s, "Runtime", lambda core, github_host: SimpleNamespace(core=core))
    monkeypatch.setattr(s, "prepare_release", lambda rt, packet, root, nonconsumers: (prepared, {"p": 1}, source, workspace))
    monkeypatch.setattr(s, "apply_release", lambda rt, prep, packet, src, approval, root: applied.append(approval) or "receipt-path")
    argv = ["apply", str(tmp_path / "p"), "--core-repo", str(tmp_path), "--review-root", str(tmp_path), "--release-main-only", "--nonconsumers", str(nonconsumers),
            "--prepared", str(approved), "--approved-sha256", "abc"]
    code, err = run_main(monkeypatch, capsys, argv)
    assert code == 0 and applied == ["abc"]
    approved.write_text('{"x": 2}')
    code, err = run_main(monkeypatch, capsys, argv)
    assert code == 1 and "prepared transaction changed" in err and applied == ["abc"]


# ---------- prepare_release, with every proof replaced by a recording fake ----------

def prepare_world(monkeypatch, tmp_path, *, proposal=None, guard=None, snapshots=None, recovery=None):
    info = {"version": "0.9.41", "head": "h" * 40, "base": "b" * 40, "tree": "t" * 40}
    packet = {"manifest": {"m": 1}, "release": info}
    workspace = tmp_path / "workspace"
    snapshot = workspace / "repo"
    source = tmp_path / "source"
    for path in (snapshot, source):
        path.mkdir(parents=True)
    log = []
    rel = s.release
    monkeypatch.setattr(rel, "verify_account", lambda rt: log.append("account"))
    monkeypatch.setattr(rel, "load_packet", lambda path, root: (packet, b"raw"))
    monkeypatch.setattr(rel, "require_no_attempt", lambda rt, i: log.append("no-attempt"))
    monkeypatch.setattr(rel, "source_proof", lambda rt, p, root: ("proof", "diff"))
    monkeypatch.setattr(rel, "remote_identity", lambda rt, p: 42)
    monkeypatch.setattr(rel, "validate_manifest", lambda *a: log.append("validated"))
    monkeypatch.setattr(rel, "MODE", "release-main")
    monkeypatch.setattr(s.a, "_prepare_core_snapshot", lambda core, base: (workspace, snapshot))
    monkeypatch.setattr(s.a, "_canonical_source", lambda snap, base: (source, "blob", REPO, s.NAMES[0], ("f",)))
    monkeypatch.setattr(s, "reader_contract", lambda snap, base: log.append("reader"))
    monkeypatch.setattr(s.a, "_literal", lambda raw, name: 77)
    (source / "scripts").mkdir()
    (source / "scripts/focused_gate.py").write_bytes(b"CI_WORKFLOW_ID = 77\n")
    expected = {name: "" for name in s.NAMES}
    snaps = iter(snapshots or [(expected, []), (expected, [])])
    monkeypatch.setattr(s, "release_snapshot", lambda rt: next(snaps))
    monkeypatch.setattr(s.a, "registry_key", lambda manifest: "k")
    monkeypatch.setattr(s.a, "_isolated_slot_append", lambda *args: proposal if proposal is not None else {"result": (0, "candidate")})
    monkeypatch.setattr(s, "release_guard", lambda *a, **kw: guard if guard is not None else {"source": "proof", "repository_id": 42, "recovery": recovery, "budget_bytes": 1})
    monkeypatch.setattr(s.resulting, "release_registry_proof", lambda *a: {"old": 1})
    monkeypatch.setattr(s.a, "canonical", lambda value: repr(sorted(value.items()) if isinstance(value, dict) else value).encode())
    rt = SimpleNamespace(core=tmp_path / "core", repo=REPO)
    return rt, packet, workspace, source, log


def test_prepare_release_builds_the_transaction(monkeypatch, tmp_path):
    rt, packet, workspace, source, log = prepare_world(monkeypatch, tmp_path, recovery={"required_entry": {"e": 1}})
    prepared, got_packet, got_source, got_workspace = s.prepare_release(rt, tmp_path / "p.json", tmp_path, nonconsumers=())
    assert prepared["slot"] == s.NAMES[1] and prepared["desired"][s.NAMES[1]] == "candidate" and prepared["after_presence"] == [s.NAMES[1]]
    assert prepared["release_main"]["old_entries"] == {"old": 1} and prepared["release_main"]["workflow"] == 77 and "nonconsumers" not in prepared
    assert got_packet is packet and got_source == source and got_workspace == workspace and source.exists() and workspace.exists()
    assert log.count("no-attempt") == 2 and "validated" in log and "reader" in log


def test_prepare_release_carries_nonconsumers(monkeypatch, tmp_path):
    rt, _packet, _workspace, _source, _log = prepare_world(monkeypatch, tmp_path)
    receipts = [{"kind": s.STUCK_KIND, "run": 3}]
    monkeypatch.setattr(s, "read_stuck_queued", lambda rt, receipt: {"x": 1})
    prepared, *_ = s.prepare_release(rt, tmp_path / "p.json", tmp_path, nonconsumers=receipts)
    assert prepared["nonconsumers"] == receipts


@pytest.mark.parametrize("name,kwargs,message", [
    ("already admitted", dict(proposal={"result": None}), "already admitted"),
    ("source changed", dict(guard={"source": "other", "repository_id": 42, "recovery": None, "budget_bytes": 1}), "source changed during prepare"),
    ("repo changed", dict(guard={"source": "proof", "repository_id": 9, "recovery": None, "budget_bytes": 1}), "source changed during prepare"),
    ("snapshot changed", dict(snapshots=[({n: "" for n in s.NAMES}, []), ({n: "x" for n in s.NAMES}, [])]), "snapshot changed during prepare"),
])
def test_prepare_release_refusals_clean_up_after_themselves(monkeypatch, tmp_path, name, kwargs, message):
    rt, _packet, workspace, source, _log = prepare_world(monkeypatch, tmp_path, **kwargs)
    with pytest.raises(a.AdmissionError, match=message):
        s.prepare_release(rt, tmp_path / "p.json", tmp_path)
    assert not source.exists() and not workspace.exists()


def test_prepare_release_refuses_an_untrusted_transport_identity(monkeypatch, tmp_path):
    rt, _packet, workspace, source, _log = prepare_world(monkeypatch, tmp_path)
    monkeypatch.setattr(s.a, "_canonical_source", lambda snap, base: (source, "blob", "other/repo", s.NAMES[0], ("f",)))
    with pytest.raises(a.AdmissionError, match="transport identity changed"):
        s.prepare_release(rt, tmp_path / "p.json", tmp_path)
    assert not source.exists() and not workspace.exists()
    # a failure before the source exists only removes the workspace
    rt, _packet, workspace, source, _log = prepare_world(monkeypatch, tmp_path)
    monkeypatch.setattr(s.a, "_canonical_source", lambda snap, base: (_ for _ in ()).throw(a.AdmissionError("no source")))
    with pytest.raises(a.AdmissionError, match="no source"):
        s.prepare_release(rt, tmp_path / "p.json", tmp_path)
    assert not workspace.exists()


def test_the_queue_label_is_the_setting_and_nothing_else():
    from steuermaschine.native import NativeRuntime, using
    from steuermaschine.testing import fake_providers

    with using(NativeRuntime(fake_providers(), {"queue_service_label": "x.sm-queue"}, None)):
        assert s._slots.queue_label() == "x.sm-queue"
    with using(NativeRuntime(fake_providers(), {}, None)):
        assert s._slots.queue_label() is None
    assert s._slots.queue_label() is None  # outside a native command
