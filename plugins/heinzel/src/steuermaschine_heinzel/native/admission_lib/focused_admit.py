"""Admit one independently reviewed pre-open focused-PR plan to the registry.

The canonical Core scripts/focused_plan.py resolver checks the complete plan
against a local synthetic merge for the exact base/head pair. This helper binds that verdict to a
review artifact and serializes the administrator-controlled registry update.
The review artifact is evidence for the separately established human review;
its prose is not parsed or treated as authorization.
`--coordinator-approved-packet-sha256` is an explicit operator confirmation,
not a cryptographic signature or identity check; GitHub permissions remain the
authority for changing the registry.

Packet envelope (the manifest is the canonical registry entry and the plan is
validated before its source is pushed or a PR is opened):
{"schema":1,"manifest":{...},"review":{
  "path":"research/review-123.md","sha256":"<artifact sha256>",
  "head":"<full sha>","base":"<full sha>","tree":"<full tree sha>",
  "manifest_sha256":"<canonical manifest sha256>"}}

The explicit --reviewed-source-pair mode additionally requires `main_manifest`,
a headless push-tree plan with the same source, selections, execution and cases,
and `review.main_manifest_sha256`. The separate --reviewed-protected-producer
opt-in permits an independently reviewed protected execution change only in
pair mode. Both plans are checked with trusted live-main resolver code in an
isolated interpreter; candidate producer code is never imported. Pair writes use
the existing cooperative local lock, not a remote compare-and-swap.
"""

from __future__ import annotations

import argparse
import ast
import base64
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from steuermaschine.config import child_environment
from ... import state as statedir  # the plugin's state paths

MAX_REGISTRY_BYTES = 48 * 1024  # scripts/focused_plan.py MAX_PLAN
MAX_PACKET_BYTES = 64 * 1024
MAX_REVIEW_BYTES = 2 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{40}\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")


def _management_core() -> str:
    """The repository authority: the plugin's reviewed repository identity (identity.CORE, a constant)."""
    from . import identity

    value = getattr(identity, "CORE", None)
    if not isinstance(value, str) or value.count("/") != 1:
        raise AdmissionError("reviewed management repository identity is malformed")
    return value


class AdmissionError(RuntimeError):
    """Refuse an unverifiable packet or registry transition."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdmissionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _stable_file(path: Path, limit: int) -> bytes:
    """Read a bounded regular file owned by this user without following links."""
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > limit:
            raise AdmissionError(f"unsafe file type, owner, or size: {path}")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise AdmissionError(f"cannot safely read {path}: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if _identity(opened) != _identity(before):
            raise AdmissionError(f"file changed while opening: {path}")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(fd, min(65536, limit + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise AdmissionError(f"file exceeds {limit} bytes: {path}")
        if _identity(os.fstat(fd)) != _identity(before) or _identity(path.lstat()) != _identity(before):
            raise AdmissionError(f"file changed while reading: {path}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _json_bytes(data: bytes, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(data, object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdmissionError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AdmissionError(f"{label} must be a JSON object")
    return parsed


def _sha(value: Any, label: str, pattern: re.Pattern[str] = SHA) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise AdmissionError(f"{label} must be a full lowercase SHA")
    return value


def _require_packet_approval(apply: bool, approval: str | None, packet_digest: str) -> None:
    if apply and approval != packet_digest:
        raise AdmissionError("--apply requires the exact coordinator-approved packet SHA256")
    if not apply and approval is not None:
        raise AdmissionError("coordinator packet approval digest applies only with --apply")


def _run(argv: list[str], *, cwd: Path | None = None, input: bytes | None = None) -> bytes:
    env = child_environment(drop_prefixes=("GIT_",))
    result = subprocess.run(argv, cwd=cwd, input=input, capture_output=True, check=False, env=env)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise AdmissionError(f"command failed ({Path(argv[0]).name}): {detail or result.returncode}")
    return result.stdout


def _read_main(repository: str) -> str:
    value = _run(["gh", "api", f"repos/{repository}/git/ref/heads/main", "--jq", ".object.sha"])
    return _sha(value.decode().strip(), "live main SHA")


def _repository_from_remote(core_repo: Path) -> str:
    remote = _run(["git", "remote", "get-url", "origin"], cwd=core_repo).decode().strip()
    normalized = remote.removesuffix(".git")
    if normalized.startswith("https://github.com/"):
        repository = normalized.removeprefix("https://github.com/")
    elif normalized.startswith("ssh://git@github.com/"):
        repository = normalized.removeprefix("ssh://git@github.com/")
    elif normalized.startswith("git@github.com:"):
        repository = normalized.removeprefix("git@github.com:")
    else:
        raise AdmissionError("Core source checkout origin is not a canonical GitHub URL")
    if len(repository.split("/")) != 2 or any(not part for part in repository.split("/")):
        raise AdmissionError("Core source checkout origin lacks an owner/repository path")
    if repository != _management_core():
        raise AdmissionError("Core source origin differs from the reviewed management repository identity")
    return repository


def load_packet(packet_path: Path, repo_root: Path, reviewed_head: str, *, source_pair: bool = False) -> tuple[dict[str, Any], bytes, bytes]:
    _sha(reviewed_head, "--reviewed-head")
    packet_bytes = _stable_file(packet_path, MAX_PACKET_BYTES)
    packet = _json_bytes(packet_bytes, "packet")
    if type(packet.get("schema")) is not int or packet["schema"] != 1:
        raise AdmissionError("packet requires schema 1")
    plan = packet.get("manifest")
    review = packet.get("review")
    if not isinstance(plan, dict) or not isinstance(review, dict):
        raise AdmissionError("packet requires manifest and review objects")
    head = _sha(plan.get("head"), "manifest head")
    base = _sha(plan.get("base"), "manifest base")
    tree = _sha(plan.get("tree"), "manifest tree")
    if head != reviewed_head:
        raise AdmissionError("reviewed head differs from manifest head")
    if plan.get("event") != "pull_request" or plan.get("ref") != "refs/pull/*/merge":
        raise AdmissionError("admission supports only exact pull_request plans")
    if source_pair:
        main_plan = packet.get("main_manifest")
        if not isinstance(main_plan, dict) or main_plan.get("event") != "push" or main_plan.get("ref") != "refs/heads/main" or "head" in main_plan:
            raise AdmissionError("reviewed source pair requires a headless push-tree plan")
        common_pr = {k: v for k, v in plan.items() if k not in {"event", "ref", "head"}}
        common_main = {k: v for k, v in main_plan.items() if k not in {"event", "ref"}}
        if common_pr != common_main:
            raise AdmissionError("PR and resulting-main manifests differ in reviewed scope")
        if _sha(review.get("main_manifest_sha256"), "review main manifest digest", HEX) != hashlib.sha256(canonical(main_plan)).hexdigest():
            raise AdmissionError("review artifact binding has a stale main manifest digest")
    elif "main_manifest" in packet:
        raise AdmissionError("resulting-main manifest requires explicit reviewed source pair mode")
    if _sha(review.get("head"), "review head") != head:
        raise AdmissionError("review artifact binding has a stale head")
    if _sha(review.get("base"), "review base") != base or _sha(review.get("tree"), "review tree") != tree:
        raise AdmissionError("review artifact binding differs from manifest base/tree")
    manifest_digest = hashlib.sha256(canonical(plan)).hexdigest()
    if _sha(review.get("manifest_sha256"), "review manifest digest", HEX) != manifest_digest:
        raise AdmissionError("review artifact binding has a stale manifest digest")
    relative = review.get("path")
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise AdmissionError("review path must be a repository-relative path")
    review_path = repo_root / relative
    try:
        review_path.resolve(strict=True).relative_to(repo_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise AdmissionError("review artifact must remain inside the management repository") from exc
    review_bytes = _stable_file(review_path, MAX_REVIEW_BYTES)
    review_digest = hashlib.sha256(review_bytes).hexdigest()
    if _sha(review.get("sha256"), "review artifact digest", HEX) != review_digest:
        raise AdmissionError("review artifact bytes do not match the packet digest")
    return packet, packet_bytes, review_bytes


def validate_live_binding(packet: dict[str, Any], reviewed_head: str, main_sha: str) -> None:
    plan = packet["manifest"]
    if plan["head"] != reviewed_head or ("review" in packet and packet["review"]["head"] != reviewed_head):
        raise AdmissionError("reviewed head differs from the packet")
    if plan["base"] != main_sha:
        raise AdmissionError("packet base is stale relative to current main")


def _require_core_remote(core_repo: Path, repository: str) -> None:
    remote = _run(["git", "remote", "get-url", "origin"], cwd=core_repo).decode().strip()
    normalized = remote.removesuffix(".git").rstrip("/")
    allowed = {
        f"https://github.com/{repository}",
        f"ssh://git@github.com/{repository}",
        f"git@github.com:{repository}",
    }
    if normalized not in allowed:
        raise AdmissionError("Core source checkout origin is not the canonical repository")


def _literal(source: bytes, name: str) -> Any:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AdmissionError(f"trusted Core source lacks literal {name}")


def _canonical_source(core_repo: Path, trusted_main: str) -> tuple[Path, str, str, str, tuple[str, ...]]:
    """Read identity constants from, and archive, the exact live Core main."""
    _run(["git", "cat-file", "-e", f"{trusted_main}^{{commit}}"], cwd=core_repo)
    repository = _literal(_run(["git", "show", f"{trusted_main}:scripts/release_candidate.py"], cwd=core_repo), "REPOSITORY")
    focus_source = _run(["git", "show", f"{trusted_main}:scripts/focused_plan.py"], cwd=core_repo)
    plan_variable = _literal(focus_source, "PLAN_VARIABLE")
    protected_files = _literal(focus_source, "PROTECTED_FILES")
    if not isinstance(repository, str) or not isinstance(plan_variable, str) or not isinstance(protected_files, tuple):
        raise AdmissionError("trusted Core identity constants have unexpected shapes")
    if repository != _management_core():
        raise AdmissionError("live Core source identity differs from reviewed management hzlib.CORE")
    archive = _run(["git", "archive", trusted_main, "scripts"], cwd=core_repo)
    temp_root = statedir.path("focused-validator")
    if temp_root.is_symlink():
        raise AdmissionError("validator cache directory is a symlink")
    temp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = temp_root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise AdmissionError("validator cache directory has unsafe owner/type/permissions")
    temp = Path(tempfile.mkdtemp(prefix="source-", dir=temp_root))
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            bundle.extractall(temp, filter="data")
        blob = _run(["git", "rev-parse", f"{trusted_main}:scripts/focused_plan.py"], cwd=core_repo).decode().strip()
        _sha(blob, "trusted focused resolver blob")
        return temp, blob, repository, plan_variable, protected_files
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def _private_work_root() -> Path:
    cache = statedir.root()
    if cache.is_symlink():
        raise AdmissionError("focused admission cache parent is a symlink")
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache_info = cache.lstat()
    if not stat.S_ISDIR(cache_info.st_mode) or cache_info.st_uid != os.getuid() or cache_info.st_mode & 0o022:
        raise AdmissionError("focused admission cache parent has unsafe owner/type/permissions")
    work_root = cache / "focused-admission-work"
    if work_root.is_symlink():
        raise AdmissionError("focused admission work directory is a symlink")
    work_root.mkdir(mode=0o700, exist_ok=True)
    work_info = work_root.lstat()
    if not stat.S_ISDIR(work_info.st_mode) or work_info.st_uid != os.getuid() or work_info.st_mode & 0o077:
        raise AdmissionError("focused admission work directory has unsafe owner/type/permissions")
    return work_root


def _prepare_core_snapshot(core_repo: Path, trusted_main: str) -> tuple[Path, Path]:
    """Fetch current main into a disposable clone without changing the caller's checkout."""
    work_root = _private_work_root()
    workspace = Path(tempfile.mkdtemp(prefix="snapshot-", dir=work_root))
    snapshot = workspace / "repo"
    try:
        _run(["git", "clone", "--shared", "--no-checkout", str(core_repo), str(snapshot)])
        upstream = _run(["git", "remote", "get-url", "origin"], cwd=core_repo).decode().strip()
        _run(["git", "remote", "set-url", "origin", upstream], cwd=snapshot)
        _run(["git", "fetch", "--no-tags", "origin", "refs/heads/main:refs/sm-admission/live-main"], cwd=snapshot)
        fetched = _run(["git", "rev-parse", "refs/sm-admission/live-main^{commit}"], cwd=snapshot).decode().strip()
        if fetched != trusted_main:
            raise AdmissionError("live main changed between API identity and isolated Git fetch")
        return workspace, snapshot
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise


def _isolated_resolve(source_dir: Path, git_dir: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Run the trusted resolver in a fresh interpreter with its package path pinned."""
    code = r'''
import base64, json, os, sys, types
root = sys.argv[1]
sys.path.insert(0, root)
pkg = types.ModuleType("scripts")
pkg.__path__ = [os.path.join(root, "scripts")]
pkg.__package__ = "scripts"
sys.modules["scripts"] = pkg
from scripts import focused_plan
from scripts.ci_workload import WorkloadSource
request = json.loads(base64.b64decode(sys.stdin.buffer.read()))
source = WorkloadSource(request["event"], focused_plan.REPOSITORY, request["ref"], request["payload"],
                        request["base"], request["head"], base64.b64decode(request["raw_diff"]))
result = focused_plan.resolve(source, request["commit"], json.dumps(request["registry"], sort_keys=True, separators=(",", ":")))
if hasattr(result, "manifest_digest"):
    out = {"tree": result.tree, "manifest_digest": result.manifest_digest}
else:
    out = {"kind": type(result).__name__}
print(json.dumps(out, sort_keys=True))
'''
    encoded = base64.b64encode(canonical(request))
    env = {"PATH": os.defpath, "HOME": str(source_dir), "GIT_CONFIG_NOSYSTEM": "1"}
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(source_dir)], cwd=git_dir, input=encoded,
        capture_output=True, env=env, check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise AdmissionError(f"isolated canonical resolver refused packet: {detail or result.returncode}")
    parsed = _json_bytes(result.stdout, "canonical resolver result")
    if "manifest_digest" not in parsed:
        raise AdmissionError("canonical resolver did not return a focused scope")
    return parsed


def validate_canonical(packet: dict[str, Any], core_repo: Path, trusted_main: str, repository: str,
                       plan_variable: str, protected_files: tuple[str, ...], source_dir: Path, *,
                       resulting_main: bool = False, protected_producer: bool = False) -> dict[str, str]:
    """Run Core's canonical pre-open PR resolver against an isolated synthetic merge."""
    plan = packet["manifest"]
    reviewed_head = packet["review"]["head"]
    _require_core_remote(core_repo, repository)
    temp_parent = Path(tempfile.mkdtemp(prefix="merge-", dir=_private_work_root()))
    temp_repo = temp_parent / "repo"
    try:
        _run(["git", "clone", "--shared", "--no-checkout", str(core_repo), str(temp_repo)])
        upstream = _run(["git", "remote", "get-url", "origin"], cwd=core_repo).decode().strip()
        _run(["git", "remote", "set-url", "origin", upstream], cwd=temp_repo)
        _run(["git", "fetch", "--no-tags", "origin", "refs/heads/main:refs/sm-admission/live-main"], cwd=temp_repo)
        fetched_main = _run(["git", "rev-parse", "refs/sm-admission/live-main^{commit}"], cwd=temp_repo).decode().strip()
        if fetched_main != trusted_main:
            raise AdmissionError("live main changed between API identity and isolated Git fetch")
        _run(["git", "cat-file", "-e", f"{reviewed_head}^{{commit}}"], cwd=temp_repo)
        tree_output = _run(["git", "merge-tree", "--write-tree", plan["base"], reviewed_head], cwd=temp_repo)
        tree = tree_output.decode().splitlines()[0].strip()
        _sha(tree, "simulated merge tree")
        parents = ["-p", plan["base"]] + ([] if resulting_main else ["-p", reviewed_head])
        commit = _run(["git", "commit-tree", tree, *parents, "-m", "focused admission synthetic merge"], cwd=temp_repo).decode().strip()
        _sha(commit, "synthetic merge commit")
        if plan["tree"] != tree:
            raise AdmissionError("packet tree differs from the simulated pre-open merge tree")
        raw_diff = _run([
            "git", "diff-tree", "--no-commit-id", "-r", "--raw", "--no-abbrev", "--no-renames", "-z",
            plan["base"], commit,
        ], cwd=temp_repo)
        trusted_execution = {
            path: _run(["git", "rev-parse", f"{trusted_main}:{path}"], cwd=temp_repo).decode().strip()
            for path in protected_files
        }
        resulting_execution = {
            path: _run(["git", "rev-parse", f"{commit}:{path}"], cwd=temp_repo).decode().strip()
            for path in protected_files
        }
        if plan.get("execution") != resulting_execution:
            raise AdmissionError("protected focused-CI source bindings differ from reviewed resulting tree")
        if resulting_execution != trusted_execution and not protected_producer:
            raise AdmissionError("PR changes protected focused-CI workflow/producer sources; use the separately reviewed source path")
        if resulting_execution != trusted_execution:
            focus_source = _run(["git", "show", f"{commit}:scripts/focused_plan.py"], cwd=temp_repo)
            release_source = _run(["git", "show", f"{commit}:scripts/release_candidate.py"], cwd=temp_repo)
            if (_literal(focus_source, "PROTECTED_FILES") != protected_files
                    or _literal(focus_source, "PLAN_VARIABLE") != plan_variable
                    or _literal(release_source, "REPOSITORY") != repository):
                raise AdmissionError("reviewed producer changes canonical source identity or protected file inventory")
        payload = {"pull_request": {
            "number": 1,
            "state": "open",
            "draft": False,
            "base": {"sha": plan["base"], "ref": "main", "repo": {"full_name": repository}},
            "head": {"sha": reviewed_head},
        }}
        key = registry_key(plan)
        request = {
            "event": "push" if resulting_main else "pull_request", "ref": "refs/heads/main" if resulting_main else "refs/pull/1/merge", "payload": payload,
            "base": plan["base"], "head": commit if resulting_main else reviewed_head,
            "raw_diff": base64.b64encode(raw_diff).decode(), "commit": commit,
            "registry": {key: plan},
        }
        result = _isolated_resolve(source_dir, temp_repo, request)
        if result["tree"] != plan["tree"]:
            raise AdmissionError("canonical resolver tree differs from reviewed pre-open merge tree")
        expected_digest = hashlib.sha256(canonical(plan)).hexdigest()
        if result["manifest_digest"] != expected_digest:
            raise AdmissionError("canonical resolver digest differs from reviewed manifest digest")
        return {
            "merge_commit": commit,
            "validator_blob": _run(["git", "rev-parse", f"{trusted_main}:scripts/focused_plan.py"], cwd=temp_repo).decode().strip(),
            "manifest_digest": result["manifest_digest"],
            "repository": repository,
            "plan_variable": plan_variable,
        }
    finally:
        shutil.rmtree(temp_parent, ignore_errors=True)


def parse_registry(raw: str | bytes) -> dict[str, Any]:
    data = raw.encode() if isinstance(raw, str) else raw
    if len(data) > MAX_REGISTRY_BYTES:
        raise AdmissionError("focused plan registry exceeds the canonical 48 KiB limit")
    if not data:
        data = b"{}"
    parsed = _json_bytes(data, "focused plan registry")
    return parsed


def registry_key(plan: dict[str, Any]) -> str:
    if plan.get("event") == "pull_request":
        return f"pull_request:{_sha(plan.get('head'), 'manifest head')}"
    if plan.get("event") == "push" and plan.get("ref") == "refs/heads/main" and "head" not in plan:
        return f"push-tree:{_sha(plan.get('tree'), 'manifest tree')}"
    raise AdmissionError("unsupported focused registry key")


def merge_registry(raw: str | bytes, plan: dict[str, Any]) -> tuple[str, bool]:
    original = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    registry = parse_registry(raw)
    key = registry_key(plan)
    if key in registry:
        existing = registry[key]
        if existing != plan:
            raise AdmissionError(f"focused registry key conflict: {key}")
        return original or "{}", False
    merged = dict(registry)
    merged[key] = plan
    encoded = canonical(merged).decode()
    if len(encoded.encode()) > MAX_REGISTRY_BYTES:
        raise AdmissionError("merged focused plan registry exceeds the canonical 48 KiB limit")
    return encoded, True


@contextmanager
def _lock(path: Path):
    if path.parent.is_symlink():
        raise AdmissionError("focused registry lock directory is a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid():
        raise AdmissionError("focused registry lock directory has unsafe owner/type")
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise AdmissionError(f"cannot open focused registry lock safely: {exc}") from exc
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise AdmissionError("focused registry lock has unsafe owner/type")
    handle = os.fdopen(fd, "r+")
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AdmissionError("focused registry writer lock is busy; retry after current admission")
                time.sleep(0.1)
        yield handle
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _gh_variable_get(repository: str, variable: str) -> str:
    result = subprocess.run(
        ["gh", "api", f"repos/{repository}/actions/variables/{variable}", "--jq", ".value"],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        error = result.stderr.decode("utf-8", "replace").lower()
        if "http 404" in error or "not found" in error:
            return "{}"
        raise AdmissionError(f"could not read focused plan registry: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout.decode("utf-8").removesuffix("\n")


def _gh_variable_set(repository: str, variable: str, value: str) -> None:
    _run(["gh", "variable", "set", variable, "--repo", repository, "--body", value])


def _write_receipt(receipt: dict[str, Any], home: Path) -> Path:
    cache = statedir.root()
    if cache.is_symlink():
        raise AdmissionError("focused admission cache is a symlink")
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache_info = cache.lstat()
    if not stat.S_ISDIR(cache_info.st_mode) or cache_info.st_uid != os.getuid():
        raise AdmissionError("focused admission cache has unsafe owner/type")
    root = cache / "focused-admission-receipts"
    if root.is_symlink():
        raise AdmissionError("focused admission receipt directory is a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not stat.S_ISDIR(root.lstat().st_mode) or root.lstat().st_uid != os.getuid():
        raise AdmissionError("focused admission receipt directory has unsafe owner/type")
    admission_id = receipt.get("admission_id")
    if not isinstance(admission_id, str) or not re.fullmatch(r"[0-9a-f]{32}", admission_id):
        raise AdmissionError("receipt requires an immutable per-admission identifier")
    path = root / f"head-{receipt['head']}-{admission_id}.json"
    temp = root / f".{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(temp, flags, 0o600)
    try:
        data = canonical(receipt) + b"\n"
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, path, follow_symlinks=False)
        except FileExistsError:
            existing = _stable_file(path, MAX_PACKET_BYTES)
            if existing != data:
                raise AdmissionError("a different receipt already exists for this PR/head")
        return path
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def apply_registry(
    plan: dict[str, Any] | list[dict[str, Any]],
    registry_get: Callable[[], str],
    registry_set: Callable[[str], None],
    receipt: dict[str, Any],
    lock_path: Path,
    home: Path,
    check_fresh: Callable[[], None] | None = None,
) -> Path:
    """Fresh read/merge/write/readback while holding only the shared writer lock."""
    # The initial caller read and validation happen before this lock; always refresh
    # under the short write lock so a prior packet can never be overwritten.
    with _lock(lock_path):
        if check_fresh is not None:
            check_fresh()
        current = registry_get()
        candidate, changed = current, False
        for item in plan if isinstance(plan, list) else [plan]:
            candidate, item_changed = merge_registry(candidate, item)
            changed |= item_changed
        if changed:
            registry_set(candidate)
        readback = registry_get()
        if readback != candidate:
            raise AdmissionError("focused plan registry readback mismatch; stop and inspect the remote value")
        if check_fresh is not None:
            check_fresh()
        receipt = dict(receipt)
        receipt["registry_sha256"] = hashlib.sha256(candidate.encode()).hexdigest()
        receipt["readback_sha256"] = hashlib.sha256(readback.encode()).hexdigest()
        receipt["registry_changed"] = changed
        return _write_receipt(receipt, home)


def _isolated_slot_append(source_dir: Path, legacy: str, slots: tuple[str, ...], additions: dict[str, Any]) -> dict[str, Any]:
    """Prepare storage using only the reviewed trusted-main archive, never PR code."""
    code = r'''
import json, os, sys, types
root = sys.argv[1]
sys.path.insert(0, root)
pkg = types.ModuleType("scripts")
pkg.__path__ = [os.path.join(root, "scripts")]
pkg.__package__ = "scripts"
sys.modules["scripts"] = pkg
from scripts import focused_plan as f
request = json.load(sys.stdin)
result = f.append_slot(request["legacy"], tuple(request["slots"]), request["additions"])
print(json.dumps({"variables": f.SLOT_VARIABLES, "result": result, "limit": f.MAX_SLOT}, sort_keys=True))
'''
    request = canonical({"legacy": legacy, "slots": slots, "additions": additions})
    result = subprocess.run([sys.executable, "-I", "-c", code, str(source_dir)], input=request,
                            capture_output=True, check=False, env={"PATH": os.defpath, "HOME": str(source_dir)})
    if result.returncode:
        raise AdmissionError("trusted-main slot preparation refused: " + result.stderr.decode("utf-8", "replace"))
    proposal = _json_bytes(result.stdout, "slot proposal")
    expected = [f"FOCUSED_CI_PLANS_V2_{i}" for i in range(4)]
    if proposal["variables"] != expected or proposal["limit"] != 40000:
        raise AdmissionError("unexpected trusted-main slot contract")
    if proposal["result"] is not None:
        index, value = proposal["result"]
        if type(index) is not int or not 0 <= index < 4 or not isinstance(value, str) or len(value.encode()) > 40000:
            raise AdmissionError("invalid trusted-main slot proposal")
    return proposal


def _gh_slot_get(repository: str, variable: str) -> str:
    """Only a missing optional variable is empty; populated '{}' remains invalid."""
    result = subprocess.run(["gh", "api", f"repos/{repository}/actions/variables/{variable}", "--jq", ".value"],
                            capture_output=True, check=False)
    if result.returncode:
        if "http 404" in result.stderr.decode("utf-8", "replace").lower():
            return ""
        raise AdmissionError("could not read optional slot: " + result.stderr.decode("utf-8", "replace"))
    return result.stdout.decode("utf-8").removesuffix("\n")


def apply_slots(
    plans: list[dict[str, Any]], source_dir: Path, expected: dict[str, str],
    registry_get: Callable[[], dict[str, str]], registry_set: Callable[[str, str], None],
    receipt: dict[str, Any], lock_path: Path, home: Path, *,
    approved_candidate_sha256: str, check_fresh: Callable[[], None],
    resulting_main: dict[str, Any] | None = None,
    check_main_append: Callable[[dict[str, str], dict[str, str]], None] | None = None,
    pairs: int = 1,
    release_main: dict[str, Any] | None = None,
    repair_main: dict[str, Any] | None = None,
) -> Path | dict[str, Any]:
    """One approved variable update; caller holds its queue/train/release exclusions.

    check_fresh must assert the reviewed main, complete active reader inventory,
    release/hold/queue exclusions and repository/org variable transport budget.
    It is deliberately mandatory. The generic CLI cannot supply that authority.
    Every plan must already have passed validate_canonical in reviewed-pair mode.
    `pairs` > 1 is the reviewed batch: exactly that many distinct PR keys and
    distinct push-tree keys, still appended by the one slot write below.
    """
    variables = ["FOCUSED_CI_PLANS", *(f"FOCUSED_CI_PLANS_V2_{i}" for i in range(4))]
    if sum(proof is not None for proof in (release_main, resulting_main, repair_main)) > 1:
        raise AdmissionError("conflicting singleton admission proof types")
    singleton = release_main if release_main is not None else repair_main if repair_main is not None else resulting_main
    mode = ("release-main-only/v1" if release_main is not None else "repair-main/v1" if repair_main is not None
            else "resulting-main-only/v1")
    if type(pairs) is not int or pairs < 1 or (singleton is not None and pairs != 1):
        raise AdmissionError("slot admission pair count is invalid")
    if set(expected) != set(variables) or (singleton is None and len(plans) != 2 * pairs):
        raise AdmissionError("slot admission requires the complete five-variable snapshot and reviewed scope")
    additions = {registry_key(plan): plan for plan in plans}
    if singleton is None:
        # Distinct keys only: a duplicated key would silently shrink the batch.
        if (len(additions) != 2 * pairs
                or sum(key.startswith("pull_request:") for key in additions) != pairs
                or sum(key.startswith("push-tree:") for key in additions) != pairs):
            raise AdmissionError("slot admission requires one PR and one resulting-main plan per reviewed pair")
    elif (singleton.get("mode") != mode or len(plans) != 1
          or not next(iter(additions)).startswith("push-tree:") or "head" in plans[0] or "commit" in plans[0]
          or plans[0].get("base") != singleton.get("base") or plans[0].get("tree") != singleton.get("tree")
          or hashlib.sha256(canonical(plans[0])).hexdigest() != singleton.get("main_manifest_sha256")
          or not callable(check_main_append)):
        raise AdmissionError("slot admission requires exact typed resulting-main proof and one manifest")
    with _lock(lock_path):
        check_fresh()
        current = registry_get()
        if current != expected:
            raise AdmissionError("registry snapshot changed; reprepare and review, do not overwrite")
        proposal = _isolated_slot_append(source_dir, current[variables[0]], tuple(current[name] for name in variables[1:]), additions)
        desired = dict(current)
        result = proposal["result"]
        changed = result is not None
        if singleton is not None and not changed:
            raise AdmissionError("resulting-main manifest already admitted; no new write required")
        if changed:
            index, candidate = result
            variable = variables[index + 1]
            if hashlib.sha256(candidate.encode()).hexdigest() != approved_candidate_sha256:
                raise AdmissionError("proposed slot bytes differ from exact coordinator approval")
            desired[variable] = candidate
            if singleton is not None:
                check_main_append(current, desired)
            check_fresh()
            # Narrow the read/write interval and reject a non-cooperative writer.
            if registry_get() != current:
                raise AdmissionError("registry changed before write; no write performed")
            registry_set(variable, candidate)
        readback = registry_get()
        if readback != desired:
            raise AdmissionError("slot readback mismatch; stop and inspect, never auto-restore")
        check_fresh()
        result_receipt = dict(receipt)
        result_receipt.update(registry_changed=changed, slot_variable=variable if changed else None,
                              before={name: hashlib.sha256(value.encode()).hexdigest() for name, value in current.items()},
                              after={name: hashlib.sha256(value.encode()).hexdigest() for name, value in readback.items()},
                              source="trusted-main compressed append-only/v1")
        # The release-only caller records its success only after the queue is
        # restored. Existing PR modes keep their original receipt timing.
        return result_receipt if release_main is not None else _write_receipt(result_receipt, home)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packet", type=Path)
    parser.add_argument("--reviewed-head", required=True)
    parser.add_argument("--reviewed-source-pair", action="store_true", help="explicitly admit a reviewed PR and resulting-main pair")
    parser.add_argument("--reviewed-protected-producer", action="store_true", help="explicitly permit reviewed protected source changes in pair mode")
    parser.add_argument("--compressed-slots", action="store_true", help="prepare a slot pair after trusted-main adoption; applying requires the guarded coordinator API")
    parser.add_argument("--apply", action="store_true", help="write the registry after validation; default is dry-run")
    parser.add_argument(
        "--coordinator-approved-packet-sha256",
        help="required with --apply; operator confirmation of the exact reviewed packet bytes",
    )
    parser.add_argument("--core-repo", type=Path, required=True, help="existing Core checkout containing the reviewed local head")
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    source_dir: Path | None = None
    workspace: Path | None = None
    try:
        if args.compressed_slots and (not args.reviewed_source_pair or args.apply):
            raise AdmissionError("compressed slots require pair mode and guarded apply_slots; generic --apply is not authorized")
        if args.reviewed_protected_producer and not args.reviewed_source_pair:
            raise AdmissionError("protected producer mode requires reviewed source pair mode")
        packet, packet_bytes, review_bytes = load_packet(args.packet, repo_root, args.reviewed_head, source_pair=args.reviewed_source_pair)
        plan = packet["manifest"]
        packet_sha256 = hashlib.sha256(packet_bytes).hexdigest()
        _require_packet_approval(args.apply, args.coordinator_approved_packet_sha256, packet_sha256)
        # The official repository slug is obtained from the exact source checkout,
        # then checked against the constants in the live-main archive below.
        repository = _repository_from_remote(args.core_repo)
        main_sha = _read_main(repository)
        workspace, source_snapshot = _prepare_core_snapshot(args.core_repo, main_sha)
        source_dir, _validator_blob, trusted_repository, variable, protected_files = _canonical_source(source_snapshot, main_sha)
        if repository != trusted_repository:
            raise AdmissionError("Core origin does not match the repository identity in live main source")
        validate_live_binding(packet, args.reviewed_head, main_sha)
        scope = validate_canonical(packet, source_snapshot, main_sha, repository, variable, protected_files, source_dir,
                                   protected_producer=args.reviewed_protected_producer)
        plans = [plan]
        if args.reviewed_source_pair:
            main_packet = {**packet, "manifest": packet["main_manifest"]}
            main_scope = validate_canonical(main_packet, source_snapshot, main_sha, repository, variable, protected_files,
                                            source_dir, resulting_main=True, protected_producer=args.reviewed_protected_producer)
            plans.append(packet["main_manifest"])
        registry = _gh_variable_get(repository, variable)
        if args.compressed_slots:
            names = [f"FOCUSED_CI_PLANS_V2_{i}" for i in range(4)]
            slots = tuple(_gh_slot_get(repository, name) for name in names)
            proposal = _isolated_slot_append(source_dir, registry, slots, {registry_key(item): item for item in plans})
            proposal.update(mode="dry-run", validator_main=main_sha, packet_sha256=packet_sha256,
                            before={name: hashlib.sha256(value.encode()).hexdigest()
                                    for name, value in zip([variable, *names], [registry, *slots], strict=True)})
            print(json.dumps(proposal, sort_keys=True))
            return 0
        candidate, changed = registry, False
        for item in plans:
            candidate, item_changed = merge_registry(candidate, item)
            changed |= item_changed
        receipt = {
            "schema": 1,
            "admission_id": uuid.uuid4().hex,
            "scope": "reviewed-source-pair" if args.reviewed_source_pair else "preopen-pull-request",
            "head": plan["head"],
            "base": plan["base"],
            "tree": plan["tree"],
            "review_path": packet["review"]["path"],
            "review_sha256": hashlib.sha256(review_bytes).hexdigest(),
            "manifest_sha256": hashlib.sha256(canonical(plan)).hexdigest(),
            "resolver_manifest_digest": scope["manifest_digest"],
            "packet_sha256": packet_sha256,
            "coordinator_approved_packet_sha256": args.coordinator_approved_packet_sha256,
            "validator_main": main_sha,
            "validator_blob": scope["validator_blob"],
            "validator_repository": scope["repository"],
            "registry_variable": scope["plan_variable"],
            "merge_commit": scope["merge_commit"],
            "registry_key": f"pull_request:{plan['head']}",
        }
        if args.reviewed_source_pair:
            receipt.update(main_manifest_sha256=hashlib.sha256(canonical(plans[1])).hexdigest(),
                           main_resolver_manifest_digest=main_scope["manifest_digest"],
                           main_registry_key=registry_key(plans[1]),
                           protected_producer_approved=args.reviewed_protected_producer)
        if not args.apply:
            current_compact = canonical(parse_registry(registry))
            prospective_compact = canonical(parse_registry(candidate))
            prospective_value_bytes = len(candidate.encode("utf-8"))
            output = {"mode": "dry-run", "would_add": changed, "key": receipt["registry_key"],
                      "head": receipt["head"], "base": receipt["base"], "tree": receipt["tree"],
                      "review_sha256": receipt["review_sha256"], "manifest_sha256": receipt["manifest_sha256"],
                      "candidate_registry_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
                      "current_value_utf8_bytes": len(registry.encode("utf-8")),
                      "current_compact_utf8_bytes": len(current_compact),
                      "prospective_value_utf8_bytes": prospective_value_bytes,
                      "prospective_compact_utf8_bytes": len(prospective_compact),
                      "local_limit_utf8_bytes": MAX_REGISTRY_BYTES,
                      "local_remaining_utf8_bytes": MAX_REGISTRY_BYTES - prospective_value_bytes,
                      "remote_acceptance": "unverified"}
            if args.reviewed_source_pair:
                output.update(main_registry_key=receipt["main_registry_key"],
                              main_manifest_sha256=receipt["main_manifest_sha256"],
                              protected_producer_approved=args.reviewed_protected_producer)
            print(json.dumps(output, sort_keys=True))
            return 0
        path = apply_registry(
            plans, lambda: _gh_variable_get(repository, variable),
            lambda value: _gh_variable_set(repository, variable, value), receipt,
            statedir.path("focused-pr-registry.lock"), Path.home(),
            check_fresh=lambda: validate_live_binding(packet, args.reviewed_head, _read_main(repository)),
        )
        print(f"ADMITTED {receipt['registry_key']} pre-open registry receipt {path}")
        return 0
    except (AdmissionError, OSError, subprocess.SubprocessError, ValueError, KeyError, ImportError, SyntaxError, tarfile.TarError) as exc:
        print(f"sm head ship: focused admission refused: {exc}", file=sys.stderr)
        return 1
    finally:
        if source_dir is not None:
            shutil.rmtree(source_dir, ignore_errors=True)
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)
