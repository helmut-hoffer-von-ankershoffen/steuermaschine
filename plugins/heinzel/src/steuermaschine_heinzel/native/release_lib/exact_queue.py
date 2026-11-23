"""An exact release candidate, reviewed before the queue may merge it or the runner may tag it."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from steuermaschine import go, verdicts
from steuermaschine.hashing import canonical_sha256
from steuermaschine.native import runtime

from ... import state as statedir
from ..queue_lib import core_facts, hzlib, main_lanes
from . import candidate_policy

MODE = "reviewed-release-queue"
KIND = "exact-release-candidate"
SHA = re.compile(r"[a-f0-9]{40}")
TOKEN = re.compile(r"[a-f0-9]{32}")
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


class ExactReleaseError(ValueError):
    """Proof or ownership is absent. Nothing may be merged or tagged."""


@dataclass(frozen=True)
class Candidate:
    version: str
    token: str
    pr: int
    head: str
    base: str
    tree: str
    run: int

    def __post_init__(self) -> None:
        if (not VERSION.fullmatch(self.version) or not TOKEN.fullmatch(self.token)
                or type(self.pr) is not int or self.pr <= 0 or type(self.run) is not int or self.run <= 0
                or any(not SHA.fullmatch(value) for value in (self.head, self.base, self.tree))):
            raise ExactReleaseError("candidate receipt does not have exact release identities")

    @property
    def digest(self) -> str:
        return canonical_sha256(json.dumps({"kind": KIND, "repository": hzlib.CORE, **asdict(self)}, sort_keys=True))

    @property
    def tag(self) -> str:
        return f"v{self.version}"

    @property
    def subject(self) -> str:
        return f"chore(release): {self.tag}"

    def review_command(self) -> str:
        return f"sm review verdict {self.digest} --approve --head {self.head} --reviewer NAME --model MODEL"


def store() -> Any:
    return runtime().providers.store


def active_receipt() -> dict:
    try:
        raw = json.loads(statedir.path("release.json").read_text())
    except (ValueError, OSError) as error:
        raise ExactReleaseError("cannot read the active Core release receipt") from error
    if not isinstance(raw, dict):
        raise ExactReleaseError("active Core release receipt is invalid")
    return raw


def require_hold(candidate: Candidate) -> None:
    data = active_receipt()
    events = data.get("hold_events", [])
    if (data.get("version"), data.get("freeze_token"), data.get("merge_route")) != (candidate.version, candidate.token, MODE):
        raise ExactReleaseError("the candidate does not own this Core release")
    if (data.get("finished") or not isinstance(events, list) or
            (events and (not isinstance(events[-1], dict) or events[-1].get("action") in {"pause", "pause-request"}))):
        raise ExactReleaseError("the Core release is paused or finished")
    root = hzlib.release_holds()
    try:
        hold = root / candidate.token
        if (hzlib.incident_frozen() or root.is_symlink() or list(root.iterdir()) != [hold]
                or hold.is_symlink() or not hold.is_file() or hold.read_text() != candidate.version):
            raise ExactReleaseError("the candidate needs its own sole release hold and no incident freeze")
    except OSError as error:
        raise ExactReleaseError("cannot verify the ownership of the release freeze") from error


def read_candidate(version: str) -> Candidate | None:
    raw = store().get_receipt(KIND, version)
    if raw is None:
        return None
    try:
        candidate = Candidate(**raw)
    except (TypeError, ValueError) as error:
        raise ExactReleaseError("malformed exact release candidate receipt") from error
    if candidate.version != version:
        raise ExactReleaseError("release candidate receipt belongs to a different version")
    return candidate


def approve_required(candidate: Candidate) -> None:
    records = store()
    if not verdicts.authors_of(records, candidate.digest, []):
        raise ExactReleaseError("candidate review authorship was never registered")
    receipt = verdicts.require_independent(records, candidate.digest, go.run_states(records),
                                           candidate.review_command(), head=candidate.head)
    if not isinstance(receipt.get("model"), str) or not receipt["model"].strip():
        raise ExactReleaseError("the independent candidate review must record its model")


def _commit_field(commit: dict, field: str) -> Any:
    inner = commit.get("commit")
    return inner.get(field) if isinstance(inner, dict) else None


def _parents(commit: dict) -> list:
    raw = commit.get("parents")
    return [parent.get("sha") if isinstance(parent, dict) else None for parent in raw] if isinstance(raw, list) else []


def _tree(commit: dict) -> str:
    tree = _commit_field(commit, "tree")
    return tree.get("sha", "") if isinstance(tree, dict) else ""


def _repo_ref(value: Any, ref: str) -> bool:
    return (isinstance(value, dict) and value.get("ref") == ref
            and isinstance(value.get("repo"), dict) and value["repo"].get("full_name") == hzlib.CORE)


def _pr_head(candidate: Candidate, pr: dict) -> bool:
    head = pr.get("head")
    return (type(pr.get("number")) is int and pr["number"] == candidate.pr and _repo_ref(pr.get("base"), "main")
            and _repo_ref(head, f"release/{candidate.tag}") and head.get("sha") == candidate.head
            and pr.get("title") == f"chore(release): {candidate.tag}")


def _api(path: str) -> dict:
    result = hzlib.ghj("api", f"repos/{hzlib.CORE}/{path}", default=None)
    if not isinstance(result, dict):
        raise ExactReleaseError(f"cannot read release evidence {path}")
    return result


def base_ready(expected: str | None = None) -> bool:
    """Both original main CI lanes must prove the same current Core commit."""
    tip = _api("git/ref/heads/main").get("object")
    sha = tip.get("sha") if isinstance(tip, dict) and tip.get("type") == "commit" else None
    if not isinstance(sha, str) or not SHA.fullmatch(sha) or (expected is not None and expected != sha):
        return False
    rows = hzlib.ghj("run", "list", "-R", hzlib.CORE, "--workflow", core_facts.ci_file(), "--branch", "main",
                     "--commit", sha, "-L", "10", "--json", "event,displayTitle,headSha,status,conclusion,attempt", default=None)
    if not isinstance(rows, list) or not all(isinstance(row, dict) and row.get("headSha") == sha for row in rows):
        return False
    push = next((row for row in rows if row.get("event") == "push"), {})
    mac = next((row for row in rows if row.get("event") == "workflow_dispatch" and main_lanes.is_macos_lane(row)), {})
    return all((row.get("status"), row.get("conclusion"), row.get("attempt")) == ("completed", "success", 1)
               for row in (push, mac))


def verify_open(candidate: Candidate, evidence: dict | None = None) -> None:
    require_hold(candidate)
    if not base_ready(candidate.base):
        raise ExactReleaseError("both original main CI lanes must pass on the reviewed base")
    data = evidence if evidence is not None else candidate_policy.verify_pr(candidate.pr)
    if (data["head"], data["base"], data["tree"], data["run_id"], data["run_attempt"]) != (
            candidate.head, candidate.base, candidate.tree, candidate.run, 1):
        raise ExactReleaseError("first-attempt candidate proof no longer matches the reviewed release")
    pr = _api(f"pulls/{candidate.pr}")
    head = _api(f"commits/{candidate.head}")
    tip = _api("git/ref/heads/main").get("object")
    if (pr.get("state") != "open" or pr.get("draft") is not False or not _pr_head(candidate, pr)
            or head.get("sha") != candidate.head or _parents(head) != [candidate.base] or _tree(head) != candidate.tree
            or not isinstance(tip, dict) or tip.get("type") != "commit" or tip.get("sha") != candidate.base):
        raise ExactReleaseError("release PR head, sole parent, tested tree or current main moved")


def prepare(version: str, token: str, pr: int) -> Candidate:
    records = store()
    with records.lock(f"{KIND}.{version}"):
        if previous := read_candidate(version):
            if (previous.token, previous.pr) != (token, pr):
                raise ExactReleaseError("the preserved review belongs to another release hold or PR")
            verify_open(previous)
            return previous
        data = candidate_policy.verify_pr(pr)
        candidate = Candidate(version, token, pr, data["head"], data["base"], data["tree"], data["run_id"])
        verify_open(candidate, data)
        verdicts.register(records, candidate.digest, KIND)
        records.put_receipt(KIND, version, asdict(candidate))
        return candidate


def reviewed_freeze_active() -> bool:
    if not hzlib.release_holds_present():
        return False
    try:
        return active_receipt().get("merge_route") == MODE
    except ExactReleaseError:
        return True


def queue_candidate(item: dict) -> Candidate | None:
    """Only the queued, independently reviewed head of the active exact release can cross its own hold."""
    if item.get("repo") != hzlib.CORE or not item.get("reviewed_head") or not hzlib.release_holds_present():
        return None
    try:
        data = active_receipt()
    except ExactReleaseError:
        # Other release modes still obey their own freeze; missing state can never open this exception.
        return None
    version = data.get("version")
    if data.get("merge_route") != MODE or not isinstance(version, str) or not VERSION.fullmatch(version):
        return None
    candidate = read_candidate(version)
    if not candidate or (item.get("pr"), item.get("reviewed_head")) != (str(candidate.pr), candidate.head):
        return None
    require_hold(candidate)
    approve_required(candidate)
    return candidate


def merged_commit(candidate: Candidate) -> str:
    pr = _api(f"pulls/{candidate.pr}")
    sha = pr.get("merge_commit_sha")
    if (pr.get("state") != "closed" or pr.get("merged") is not True or not _pr_head(candidate, pr)
            or not isinstance(sha, str) or not SHA.fullmatch(sha)):
        raise ExactReleaseError("the exact reviewed release PR has not merged to main")
    commit = _api(f"commits/{sha}")
    message = _commit_field(commit, "message")
    if (commit.get("sha") != sha or _parents(commit) != [candidate.base] or _tree(commit) != candidate.tree
            or not isinstance(message, str) or message.splitlines()[:1] != [candidate.subject]):
        raise ExactReleaseError("merged release source differs from the reviewed base, tested tree or canonical subject")
    if _api(f"compare/{sha}...main").get("status") not in ("ahead", "identical"):
        raise ExactReleaseError("merged release source cannot be proven to be on main")
    return sha
