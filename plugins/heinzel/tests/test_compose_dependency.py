"""Dependency evidence, immutable release pairs, and crash-safe recovery on an isolated source."""

import hashlib
import io
import json
import zipfile
from copy import deepcopy
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel.native.release_lib import compose_dependency as dep

HEAD = "a" * 40
CORE = "b" * 40
DIGEST = "c" * 64
VERSION = "0.9.57"
NAME = "heinzel-clock"
ID = 46088593436  # synthetic future producer at the shape of the observed legacy failure
TAG = NAME + "/v0.9.12"
ORIGINAL = {
    "id": ID,
    "run_attempt": 1,
    "head_sha": HEAD,
    "head_branch": "main",
    "path": dep.WORKFLOW,
    "repository": {"full_name": dep.hzlib.PLUGINS},
    "head_repository": {"full_name": dep.hzlib.PLUGINS},
    "event": "workflow_dispatch",
    "status": "completed",
    "conclusion": "failure",
    "created_at": "2026-11-28T00:34:00Z",
}
PROOF = {
    "schema": 1,
    "kind": "enrolled-plugin-publication-missing",
    "core_version": VERSION,
    "core_sha": CORE,
    "core_wheel_sha256": DIGEST,
    "plugins_sha": HEAD,
    "run_id": ID,
    "run_attempt": 1,
    "missing": [{"name": NAME, "version": "0.9.12", "tag": TAG}],
}


@pytest.fixture
def world(monkeypatch):
    plugin = dep.hzlib.PLUGINS
    proof = deepcopy(PROOF)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(dep.MEMBER, json.dumps(proof))
    raw = archive.getvalue()
    artifact = {"id": 22, "name": dep.ARTIFACT, "size_in_bytes": len(raw), "digest": "sha256:" + hashlib.sha256(raw).hexdigest(), "expired": False, "workflow_run": {"id": ID, "head_sha": HEAD}}

    def asset(name):
        return {"name": name, "digest": "sha256:" + DIGEST, "state": "uploaded", "size": 1}

    wheel = asset("heinzel-0.9.57-py3-none-any.whl")
    dist = {"tag_name": TAG, "draft": False, "prerelease": False, "assets": [asset("heinzel_clock-0.9.12-py3-none-any.whl"), asset("heinzel_clock-0.9.12.tar.gz")]}
    jobs = [
        {"id": n, "run_id": ID, "status": "completed", "name": name, "conclusion": result}
        for n, (name, result) in enumerate(zip(("select", "compose", "publish", "finalize"), ("success", "failure", "skipped", "skipped"), strict=True), 1)
    ]
    jobs[1]["steps"] = [{"name": "Check enrolled plugin publications", "conclusion": "failure"}, {"name": "Upload enrolled plugin dependency evidence", "conclusion": "success"}]
    pins = json.dumps({TAG: {"revision": HEAD, "assets": {a["name"]: DIGEST for a in dist["assets"]}}}, sort_keys=True, separators=(",", ":"))
    marker = f"dep-{ID}-" + hashlib.sha256(pins.encode()).hexdigest()
    replacement = dict(deepcopy(ORIGINAL), id=ID + 1, display_title=f"Compose recovery {marker} {VERSION} {CORE} {DIGEST}", status="queued", conclusion=None)
    inventory = []
    api = {
        (plugin, f"actions/runs/{ID}"): deepcopy(ORIGINAL),
        (plugin, f"actions/runs/{ID + 1}"): replacement,
        (plugin, f"actions/runs/{ID}/artifacts?per_page=100"): {"total_count": 1, "artifacts": [artifact]},
        (plugin, f"actions/runs/{ID}/attempts/1/jobs?per_page=100"): {"total_count": 4, "jobs": jobs},
        (plugin, "branches/main"): {"name": "main", "protected": True, "commit": {"sha": HEAD}},
        (plugin, "releases/tags/heinzel-clock%2Fv0.9.12"): dist,
        (plugin, "git/ref/tags/heinzel-clock%2Fv0.9.12"): {"ref": "refs/tags/" + TAG, "object": {"type": "commit", "sha": HEAD}},
        (dep.hzlib.CORE, "git/ref/tags/v0.9.57"): {"ref": "refs/tags/v0.9.57", "object": {"type": "commit", "sha": CORE}},
        (dep.hzlib.CORE, "releases/tags/v0.9.57"): {"tag_name": "v0.9.57", "draft": False, "prerelease": False, "assets": [wheel]},
    }

    def query(repo, path):
        if path.startswith("actions/workflows/compose.yml/runs?"):
            return {"total_count": len(inventory), "workflow_runs": inventory}
        return api[repo, path]

    monkeypatch.setattr(dep, "api", query)
    monkeypatch.setattr(dep.edge, "run", lambda *a, **k: SimpleNamespace(stdout=raw))
    monkeypatch.setattr(dep, "plugin_release", lambda tag: dist if tag == TAG else pytest.fail(tag))
    monkeypatch.setattr(dep, "tag_publication", lambda tag: {"release": None, "ref": None} if tag == "app-v" + VERSION else pytest.fail(tag))
    verified, calls = [], []
    monkeypatch.setattr(dep, "checked_assets", lambda *a: verified.append(a))

    def dispatch(*args):
        assert data.get("compose_dependency")  # durable before external side effect
        calls.append(args)
        return 0, ""

    monkeypatch.setattr(dep.hzlib, "gh", dispatch)
    data = {
        "version": VERSION,
        "compose_run_id": ID,
        "compose_run_attempt": 1,
        "compose_dispatch": {"repo": plugin, "workflow": "compose.yml", "ref": "main", "sha": HEAD, "event": "workflow_dispatch", "inputs": {}},
    }
    run = {"databaseId": ID, "headSha": HEAD}
    return SimpleNamespace(**locals())


def begin(w):
    return dep.begin(w.data, VERSION, w.run, w.data.update)


def test_one_request_then_wait_and_adopt_original(world):
    w = world
    assert begin(w)
    assert len(w.calls) == 1 and len(w.verified) == 2
    intent = w.data["compose_dependency"]
    assert not dep.recovered(intent, w.data.update)
    w.inventory.append(w.replacement)
    assert not dep.recovered(intent, w.data.update)
    assert w.data["compose_dependency"]["run_id"] == ID + 1
    w.replacement.update(status="completed", conclusion="success")
    jobs = [dict(j, run_id=ID + 1, conclusion="success" if j["name"] != "finalize" else "skipped") for j in w.jobs]
    w.api[w.plugin, f"actions/runs/{ID + 1}/attempts/1/jobs?per_page=100"] = {"total_count": 4, "jobs": jobs}
    assert dep.recovered(w.data["compose_dependency"], w.data.update)
    jobs[2]["conclusion"] = "skipped"
    with pytest.raises(dep.Invalid, match="did not publish"):
        dep.recovered(w.data["compose_dependency"], w.data.update)
    assert len(w.calls) == 1


@pytest.mark.parametrize(
    "case", ["no-artifact", "wrong-origin", "rerun", "fork", "branch", "event", "job", "step", "core", "sdist", "wheel", "duplicate", "bad-sha", "moved", "unprotected", "receipt-attempt", "unbound"]
)
def test_failures_cannot_authorize_publication(world, case, monkeypatch):
    w = world
    actual = w.api[w.plugin, f"actions/runs/{ID}"]
    if case == "no-artifact":
        w.api[w.plugin, f"actions/runs/{ID}/artifacts?per_page=100"] = {"total_count": 0, "artifacts": []}
    elif case == "wrong-origin":
        w.artifact["workflow_run"]["id"] = ID + 1
    elif case == "rerun":
        actual["run_attempt"] = 2
    elif case == "fork":
        actual["head_repository"] = {"full_name": "fork/repo"}
    elif case == "branch":
        actual["head_branch"] = "topic"
    elif case == "event":
        actual["event"] = "schedule"
    elif case == "job":
        w.jobs[0]["conclusion"] = "failure"
    elif case == "step":
        w.jobs[1]["steps"][0]["name"] = "Build and verify"
    elif case == "core":
        w.api[dep.hzlib.CORE, "git/ref/tags/v0.9.57"]["object"]["sha"] = HEAD
    elif case == "sdist":
        w.dist["assets"].pop()
    elif case == "wheel":
        w.dist["assets"][0]["name"] = "../other.whl"
    elif case == "duplicate":
        w.proof["missing"] *= 2
    elif case == "bad-sha":
        w.artifact["digest"] = "sha256:" + DIGEST
    elif case == "moved":
        w.api[w.plugin, "branches/main"]["commit"]["sha"] = CORE
    elif case == "unprotected":
        w.api[w.plugin, "branches/main"]["protected"] = False
    elif case == "receipt-attempt":
        w.proof["run_attempt"] = 2
    elif case == "unbound":
        del w.data["compose_dispatch"]
    if case in {"receipt-attempt", "duplicate"}:
        monkeypatch.setattr(dep, "read_receipt", lambda *a: w.proof)
    if case == "unbound":
        assert not begin(w)
    else:
        with pytest.raises(dep.Invalid):
            begin(w)
    assert not w.calls and "compose_dependency" not in w.data


def test_delayed_listing_does_not_replace_a_bound_run(world):
    w = world
    w.inventory.append(w.replacement)
    assert begin(w) and not w.calls
    w.inventory.clear()
    assert not dep.recovered(w.data["compose_dependency"], w.data.update)
    w.replacement["run_attempt"] = 2
    with pytest.raises(dep.Invalid):
        dep.recovered(w.data["compose_dependency"], w.data.update)


def test_concurrent_run_and_conflicting_marker(world):
    w = world
    w.inventory.append(dict(w.replacement, display_title="Standard app composition"))
    assert begin(w) and "compose_dependency" not in w.data and not w.calls
    w.inventory[0]["display_title"] = w.replacement["display_title"]
    w.inventory.append(dict(w.replacement, id=ID + 2))
    with pytest.raises(dep.Invalid, match="ambiguous"):
        begin(w)
    assert not w.calls


def test_uncertain_post_stays_pending(world, monkeypatch):
    w = world
    monkeypatch.setattr(dep.hzlib, "gh", lambda *a: (1, ""))
    with pytest.raises(dep.Invalid, match="uncertain"):
        begin(w)
    assert not dep.recovered(w.data["compose_dependency"], w.data.update)


def test_same_marker_different_release_refuses(world):
    w = world
    w.inventory.append(dict(w.replacement, display_title=w.replacement["display_title"].replace("0.9.57", "0.9.58")))
    with pytest.raises(dep.Invalid, match="conflicting inputs"):
        begin(w)
    assert not w.calls


def test_pending_publication_defers_without_downloading_or_dispatching(world, monkeypatch):
    w = world

    def pending(_tag):
        raise dep.Pending(TAG)

    monkeypatch.setattr(dep, "plugin_release", pending)
    assert begin(w) and not w.verified and not w.calls
    assert "compose_dependency" not in w.data


@pytest.mark.parametrize("change", ["wrong-repo", "errors", "hidden", "missing", "draft", "id", "valid"])
def test_authenticated_publication_state(monkeypatch, change):
    data = {"nameWithOwner": dep.hzlib.PLUGINS, "ref": None, "release": {"databaseId": 10, "isDraft": False}}
    result = {"data": {"repository": data}}
    if change == "wrong-repo":
        data["nameWithOwner"] = "wrong"
    if change == "errors":
        result["errors"] = ["denied"]
    if change == "hidden":
        del data["release"]
    if change == "missing":
        data["release"] = None
    if change == "draft":
        data["release"]["isDraft"] = True
    monkeypatch.setattr(dep.hzlib, "ghj", lambda *a, **k: result)
    monkeypatch.setattr(dep, "api", lambda *a: {"id": 11 if change == "id" else 10})
    if change == "valid":
        assert dep.plugin_release(TAG) == {"id": 10}
    else:
        with pytest.raises(dep.Pending if change in ("missing", "draft") else dep.Invalid):
            dep.plugin_release(TAG)


def test_real_compose_resume_recovers_held_original_and_joins_once(world, monkeypatch):
    from steuermaschine_heinzel.native.release_lib import runner

    w = world
    monkeypatch.setattr(runner, "state", lambda: w.data)
    monkeypatch.setattr(runner, "remember", w.data.update)
    w.data["compose"] = w.api[w.plugin, f"actions/runs/{ID}"]["created_at"]
    failed = dict(w.run, status="completed", conclusion="failure", attempt=1)
    monkeypatch.setattr(runner, "ours", lambda *a: failed)
    monkeypatch.setattr(runner, "published_release", lambda *a: None)
    monkeypatch.setattr(runner, "compose_busy", lambda: False)
    runner.compose_step(VERSION, 30, wait_for=False)
    runner.compose_step(VERSION, 30, wait_for=False)
    assert len(w.calls) == 1

    def settle(_name, done, _act, _timeout, check):
        assert not done()
        check()
        w.inventory.append(w.replacement)
        check()
        assert len(w.calls) == 1

    monkeypatch.setattr(runner, "step", settle)
    runner.compose_step(VERSION, 30)
    assert w.data["compose_dependency"]["run_id"] == ID + 1


@pytest.mark.parametrize("direct", [True, False])
def test_run_chronology_in_listing_and_by_id(world, direct):
    w = world
    w.inventory.append(w.replacement)
    assert begin(w)
    if direct:
        w.inventory.clear()
    w.replacement["created_at"] = "2026-11-27T10:00:00Z"
    with pytest.raises(dep.Invalid, match="predates"):
        dep.recovered(w.data["compose_dependency"], w.data.update)


@pytest.mark.parametrize("fault", ["none", "total", "duplicate", "omission"])
def test_complete_paginated_inventory(monkeypatch, fault):
    first = [{"id": x + 1} for x in range(100)]

    def api(repo, url):
        if url.endswith("&page=2"):
            return {"total_count": 100 if fault == "total" else 101, "jobs": [] if fault == "omission" else [{"id": 1 if fault == "duplicate" else 101}]}
        return {"total_count": 101, "jobs": first}

    monkeypatch.setattr(dep, "api", api)
    if fault == "none":
        assert len(dep.rows("actions/test", "jobs")) == 101
    else:
        with pytest.raises(dep.Invalid):
            dep.rows("actions/test", "jobs")


def test_ref_peeling_rejects_an_object_from_another_ref(monkeypatch):
    data = {"git/ref/tags/v1": {"ref": "refs/tags/v1", "object": {"type": "tag", "sha": HEAD}}, "git/tags/" + HEAD: {"sha": HEAD, "object": {"type": "commit", "sha": CORE}}}
    monkeypatch.setattr(dep, "api", lambda repo, path: data[path])
    assert dep.tag_source(dep.hzlib.CORE, "v1") == CORE
    data["git/tags/" + HEAD]["sha"] = CORE
    with pytest.raises(dep.Invalid):
        dep.tag_source(dep.hzlib.CORE, "v1")


@pytest.mark.parametrize("found", [{"ref": None, "release": {"databaseId": 3}}, {"ref": {"name": "app-v0.9.57"}, "release": None}])
def test_existing_app_is_not_replaced(world, monkeypatch, found):
    monkeypatch.setattr(dep, "tag_publication", lambda tag: found)
    with pytest.raises(dep.Invalid, match="already exists"):
        begin(world)
    assert not world.calls


def test_only_verified_bytes_reach_attestation(monkeypatch):
    from pathlib import Path

    calls = []

    def gh(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["release", "download"]:
            Path(argv[-1], "example.tar.gz").write_bytes(b"123")

    monkeypatch.setattr(dep.edge, "run", gh)
    asset = {"state": "uploaded", "name": "example.tar.gz", "size": 3, "digest": "sha256:" + DIGEST}
    with pytest.raises(dep.Invalid, match="digest differs"):
        dep.checked_assets(dep.hzlib.PLUGINS, TAG, HEAD, [asset])
    assert len(calls) == 1
    dep.checked_assets(dep.hzlib.PLUGINS, TAG, HEAD, [dict(asset, digest="sha256:" + hashlib.sha256(b"123").hexdigest())])
    assert calls[-1][1:3] == ["attestation", "verify"] and calls[-1][-4:] == ["--source-ref", "refs/heads/main", "--signer-workflow", f"{dep.hzlib.PLUGINS}/.github/workflows/release.yml"]


def test_changed_distribution_at_same_version_cannot_reuse_or_dispatch(world):
    w = world
    w.inventory.append(w.replacement)
    w.dist["assets"][1]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(dep.Invalid, match="conflicting inputs"):
        begin(w)
    assert not w.calls


def test_recovery_pins_every_verified_distribution_and_source(world):
    w = world
    assert begin(w)
    inputs = w.data["compose_dependency"]["inputs"]
    assert inputs["dependency_pins"] == w.pins
    assert inputs["recovery_marker"] == w.marker
    assert "inputs[dependency_pins]=" + w.pins in w.calls[0]


def test_legacy_uninstrumented_failure_has_no_dependency_route(world):
    w = world
    w.api[w.plugin, f"actions/runs/{ID}/artifacts?per_page=100"] = {"total_count": 0, "artifacts": []}
    w.jobs[1]["steps"] = [{"name": "Build and verify the locally enrolled standard profile", "conclusion": "failure"}]
    assert not begin(w)
    assert not w.calls and "compose_dependency" not in w.data
