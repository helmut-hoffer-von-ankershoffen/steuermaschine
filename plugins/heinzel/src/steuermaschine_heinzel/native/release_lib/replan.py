"""A narrowly scoped local replan before any release external work; never reset a candidate."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from steuermaschine.engine import Refuse, stamp
from steuermaschine.hashing import canonical_sha256

from ... import state as statedir
from ..admission_lib import focused_admit, focused_slots
from ..release_admit_lib import release_slots

KIND = "runs.release.cut"
ARCHIVE = "runs.release.cut.replanned"
SHA = re.compile(r"[0-9a-f]{40}")
EARLY = {"ci change", "notes", "packet", "admit prepare"}
STATE_KEYS = {"schema", "project", "pipeline", "key", "label", "arg", "lane", "author", "inputs", "data", "steps",
              "fingerprint", "created", "updated", "status", "phase", "next", "waiting_on", "waiting_since", "trap", "review"}


def require(ok: bool, message: str) -> None:
    if not ok:
        raise Refuse(f"release replan refused: {message}")


def digest(value: Any) -> str:
    return canonical_sha256(json.dumps(value))


def document(path: Path) -> dict[str, Any]:
    require(not path.is_symlink(), f"symlink evidence: {path}")
    value = json.loads(path.read_bytes())
    require(isinstance(value, dict), f"malformed evidence: {path}")
    return dict(value)


def local_state(state: dict[str, Any], version: str) -> None:
    require(set(state) <= STATE_KEYS, "unknown or running cut bookkeeping")
    require(state.get("schema") == 1 and state.get("pipeline") == "release.cut" and state.get("key") == version,
            "unknown cut state identity")
    require(state.get("status") in ("refused", "waiting", "review", "stopped"), "cut is not a stopped pre-external run")
    steps, data, inputs = state.get("steps"), state.get("data"), state.get("inputs")
    if not isinstance(steps, dict) or not isinstance(data, dict) or not isinstance(inputs, dict):
        raise Refuse("release replan refused: unknown cut state shape")
    require(set(steps) <= EARLY, "admission review or external/publishing step already started")
    require(all(isinstance(v, dict) and v.get("status") in ("done", "refused", "waiting") for v in steps.values()),
            "unknown or running step state")
    require(not any(data.get(k) for k in ("admission_sha256", "receipt", "publish_sha256", "publish_plan")),
            "admission/publish approval or receipt already recorded")
    require(set(data) <= {"ci_change_head", "ci_change_detail", "main_ci"}, "unknown cut data; inspect before replanning")
    require(data.get("main_ci") == "focused", "only a stopped focused cut can be replanned to full")
    require(not state.get("review"), "a pending review must not be erased")
    require(state.get("arg") == version and isinstance(state.get("author"), str) and bool(state["author"]),
            "missing original cut identity")


def local_evidence(root: Path, version: str, head: str) -> None:
    """Any uncertain admission attempt is external work, even if it never wrote a receipt."""
    for directory, pattern in (("focused-admission-receipts", f"head-{head}-"),
                               ("focused-release-admission-attempts", f"v{version}-")):
        parent = root / directory
        require(not parent.is_symlink(), f"unsafe {directory}")
        try:
            children = list(parent.iterdir())
        except FileNotFoundError:
            children = []
        require(not any(p.name.startswith(pattern) for p in children), f"existing {directory} evidence")
    tail = root / "release" / f"v{version}.json"
    require(not tail.parent.is_symlink(), "unsafe release receipt directory")
    require(not tail.exists() and not tail.is_symlink(), "release tail receipt exists")
    for name in ("release.json", "release-waiver.json"):
        path = root / name
        if path.exists() or path.is_symlink():
            value = document(path)
            prior = value.get("version")
            require(isinstance(prior, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", prior) is not None,
                    f"unknown version in {name}")
            require(prior != version, f"same-version {name} exists")


def git(rt: Any, clone: str, *args: str) -> str:
    result = rt.providers.commands.run(["git", "-C", clone, *args], timeout=60)
    require(result.rc == 0, "cannot verify release clone")
    return str(result.out).strip()


def binding(rt: Any, state: dict[str, Any], head: str, packet: Path) -> dict[str, Any]:
    from ...pipelines.release_cut import official_origin

    clone = state["inputs"].get("clone")
    require(isinstance(clone, str) and Path(clone).is_absolute(), "missing release clone")
    repo = rt.settings.get("repo")
    official_origin(clone, str(repo))
    base = state.get("fingerprint", {}).get("main")
    require(isinstance(base, str) and SHA.fullmatch(base) is not None, "missing protected base")
    require(git(rt, clone, "rev-parse", "HEAD") == head, "release head moved")
    require(git(rt, clone, "rev-list", "--parents", "-n", "1", head).split() == [head, base], "release parent changed")
    require(not git(rt, clone, "status", "--porcelain"), "release clone is dirty")
    require(rt.providers.vcs.remote_head(clone, "main") == base, "protected main moved or is unknown")
    tree = git(rt, clone, "rev-parse", f"{head}^{{tree}}")
    require(SHA.fullmatch(tree) is not None, "invalid release tree")
    old_packet = Path(str(state["inputs"].get("packet", ""))).resolve()
    require(packet.is_absolute() and packet.resolve().parent != old_packet.parent, "new packet needs a separate output directory")
    for name in ("release-reservation-packet.json", "prepare.out", "prepare.args.json", "full-ci-plan.json"):
        path = packet.parent / name
        require(not path.exists() and not path.is_symlink(), "new packet directory already contains cut artifacts")
    pair = document(packet)
    plan = pair.get("main_manifest")
    require(isinstance(plan, dict) and plan.get("base") == base and plan.get("tree") == tree and plan.get("event") == "push",
            "new pair packet does not bind the release base/tree")
    old_pair = document(old_packet)
    require(pair == old_pair, "new pair packet differs from the original input; review new evidence separately")
    require(state["data"].get("ci_change_head") == head, "original cut source review does not name this release head")
    return {"head": head, "base": base, "tree": tree, "clone": clone, "repository": repo,
            "packet": str(packet), "packet_sha256": hashlib.sha256(packet.read_bytes()).hexdigest(),
            "main_ci": "full", "state_sha256": digest(state)}


def remote_evidence(rt: Any, module: Any, version: str, proof: dict[str, Any]) -> None:
    require(proof["repository"] == module.CORE, "runner repository differs from reviewed source")
    identity = module.drop_identity(version)  # typed absence of tag, GitHub release, PyPI; no mutations
    require(identity == {"open": [], "closed": [], "branch": False}, "release branch or PR has existed")
    # Include completed failures and dispatches on another branch: they must not be erased by a replan.
    for query in (f"branch=release/v{version}", f"head_sha={proof['head']}"):
        result = rt.providers.commands.run(["gh", "api", f"repos/{proof['repository']}/actions/runs?{query}&per_page=1"], timeout=60)
        require(result.rc == 0, "cannot establish candidate run absence")
        page = json.loads(result.out)
        require(isinstance(page, dict) and type(page.get("total_count")) is int
                and page["total_count"] == 0 and page.get("workflow_runs") == [], "candidate runs exist or inventory is unknown")
    admission = cast(Callable[..., Any], release_slots.Runtime)(proof["clone"])
    require(admission.repo == proof["repository"], "admission repository mismatch")
    registry = cast(Callable[..., str], focused_slots.registry_text)(admission.registry())
    require(proof["head"] not in registry and proof["tree"] not in registry, "release already has a registry reservation")


def execute(rt: Any, module: Any, version: str, head: str, packet: Path, reason: str, live: bool) -> dict[str, Any]:
    """Caller holds the cut lock. Hold release and registry exclusion until the new state is saved."""
    require(bool(reason.strip()), "a reason is required")
    require(SHA.fullmatch(head) is not None, "head must be an exact commit")
    store = rt.providers.store
    state = store.load(KIND, version)
    require(isinstance(state, dict), "no existing cut to replan")
    local_state(state, version)
    module.acquire_global_lock()
    try:
        with focused_admit._lock(statedir.path("focused-pr-registry.lock")):
            proof = binding(rt, state, head, packet)
            local_evidence(statedir.root(), version, head)
            remote_evidence(rt, module, version, proof)
            require(binding(rt, state, head, packet) == proof, "release binding changed during remote probes")
            local_evidence(statedir.root(), version, head)
            require(store.load(KIND, version) == state, "cut changed while proving replan")
            proof.update(version=version, reason=reason, live=live, archive="")
            if live:
                key = f"{version}-{uuid.uuid4().hex}"
                archive = {"schema": 1, "original": copy.deepcopy(state), "proof": copy.deepcopy(proof),
                           "at": rt.providers.clock.now()}
                store.save(ARCHIVE, key, archive)
                require(store.load(ARCHIVE, key) == archive, "archive readback failed; active cut preserved")
                fresh = {name: copy.deepcopy(state[name]) for name in
                         ("schema", "project", "pipeline", "key", "label", "arg", "lane", "author", "inputs", "fingerprint")}
                fresh.update(steps={}, data={}, review={}, status="replanned", phase="full route requires fresh reviews",
                             next=f"sm release cut {version}", waiting_on="", replan_archive=f"{ARCHIVE}/{key}",
                             created=stamp(rt.providers.clock.now()), updated=stamp(rt.providers.clock.now()))
                fresh["inputs"].update(main_ci="full", packet=str(packet))
                # One atomic store replacement under the pipeline lock; all artifacts and receipts stay untouched.
                store.save(KIND, version, fresh)
                proof["archive"] = f"{ARCHIVE}/{key}"
            return {name: proof[name] for name in
                    ("version", "head", "base", "tree", "main_ci", "state_sha256", "packet_sha256", "live", "archive")}
    finally:
        module.free_global_lock()
