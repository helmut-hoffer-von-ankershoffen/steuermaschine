"""Typed entry of packet building: what the native commands call.

The checks and their order are those of the moved modules (`focused_manifest`, `wave_size`, `floor_check`); this
module only turns their inputs and outputs into typed values. Nothing here writes outside this machine: building a
packet writes files under the given output path, everything else reads.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

from steuermaschine.models import Model

from . import floor_check as fc
from . import focused_manifest as fm
from . import wave_size as ws

MANIFEST_ERRORS = (fm.ManifestError, OSError, ValueError, KeyError, ET.ParseError)


class PacketRefused(Exception):
    """The moved code refused; `summary` is what it had established up to that point."""

    def __init__(self, message: str, summary: list[str] | None = None):
        super().__init__(message)
        self.summary = summary or []


# ----------------------------------------------------------------------------- head manifest
class ManifestResult(Model):
    """One packet built, or one packet compared with its regeneration."""

    status: str  # built | byte_equal | different
    head: str
    base: str
    tree: str
    packet_sha256: str  # of the packet file's bytes
    manifest_sha256: str
    main_manifest_sha256: str
    review_path: str = ""
    review_sha256: str = ""
    output: str = ""
    layout: str = "indent"
    summary: list[str] = []
    differences: list[str] = []

    @property
    def exit_code(self) -> int:
        return 1 if self.status == "different" else 0

    def trailer(self) -> list[str]:
        if self.status == "built":
            return [f"wrote {self.output} sha256 {self.packet_sha256}"]
        if self.status == "byte_equal":
            return [f"BYTE-EQUAL sha256 {self.packet_sha256}"]
        return list(self.differences)

    def report(self) -> str:
        """The reviewer's report: the summary and the outcome line(s), one text ending in a newline."""
        return "\n".join(["\n".join(self.summary), *self.trailer()]) + "\n"

    def render(self) -> list[str]:
        return [*self.summary, *self.trailer()]


def _result(status: str, packet: dict[str, Any], data: bytes, summary: list[str], layout: str, output: str = "", differences: list[str] | None = None) -> ManifestResult:
    plan, binding = packet["manifest"], packet["review"]
    return ManifestResult(status=status, head=plan["head"], base=plan["base"], tree=plan["tree"], packet_sha256=fm.sha256(data),
                          manifest_sha256=binding["manifest_sha256"], main_manifest_sha256=binding["main_manifest_sha256"],
                          review_path=binding.get("path", ""), review_sha256=binding.get("sha256", ""), output=output, layout=layout,
                          summary=summary, differences=differences or [])


TRUSTED_MAIN = "origin/main"  # as floor_check names it


def trusted_base(repo: fm.Repo, base: str) -> str:
    """BASE as a commit whose scripts are main's. The packet runs the base's own scripts (affected.py, focused_plan.py
    and its siblings), so the base must be origin/main or an ancestor of it (the rule of floor_check), or a predicted
    main (`head packet predict`) whose scripts/ tree is byte-equal to that of its fork point on origin/main. Anything
    else is a ManifestError, raised before any base script runs."""
    commit = repo.commit(base)
    fm.require(repo.git("rev-parse", "--verify", "--quiet", f"{TRUSTED_MAIN}^{{commit}}", check=False).returncode == 0,
               "cannot read origin/main in the Core repository (git fetch origin main): a packet base must be on main")
    if repo.git("merge-base", "--is-ancestor", commit, TRUSTED_MAIN, check=False).returncode == 0:
        return commit
    fork = repo.git("merge-base", commit, TRUSTED_MAIN, check=False).stdout.decode().strip()

    def scripts(rev: str) -> str:
        return str(repo.git("rev-parse", "--verify", "--quiet", f"{rev}:scripts", check=False).stdout.decode().strip())

    fm.require(bool(fork) and bool(scripts(commit)) and scripts(commit) == scripts(fork),
               f"packet base {commit[:12]} is neither origin/main nor an ancestor of it, and its scripts/ differ from main's: "
               "its scripts would run; fetch origin main in the Core repository if main moved, else re-admit on current main")
    return commit


def build_packet(core_repo: Path, target: str, base: str, output: Path, *, selection: Path | None = None, junit: list[str] | None = None,
                 production: list[str] | None = None, review: str | None = None, review_root: Path | None = None, layout: str = "indent") -> ManifestResult:
    """Build one packet from Git objects and write it. `target` is a branch or commit present in `core_repo`."""
    if layout not in fm.FORMATS:
        raise PacketRefused(f"unknown layout {layout}: one of {', '.join(fm.FORMATS)}")
    if review is not None and review_root is None:
        raise PacketRefused("--review needs --review-root (the directory the review path is relative to)")
    repo = fm.Repo(Path(core_repo).expanduser())
    summary: list[str] = []
    try:
        declared = json.loads(Path(selection).read_bytes()) if selection else None
        roots = [Path(review_root).expanduser()] if review_root else []
        packet = fm.build(repo, repo.commit(target), trusted_base(repo, base), selection=declared, cases={}, junit=fm.read_junit(junit or []),
                          extra_production=list(production or []), review=fm.read_review(review, roots), summary=summary)
        data = fm.packet_bytes(packet, layout)
        Path(output).write_bytes(data)
    except MANIFEST_ERRORS as error:
        raise PacketRefused(str(error), summary) from error
    return _result("built", packet, data, summary, layout, str(output))


def verify_packet(core_repo: Path, packet_path: Path, *, junit: list[str] | None = None, review_root: Path | None = None) -> ManifestResult:
    """Regenerate a packet from its own head and base and compare bytes. Reads only."""
    repo = fm.Repo(Path(core_repo).expanduser())
    summary: list[str] = []
    try:
        original = Path(packet_path).read_bytes()
        theirs = json.loads(original)
        plan = theirs["manifest"]
        fm.require(set(plan) <= fm.MANIFEST_KEYS, f"packet manifest has unsupported fields: {sorted(set(plan) - fm.MANIFEST_KEYS)}")
        changed_python = {p for p, s in plan.get("changed", {}).items() if fm.production_file(p) and s != "D"}
        roots = [Path(review_root).expanduser()] if review_root else [Path(packet_path).resolve().parent]
        packet = fm.build(repo, repo.commit(plan["head"]), trusted_base(repo, plan["base"]), selection=plan["selection"], cases=plan["case_sha256"],
                          junit=fm.read_junit(junit or []), extra_production=sorted(set(plan["production"]) - changed_python),
                          review=fm.read_review(theirs["review"].get("path"), roots), summary=summary)
    except MANIFEST_ERRORS as error:
        raise PacketRefused(str(error), summary) from error
    layout = fm.layout_of(original)
    data = fm.packet_bytes(packet, layout)
    if data == original:
        return _result("byte_equal", packet, data, summary, layout, str(packet_path))
    differences = [f"DIFFERENT {part}.{key}" for part in ("manifest", "main_manifest", "review")
                   for key in sorted(set(packet[part]) | set(theirs.get(part, {}))) if packet[part].get(key) != theirs.get(part, {}).get(key)]
    if json.loads(data) == theirs:
        differences.append("DIFFERENT formatting only (same JSON value)")
    return _result("different", packet, data, summary, layout, str(packet_path), differences)


# ----------------------------------------------------------------------------- head size
class BranchSize(Model):
    branch: str
    head: str
    conflict: str = ""
    files: int
    selection: dict[str, list[str]]
    production: list[str]
    protected: list[str]
    reasons: list[str] = []


class Overlap(Model):
    first: str
    second: str
    files: list[str]


class SizeResult(Model):
    """Whether one wave of heads fits the focused admission limits."""

    fits: bool
    base: str
    branches: list[BranchSize]
    union: dict[str, int]  # test ids per lane
    limits: dict[str, int]  # the limit of each lane in `union`, and of "production"
    production: int
    overlaps: list[Overlap] = []
    conflicts: list[list[str]] = []
    problems: list[str] = []
    move: str = ""  # the branch to move to the next wave
    lines: list[str] = []

    @property
    def exit_code(self) -> int:
        return 0 if self.fits else 1

    def render(self) -> list[str]:
        return list(self.lines)


def size_wave(specs: list[str], core: Path, base: str, gather: Callable[..., dict[str, Any]] = ws.facts_for,
              conflicts: Callable[..., list[tuple[str, str]]] = ws.pair_conflicts) -> SizeResult:
    """`wave_size.run`, returning what it printed together with the facts behind it."""
    repo_path = Path(core).expanduser()
    try:
        repos = []
        for spec in specs:
            path, _, branch = spec.rpartition(":")
            repos.append((Path(path).expanduser() if path else repo_path, branch or spec))
        repo = fm.Repo(repos[0][0])
        base = repo.commit(base)
        policy = fm.Policy(repo, base)
        limits = ws.limits_of(policy)
        facts = [gather(fm.Repo(p), base, b, policy) for p, b in repos]
        pairs = conflicts(repo, facts)
    except (fm.ManifestError, OSError, ValueError) as error:
        raise PacketRefused(str(error)) from error
    problems, over, lanes, prod = ws.evaluate(facts, limits)
    problems += [f"conflict {a} x {b}" for a, b in pairs]
    lines = ws.report(facts, pairs, limits)
    move = ""
    if not problems:
        lines.append("FITS")
    else:
        lines.append("REFUSED: " + "; ".join(problems))
        move = (ws.to_move(facts, limits) if over else (pairs[0][1] if pairs else None)) or ""
        if move:
            lines.append(f"MOVE {move} to the next wave")
    return SizeResult(
        fits=not problems, base=base, production=len(prod), problems=problems, move=move, lines=lines,
        branches=[BranchSize(branch=f["branch"], head=f["head"], conflict=f["conflict"], files=len(f["changed"]), selection=f["selection"],
                             production=f["production"], protected=f["protected"], reasons=list(f.get("reasons") or [])) for f in facts],
        union={lane: len(ids) for lane, ids in sorted(lanes.items())},
        limits={**{lane: ws.lane_limit(lane, limits) for lane in sorted(lanes)}, "production": limits["production"]},
        overlaps=[Overlap(first=a, second=b, files=files) for (a, b), files in ws.overlaps(facts).items()],
        conflicts=[[a, b] for a, b in pairs])


# ----------------------------------------------------------------------------- head floor
class FloorResult(Model):
    """Whether an admitted head is ready for the merge gate."""

    ready: bool
    head: str
    main: str
    pr: int | None = None
    reasons: list[str] = []

    @property
    def exit_code(self) -> int:
        return 0 if self.ready else 1

    def render(self) -> list[str]:
        return ["READY" if self.ready else "NOT READY: " + "; ".join(self.reasons)]


def floor(target: str, core: Path, repo: str, base: str, *, git: Callable[[list[str]], str], gh: Callable[[list[str]], str],
          decode: Callable[..., Any] | None = None) -> FloorResult:
    """`floor_check.gather` and `evaluate`. `git` and `gh` take the full argv and return stdout, raising `Unreadable`."""
    try:
        facts = fc.gather(target, str(Path(core).expanduser()), repo, base, git, gh, decode or fc.decode_plan)
    except (fc.Unreadable, ValueError, KeyError, OSError, fc.subprocess.SubprocessError) as error:  # type: ignore[attr-defined]  # the plan decoder's own process
        raise PacketRefused(str(error)) from error
    reasons = fc.evaluate(facts)
    return FloorResult(ready=not reasons, head=facts["head"], main=facts["main"], pr=(facts.get("pr") or {}).get("number"), reasons=reasons)
