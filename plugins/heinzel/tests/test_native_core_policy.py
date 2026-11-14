"""Core's reviewed CI facts come from the checked commit's manifest: both CI shapes, every tamper case refused."""

from __future__ import annotations

import hashlib
import json

import policy_kit as kit
import pytest

from steuermaschine_heinzel.native.queue_lib import core_policy
from steuermaschine_heinzel.native.queue_lib.release_candidate import CandidateError

C = "c" * 40


def api(blobs: dict[str, bytes], commit: str = C):
    routes = kit.contents_routes(commit, blobs)

    def get(path):
        return routes.get(path)  # None: the API's "not found"

    return get


@pytest.mark.parametrize(("shape", "tests", "jobs"), [("old", 16, 69), ("new", 4, 57)])
def test_both_ci_shapes_read_from_their_own_policy(shape, tests, jobs):
    policy = core_policy.at_commit_api(api(kit.files(shape)), C)
    assert len(policy.test_jobs) == tests  # the fixture shapes' own sizes; the plugin keeps none
    for event in core_policy.EVENTS:
        assert policy.expected_jobs(event) == kit.expected_jobs(event, shape)
        assert len(policy.expected_jobs(event)) == jobs
    assert policy.commit == C and policy.sources == kit.manifest(shape)["sources"]
    assert policy.lane_kind("web") == "vitest" and policy.lane_kind("nope") is None
    assert policy.aggregate == "CI OK" and policy.required_contexts == {"CI OK", "coverage-floor", "merge-gate"}
    assert policy.focused_job_name("web") == "Focused tests (web)"
    assert policy.is_not_ci("docs/a/b.txt") and policy.is_not_ci("src/README.md") and not policy.is_not_ci("src/a.py")


def test_git_reader_uses_commit_objects_never_the_working_tree(tmp_path):
    repo, commit = kit.git_repo(tmp_path / "core", kit.files("new"))
    (repo / kit.PATH).write_text("{}")  # a dirty working tree is never read
    (repo / kit.WORKFLOW).write_bytes(b"tampered\n")
    policy = core_policy.at_commit_git(repo, commit)
    assert len(policy.test_jobs) == 4
    tree = __import__("subprocess").run(["git", "-C", str(repo), "rev-parse", f"{commit}^{{tree}}"], capture_output=True, text=True).stdout.strip()
    assert core_policy.at_tree_git(repo, commit[:12]).commit == tree
    assert core_policy.collected_kind(repo, commit, "browser") == "e2e"
    with pytest.raises(CandidateError, match="cannot list"):
        core_policy.collected_kind(repo, commit, "managed")
    with pytest.raises(CandidateError, match="not a focused lane"):
        core_policy.collected_kind(repo, commit, "python313")
    with pytest.raises(CandidateError, match="no Core tree"):
        core_policy.at_tree_git(repo, "0" * 12)


def test_an_inherited_git_dir_cannot_redirect_the_read(tmp_path, monkeypatch):
    repo, commit = kit.git_repo(tmp_path / "core", kit.files("new"))
    other, _ = kit.git_repo(tmp_path / "other", {"x": b"x"})
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert len(core_policy.at_commit_git(repo, commit).test_jobs) == 4
    assert core_policy.at_tree_git(repo, commit).lanes


def test_a_commit_without_the_manifest_is_refused(tmp_path):
    blobs = kit.files()
    del blobs[kit.PATH]
    with pytest.raises(CandidateError, match=r"release_policy\.json missing"):
        core_policy.at_commit_api(api(blobs), C)
    repo, commit = kit.git_repo(tmp_path / "core", blobs)
    with pytest.raises(CandidateError, match=r"release_policy\.json missing"):
        core_policy.at_commit_git(repo, commit)


@pytest.mark.parametrize("path", [kit.WORKFLOW, kit.RECORDER])
def test_a_pinned_file_that_differs_from_the_policy_digest_is_refused(path):
    blobs = kit.files("new")
    blobs[path] = blobs[path] + b"# tampered\n"
    with pytest.raises(CandidateError, match="does not describe its own"):
        core_policy.at_commit_api(api(blobs), C)


EXTRA = {".github/actions/setup-uv/action.yml": b"runs: {}\n", "scripts/focused_plan.py": b"LANES = {}\n"}


def test_every_file_the_workflow_reaches_is_pinned_and_checked_at_the_commit():
    policy = core_policy.at_commit_api(api(kit.files("new", extra=EXTRA)), C)
    assert set(policy.sources) == {kit.WORKFLOW, kit.RECORDER, *EXTRA}
    assert policy.recorded_sources == {k: policy.sources[k] for k in (kit.WORKFLOW, kit.RECORDER)}
    blobs = kit.files("new", extra=EXTRA)
    blobs["scripts/focused_plan.py"] = b"LANES = {'x': 1}\n"
    with pytest.raises(CandidateError, match=r"does not describe its own scripts/focused_plan\.py"):
        core_policy.at_commit_api(api(blobs), C)


def test_a_file_over_the_contents_limit_is_read_as_its_git_blob():
    big = b"lock: 1\n" * 200_000  # over the contents API's 1 MB
    blobs = kit.files("new", extra={"web/pnpm-lock.yaml": big})
    routes = kit.contents_routes(C, blobs)
    blob_id = "9" * 40
    routes[f"contents/web/pnpm-lock.yaml?ref={C}"] = {"type": "file", "encoding": "none", "content": "", "sha": blob_id}
    routes[f"git/blobs/{blob_id}"] = kit.contents(big)
    assert "web/pnpm-lock.yaml" in core_policy.at_commit_api(routes.get, C).sources
    routes[f"git/blobs/{blob_id}"] = {"encoding": "utf-8", "content": "x"}
    with pytest.raises(CandidateError, match="missing at"):
        core_policy.at_commit_api(routes.get, C)


def test_a_pinned_file_missing_at_the_commit_is_refused():
    blobs = kit.files()
    del blobs[kit.RECORDER]
    with pytest.raises(CandidateError, match="missing at"):
        core_policy.at_commit_api(api(blobs), C)


def _raw(**override):
    return kit.encode(kit.manifest("new", **override))


GARBLED = {
    "not json": b"{not json\n",
    "empty": b"",
    "not an object": b"[]\n",
    "not utf-8": b"\xff\xfe\n",
    "oversized": b" " * (core_policy.MAX_BYTES + 1),
    "duplicate key": b'{"policy": 1, "policy": 1}\n',
    "version 2": _raw(policy=2),
    "version as text": _raw(policy="1"),
    "unknown key": kit.encode(kit.manifest() | {"extra": 1}),
    "missing key": kit.encode({k: v for k, v in kit.manifest().items() if k != "lanes"}),
    "extra pinned file": _raw(sources=kit.manifest()["sources"] | {"x.py": "0" * 64}),
    "short digest": _raw(sources={kit.WORKFLOW: "0" * 63, kit.RECORDER: "0" * 64}),
    "workflow outside workflows": _raw(workflow="ci.yml", sources={"ci.yml": "0" * 64, kit.RECORDER: "0" * 64}),
    "escaping path": _raw(recorder="../x.py", sources={kit.WORKFLOW: "0" * 64, "../x.py": "0" * 64}),
    "missing event": _raw(expected_jobs={"pull_request": {"success": ["CI OK"], "skipped": []}}),
    "job both ways": _raw(expected_jobs={e: {"success": ["CI OK", "A"], "skipped": ["A"]} for e in core_policy.EVENTS}),
    "aggregate not a success job": _raw(aggregate="Nope"),
    "duplicate job": _raw(expected_jobs={e: {"success": ["CI OK", "CI OK"], "skipped": []} for e in core_policy.EVENTS}),
    "empty success": _raw(expected_jobs={e: {"success": [], "skipped": []} for e in core_policy.EVENTS}),
    "quarantine job in inventory": _raw(quarantine=kit.manifest()["quarantine"] | {"plan": "Build"}),
    "quarantine names repeat": _raw(quarantine=kit.manifest()["quarantine"] | {"plan": "Quarantine registry"}),
    "test jobs name the aggregate": _raw(test_jobs=["CI OK"]),
    "no test jobs": _raw(test_jobs=[]),
    "lane missing kind": _raw(lanes={"python": {"runner": "u", "python": "3.14"}}),
    "lane bad name": _raw(lanes={"Py thon": {"runner": "u", "python": "3.14", "kind": "pytest"}}),
    "no lanes": _raw(lanes={}),
    "context not a name": _raw(required_contexts=["CI OK", " x"]),
    "repository malformed": _raw(repository="no-slash"),
    "too many sources": _raw(sources=kit.manifest()["sources"] | {f"s/{i}.py": "0" * 64 for i in range(core_policy.MAX_SOURCES)}),
    "dot segment source": _raw(sources=kit.manifest()["sources"] | {"scripts/./x.py": "0" * 64}),
    "absolute source": _raw(sources=kit.manifest()["sources"] | {"/etc/passwd": "0" * 64}),
    "recorder not pinned": _raw(sources={kit.WORKFLOW: "0" * 64, "x.py": "0" * 64}),
    "focused job without lane": _raw(focused_job="Focused tests"),
    "focused job twice": _raw(focused_job="{lane} {lane}"),
    "focused job other brace": _raw(focused_job="Focused {x} ({lane})"),
    "no not-CI globs": _raw(not_ci=[]),
    "absolute not-CI glob": _raw(not_ci=["/etc/*"]),
    "escaping not-CI glob": _raw(not_ci=["docs/../src/*"]),
}


@pytest.mark.parametrize("name", sorted(GARBLED))
def test_a_garbled_manifest_fails_closed(name):
    blobs = kit.files("new", policy=GARBLED[name])
    with pytest.raises(CandidateError):
        core_policy.at_commit_api(api(blobs), C)


def test_the_manifest_must_be_read_at_an_exact_commit():
    with pytest.raises(CandidateError, match="exact Core commit"):
        core_policy.at_commit_api(api(kit.files()), "main")
    with pytest.raises(CandidateError, match="exact Core commit"):
        core_policy.at_commit_git(".", "HEAD")
    with pytest.raises(CandidateError, match="exact Core commit"):
        core_policy.parse(kit.encode(kit.manifest()), "abc")
    assert json.loads(kit.encode(kit.manifest()))["policy"] == core_policy.VERSION


def test_unsupported_event_and_contents_shape_refuse():
    policy = core_policy.at_commit_api(api(kit.files()), C)
    with pytest.raises(CandidateError, match="unsupported candidate event"):
        policy.expected_jobs("push")
    with pytest.raises(CandidateError, match="missing at"):
        core_policy.at_commit_api(lambda path: {"type": "dir"}, C)
    assert hashlib.sha256(kit.workflow_bytes()).hexdigest() == policy.sources[kit.WORKFLOW]
