"""Heinzel's reviewed CI facts, read from the exact Core commit being checked: never typed into this plugin.

Core keeps a generated manifest, `scripts/release_policy.json`, next to its candidate recorder (a Core test regenerates
it and compares bytes). It names the pinned files (every file the candidate
executes that the workflow reaches, the workflow and the recorder always among them) and their sha256, the full candidate job inventory per event, the
quarantine lane jobs, the Test cells a full main pass needs, the focused lanes, the required status contexts, the
focused-lane job name template and the paths Core classifies as definitely not CI.

Trust: the manifest is read only from the commit (git object or the contents API at that SHA), never from a working
tree, the environment or this repository. It must describe its own commit: the `sources` digests must equal the
sha256 of the pinned blobs at that same commit, so a manifest edited without its sources (or the reverse) is refused.
A commit without the manifest, an unknown version, an unknown or missing key, a wrong type: refused, no fallback.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from steuermaschine.config import child_environment

from .release_candidate import CandidateError, contents_bytes, require, unique_object

if TYPE_CHECKING:
    from collections.abc import Callable

PATH = "scripts/release_policy.json"
VERSION = 1
MAX_BYTES = 256 * 1024
MAX_SOURCES = 200  # every file the candidate executes that the workflow reaches, workflow and recorder included
EVENTS = ("pull_request", "workflow_dispatch")
KEYS = frozenset((
    "policy", "repository", "workflow", "recorder", "sources", "aggregate", "expected_jobs", "quarantine", "test_jobs",
    "lanes", "required_contexts", "focused_job", "not_ci",
))
QUARANTINE_KEYS = frozenset(("registry", "plan", "runs", "idle"))
LANE_KEYS = frozenset(("runner", "python", "kind"))
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
REPO_PATH = re.compile(r"(?!/)(?!.*(?:^|/)\.\.?(?:/|$))[A-Za-z0-9._/-]{1,200}")
REPOSITORY = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class Lane:
    runner: str
    python: str
    kind: str


@dataclass(frozen=True, slots=True)
class Quarantine:
    registry: str
    plan: str
    runs: frozenset[str]
    idle: str


@dataclass(frozen=True, slots=True)
class Policy:
    """The reviewed facts of one Core commit."""

    commit: str
    repository: str
    workflow: str
    recorder: str
    sources: dict[str, str]
    aggregate: str
    jobs: dict[str, dict[str, str]]
    quarantine: Quarantine
    test_jobs: frozenset[str]
    lanes: dict[str, Lane]
    required_contexts: frozenset[str]
    focused_job: str
    not_ci: tuple[str, ...]

    def expected_jobs(self, event: str) -> dict[str, str]:
        """The complete candidate inventory of `event`: job name -> its required conclusion."""
        require(event in self.jobs, "unsupported candidate event")
        return dict(self.jobs[event])

    @property
    def recorded_sources(self) -> dict[str, str]:
        """What the candidate's own proof records: the digests of the workflow and the recorder only. The other
        pinned files are bound by the proof's source commit, whose blobs the readers compare with every pin."""
        return {self.workflow: self.sources[self.workflow], self.recorder: self.sources[self.recorder]}

    def focused_job_name(self, lane: str) -> str:
        """The PR job name of focused `lane` (Core's template, e.g. "Focused tests ({lane})")."""
        return self.focused_job.replace("{lane}", lane)

    def is_not_ci(self, path: str) -> bool:
        """True when Core classifies the repo-relative POSIX `path` as definitely not CI (fnmatchcase: `*` crosses `/`)."""
        return any(fnmatch.fnmatchcase(path, glob) for glob in self.not_ci)

    def lane_kind(self, lane: str) -> str | None:
        found = self.lanes.get(lane)
        return found.kind if found else None


def _names(value: object, what: str, *, empty: bool = False) -> list[str]:
    require(
        isinstance(value, list) and (empty or value) and all(isinstance(v, str) and v.strip() == v and v for v in value)
        and len(set(value)) == len(value),
        f"release policy {what} is not a list of unique names",
    )
    return list(value)  # type: ignore[arg-type]


def _text(value: object, what: str) -> str:
    require(isinstance(value, str) and value and value.strip() == value, f"release policy {what} is not a name")
    return value  # type: ignore[return-value]


def _jobs(value: object) -> dict[str, dict[str, str]]:
    require(isinstance(value, dict) and set(value) == set(EVENTS), "release policy expected_jobs events differ")
    out = {}
    for event in EVENTS:
        rows = value[event]  # type: ignore[index]
        require(isinstance(rows, dict) and set(rows) == {"success", "skipped"}, f"release policy {event} jobs malformed")
        success = _names(rows["success"], f"{event} success jobs")
        skipped = _names(rows["skipped"], f"{event} skipped jobs", empty=True)
        require(not set(success) & set(skipped), f"release policy {event} job is both success and skipped")
        out[event] = dict.fromkeys(success, "success") | dict.fromkeys(skipped, "skipped")
    return out


def parse(raw: bytes, commit: str) -> Policy:
    """Strictly read one manifest; anything unknown or missing refuses (fail closed).

    Raises:
        CandidateError: The manifest is absent, garbled or outside the known format.
    """
    require(isinstance(commit, str) and SHA.fullmatch(commit), "release policy needs an exact Core commit")
    require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_BYTES, f"release policy {PATH} missing or oversized at {commit}")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, ValueError) as exc:
        raise CandidateError(f"release policy {PATH} at {commit} is not valid JSON") from exc
    require(isinstance(data, dict), "release policy is not an object")
    require(type(data.get("policy")) is int and data["policy"] == VERSION, f"unknown release policy version at {commit}")
    require(set(data) == KEYS, f"release policy keys differ: {sorted(set(data) ^ KEYS)}")
    repository = _text(data["repository"], "repository")
    require(REPOSITORY.fullmatch(repository), "release policy repository malformed")
    workflow, recorder = _text(data["workflow"], "workflow"), _text(data["recorder"], "recorder")
    require(
        REPO_PATH.fullmatch(workflow) and REPO_PATH.fullmatch(recorder) and workflow != recorder
        and workflow.startswith(".github/workflows/"),
        "release policy pinned paths malformed",
    )
    sources = data["sources"]
    require(
        isinstance(sources, dict) and 2 <= len(sources) <= MAX_SOURCES and {workflow, recorder} <= set(sources)
        and all(isinstance(k, str) and REPO_PATH.fullmatch(k) for k in sources)
        and all(isinstance(v, str) and DIGEST.fullmatch(v) for v in sources.values()),
        "missing reviewed CI source pins",
    )
    aggregate = _text(data["aggregate"], "aggregate")
    jobs = _jobs(data["expected_jobs"])
    require(all(job.get(aggregate) == "success" for job in jobs.values()), "release policy aggregate is not a success job")
    q = data["quarantine"]
    require(isinstance(q, dict) and set(q) == QUARANTINE_KEYS, "release policy quarantine malformed")
    quarantine = Quarantine(_text(q["registry"], "quarantine registry"), _text(q["plan"], "quarantine plan"),
                            frozenset(_names(q["runs"], "quarantine runs")), _text(q["idle"], "quarantine idle"))
    lane_names = {quarantine.registry, quarantine.plan, quarantine.idle, *quarantine.runs}
    require(len(lane_names) == 3 + len(quarantine.runs), "release policy quarantine names repeat")
    require(not lane_names & {name for job in jobs.values() for name in job}, "release policy quarantine job in inventory")
    test_jobs = frozenset(_names(data["test_jobs"], "test jobs"))
    require(aggregate not in test_jobs, "release policy test jobs name the aggregate")
    lanes_raw = data["lanes"]
    require(isinstance(lanes_raw, dict) and lanes_raw, "release policy lanes malformed")
    lanes = {}
    for name, lane in lanes_raw.items():
        require(isinstance(name, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]*", name), "release policy lane name malformed")
        require(isinstance(lane, dict) and set(lane) == LANE_KEYS, f"release policy lane {name} malformed")
        lanes[name] = Lane(_text(lane["runner"], "lane runner"), _text(lane["python"], "lane python"),
                           _text(lane["kind"], "lane kind"))
    contexts = frozenset(_names(data["required_contexts"], "required contexts"))
    focused = _text(data["focused_job"], "focused job")
    rest = focused.replace("{lane}", "")
    require(focused.count("{lane}") == 1 and "{" not in rest and "}" not in rest,
            "release policy focused job is not a {lane} template")
    not_ci = tuple(_names(data["not_ci"], "not-CI globs"))
    require(all(not g.startswith("/") and not re.search(r"(?:^|/)\.\.(?:/|$)", g) for g in not_ci),
            "release policy not-CI glob is not repository-relative")
    return Policy(commit, repository, workflow, recorder, dict(sources), aggregate, jobs, quarantine, test_jobs, lanes,
                  contexts, focused, not_ci)


def verified(raw: bytes, commit: str, blob: Callable[[str], bytes]) -> Policy:
    """Parse the manifest and require it to describe its own commit: each pin is the sha256 of that commit's blob.

    Raises:
        CandidateError: Garbled manifest, or a pinned file at the commit differs from the manifest's digest.
    """
    policy = parse(raw, commit)
    for path, digest in policy.sources.items():
        require(hashlib.sha256(blob(path)).hexdigest() == digest,
                f"release policy at {commit} does not describe its own {path}")
    return policy


def at_commit_api(get: Callable[[str], Any], commit: str) -> Policy:
    """The manifest of `commit`, through the contents API at that exact SHA (the candidate readers' transport)."""
    require(isinstance(commit, str) and SHA.fullmatch(commit), "release policy needs an exact Core commit")

    def read(path: str) -> bytes:
        require(REPO_PATH.fullmatch(path), "invalid release policy source request")
        blob = contents_bytes(get, path, commit, f"{path} missing at {commit}")
        require(blob is not None, f"{path} missing at {commit}")
        return blob  # type: ignore[return-value]

    return verified(read(PATH), commit, read)


def _git_env() -> dict[str, str]:
    """The environment of every git read here: no inherited GIT_* variable may redirect it to another repository."""
    return child_environment(drop_prefixes=("GIT_",))


def at_commit_git(repo: str | Path, commit: str) -> Policy:
    """The manifest of `commit` from the git objects of a local Core clone (`git show COMMIT:PATH`), never its tree."""
    require(isinstance(commit, str) and SHA.fullmatch(commit), "release policy needs an exact Core commit")
    git = shutil.which("git")
    require(git, "git is not installed")

    def read(path: str) -> bytes:
        require(REPO_PATH.fullmatch(path), "invalid release policy source request")
        done = subprocess.run([str(git), "-C", str(repo), "show", f"{commit}:{path}"],  # noqa: S603 - fixed git, no shell
                              capture_output=True, check=False, timeout=30, env=_git_env())
        require(done.returncode == 0, f"{path} missing at {commit}")
        return done.stdout

    return verified(read(PATH), commit, read)


def at_tree_git(repo: str | Path, treeish: str) -> Policy:
    """The manifest of a commit or tree named by `treeish`, resolved to its exact tree id first (a packet's trees)."""
    git = shutil.which("git")
    require(git, "git is not installed")
    require(isinstance(treeish, str) and re.fullmatch(r"[0-9a-f]{4,40}", treeish), "invalid Core tree")
    done = subprocess.run([str(git), "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{treeish}^{{tree}}"],  # noqa: S603
                          capture_output=True, check=False, timeout=30, text=True, env=_git_env())
    tree = done.stdout.strip() if done.returncode == 0 else ""
    require(SHA.fullmatch(tree), f"no Core tree {treeish}")
    return at_commit_git(repo, tree)


COLLECTED_KINDS = ("pytest", "e2e", "vitest")  # what the case collector of this plugin can list


def collected_kind(repo: str | Path, treeish: str, lane: str) -> str:
    """The kind of focused `lane` in the reviewed policy of `treeish`; refuses a lane it does not name or can't collect.

    Raises:
        CandidateError: No policy, the lane is unknown there, or its kind has no collector here.
    """
    kind = at_tree_git(repo, treeish).lane_kind(lane)
    require(kind is not None, f"lane {lane} is not a focused lane of Core's release policy at {treeish}")
    require(kind in COLLECTED_KINDS, f"lane {lane} has kind {kind}, which the case collector cannot list")
    return kind  # type: ignore[return-value]
