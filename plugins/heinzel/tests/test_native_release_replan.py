"""Replanning preserves provenance and cannot erase an attempted candidate or admission."""

import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from test_release_cut_full_ci import full as full

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import fake_providers, invoke
from steuermaschine.testing.fakes import FakeVcs
from steuermaschine_heinzel.native import release
from steuermaschine_heinzel.native.queue_lib import core_policy
from steuermaschine_heinzel.native.release_lib import replan
from steuermaschine_heinzel.pipelines import common, release_cut
from steuermaschine_heinzel.plugin import plugin

VERSION, HEAD, BASE, TREE = "0.9.51", "a" * 40, "b" * 40, "c" * 40


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("STEUERMASCHINE_HOME", str(tmp_path / "state-home"))
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {"repo": "o/core"}}, tmp_path)
    gh = FakeGh().on("actions/runs?", {"total_count": 0, "workflow_runs": []})
    gh.on("rev-parse HEAD", HEAD).on("rev-list", f"{HEAD} {BASE}")
    gh.on("status --porcelain", "").on(f"rev-parse {HEAD}^{{tree}}", TREE)
    vcs = FakeVcs(head=HEAD, base=BASE)
    vcs.remote["main"] = BASE
    rt = Runtime(Project(name="t", plugin="heinzel", root=tmp_path, plugins=["heinzel"], allow_unreviewed=True),
                 stack, settings, _providers=fake_providers(commands=gh, vcs=vcs))
    root = tmp_path / "isolated-state"
    root.mkdir()
    monkeypatch.setattr(replan.statedir, "root", lambda: root)
    monkeypatch.setattr(replan.statedir, "path", lambda name: root / name)
    locks = []
    module = SimpleNamespace(CORE="o/core", Stop=RuntimeError,
        acquire_global_lock=lambda: locks.append("lock"), free_global_lock=lambda: locks.append("unlock"),
        drop_identity=lambda version: {"open": [], "closed": [], "branch": False})
    monkeypatch.setattr(release, "runner", lambda: module)
    monkeypatch.setattr(replan.focused_admit, "_lock", lambda path: nullcontext())
    monkeypatch.setattr(release_cut, "official_origin", lambda *args: None)
    registry = {name: "" for name in replan.focused_slots.NAMES}
    monkeypatch.setattr(replan.release_slots, "Runtime", lambda clone: SimpleNamespace(repo="o/core", registry=lambda: registry))
    pair = {"main_manifest": {"base": BASE, "tree": TREE, "event": "push"}}
    old, new = tmp_path / "old" / "packet.json", tmp_path / "new" / "packet.json"
    for path in (old, new):
        path.parent.mkdir()
        path.write_text(json.dumps(pair))
    state = {"schema": 1, "pipeline": "release.cut", "project": "t", "key": VERSION, "arg": VERSION,
             "label": f"v{VERSION}", "lane": "default", "author": "coordinator", "status": "refused",
             "inputs": {"clone": str(tmp_path / "clone"), "packet": str(old), "main_ci": "full", "commit_log": "original.log"},
             "fingerprint": {"main": BASE}, "steps": {"packet": {"status": "done"}},
             "data": {"main_ci": "focused", "ci_change_head": HEAD, "ci_change_detail": "independently approved"}}
    rt.providers.store.save(replan.KIND, VERSION, state)
    argv = ["--format", "json", "release", "replan", VERSION, "--head", HEAD, "--packet", str(new),
            "--main-ci", "full", "--reason", "full candidate and original hook transcript"]
    return SimpleNamespace(rt=rt, gh=gh, root=root, old=old, new=new, state=state, argv=argv,
                           module=module, registry=registry, locks=locks)


def test_dry_run_and_live_archive_exact_conflicting_inputs_without_artifact_changes(env):
    out = invoke(env.rt, env.argv)
    assert out.exit_code == 0, out.stderr
    proof = json.loads(out.stdout)["proof"]
    assert proof["head"] == HEAD and proof["base"] == BASE and proof["main_ci"] == "full"
    assert proof["state_sha256"] == replan.digest(env.state)
    assert env.rt.providers.store.load(replan.KIND, VERSION) == env.state
    assert env.rt.providers.store.keys(replan.ARCHIVE) == []
    env.old.parent.joinpath("release-hook-summary.log").write_text("preserved")
    before = {str(p): p.read_bytes() for p in env.old.parent.iterdir()}
    out = invoke(env.rt, [*env.argv, "--live"])
    assert out.exit_code == 0, out.stderr
    archive = env.rt.providers.store.load(replan.ARCHIVE, env.rt.providers.store.keys(replan.ARCHIVE)[0])
    assert archive["original"] == env.state
    fresh = env.rt.providers.store.load(replan.KIND, VERSION)
    assert fresh["steps"] == fresh["data"] == fresh["review"] == {}
    assert fresh["inputs"]["main_ci"] == "full" and fresh["inputs"]["packet"] == str(env.new)
    assert fresh["inputs"]["commit_log"] == "original.log"
    assert {str(p): p.read_bytes() for p in env.old.parent.iterdir()} == before
    assert env.gh.writes() == [] and env.locks == ["lock", "unlock", "lock", "unlock"]


@pytest.mark.parametrize("change", [
    {"status": "running"}, {"pid": 123}, {"running": "admit apply"}, {"schema": 99},
    {"steps": {"admit apply": {"status": "done"}}}, {"steps": {"resume": {"status": "waiting"}}},
    {"steps": {"admission review": {"status": "waiting"}}},
    {"steps": {"packet": {"status": "running"}}}, {"steps": None},
    {"data": {"main_ci": "focused", "admission_sha256": "d" * 64}},
    {"data": {"main_ci": "focused", "receipt": "receipt.json"}},
    {"data": {"main_ci": "focused", "publish_plan": {"version": VERSION}}},
    {"data": {"main_ci": "focused", "unknown": True}}, {"data": {"main_ci": "full"}},
])
def test_state_uncertainty_or_started_review_refuses_without_changes(env, change):
    original = copy.deepcopy(env.state)
    original.update(change)
    env.rt.providers.store.save(replan.KIND, VERSION, original)
    out = invoke(env.rt, [*env.argv, "--live"])
    assert out.exit_code == 1
    assert env.rt.providers.store.load(replan.KIND, VERSION) == original
    assert env.rt.providers.store.keys(replan.ARCHIVE) == []


@pytest.mark.parametrize("path,contents", [
    (f"focused-admission-receipts/head-{HEAD}-id.json", "{}"),
    (f"focused-release-admission-attempts/v{VERSION}-{HEAD}.attempt.json", "{}"),
    (f"release/v{VERSION}.json", "{}"), ("release.json", '{"version":"0.9.51"}'),
    ("release-waiver.json", '{"version":"0.9.51"}'), ("release.json", "{}"),
    ("release.json", "invalid"),
])
def test_external_local_evidence_and_malformed_receipts_refuse(env, path, contents):
    file = env.root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(contents)
    out = invoke(env.rt, [*env.argv, "--live"])
    assert out.exit_code == 1
    assert env.rt.providers.store.load(replan.KIND, VERSION) == env.state
    assert file.read_text() == contents
    assert env.locks == ["lock", "unlock"]


def test_previous_published_version_does_not_block_new_version(env):
    (env.root / "release.json").write_text('{"version":"0.9.50","finished":true}')
    out = invoke(env.rt, env.argv)
    assert out.exit_code == 0, out.stderr


@pytest.mark.parametrize("identity", [{"open": [1], "closed": [], "branch": False},
    {"open": [], "closed": [1], "branch": False}, {"open": [], "closed": [], "branch": True}, {}])
def test_any_previous_pr_branch_or_unknown_identity_refuses(env, identity):
    env.module.drop_identity = lambda version: identity
    out = invoke(env.rt, [*env.argv, "--live"])
    assert out.exit_code == 1 and env.rt.providers.store.keys(replan.ARCHIVE) == []


@pytest.mark.parametrize("response", [503, {}, {"total_count": True, "workflow_runs": []},
    {"total_count": 1, "workflow_runs": [{"status": "completed", "conclusion": "failure"}]},
    {"total_count": 0, "workflow_runs": [{}]}])
def test_any_candidate_attempt_or_unknown_inventory_refuses(env, response):
    env.gh.on("actions/runs?", response)
    out = invoke(env.rt, [*env.argv, "--live"])
    assert out.exit_code == 1 and env.rt.providers.store.keys(replan.ARCHIVE) == []


@pytest.mark.parametrize("value", ["invalid", json.dumps({"plan": HEAD}), json.dumps({"plan": TREE})])
def test_existing_or_unreadable_registry_refuses(env, value):
    env.registry[replan.focused_slots.NAMES[0]] = value
    out = invoke(env.rt, [*env.argv, "--live"])
    assert out.exit_code == 1 and env.rt.providers.store.keys(replan.ARCHIVE) == []


@pytest.mark.parametrize("defect", ["head", "main", "dirty", "packet", "reused", "source"])
def test_changed_binding_or_reused_output_directory_refuses(env, defect):
    if defect == "head":
        env.gh.on("rev-parse HEAD", "d" * 40)
    elif defect == "main":
        env.rt.providers.vcs.remote["main"] = "d" * 40
    elif defect == "dirty":
        env.gh.on("status --porcelain", " M pyproject.toml")
    elif defect == "packet":
        env.new.write_text('{"main_manifest":{}}')
    elif defect == "reused":
        (env.new.parent / "prepare.out").write_text("old")
    else:
        env.state["data"]["ci_change_head"] = "d" * 40
        env.rt.providers.store.save(replan.KIND, VERSION, env.state)
    out = invoke(env.rt, [*env.argv, "--live"])
    assert out.exit_code == 1 and env.rt.providers.store.keys(replan.ARCHIVE) == []


def test_archive_write_failure_never_clears_original(env, monkeypatch):
    save = env.rt.providers.store.save
    def fail(kind, key, data):
        if kind == replan.ARCHIVE:
            raise OSError("archive unavailable")
        return save(kind, key, data)
    monkeypatch.setattr(env.rt.providers.store, "save", fail)
    out = invoke(env.rt, [*env.argv, "--live"])
    assert out.exit_code == 1
    assert env.rt.providers.store.load(replan.KIND, VERSION) == env.state


def test_cut_lock_contention_refuses_before_probes(env):
    with env.rt.providers.store.lock(f"release.cut.{VERSION}"):
        out = invoke(env.rt, [*env.argv, "--live"])
    assert out.exit_code == 75 and not env.gh.calls


def test_active_state_save_failure_keeps_original_and_a_recoverable_archive(env, monkeypatch):
    save = env.rt.providers.store.save
    def fail(kind, key, data):
        if kind == replan.KIND:
            raise OSError("atomic replacement failed")
        return save(kind, key, data)
    monkeypatch.setattr(env.rt.providers.store, "save", fail)
    out = invoke(env.rt, [*env.argv, "--live"])
    assert out.exit_code == 1
    assert env.rt.providers.store.load(replan.KIND, VERSION) == env.state
    archive = env.rt.providers.store.load(replan.ARCHIVE, env.rt.providers.store.keys(replan.ARCHIVE)[0])
    assert archive["original"] == env.state


def test_binding_changed_during_remote_probes_preserves_original(env):
    def change(version):
        env.gh.on("rev-parse HEAD", "d" * 40)
        return {"open": [], "closed": [], "branch": False}
    env.module.drop_identity = change
    out = invoke(env.rt, [*env.argv, "--live"])
    assert out.exit_code == 1 and env.rt.providers.store.keys(replan.ARCHIVE) == []
    assert env.rt.providers.store.load(replan.KIND, VERSION) == env.state


def test_replanned_generation_rebuilds_packet_and_reaches_real_full_review(full, tmp_path, monkeypatch):
    """Real engine, packet assembly and full source binding on the fixture Git release; only external probes stubbed."""
    rt, ctx, clone, base, head = full
    version = "0.9.41"
    tree = core_policy.git_read(clone, "rev-parse", "HEAD^{tree}").decode().strip()
    class GitReads:
        def run(self, argv, **kwargs):
            assert argv[:2] == ["git", "-C"], argv
            return Result(0, core_policy.git_read(argv[2], *argv[3:]).decode(), "")
    monkeypatch.setattr(rt.providers, "commands", GitReads())
    # The normal hook evidence reader reads these fixture hook files and their pinned runner itself.
    hook_runner = tmp_path / "prek"
    hook_runner.write_text("fixture hook interpreter")
    for name in release_cut.HOOKS:
        (clone / ".git" / "hooks" / name).write_text(f'PREK="{hook_runner}"\n')
    (tmp_path / "hook.log").write_text(f"original hook transcript\nv{version}\n")
    pair = {"main_manifest": {"base": base, "tree": tree, "event": "push"}}
    old, new = tmp_path / "old-pair" / "packet.json", tmp_path / "new-pair" / "packet.json"
    for path in (old, new):
        path.parent.mkdir()
        path.write_text(json.dumps(pair))
    original = {"schema": 1, "pipeline": "release.cut", "project": "t", "key": version, "arg": version,
        "label": f"v{version}", "lane": "default", "author": "coordinator", "status": "refused",
        "fingerprint": {"main": base}, "inputs": {**ctx.inputs, "packet": str(old), "commit_log": "hook.log"},
        "data": {"ci_change_head": head, "main_ci": "focused"},
        "steps": {name: {"status": "done"} for name in ("ci change", "notes", "packet")}}
    rt.providers.store.save(replan.KIND, version, original)
    root = tmp_path / "state"
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(replan.statedir, "root", lambda: root)
    monkeypatch.setattr(replan.statedir, "path", lambda name: root / name)
    module = SimpleNamespace(Stop=RuntimeError, acquire_global_lock=lambda: None, free_global_lock=lambda: None)
    monkeypatch.setattr(release, "runner", lambda: module)
    monkeypatch.setattr(replan, "remote_evidence", lambda *args: None)
    out = invoke(rt, ["release", "replan", version, "--head", head, "--packet", str(new),
        "--main-ci", "full", "--reason", "full candidate requested", "--live"])
    assert out.exit_code == 0, out.stderr
    common.OVERRIDE["release notes-audit"] = lambda **kwargs: SimpleNamespace(exit_code=0, render=lambda: ["ok"])
    common.OVERRIDE["release admit prepare"] = lambda **kwargs: pytest.fail("focused prepare invoked")
    common.OVERRIDE["release admit apply"] = lambda **kwargs: pytest.fail("registry write invoked")
    common.OVERRIDE["release resume"] = lambda **kwargs: pytest.fail("publication invoked")
    out = invoke(rt, ["--format", "json", "release", "cut", version])
    assert out.exit_code == 0, out.stderr
    report = json.loads(out.stdout)
    assert report["status"] == "review" and report["review"]["step"] == "admission review"
    rebuilt = json.loads((new.parent / release_cut.PACKET_NAME).read_text())
    assert rebuilt["release"]["head"] == head and rebuilt["release"]["commit_log"]["path"] == "hook.log"
    plan = json.loads((new.parent / "full-ci-plan.json").read_text())
    assert plan["registry_write"] is False and plan["source_review"]["validation_requested"] == "full-first-attempt"
    assert plan["source_review"]["execution_evidence"] is None
    current = rt.providers.store.load(replan.KIND, version)
    assert current["steps"]["packet"]["detail"].startswith("built ")
    assert "admit apply" not in current["steps"]
