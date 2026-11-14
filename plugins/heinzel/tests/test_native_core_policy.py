"""Core's reviewed CI facts come from the checked commit's manifest (both CI shapes; a missing or garbled one refuses),
and ci_changes judges a change by the not-CI paths of its base."""

from __future__ import annotations

import json
import os
import subprocess

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
    assert policy.commit == C and policy.pinned == (kit.WORKFLOW, kit.RECORDER)
    assert policy.lane_kind("web") == "vitest" and policy.lane_kind("nope") is None
    assert policy.aggregate == "CI OK" and policy.required_contexts == {"CI OK", "coverage-floor", "merge-gate"}
    assert policy.focused_job_name("web") == "Focused tests (web)"
    assert policy.is_not_ci("docs/a/b.txt") and policy.is_not_ci("src/README.md") and not policy.is_not_ci("src/a.py")
    assert not policy.is_not_ci("docs/conftest.md")  # a literal except path is CI although a glob matches it


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


def test_a_file_over_the_contents_limit_is_read_as_its_git_blob():
    big = kit.encode(kit.manifest("new", lanes={f"l{i}": {"kind": "pytest", "python": "3.14", "runner": "u" * 900}
                                              for i in range(1200)}))
    assert len(big) > 1024 * 1024  # over the contents API's 1 MB
    routes = kit.contents_routes(C, kit.files("new", policy=big))
    blob_id = "9" * 40
    routes[f"contents/{kit.PATH}?ref={C}"] = {"type": "file", "encoding": "none", "content": "", "sha": blob_id}
    routes[f"git/blobs/{blob_id}"] = kit.contents(big)
    assert len(core_policy.at_commit_api(routes.get, C).lanes) == 1200
    routes[f"git/blobs/{blob_id}"] = {"encoding": "utf-8", "content": "x"}
    with pytest.raises(CandidateError, match="missing at"):
        core_policy.at_commit_api(routes.get, C)


def _raw(**override):
    return kit.encode(kit.manifest("new", **override))


NOT_CI = kit.manifest()["not_ci"]
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
    "sources key": kit.encode(kit.manifest() | {"sources": {}}),  # policy 1 has no checksums
    "missing key": kit.encode({k: v for k, v in kit.manifest().items() if k != "lanes"}),
    "workflow outside workflows": _raw(workflow="ci.yml"),
    "escaping path": _raw(recorder="../x.py"),
    "control character path": _raw(recorder="scripts/x\ty.py"),
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
    "focused job without lane": _raw(focused_job="Focused tests"),
    "focused job twice": _raw(focused_job="{lane} {lane}"),
    "focused job other brace": _raw(focused_job="Focused {x} ({lane})"),
    "not_ci a list": _raw(not_ci=["*.md"]),
    "not_ci extra key": _raw(not_ci=NOT_CI | {"other": []}),
    "no not-CI globs": _raw(not_ci=NOT_CI | {"match": []}),
    "absolute not-CI glob": _raw(not_ci=NOT_CI | {"match": ["/etc/*"]}),
    "escaping not-CI glob": _raw(not_ci=NOT_CI | {"match": ["docs/../src/*"]}),
    "glob in except": _raw(not_ci=NOT_CI | {"except": ["docs/*.md"]}),
    "class in except": _raw(not_ci=NOT_CI | {"except": ["docs/[a].md"]}),
    "query in except": _raw(not_ci=NOT_CI | {"except": ["docs/a?.md"]}),
    "duplicate except": _raw(not_ci=NOT_CI | {"except": ["a.md", "a.md"]}),
    "absolute except": _raw(not_ci=NOT_CI | {"except": ["/a.md"]}),
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


# ----------------------------------------------------------------------------- ci_changes
def _commit(repo, files=None, remove=(), message="change"):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    for path, blob in (files or {}).items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    for path in remove:
        (repo / path).unlink()
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", message],
                   check=True, env=env)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, env=env).stdout.strip()


@pytest.fixture
def core(tmp_path):
    repo, base = kit.git_repo(tmp_path / "core", kit.files("new", extra={"docs/a.md": b"a\n", "src/a.py": b"x = 1\n"}))
    return repo, base


def test_a_docs_only_change_is_not_ci(core):
    repo, base = core
    head = _commit(repo, {"docs/a.md": b"b\n", "docs/new/b.md": b"n\n"})
    assert core_policy.ci_changes(repo, base, head) == () and core_policy.ci_changed(repo, base, head) is False


def test_every_ci_path_is_a_reason(core):
    repo, base = core
    head = _commit(repo, {"src/a.py": b"x = 2\n", "docs/conftest.md": b"except\n", "docs/a.md": b"b\n"})
    reasons = core_policy.ci_changes(repo, base, head)
    assert [r.split(":")[0] for r in reasons] == ["docs/conftest.md", "src/a.py"] and core_policy.ci_changed(repo, base, head)


def test_a_rename_is_a_delete_and_an_add(core):
    repo, base = core
    (repo / "src/a.py").rename(repo / "docs/a.py.md")
    head = _commit(repo)
    assert [r.split(":")[0] for r in core_policy.ci_changes(repo, base, head)] == ["src/a.py"]


def test_the_not_ci_paths_are_the_bases_never_the_heads(core):
    repo, base = core
    widened = kit.encode(kit.manifest("new", not_ci={"except": [], "match": ["*"]}))
    head = _commit(repo, {kit.PATH: widened, "src/a.py": b"x = 3\n"})
    paths = [r.split(":")[0] for r in core_policy.ci_changes(repo, base, head)]
    assert paths == [kit.PATH, "src/a.py"]  # the head declares everything harmless; the base does not
    later = _commit(repo, {"src/a.py": b"x = 4\n"})
    assert core_policy.ci_changes(repo, head, later) == ()  # once reviewed and merged, the wider list applies


def test_symlinks_and_submodules_are_ci_wherever_they_are(core):
    repo, base = core
    os.symlink("a.md", repo / "docs/link.md")
    head = _commit(repo)
    assert core_policy.ci_changes(repo, base, head) == ("docs/link.md: symlink or submodule",)


def test_ci_changes_fails_closed_and_never_raises(core, tmp_path):
    repo, base = core
    head = _commit(repo, {"docs/a.md": b"c\n"})
    assert core_policy.ci_changes(repo, base, base)[0].endswith("is empty or garbled")
    assert core_policy.ci_changes(repo, "main", head) == ("base and head must be exact commits",)
    assert "is not an ancestor" in core_policy.ci_changes(repo, base, "0" * 40)[0]  # a missing head
    bare, old = kit.git_repo(tmp_path / "old", {"README.md": b"r\n"})  # a base without the manifest
    new = _commit(bare, {"README.md": b"s\n"})
    assert core_policy.ci_changes(bare, old, new)[0].startswith(f"no reviewed release policy at base {old}")
    assert "is not an ancestor" in core_policy.ci_changes(tmp_path / "nowhere", base, head)[0]


def test_a_base_that_is_not_an_ancestor_of_the_head_fails_closed(core):
    """A sibling with a wider not_ci list never becomes the base: the base must be an ancestor of the head."""
    import subprocess as sp

    repo, base = core
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    sibling = _commit(repo, {kit.PATH: kit.encode(kit.manifest("new", not_ci={"except": [], "match": ["*"]}))})
    sp.run(["git", "-C", str(repo), "checkout", "-q", base], check=True, env=env)
    head = _commit(repo, {"src/a.py": b"x = 9\n"})
    assert core_policy.ci_changes(repo, sibling, head) == (
        f"base {sibling[:12]} is not an ancestor of head {head[:12]} (or either is missing in the clone)",)
    assert core_policy.ci_changes(repo, base, head) == ("src/a.py: CI path (not listed as not CI at base " + base[:12] + ")",)
