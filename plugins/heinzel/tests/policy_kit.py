"""Core release-policy manifests for tests: two CI shapes, a contents-API reader and a git repository holding one.

The shapes are test fixtures, not facts the plugin keeps: OLD is Core's full candidate of 0.9.43 (Test cells on two
runners x Python 3.12-3.15 x unit/integration), NEW the 3.15-only candidate (Test cells on two runners x unit and
integration). The plugin reads whichever shape the checked commit's manifest names.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "core_policy"
WORKFLOW = ".github/workflows/ci.yml"
RECORDER = "scripts/release_candidate.py"
PATH = "scripts/release_policy.json"
REPOSITORY = "helmut-hoffer-von-ankershoffen/heinzel"
CONSUMERS = ("atlassian", "clock", "example", "github", "google", "resource", "tailscale", "theme", "weather", "zoom")
LANES = {
    "browser": ("ubuntu-24.04", "3.14", "e2e"),
    "macos": ("macos-latest", "3.14", "pytest"),
    "managed": ("ubuntu-24.04", "3.14", "managed"),
    "python": ("ubuntu-24.04", "3.14.8", "pytest"),
    "python312": ("ubuntu-24.04", "3.12", "pytest"),
    "web": ("ubuntu-24.04", "3.14", "vitest"),
}


def workflow_bytes() -> bytes:
    """A real reviewed Core ci.yml (the old shape), for readers that parse the workflow (capacity's needs graph)."""
    return (FIXTURES / "ci.yml").read_bytes()


def recorder_bytes() -> bytes:
    return (FIXTURES / "release_candidate.py.txt").read_bytes()


def test_cells(shape: str) -> list[str]:
    minors = range(12, 16) if shape == "old" else (15,)
    return [f"Test ({runner}, Python 3.{minor}, {level})"
            for runner in ("ubuntu-24.04", "macos-latest") for minor in minors for level in ("unit", "integration")]


def expected_jobs(event: str, shape: str = "old") -> dict[str, str]:
    """The candidate inventory of one event in one shape (what Core's recorder names)."""
    success = {
        "Main batch admission", "Changed paths", "Secret scan", "Dependency audit", "Coverage", "Optional source plugins",
        "Plugin SDK candidate", "Lint, contracts and types", "Performance tests", "Heinzel Notifier (macOS app)",
        "macOS-specific tests", "Public contract consumers", "Build", "Plugin template", "Release candidate source", "CI OK",
        *[f"make setup ({runner})" for runner in ("ubuntu-24.04", "ubuntu-24.04-arm", "macos-latest")],
        *test_cells(shape),
        *[f"End-to-end {suite} ({runner})" for suite in ("supervised", "cli", "web-1", "web-2")
          for runner in ("ubuntu-24.04", "macos-latest")],
        *[f"Electron ({runner})" for runner in ("ubuntu-24.04", "macos-latest")],
        *[f"Consumer (heinzel-{name})" for name in CONSUMERS],
    }
    skipped = {
        "Main batch focused scope (${{ needs.admission.outputs.base }})", "Main batch delegated to ${{ needs.admission.outputs.run }}",
        "Site tests", "Focused tests (${{ matrix.lane }})", "macOS smoke", "Source plugin (${{ matrix.name }})",
        *[f"Stop after {name} failure" for name in ("commits", "dependency-review", "secrets", "audit", "setup", "lint")],
    }
    (success if event == "pull_request" else skipped).update(("Conventional commits", "Dependency review"))
    return dict.fromkeys(success, "success") | dict.fromkeys(skipped, "skipped")


def manifest(shape: str = "old", *, workflow_blob: bytes | None = None, recorder_blob: bytes | None = None,
             extra: dict[str, bytes] | None = None, **override) -> dict:
    """One manifest as Core's generator writes it, describing the given workflow and recorder bytes."""
    workflow = workflow_bytes() if workflow_blob is None else workflow_blob
    recorder = recorder_bytes() if recorder_blob is None else recorder_blob
    jobs = {}
    for event in ("pull_request", "workflow_dispatch"):
        inventory = expected_jobs(event, shape)
        jobs[event] = {"success": sorted(n for n, c in inventory.items() if c == "success"),
                       "skipped": sorted(n for n, c in inventory.items() if c == "skipped")}
    data = {
        "policy": 1, "repository": REPOSITORY, "workflow": WORKFLOW, "recorder": RECORDER,
        "sources": {WORKFLOW: hashlib.sha256(workflow).hexdigest(), RECORDER: hashlib.sha256(recorder).hexdigest(),
                    **{path: hashlib.sha256(blob).hexdigest() for path, blob in (extra or {}).items()}},
        "aggregate": "CI OK", "expected_jobs": jobs,
        "quarantine": {"registry": "Quarantine registry", "plan": "Quarantine plan",
                       "runs": ["Quarantine (macos-latest)", "Quarantine (ubuntu-24.04)"], "idle": "Quarantine (${{ matrix.os }})"},
        "test_jobs": sorted(test_cells(shape)),
        "lanes": {name: {"kind": kind, "python": python, "runner": runner} for name, (runner, python, kind) in LANES.items()},
        "required_contexts": ["CI OK", "coverage-floor", "merge-gate"],
        "focused_job": "Focused tests ({lane})",
        "not_ci": ["*.md", "docs/**"],
    }
    return data | override


def encode(data: dict) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()


def files(shape: str = "old", *, workflow_blob: bytes | None = None, recorder_blob: bytes | None = None,
          policy: bytes | None = None, extra: dict[str, bytes] | None = None, **override) -> dict[str, bytes]:
    """The three files of a Core commit: the workflow, the recorder and the manifest describing them."""
    workflow = workflow_bytes() if workflow_blob is None else workflow_blob
    recorder = recorder_bytes() if recorder_blob is None else recorder_blob
    raw = (encode(manifest(shape, workflow_blob=workflow, recorder_blob=recorder, extra=extra, **override))
           if policy is None else policy)
    return {WORKFLOW: workflow, RECORDER: recorder, **(extra or {}), PATH: raw}


def contents(blob: bytes) -> dict:
    """A contents-API answer for one file."""
    return {"type": "file", "encoding": "base64", "content": base64.b64encode(blob).decode()}


def contents_routes(commit: str, blobs: dict[str, bytes]) -> dict[str, dict]:
    """path -> answer for every file at `commit`, as `get(path)` sees the contents API."""
    return {f"contents/{path}?ref={commit}": contents(blob) for path, blob in blobs.items()}


def git_repo(root: Path, blobs: dict[str, bytes], extra: dict[str, bytes] | None = None) -> tuple[Path, str]:
    """A git repository with one commit holding `blobs` (and `extra`); returns (repo, commit)."""
    root.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    for path, blob in {**blobs, **(extra or {})}.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "core"], check=True, env=env)
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, env=env, capture_output=True, text=True)
    return root, head.stdout.strip()


def policy(*, commit: str = "c" * 40, sources: dict[str, str] | None = None, jobs: dict[str, str] | None = None,
           shape: str = "old", **override):
    """A Policy object directly (no manifest round trip), for readers whose other inputs are fakes."""
    from steuermaschine_heinzel.native.queue_lib import core_policy

    data = manifest(shape)
    inventory = {event: dict(jobs) if jobs is not None else expected_jobs(event, shape) for event in core_policy.EVENTS}
    q = data["quarantine"]
    fields = dict(
        commit=commit, repository=REPOSITORY, workflow=WORKFLOW, recorder=RECORDER,
        sources=dict(data["sources"] if sources is None else sources), aggregate="CI OK", jobs=inventory,
        quarantine=core_policy.Quarantine(q["registry"], q["plan"], frozenset(q["runs"]), q["idle"]),
        test_jobs=frozenset(data["test_jobs"]),
        lanes={name: core_policy.Lane(**lane) for name, lane in data["lanes"].items()},
        required_contexts=frozenset(data["required_contexts"]),
        focused_job=data["focused_job"], not_ci=tuple(data["not_ci"]),
    )
    return core_policy.Policy(**(fields | override))
