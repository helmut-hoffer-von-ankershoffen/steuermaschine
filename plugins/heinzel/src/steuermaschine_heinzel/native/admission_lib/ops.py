"""Typed entry of the focused admission: what the native commands call.

Single-packet path of the former `prepare` / `apply` / `blockers` operations plus the canonical digest. The checks
and their order are those of `focused_slots.main` (kept there as the argv form for the moved tests and for parity);
this module adds only: prepare stores its output and its own arguments next to the packet, apply needs only the
approved digest, and apply refuses with a fix when the worktree is not at the reviewed head.

Nothing here writes outside this machine except `apply`, through `focused_slots.apply_prepared`.
"""

# mypy: disable-error-code="no-untyped-call"
# (the moved focused_* modules are untyped, as at their source; this module is the typed layer over them)
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from steuermaschine.models import Model as _Model

from . import focused_admit as a
from . import focused_slots as s
from . import focused_update as update

FIRST_MERGE_ONLY = ("RULE: these plans bind main as it is now and are valid for the FIRST merge only. After one member merged, every other "
                    "member needs `sm head packet bind` and its own admission at the new main; a second merge on these plans turns main red.")
ARGS_FILE = "prepare.args.json"
PREPARED_FILE = "prepare.out"


class HashResult(_Model):
    path: str
    sha256: str

    def render(self) -> list[str]:
        return [self.sha256]


class BlockingRun(_Model):
    id: int
    status: str | None = None
    event: str | None = None
    head_branch: str | None = None
    path: str | None = None
    head_sha: str | None = None


class BlockersResult(_Model):
    blocking: list[BlockingRun]

    def render(self) -> list[str]:
        if not self.blocking:
            return ["no active run blocks apply"]
        return [f"{r.id} {r.status} {r.event} {r.head_branch} {r.path} {str(r.head_sha)[:8]}" for r in self.blocking]


class NonconsumerResult(_Model):
    receipts: list[dict[str, Any]]
    excluded: list[dict[str, Any]]

    def render(self) -> list[str]:
        return [json.dumps(self.model_dump(), indent=1, sort_keys=True),
                "Independent review required; prepare and apply revalidate these receipts."]


class PrepareResult(_Model):
    sha256: str
    head: str
    main: str
    slot: str
    pr: int | None
    prepared_path: str
    args_path: str
    prepared: dict[str, Any]

    def render(self) -> list[str]:
        return [f"prepared {self.prepared_path}", f"sha256 {self.sha256}", f"head {self.head[:8]} main {self.main[:8]} slot {self.slot}",
                "a reviewer who is not the author approves exactly this sha256; then: sm head apply PACKET --approved SHA256"]


class BatchMember(_Model):
    head: str
    pr: int | None
    packet_sha256: str


class BatchPrepareResult(_Model):
    sha256: str
    main: str
    slot: str
    members: list[BatchMember]
    prepared_path: str
    args_path: str
    prepared: dict[str, Any]

    def render(self) -> list[str]:
        return [f"prepared {self.prepared_path}", f"sha256 {self.sha256}",
                f"batch of {len(self.members)} pairs on main {self.main[:8]} slot {self.slot}: " + ", ".join(m.head[:8] for m in self.members),
                FIRST_MERGE_ONLY,
                "a reviewer who is not the author approves exactly this sha256; then: sm head apply BATCH_DIR --approved SHA256"]


class ApplyResult(_Model):
    receipt_path: str
    sha256: str
    head: str

    def render(self) -> list[str]:
        return [self.receipt_path]


def canonical_sha256(value: Any) -> str:
    """THE digest a reviewer approves: sha256 of canonical JSON (sorted keys, compact separators, ASCII-escaped, no newline)."""
    return hashlib.sha256(a.canonical(value)).hexdigest()  # = focused_slots.digest(focused_admit.canonical(value))


def hash_file(path: Path) -> HashResult:
    parsed = a._json_bytes(a._stable_file(Path(path), 1024 * 1024), "prepared transaction")
    return HashResult(path=str(path), sha256=canonical_sha256(parsed))


def _receipts(nonconsumers: Path | None) -> list[dict[str, Any]]:
    if nonconsumers is None:
        return []
    receipts: list[dict[str, Any]] = a._json_bytes(a._stable_file(Path(nonconsumers), a.MAX_REVIEW_BYTES), "nonconsumers")["receipts"]
    return receipts


def receipts_of(path: Path) -> list[dict[str, Any]]:
    """The receipts of one reviewed receipts file, read as prepare reads it (to name a file that covers the blockers)."""
    return _receipts(Path(path))


def blockers(core_repo: Path, nonconsumers: Path | None = None, *, runtime: Any = None) -> BlockersResult:
    rt = runtime or s.Runtime(core_repo)
    report = s.report_blockers(rt, {receipt["run"] for receipt in _receipts(nonconsumers)})
    return BlockersResult(blocking=[BlockingRun(**row) for row in report["blocking"]])


def nonconsumer_receipts(core_repo: Path, *, runtime: Any = None) -> NonconsumerResult:
    rt = runtime or s.Runtime(core_repo)
    output, excluded = s.stuck_queued_receipts(rt, set())
    return NonconsumerResult(receipts=output["receipts"], excluded=excluded)


def _checks(pr: int | None, protected: bool, expected_remote_head: str | None, resulting_main_only: bool,
            repair_main: bool = False) -> None:
    s.require(pr is None or pr > 0, "PR must be positive")
    s.require(not repair_main or (pr is None and not protected and expected_remote_head is None and not resulting_main_only),
              "repair-main takes the packet only: no --pr, --expected-remote-head, --resulting-main-only or protected producer")
    s.require(not resulting_main_only or (pr is not None and not protected),
              "resulting-main-only requires a PR and excludes protected producer approval")
    s.require(not (expected_remote_head is not None and resulting_main_only), "--expected-remote-head excludes --resulting-main-only")
    if expected_remote_head is not None:
        update.validate_option(pr, expected_remote_head)


def _cleanup(source: Path | None, workspace: Path | None) -> None:
    if source is not None:
        shutil.rmtree(source, ignore_errors=True)
    if workspace is not None:
        shutil.rmtree(workspace, ignore_errors=True)


def prepare(packet: Path, core_repo: Path, review_root: Path, *, pr: int | None = None, protected: bool = False,
            nonconsumers: Path | None = None, expected_remote_head: str | None = None, resulting_main_only: bool = False,
            out_dir: Path | None = None, runtime: Any = None, probe: bool = False, repair_main: bool = False) -> PrepareResult:
    """Read-only preparation of the exact transaction; stores it and its arguments next to the packet.

    `probe=False` (the default here): no cancel probe is sent for stuck-queued receipts, so prepare sends nothing to
    GitHub but reads. Every read check of those runs still holds, and apply sends the probe before the write.
    The transaction bytes do not depend on it."""
    packet, core_repo, review_root = Path(packet), Path(core_repo), Path(review_root)
    source = workspace = None
    try:
        _checks(pr, protected, expected_remote_head, resulting_main_only, repair_main)
        receipts = _receipts(nonconsumers)
        rt = runtime or s.Runtime(core_repo)
        if pr is not None and expected_remote_head is None and not resulting_main_only and not repair_main:
            expected_remote_head = moved_pr_head(rt, packet, pr)
        prepared, _packet, source, workspace, _proofs = s.prepare(
            rt, packet, review_root, protected=protected, pr=pr, nonconsumers=receipts,
            expected_remote_head=expected_remote_head, resulting_main_only=resulting_main_only, probe=probe,
            repair_main=repair_main)
    finally:
        _cleanup(source, workspace)
    text = a.canonical(prepared).decode()
    directory = Path(out_dir) if out_dir is not None else packet.parent
    target, args_path = directory / PREPARED_FILE, directory / ARGS_FILE
    sha = canonical_sha256(prepared)
    _atomic(target, text + "\n")
    _atomic(args_path, json.dumps(dict(
        schema=1, sha256=sha, packet=str(packet.resolve()), core_repo=str(core_repo.resolve()), review_root=str(review_root.resolve()),
        pr=pr, protected=protected, nonconsumers=None if nonconsumers is None else str(Path(nonconsumers).resolve()),
        expected_remote_head=expected_remote_head, resulting_main_only=resulting_main_only,
        **({"repair_main": True} if repair_main else {})), indent=1, sort_keys=True) + "\n")
    return PrepareResult(sha256=sha, head=prepared["head"], main=prepared["main"], slot=prepared["slot"], pr=prepared["pr"],
                         prepared_path=str(target), args_path=str(args_path), prepared=prepared)


def moved_pr_head(rt: Any, packet: Path, pr: int) -> str | None:
    """The supported update of an existing PR (bead m598): the reviewed head is a new head of PR `pr` (a fast-forward
    or a merge of main into the branch, pushed after apply). Returns the PR's current head as the expected remote head
    when it differs from the packet's head, None when the PR is already at it (the strict mode). Whether the old head
    is an ancestor of the new one, and the PR's and branch's identity, are proved by the update mode itself."""
    head = a._json_bytes(a._stable_file(Path(packet), a.MAX_PACKET_BYTES), "packet")["manifest"]["head"]
    pull = rt.api(f"repos/{rt.repo}/pulls/{pr}")
    old = (pull.get("head") or {}).get("sha") if isinstance(pull, dict) else None
    s.require(isinstance(old, str) and update.SHA.fullmatch(old) is not None, "PR head unreadable")
    return None if old == head else str(old)


def prepare_waiting(packet: Path, core_repo: Path, review_root: Path, *, clock: Any, say: Any, max_minutes: float, poll_seconds: float,
                    nonconsumers: Path | None = None, runtime: Any = None, **options: Any) -> PrepareResult:
    """`prepare --wait`: poll the read-only run inventory until no uncovered run is active, then prepare; retry a
    truncated inventory with the identical prepare. Raises wait.StillWaiting after `max_minutes`."""
    from . import wait

    result: PrepareResult = wait.prepare_when_clear(
        lambda: prepare(packet, core_repo, review_root, nonconsumers=nonconsumers, runtime=runtime, **options),
        lambda: blockers(core_repo, nonconsumers, runtime=runtime).blocking,
        clock=clock, say=say, max_minutes=max_minutes, poll_seconds=poll_seconds)
    return result


def _atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def stored_args(packet: Path) -> dict[str, Any]:
    path = Path(packet).parent / ARGS_FILE
    s.require(path.is_file(), f"no {ARGS_FILE} next to the packet: run `sm head prepare` for this packet first")
    args = a._json_bytes(a._stable_file(path, 1024 * 1024), "stored prepare arguments")
    s.require(args.get("schema") == 1 and args.get("packet") == str(Path(packet).resolve()), "stored prepare arguments belong to another packet")
    return args


def check_worktree_head(core_repo: Path, head: str) -> None:
    """One worktree per head: apply refuses, with the fix, when the worktree moved off the reviewed head."""
    actual = a._run(["git", "rev-parse", "HEAD"], cwd=Path(core_repo)).decode().strip()
    s.require(actual == head,
              f"worktree {core_repo} is at {actual[:12]}, the reviewed head is {head[:12]}; do not move this worktree back: "
              f"create a new one on its own branch at the reviewed head "
              f"(git -C {core_repo} worktree add -b admit/{head[:12]} ../admit-{head[:12]} {head}) "
              "and prepare, review and apply from there")


def check_stored_args(args: dict[str, Any], approved_doc: dict[str, Any], approved: str) -> None:
    """The stored arguments are not trusted: each one must agree with the reviewed transaction before anything runs.

    The digest covers the transaction, not the arguments file. So every argument is checked against a field the
    digest does cover; a changed arguments file refuses here, before any process starts. (Afterwards the complete
    preparation is recomputed from these arguments and must equal the reviewed transaction byte for byte, which
    covers what is not a plain field: the review root through the pinned review digests, the checkout through
    repository and head.)
    """
    s.require(args.get("sha256") == approved, "stored prepare arguments belong to another prepared transaction")
    s.require(args.get("pr") == approved_doc.get("pr"), "stored prepare arguments differ from the reviewed transaction: pr")
    s.require(bool(args.get("protected")) == approved_doc.get("protected_producer"),
              "stored prepare arguments differ from the reviewed transaction: protected producer")
    s.require(bool(args.get("resulting_main_only")) == ("resulting_main" in approved_doc),
              "stored prepare arguments differ from the reviewed transaction: resulting-main-only")
    s.require(bool(args.get("repair_main")) == ("repair_main" in approved_doc),
              "stored prepare arguments differ from the reviewed transaction: repair-main")
    s.require(args.get("expected_remote_head") == (approved_doc.get("pr_update") or {}).get("old"),
              "stored prepare arguments differ from the reviewed transaction: expected remote head")
    s.require(s.digest(a._stable_file(Path(args["packet"]), a.MAX_PACKET_BYTES)) == approved_doc.get("packet_sha256"),
              "stored prepare arguments differ from the reviewed transaction: packet bytes")
    nonconsumers = Path(args["nonconsumers"]) if args.get("nonconsumers") else None
    s.require(_receipts(nonconsumers) == approved_doc.get("nonconsumers"),
              "stored prepare arguments differ from the reviewed transaction: nonconsumer receipts")


def apply(packet: Path, approved: str, *, prepared_path: Path | None = None, runtime: Any = None) -> ApplyResult:
    """The one registry write: recompute the preparation, require byte equality with the reviewed one, then apply."""
    packet = Path(packet)
    s.require(isinstance(approved, str) and a.HEX.fullmatch(approved) is not None, "apply needs --approved with the sha256 the reviewer approved")
    args = stored_args(packet)
    prepared_path = Path(prepared_path) if prepared_path is not None else packet.parent / PREPARED_FILE
    approved_doc = a._json_bytes(a._stable_file(prepared_path, 1024 * 1024), "prepared transaction")
    s.require(canonical_sha256(approved_doc) == approved,
              "the approved sha256 is not the digest of the stored prepared transaction; reprepare and review")
    s.require("mode" not in approved_doc, "this is a batch transaction; the single-packet apply does not take it")
    check_stored_args(args, approved_doc, approved)
    core_repo, review_root = Path(args["core_repo"]), Path(args["review_root"])
    nonconsumers = Path(args["nonconsumers"]) if args.get("nonconsumers") else None
    source = workspace = None
    try:
        _checks(args.get("pr"), bool(args.get("protected")), args.get("expected_remote_head"), bool(args.get("resulting_main_only")),
                bool(args.get("repair_main")))
        if not args.get("resulting_main_only"):
            check_worktree_head(core_repo, approved_doc["head"])
        receipts = _receipts(nonconsumers)
        rt = runtime or s.Runtime(core_repo)
        prepared, packet_doc, source, workspace, proofs = s.prepare(
            rt, packet, review_root, protected=bool(args.get("protected")), pr=args.get("pr"), nonconsumers=receipts,
            expected_remote_head=args.get("expected_remote_head"), resulting_main_only=bool(args.get("resulting_main_only")),
            repair_main=bool(args.get("repair_main")))
        s.require(approved_doc == prepared, "prepared transaction changed; reprepare and review")
        from . import wait

        receipt = wait.tracked(rt, lambda: s.apply_prepared(rt, prepared, packet_doc, source, proofs, approved, snapshot=workspace / "repo"))
    finally:
        _cleanup(source, workspace)
    return ApplyResult(receipt_path=str(receipt), sha256=approved, head=prepared["head"])


# ----------------------------------------------------------------------------- batch: many reviewed pairs, one transaction
def _entries(packets: list[Path], core_repos: list[Path], prs: list[int] | None) -> list[tuple[Path, Path, int | None]]:
    """The entries of `focused_slots.prepare_batch`, with the checks of the former `--packets` branch."""
    numbers = prs if prs is not None else [0] * len(packets)
    s.require(len(core_repos) == len(packets) == len(numbers) and all(type(n) is int and n >= 0 for n in numbers),
              "batch needs one --core-repos checkout and one --prs number (0 = pre-open) per packet")
    return [(Path(path), Path(core), number or None) for path, core, number in zip(packets, core_repos, numbers, strict=True)]


def _cleanup_batch(source: Path | None, workspaces: list[Path]) -> None:
    _cleanup(source, None)
    for workspace in workspaces:
        _cleanup(None, workspace)


def prepare_batch(packets: list[Path], core_repos: list[Path], review_root: Path, out_dir: Path, *, prs: list[int] | None = None,
                  nonconsumers: Path | None = None, runtime: Any = None, probe: bool = False) -> BatchPrepareResult:
    """Prepare 2..20 reviewed pairs as ONE transaction; stores it and its arguments in `out_dir`. Sends no cancel probe."""
    review_root, out_dir = Path(review_root), Path(out_dir)
    entries = _entries(packets, core_repos, prs)
    receipts = _receipts(nonconsumers)
    source, workspaces = None, []
    try:
        rt = runtime or s.Runtime(entries[0][1])
        prepared, _members, source, workspaces, _proofs = s.prepare_batch(rt, entries, review_root, receipts, probe=probe)
    finally:
        _cleanup_batch(source, workspaces)
    sha = canonical_sha256(prepared)
    out_dir.mkdir(parents=True, exist_ok=True)
    target, args_path = out_dir / PREPARED_FILE, out_dir / ARGS_FILE
    _atomic(target, a.canonical(prepared).decode() + "\n")
    _atomic(args_path, json.dumps({
        "schema": 1, "mode": s.BATCH_MODE, "sha256": sha, "packets": [str(path.resolve()) for path, _, _ in entries],
        "core_repos": [str(core.resolve()) for _, core, _ in entries], "prs": [pr for _, _, pr in entries],
        "review_root": str(review_root.resolve()), "nonconsumers": None if nonconsumers is None else str(Path(nonconsumers).resolve())},
        indent=1, sort_keys=True) + "\n")
    return BatchPrepareResult(sha256=sha, main=prepared["main"], slot=prepared["slot"], members=[BatchMember(**m) for m in prepared["members"]],
                              prepared_path=str(target), args_path=str(args_path), prepared=prepared)


def check_stored_batch_args(args: dict[str, Any], approved_doc: dict[str, Any], approved: str) -> None:
    """As check_stored_args, for a batch: every member's packet bytes and PR, and the receipts, against the transaction."""
    members: Any = approved_doc.get("members")
    s.require(args.get("schema") == 1 and args.get("mode") == s.BATCH_MODE and approved_doc.get("mode") == s.BATCH_MODE
              and isinstance(members, list), "this is not a batch transaction")
    s.require(args.get("sha256") == approved, "stored prepare arguments belong to another prepared transaction")
    packets: Any = args.get("packets")
    cores: Any = args.get("core_repos")
    prs: Any = args.get("prs")
    s.require(isinstance(packets, list) and isinstance(cores, list) and isinstance(prs, list)
              and len(packets) == len(cores) == len(prs) == len(members), "stored prepare arguments differ from the reviewed transaction: member count")
    for packet, pr, member in zip(packets, prs, members, strict=True):
        s.require(pr == member["pr"], "stored prepare arguments differ from the reviewed transaction: pr of a member")
        s.require(s.digest(a._stable_file(Path(packet), a.MAX_PACKET_BYTES)) == member["packet_sha256"],
                  "stored prepare arguments differ from the reviewed transaction: packet bytes of a member")
    nonconsumers = Path(args["nonconsumers"]) if args.get("nonconsumers") else None
    s.require(_receipts(nonconsumers) == approved_doc.get("nonconsumers"),
              "stored prepare arguments differ from the reviewed transaction: nonconsumer receipts")


def apply_batch(out_dir: Path, approved: str, *, runtime: Any = None) -> ApplyResult:
    """The one registry write of a batch: recompute, require equality with the reviewed transaction, one slot write."""
    out_dir = Path(out_dir)
    s.require(isinstance(approved, str) and a.HEX.fullmatch(approved) is not None, "apply needs --approved with the sha256 the reviewer approved")
    s.require((out_dir / ARGS_FILE).is_file(), f"no {ARGS_FILE} in {out_dir}: run `sm head prepare --packets ... --out {out_dir}` first")
    args = a._json_bytes(a._stable_file(out_dir / ARGS_FILE, 1024 * 1024), "stored prepare arguments")
    approved_doc = a._json_bytes(a._stable_file(out_dir / PREPARED_FILE, 1024 * 1024), "prepared transaction")
    s.require(canonical_sha256(approved_doc) == approved,
              "the approved sha256 is not the digest of the stored prepared transaction; reprepare and review")
    check_stored_batch_args(args, approved_doc, approved)
    entries = [(Path(path), Path(core), pr) for path, core, pr in zip(args["packets"], args["core_repos"], args["prs"], strict=True)]
    for (_, core, _), member in zip(entries, approved_doc["members"], strict=True):
        check_worktree_head(core, member["head"])
    receipts = _receipts(Path(args["nonconsumers"]) if args.get("nonconsumers") else None)
    source, workspaces = None, []
    try:
        rt = runtime or s.Runtime(entries[0][1])
        prepared, members, source, workspaces, proofs = s.prepare_batch(rt, entries, Path(args["review_root"]), receipts)
        s.require(approved_doc == prepared, "prepared transaction changed; reprepare and review")
        receipt = s.apply_batch(rt, prepared, members, source, proofs, approved)
    finally:
        _cleanup_batch(source, workspaces)
    return ApplyResult(receipt_path=str(receipt), sha256=approved, head=prepared["members"][0]["head"])


def first_merge_only(prepared: dict[str, Any], current_main: str) -> None:
    """The batch rule as a check: plans of a batch are valid while main is the main they were prepared on.

    A batch admits, for every member, the pair (PR plan, resulting-main plan) for THIS main. The first merge moves main;
    the resulting tree of every other member is then another tree, without a plan, and its merge turns main red
    (2026-11-12). For whoever merges: refuse a member of a batch once main moved, and name the way on.
    """
    s.require(prepared.get("mode") == s.BATCH_MODE, "not a batch transaction")
    s.require(current_main == prepared.get("main"),
              f"main moved ({str(current_main)[:8]}) since this batch was prepared on {str(prepared.get('main'))[:8]}: its remaining plans are void. "
              "Do not merge another member on them; bind each remaining head to the new main (`sm head packet bind`) and admit it on its own")
