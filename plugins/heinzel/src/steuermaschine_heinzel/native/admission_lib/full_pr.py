"""Two-review, append-only authorization of reviewed full-PR Makefile inputs.

This is source authorization, never CI evidence. Only deployed Core constructs and
validates the entry. Existing capacity, queue exclusion and registry locks still apply.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from steuermaschine.defaults import Subprocess

from ..release_admit_lib import focused_release as _focused_release
from ..release_admit_lib import release_ops
from ..release_admit_lib.release_slots import Runtime as _Runtime
from . import focused_admit as a
from . import focused_slots as _slots
from . import focused_update as _update
from . import ops

# Typed boundary into the existing moved admission implementation.
Runtime: Any = _Runtime
slots: Any = _slots
update: Any = _update
focused_release: Any = _focused_release
MODE = "full-pr-reviewed/v1"
MAX_ARTIFACT = 1024 * 1024


def producer_identity() -> dict[str, str]:
    here = Path(__file__).parent
    paths = sorted(here.glob("*.py")) + sorted((here.parent / "release_admit_lib").glob("*.py"))
    paths += [here.parent / name for name in ("full_pr.py", "admission.py", "release_window.py")]
    return {str(path.relative_to(here.parent)): slots.digest(a._stable_file(path, a.MAX_REVIEW_BYTES)) for path in paths}


def immutable(path: Path, value: Any) -> None:
    raw = a.canonical(value)
    slots.require(len(raw) <= MAX_ARTIFACT, "review artifact exceeds bound")
    path = Path(path)
    if path.exists() or path.is_symlink():
        slots.require(a._stable_file(path, MAX_ARTIFACT) == raw, "existing review artifact differs; choose a new output directory")
        return
    with path.open("xb") as stream:
        path.chmod(0o600)
        stream.write(raw)


def read(path: Path) -> dict[str, Any]:
    return a._json_bytes(a._stable_file(Path(path), MAX_ARTIFACT), "full PR artifact")


def local_identity(rt: Any) -> tuple[str, str, str, int]:
    update._official_origin(rt.core, rt.repo)
    update._plain_history(rt.core)
    slots.require(not a._run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=rt.core), "source checkout is not clean")
    branch = a._run(["git", "branch", "--show-current"], cwd=rt.core).decode().strip()
    slots.require(branch and branch != "main" and not branch.startswith(("release/", "bump/")), "full PR requires a named feature branch")
    head = a._run(["git", "rev-parse", "HEAD"], cwd=rt.core).decode().strip()
    repository = rt.api(f"repos/{rt.repo}")
    slots.require(repository.get("full_name") == rt.repo and repository.get("fork") is False and repository.get("default_branch") == "main", "canonical repository identity differs")
    repository_id = repository.get("id")
    slots.require(type(repository_id) is int and repository_id > 0, "repository ID unavailable")
    base = rt.api(f"repos/{rt.repo}/git/ref/heads/main")["object"]["sha"]
    protected = rt.api(f"repos/{rt.repo}/branches/main")
    slots.require(protected.get("protected") is True and protected.get("commit", {}).get("sha") == base, "protected main identity differs")
    a._sha(head, "head")
    a._sha(base, "base")
    slots.require(head != base, "source must differ from main")
    a._run(["git", "merge-base", "--is-ancestor", base, head], cwd=rt.core)
    slots.check_preopen(rt, {"head": head})
    return head, base, branch, repository_id


def trusted_entry(source: Path, snapshot: Path, head: str, base: str) -> dict[str, Any]:
    """Run only deployed base scripts; PR source is Git data, never imported."""
    tree = a._run(["git", "merge-tree", "--write-tree", base, head], cwd=snapshot).decode().splitlines()[0]
    a._sha(tree, "predicted merge tree")
    commit = (
        a._run(["git", "-c", "user.name=Admission", "-c", "user.email=admission@example.invalid", "commit-tree", tree, "-p", base, "-p", head, "-m", "full PR source prediction"], cwd=snapshot)
        .decode()
        .strip()
    )
    code = r"""
import json, os, sys, types
sys.path.insert(0, sys.argv[1])
pkg = types.ModuleType("scripts"); pkg.__path__ = [os.path.join(sys.argv[1], "scripts")]; sys.modules["scripts"] = pkg
from scripts import focused_gate as fg
head, base, commit, tree = sys.argv[2:]
if (getattr(fg, "FULL_PR_KIND", None) != "full-pr-reviewed" or not callable(getattr(fg, "full_pr_inputs", None))
    or not callable(getattr(fg, "selected_pr_registry", None))):
    raise ValueError("protected verifier lacks reviewed full PR support")
entry = dict(schema=1, kind=fg.FULL_PR_KIND, repository=fg.REPOSITORY, head=head, base=base, tree=tree,
             changed=fg.changed_paths(fg.raw_diff(base, commit)),
             execution={path:fg.git_blob(commit,path) for path in fg.FULL_EXECUTION_INPUTS})
fg.full_pr_inputs(types.SimpleNamespace(commit=commit, source=types.SimpleNamespace(head=head)), base, entry)
print(json.dumps(entry, sort_keys=True))
"""
    rc, output = Subprocess().run_bytes([sys.executable, "-I", "-c", code, str(source), head, base, commit, tree], cwd=str(snapshot), env={"PATH": os.defpath, "HOME": str(source)}, timeout=90)
    slots.require(rc == 0, "deployed protected full PR verifier refused; no authorization prepared")
    return a._json_bytes(output, "protected full PR entry")


@contextmanager
def source_packet(rt: Any) -> Iterator[tuple[dict[str, Any], Path]]:
    slots.require(release_ops.isolated_startup(), "full PR admission requires isolated imports")
    focused_release.verify_account(rt)
    head, base, branch, repository_id = local_identity(rt)
    workspace = source = None
    try:
        workspace, snapshot = a._prepare_core_snapshot(rt.core, base)
        source, _, repository, variable, _ = a._canonical_source(snapshot, base)
        slots.require(repository == rt.repo and variable == slots.NAMES[0], "protected repository or transport differs")
        entry = trusted_entry(source, snapshot, head, base)
        slots.require(entry["repository"] == rt.repo and entry["head"] == head and entry["base"] == base, "protected entry identity differs")
        verifier = a._run(["git", "rev-parse", f"{base}:scripts/focused_gate.py"], cwd=snapshot).decode().strip()
        packet = dict(schema=1, mode=MODE, core_repo=str(rt.core), branch=branch, repository_id=repository_id, entry=entry, verifier_blob=verifier, producer=producer_identity())
        slots.require(local_identity(rt) == (head, base, branch, repository_id), "source changed during inspection")
        yield packet, source
    finally:
        for path in (source, workspace):
            if path is not None:
                shutil.rmtree(path, ignore_errors=True)


def registry(rt: Any) -> tuple[dict[str, str], list[str]]:
    values, org = rt.variables()
    slots.require(not set(slots.NAMES) & org.keys(), "organization transport shadowing")
    return {name: values.get(name, "") for name in slots.NAMES}, sorted(set(slots.NAMES) & values.keys())


def validate_registry(source: Path, values: dict[str, str], entry: dict[str, Any], *, present: bool) -> None:
    """Use deployed duplicate and bounded transport policy before/after the append."""
    code = r"""
import json, os, sys, types
sys.path.insert(0, sys.argv[1])
pkg = types.ModuleType("scripts"); pkg.__path__ = [os.path.join(sys.argv[1], "scripts")]; sys.modules["scripts"] = pkg
from scripts import focused_gate as fg
request = json.load(sys.stdin)
if not callable(getattr(fg, "selected_pr_registry", None)):
    raise ValueError("protected duplicate policy unavailable")
selected = json.loads(fg.selected_pr_registry(request["legacy"], tuple(request["slots"]), request["key"]))
if selected != ({request["key"]:request["entry"]} if request["present"] else {}):
    raise ValueError("registry entry differs from exact append")
"""
    request = dict(legacy=values[slots.NAMES[0]], slots=[values[name] for name in slots.NAMES[1:]], key="pull_request:" + entry["head"], entry=entry, present=present)
    rc, _ = Subprocess().run_bytes([sys.executable, "-I", "-c", code, str(source)], cwd=str(source), input=a.canonical(request), env={"PATH": os.defpath, "HOME": str(source)}, timeout=30)
    slots.require(rc == 0, "protected registry policy refused exact unique entry")


def proposal(rt: Any, packet: dict[str, Any], source: Path, nonconsumers: list[Any], review_root: Path) -> dict[str, Any]:
    expected, presence = registry(rt)
    entry = packet["entry"]
    validate_registry(source, expected, entry, present=False)
    append = a._isolated_slot_append(source, expected[slots.NAMES[0]], tuple(expected[n] for n in slots.NAMES[1:]), {"pull_request:" + entry["head"]: entry})
    slots.require(append["result"] is not None, "entry already exists; no write required")
    index, candidate = append["result"]
    slot = slots.NAMES[index + 1]
    desired = {**expected, slot: candidate}
    validate_registry(source, desired, entry, present=True)
    exceptions = slots.read_nonconsumers(rt, nonconsumers, review_root)
    slots.global_guard(rt, desired, exceptions, paused=False, full=False, probe=False)
    slots.require(registry(rt) == (expected, presence), "registry changed while preparing")
    return dict(
        schema=1,
        mode=MODE,
        source=packet,
        source_sha256=slots.digest(a.canonical(packet)),
        review_root=str(review_root.resolve()),
        nonconsumers=nonconsumers,
        expected=expected,
        presence=presence,
        desired=desired,
        slot=slot,
        candidate_sha256=slots.digest(candidate.encode()),
    )


def private_directory(out: Path, *, create: bool = False) -> Path:
    slots.require(not out.is_symlink(), "review directory must not be a symlink")
    if create:
        out.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = out.stat()
    slots.require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077, "review directory must be owner-only")
    return out.resolve()


def prepare(core: Path, out: Path, source_approved: str | None = None, nonconsumers: Path | None = None, review_root: Path | None = None, *, rt: Any = None) -> dict[str, Any]:
    rt = rt or Runtime(core, github_host="github.com")
    out = private_directory(Path(out), create=True)
    review_root = Path(review_root or out).resolve()
    receipts = ops._receipts(nonconsumers)
    with source_packet(rt) as (packet, source):
        digest = slots.digest(a.canonical(packet))
        immutable(out / "source.json", packet)
        if source_approved is None:
            return dict(stage="source-review", sha256=digest, path=str(out / "source.json"))
        slots.require(source_approved == digest, "source approval does not bind current exact source")
        prepared = proposal(rt, packet, source, receipts, review_root)
        immutable(out / "prepare.out", prepared)
        return dict(stage="apply-review", sha256=slots.digest(a.canonical(prepared)), path=str(out / "prepare.out"))


def journal_path(rt: Any, packet: dict[str, Any], suffix: str) -> Path:
    # Bind the authorized source, not caller-selected packet paths, receipts or
    # transport snapshots. A different transaction cannot replay this head.
    identity = dict(repository=rt.repo, repository_id=packet["repository_id"], head=packet["entry"]["head"])
    return Path(rt.cache) / "full-pr-admission-attempts" / (str(slots.digest(a.canonical(identity))) + "." + suffix + ".json")


def require_no_attempt(rt: Any, packet: dict[str, Any]) -> None:
    marker = journal_path(rt, packet, "attempt")
    for directory in (rt.cache, marker.parent):
        if directory.exists() or directory.is_symlink():
            info = directory.lstat()
            slots.require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022, "unsafe full PR journal directory")
    slots.require(not marker.exists() and not marker.is_symlink(), "source admission attempt already exists; inspect canonical journal, do not replay")


def apply(out: Path, approved: str, *, rt: Any = None) -> dict[str, Any]:
    out = private_directory(Path(out))
    prepared = read(out / "prepare.out")
    slots.require(prepared.get("mode") == MODE and slots.digest(a.canonical(prepared)) == approved, "approved transaction digest differs")
    packet = read(out / "source.json")
    slots.require(packet == prepared["source"] and slots.digest(a.canonical(packet)) == prepared["source_sha256"], "source review packet changed")
    rt = rt or Runtime(packet["core_repo"], github_host="github.com")
    slots.require(str(rt.core) == packet["core_repo"], "core checkout differs")
    slots.require(not (out / "attempt.json").exists() and not (out / "attempt.json").is_symlink(), "write attempt already exists; inspect readback, do not replay")
    require_no_attempt(rt, packet)
    with source_packet(rt) as (current, source):
        slots.require(current == packet, "reviewed source changed")
        recomputed = proposal(rt, packet, source, prepared["nonconsumers"], Path(prepared["review_root"]))
        slots.require(recomputed == prepared, "prepared transaction changed; new review required")
        exceptions = slots.read_nonconsumers(rt, prepared["nonconsumers"], Path(prepared["review_root"]))

        def fresh(*, probe: bool = False) -> None:
            head, base, branch, repository_id = local_identity(rt)
            slots.require(
                (head, base, branch, repository_id) == (packet["entry"]["head"], packet["entry"]["base"], packet["branch"], packet["repository_id"]) and producer_identity() == packet["producer"],
                "source or producer changed",
            )
            slots.global_guard(rt, prepared["desired"], exceptions, paused=True, full=probe, probe=probe)

        with rt.exclusions(), a._lock(rt.cache / "focused-pr-registry.lock"):
            require_no_attempt(rt, packet)
            fresh()
            slots.require(registry(rt) == (prepared["expected"], prepared["presence"]), "registry snapshot changed")
            validate_registry(source, prepared["expected"], packet["entry"], present=False)
            validate_registry(source, prepared["desired"], packet["entry"], present=True)
            candidate = a._isolated_slot_append(
                source, prepared["expected"][slots.NAMES[0]], tuple(prepared["expected"][n] for n in slots.NAMES[1:]), {"pull_request:" + packet["entry"]["head"]: packet["entry"]}
            )
            slots.require(candidate["result"] == [slots.NAMES.index(prepared["slot"]) - 1, prepared["desired"][prepared["slot"]]], "approved append bytes changed")
            fresh(probe=True)
            slots.require(registry(rt) == (prepared["expected"], prepared["presence"]), "registry changed before write")
            attempt = dict(transaction_sha256=approved, source_sha256=prepared["source_sha256"], slot=prepared["slot"], candidate_sha256=prepared["candidate_sha256"])
            require_no_attempt(rt, packet)
            focused_release.write_once(journal_path(rt, packet, "attempt"), attempt)
            immutable(out / "attempt.json", attempt)
            rt.set_variable(prepared["slot"], prepared["desired"][prepared["slot"]])
            observed, presence = registry(rt)
            observation = dict(transaction_sha256=approved, hashes={name: slots.digest(value.encode()) for name, value in observed.items()}, presence=presence)
            focused_release.write_once(journal_path(rt, packet, "observed"), observation)
            immutable(out / "observed.json", observation)
            slots.require(observed == prepared["desired"] and presence == sorted(set(prepared["presence"]) | {prepared["slot"]}), "registry readback mismatch; never auto-restore")
            validate_registry(source, observed, packet["entry"], present=True)
            fresh()
        state = rt.queue_state()
        slots.require(state is not None and "state = running" in state, "queue restoration unverified")
        receipt = dict(
            schema=1,
            mode=MODE,
            transaction_sha256=approved,
            source_sha256=prepared["source_sha256"],
            repository=rt.repo,
            head=packet["entry"]["head"],
            base=packet["entry"]["base"],
            tree=packet["entry"]["tree"],
            slot=prepared["slot"],
            readback={name: slots.digest(value.encode()) for name, value in observed.items()},
            queue_restored=True,
        )
        focused_release.write_once(journal_path(rt, packet, "receipt"), receipt)
        immutable(out / "receipt.json", receipt)
        return receipt
