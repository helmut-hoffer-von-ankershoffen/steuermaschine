"""Heinzel's reviewed CI facts, read from the exact Core commit being checked: never typed into this plugin.

Core keeps a generated manifest, `scripts/release_policy.json`, next to its candidate recorder (Core's CI regenerates
it and refuses one that drifted). It names the workflow and the recorder, the full candidate job inventory per event,
the quarantine lane jobs, the Test cells a full main pass needs, the focused lanes, the required status contexts, the
focused-lane job name template and the paths Core classifies as definitely not CI.

Trust: what binds the facts is the commit itself and its recorded review verdict, not a checksum. The manifest is read
only from that commit (git objects or the contents API at that SHA), never from a working tree, the environment or
this repository. A commit without the manifest, an unknown version, an unknown or missing key, a wrong type: refused,
no fallback. `ci_changes(base, head)` says whether a change touches CI, by the not-CI paths of the BASE's manifest.
"""

from __future__ import annotations

import fnmatch
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
MAX_BYTES = 4 * 1024 * 1024
MAX_PATH = 1024  # bytes of one path, UTF-8
EVENTS = ("pull_request", "workflow_dispatch")
KEYS = frozenset((
    "policy", "repository", "workflow", "recorder", "aggregate", "expected_jobs", "quarantine", "test_jobs",
    "lanes", "required_contexts", "focused_job", "not_ci", "judge_workflow", "gate_checks",
))
OPTIONAL_KEYS = frozenset(("lanes", "focused_job", "gate_checks", "judge_workflow"))  # Core's focused-plan and merge-gate parts: a policy may drop them
GATE_ROLES = frozenset(("coverage", "sonar"))  # what each check the merge-gate workflow posts judges
QUARANTINE_KEYS = frozenset(("registry", "plan", "runs", "idle"))
LANE_KEYS = frozenset(("runner", "python", "kind"))
NOT_CI_KEYS = frozenset(("except", "match"))
SHA = re.compile(r"[0-9a-f]{40}")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def repo_path(value: object) -> bool:
    """A path as `git ls-files` prints it: relative, POSIX, no empty, "." or ".." segment, no control character."""
    return (isinstance(value, str) and 0 < len(value.encode("utf-8", "surrogatepass")) <= MAX_PATH
            and not CONTROL.search(value) and not value.startswith("/")
            and all(part not in ("", ".", "..") for part in value.split("/")))


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
    judge_workflow: str
    aggregate: str
    jobs: dict[str, dict[str, str]]
    quarantine: Quarantine
    test_jobs: frozenset[str]
    lanes: dict[str, Lane]
    required_contexts: frozenset[str]
    focused_job: str  # "" when the policy has none
    gate_checks: dict[str, str]  # role (coverage, sonar) -> the check's name; {} when the policy has no merge-gate checks
    not_ci: tuple[str, ...]
    ci_except: frozenset[str]

    def expected_jobs(self, event: str) -> dict[str, str]:
        """The complete candidate inventory of `event`: job name -> its required conclusion."""
        require(event in self.jobs, "unsupported candidate event")
        return dict(self.jobs[event])

    @property
    def workflow_file(self) -> str:
        """The candidate workflow's file name, as `gh run list --workflow` and the Actions API name it."""
        return self.workflow.rsplit("/", 1)[-1]

    @property
    def judge_file(self) -> str:
        """The merge-gate workflow's file name; "" when the policy has none."""
        return self.judge_workflow.rsplit("/", 1)[-1]

    @property
    def api_checks(self) -> frozenset[str]:
        """The required contexts the merge-gate workflow posts through the API: all but the CI aggregate."""
        return self.required_contexts - {self.aggregate}

    @property
    def workflow_runs(self) -> str:
        """The Actions API path of the runs of the candidate workflow (by its file name, as the API names it)."""
        return f"actions/workflows/{self.workflow.rsplit('/', 1)[-1]}/runs"

    @property
    def pinned(self) -> tuple[str, str]:
        """The files a candidate's own proof names: the workflow and the recorder."""
        return (self.workflow, self.recorder)

    def focused_job_name(self, lane: str) -> str:
        """The PR job name of focused `lane` (Core's template, e.g. "Focused tests ({lane})")."""
        return self.focused_job.replace("{lane}", lane)

    def is_not_ci(self, path: str) -> bool:
        """True when Core classifies the repo-relative POSIX `path` as definitely not CI: a match glob fits it
        (fnmatchcase: `*` crosses `/`) and it is not one of the literal except paths."""
        return path not in self.ci_except and any(fnmatch.fnmatchcase(path, glob) for glob in self.not_ci)

    def lane_kind(self, lane: str) -> str | None:
        found = self.lanes.get(lane)
        return found.kind if found else None


def _names(value: object, what: str, *, empty: bool = False) -> list[str]:
    require(
        isinstance(value, list) and (empty or value) and all(isinstance(v, str) and v.strip() == v and v for v in value)
        and len(set(value)) == len(value),
        f"release policy {what} is not a list of unique names",
    )
    assert isinstance(value, list)
    return [str(v) for v in value]


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
    require(set(data) <= KEYS and KEYS - OPTIONAL_KEYS <= set(data), f"release policy keys differ: {sorted(set(data) ^ KEYS)}")
    repository = _text(data["repository"], "repository")
    require(REPOSITORY.fullmatch(repository), "release policy repository malformed")
    workflow, recorder = _text(data["workflow"], "workflow"), _text(data["recorder"], "recorder")
    require(
        repo_path(workflow) and repo_path(recorder) and workflow != recorder
        and workflow.startswith(".github/workflows/"),
        "release policy pinned paths malformed",
    )
    judge = _text(data["judge_workflow"], "judge workflow") if "judge_workflow" in data else ""
    require(not judge or (repo_path(judge) and judge.startswith(".github/workflows/") and judge not in (workflow, recorder)),
            "release policy judge workflow malformed")
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
    lanes_raw = data.get("lanes", {})
    require(isinstance(lanes_raw, dict) and ("lanes" not in data or lanes_raw), "release policy lanes malformed")
    lanes = {}
    for name, lane in lanes_raw.items():
        require(isinstance(name, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]*", name), "release policy lane name malformed")
        require(isinstance(lane, dict) and set(lane) == LANE_KEYS, f"release policy lane {name} malformed")
        lanes[name] = Lane(_text(lane["runner"], "lane runner"), _text(lane["python"], "lane python"),
                           _text(lane["kind"], "lane kind"))
    contexts = frozenset(_names(data["required_contexts"], "required contexts"))
    focused = ""
    if "focused_job" in data:
        focused = _text(data["focused_job"], "focused job")
        rest = focused.replace("{lane}", "")
        require(focused.count("{lane}") == 1 and "{" not in rest and "}" not in rest,
                "release policy focused job is not a {lane} template")
    gates: dict[str, str] = {}
    if "gate_checks" in data:
        gates = data["gate_checks"]
        require(isinstance(gates, dict) and set(gates) == GATE_ROLES and all(isinstance(v, str) and v for v in gates.values())
                and len(set(gates.values())) == len(gates) and set(gates.values()) == contexts - {aggregate},
                "release policy gate_checks do not name the merge-gate workflow's checks")
    else:  # no merge-gate checks: the aggregate is the one required context
        require(aggregate in contexts, "release policy required contexts do not name the aggregate")
    raw_not_ci = data["not_ci"]
    require(isinstance(raw_not_ci, dict) and set(raw_not_ci) == NOT_CI_KEYS, "release policy not_ci malformed")
    not_ci = tuple(_names(raw_not_ci["match"], "not-CI globs"))
    require(all(not g.startswith("/") and not re.search(r"(?:^|/)\.\.(?:/|$)", g) for g in not_ci),
            "release policy not-CI glob is not repository-relative")
    ci_except = frozenset(_names(raw_not_ci["except"], "not-CI exceptions", empty=True))
    require(all(repo_path(p) and not set(p) & set("[*?") for p in ci_except),
            "release policy not-CI exception is not a literal repository path")
    return Policy(commit, repository, workflow, recorder, judge, aggregate, jobs, quarantine, test_jobs, lanes, contexts,
                  focused, dict(gates), not_ci, ci_except)


def at_commit_api(get: Callable[[str], Any], commit: str) -> Policy:
    """The manifest of `commit`, through the contents API at that exact SHA (the candidate readers' transport)."""
    require(isinstance(commit, str) and SHA.fullmatch(commit), "release policy needs an exact Core commit")
    raw = contents_bytes(get, PATH, commit, f"{PATH} missing at {commit}")
    require(raw is not None, f"{PATH} missing at {commit}")
    return parse(raw, commit)  # type: ignore[arg-type]


def _git_env() -> dict[str, str]:
    """The environment of every git read here: no inherited GIT_* variable may redirect it to another repository."""
    return child_environment(drop_prefixes=("GIT_",))


def git_read(repo: str | Path, *args: str, stdin: bytes | None = None) -> bytes:
    """Run one read-only git command in the Core clone, GIT_* stripped; a failure refuses (CandidateError)."""
    git = shutil.which("git")
    require(git, "git is not installed")
    done = subprocess.run([str(git), "-C", str(repo), *args], input=stdin, capture_output=True,
                          check=False, timeout=120, env=_git_env())
    require(done.returncode == 0, f"git {args[0]} failed in the Core clone")
    return done.stdout


def blobs_at(repo: str | Path, commit: str, paths: set[str]) -> dict[str, bytes]:
    """The bytes of `paths` at `commit` from git objects in two processes (ls-tree, cat-file --batch); a symlink is
    its link text, as `git show` prints it. A path that is absent or not a blob (a gitlink) refuses."""
    listing = git_read(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    ids: dict[str, str] = {}
    for row in listing.split(b"\0"):
        if not row:
            continue
        meta, _, name = row.partition(b"\t")
        mode, kind, oid = meta.decode().split(" ")
        path = name.decode("utf-8", "surrogateescape")
        if path in paths:
            require(kind == "blob" and mode in ("100644", "100755", "120000"), f"{path} at {commit} is not a file")
            ids[path] = oid
    missing = sorted(paths - set(ids))
    require(not missing, f"{missing[0] if missing else ''} missing at {commit}")
    order = sorted(ids)
    out = git_read(repo, "cat-file", "--batch", stdin="".join(f"{ids[p]}\n" for p in order).encode())
    found, at = {}, 0
    for path in order:
        end = out.index(b"\n", at)
        oid, kind, size = out[at:end].decode().split(" ")
        require(oid == ids[path] and kind == "blob", f"{path} at {commit} unreadable")
        start = end + 1
        found[path] = out[start:start + int(size)]
        at = start + int(size) + 1
    return found


def at_commit_git(repo: str | Path, commit: str) -> Policy:
    """The manifest of `commit` from the git objects of a local Core clone, never its working tree."""
    require(isinstance(commit, str) and SHA.fullmatch(commit), "release policy needs an exact Core commit")
    return parse(blobs_at(repo, commit, {PATH})[PATH], commit)


def at_tree_git(repo: str | Path, treeish: str) -> Policy:
    """The manifest of a commit or tree named by `treeish`, resolved to its exact tree id first (a packet's trees)."""
    git = shutil.which("git")
    require(git, "git is not installed")
    require(isinstance(treeish, str) and re.fullmatch(r"[0-9a-f]{4,40}", treeish), "invalid Core tree")
    done = subprocess.run([str(git), "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{treeish}^{{tree}}"],
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


SPECIAL_MODES = frozenset(("120000", "160000"))  # a symlink or a gitlink: never "not CI", whatever its path


def ci_changes(repo: str | Path, base: str, head: str) -> tuple[str, ...]:
    """Why the change from `base` to `head` touches Core's CI: one line per reason, empty when it does not.

    The not-CI paths are those of the manifest at BASE (`git show BASE:scripts/release_policy.json`), never the head's:
    a change cannot declare itself harmless, and the base must be an ancestor of the head (a sibling's wider list never
    applies). Diff: `git diff --raw --no-renames -z BASE HEAD` in the Core clone, GIT_*
    variables stripped; a rename is a delete and an add. Fail closed, never raising: an unreadable diff, no manifest at
    base, an empty or garbled diff, a non-UTF-8 path, a symlink or gitlink mode on either side and every path the base
    does not list as not CI are each a reason.
    """
    if not (isinstance(base, str) and SHA.fullmatch(base) and isinstance(head, str) and SHA.fullmatch(head)):
        return ("base and head must be exact commits",)
    try:
        git_read(repo, "merge-base", "--is-ancestor", base, head)
    except (CandidateError, OSError, ValueError, subprocess.SubprocessError):
        return (f"base {base[:12]} is not an ancestor of head {head[:12]} (or either is missing in the clone)",)
    try:
        policy = at_commit_git(repo, base)
    except (CandidateError, OSError, ValueError, subprocess.SubprocessError) as exc:
        return (f"no reviewed release policy at base {base}: {exc}",)
    try:
        raw = git_read(repo, "diff", "--raw", "--no-renames", "--no-ext-diff", "--no-textconv", "-z", base, head, "--")
    except (CandidateError, OSError, ValueError, subprocess.SubprocessError) as exc:
        return (f"diff {base[:12]}..{head[:12]} unreadable: {exc}",)
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if not fields or len(fields) % 2:
        return (f"diff {base[:12]}..{head[:12]} is empty or garbled",)
    reasons = []
    for meta, name in zip(fields[::2], fields[1::2], strict=True):
        parts = meta.decode("ascii", "replace").split(" ")
        try:
            path = name.decode("utf-8")
        except UnicodeDecodeError:
            reasons.append(f"{name!r}: not a UTF-8 path")
            continue
        if len(parts) != 5 or not parts[0].startswith(":"):
            reasons.append(f"{path}: garbled diff entry")
        elif SPECIAL_MODES & {parts[0][1:], parts[1]}:
            reasons.append(f"{path}: symlink or submodule")
        elif not policy.is_not_ci(path):
            reasons.append(f"{path}: CI path (not listed as not CI at base {base[:12]})")
    return tuple(reasons)


def ci_changed(repo: str | Path, base: str, head: str) -> bool:
    """True when the change from `base` to `head` touches Core's CI (see ci_changes; any doubt is True)."""
    return bool(ci_changes(repo, base, head))
