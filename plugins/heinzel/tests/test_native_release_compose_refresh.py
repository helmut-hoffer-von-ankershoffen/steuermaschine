"""`sm release resume`, compose step: a failed compose run is no longer re-adopted forever. When plugins main moved
past the failed run and a plugin version that changed since is on PyPI (0.9.40: relock #88 plus plugin releases, the
coordinator's manual run 43443233990), the runner dispatches a fresh compose with its own inputs, binds it and keeps
a receipt. Otherwise it stops as before."""

import base64
import subprocess

import pytest

from steuermaschine_heinzel.native.queue_lib import edge, hzlib

FAILED, OLD, NEW = 43434421352, "a" * 40, "b" * 40


def toml(version):
    return base64.b64encode(f'[project]\nname = "x"\nversion = "{version}"\n'.encode()).decode()


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now


@pytest.fixture
def rel(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HZ_RELEASE_FLOW_DIR", str(tmp_path / "flow"))
    monkeypatch.setattr(hzlib, "_gh_process", lambda *a, **_: subprocess.CompletedProcess(a, 1, "", ""))
    module = edge.fresh(edge.module_of("hz-release"))
    module.remember(version="0.9.40", compose="2026-11-13T09:00:00Z")
    clock = Clock()
    monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(module.hzlib, "sleep", lambda s: setattr(clock, "now", clock.now + s))
    said, dispatched = [], []
    monkeypatch.setattr(module.hzlib, "say", lambda *a, **k: said.append(a[0]))
    monkeypatch.setattr(module, "compose_busy", lambda: False)
    monkeypatch.setattr(module, "published_release", lambda repo, tag: {"prerelease": False} if tag == "v0.9.40" else None)
    world = {"main": NEW, "files": ["plugins/heinzel-audio/pyproject.toml", "uv.lock"], "versions": {OLD: "0.1.4", NEW: "0.1.5"},
             "pypi": {"0.1.5"}, "compare": "ahead"}

    def gh(*args):
        if args[:2] == ("api", f"repos/{module.PLUGINS}/commits/main"):
            return 0, world["main"] + "\n"
        if args[0] == "api" and "/contents/plugins/heinzel-audio/pyproject.toml?ref=" in args[1]:
            return 0, toml(world["versions"][args[1].rsplit("=", 1)[1]])
        if args[:2] == ("release", "view"):
            return 0, "https://example.invalid/heinzel-0.9.40-py3-none-any.whl\n"
        raise AssertionError(args)

    monkeypatch.setattr(module.hzlib, "gh", gh)
    monkeypatch.setattr(module.hzlib, "ghj", lambda *a, **k: {"status": world["compare"], "files": [{"filename": f} for f in world["files"]]}
                        if a[1].startswith(f"repos/{module.PLUGINS}/compare/{OLD}...") else pytest.fail(str(a)))
    monkeypatch.setattr(module, "pypi_has", lambda dist, version: version in world["pypi"])

    def dispatch(repo, workflow, ref, key, **inputs):
        dispatched.append((workflow, ref, key, inputs))
        module.remember(**{key: f"2026-11-13T13:3{len(dispatched)}:00Z"})

    monkeypatch.setattr(module, "dispatch", dispatch)
    module.world, module.said, module.dispatched = world, said, dispatched
    return module


def failed_then(rel, monkeypatch, fresh):
    """The failed run until a fresh dispatch is recorded, then `fresh` (None: not listed yet)."""
    failed = {"databaseId": FAILED, "status": "completed", "conclusion": "failure", "headSha": OLD}
    monkeypatch.setattr(rel, "ours", lambda repo, wf, key: (failed if rel.state()["compose"] == "2026-11-13T09:00:00Z" else fresh) if key == "compose" else None)


def test_a_failed_compose_with_a_published_fix_on_plugins_main_gets_a_fresh_bound_compose(rel, monkeypatch):
    fresh = {"databaseId": 43443233990, "status": "in_progress", "conclusion": None, "headSha": NEW}
    failed_then(rel, monkeypatch, fresh)
    polls = []

    def published(repo, tag):  # app-v0.9.40 appears two polls after the fresh dispatch
        if tag == "v0.9.40":
            return {"prerelease": False}
        polls.append(bool(rel.dispatched))
        return {"prerelease": False} if sum(polls) > 2 else None

    monkeypatch.setattr(rel, "published_release", published)
    rel.compose_step("0.9.40", 3600)
    assert rel.dispatched == [("compose.yml", "main", "compose", {"version": "0.9.40", "core_wheel": "https://example.invalid/heinzel-0.9.40-py3-none-any.whl"})]
    receipt = rel.state()["compose_replaced"]
    assert len(receipt) == 1 and {k: v for k, v in receipt[0].items() if k != "at"} == {
        "failed_run": FAILED, "failed_conclusion": "failure", "failed_head": OLD, "main": NEW, "plugins": {"heinzel-audio": ["0.1.4", "0.1.5"]}}
    assert rel.state()["compose"] == "2026-11-13T13:31:00Z"  # the fresh run is bound by its dispatch time
    assert any("heinzel-audio 0.1.4 -> 0.1.5: dispatching a fresh compose" in line for line in rel.said)


@pytest.mark.parametrize("change", [
    {"main": OLD},                                    # plugins main did not move
    {"compare": "diverged"},                          # main is not ahead of the failed head
    {"files": ["uv.lock"]},                           # no plugin version changed
    {"versions": {OLD: "0.1.5", NEW: "0.1.5"}},       # the version did not change
    {"pypi": set()},                                  # the new version is not published
])
def test_without_a_moved_main_and_a_published_plugin_change_a_failed_compose_still_stops(rel, monkeypatch, change):
    rel.world.update(change)
    failed_then(rel, monkeypatch, None)
    with pytest.raises(rel.Stop, match=f"compose run {FAILED} ended failure"):
        rel.compose_step("0.9.40", 3600)
    assert rel.dispatched == [] and "compose_replaced" not in rel.state()


def test_a_failed_run_is_replaced_once_and_a_failed_replacement_stops(rel, monkeypatch):
    failed_then(rel, monkeypatch, None)
    rel.remember(compose_replaced=[{"failed_run": FAILED}])
    with pytest.raises(rel.Stop, match=f"compose run {FAILED} ended failure"):
        rel.compose_step("0.9.40", 3600)
    assert rel.dispatched == []


def test_a_busy_compose_queue_defers_the_fresh_dispatch(rel, monkeypatch):
    failed_then(rel, monkeypatch, None)
    busy = iter([True, False])
    monkeypatch.setattr(rel, "compose_busy", lambda: next(busy))
    with pytest.raises(rel.Stop, match="timed out"):
        rel.compose_step("0.9.40", 60)
    assert len(rel.dispatched) == 1 and len(rel.state()["compose_replaced"]) == 1


@pytest.mark.parametrize("defect", ["missing", "old_id", "wrong_head", "rerun", "changed_id"])
def test_guard_refuses_ambiguous_replacement_even_if_app_exists(rel, monkeypatch, defect):
    rel.remember(compose_replacement_guard={"failed_run": FAILED, "reviewed_head": NEW})
    run = {"databaseId": 999, "headSha": NEW, "attempt": 1, "status": "completed", "conclusion": "success"}
    if defect == "old_id":
        run["databaseId"] = FAILED
    if defect == "wrong_head":
        run["headSha"] = OLD
    if defect == "rerun":
        run["attempt"] = 2
    if defect == "changed_id":
        rel.remember(compose_replacement_guard={"failed_run": FAILED, "reviewed_head": NEW, "replacement_run": 1000})
    monkeypatch.setattr(rel, "ours", lambda *args: None if defect == "missing" else run)
    monkeypatch.setattr(rel, "published_release", lambda *args: {"prerelease": False})
    with pytest.raises(rel.Stop, match="replacement is pending or its identity changed"):
        rel.compose_step("0.9.40", 60)
    assert rel.dispatched == []


def test_failed_guarded_replacement_cannot_automatically_dispatch_again(rel, monkeypatch):
    rel.remember(compose_replacement_guard={"failed_run": FAILED, "reviewed_head": NEW})
    run = {"databaseId": 999, "headSha": NEW, "attempt": 1, "status": "completed", "conclusion": "failure"}
    monkeypatch.setattr(rel, "ours", lambda *args: run)
    with pytest.raises(rel.Stop, match="no further dispatch"):
        rel.compose_step("0.9.40", 60)
    assert rel.dispatched == []
