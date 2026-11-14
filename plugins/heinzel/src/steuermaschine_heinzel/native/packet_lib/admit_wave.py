#!/usr/bin/env python3
"""hz-admit-wave: build reviewed focused-admission packets for one wave of Core heads. Builder tooling only.

    hz-admit-wave preopen BRANCH --main SHA  --core-repo GIT --worktree WT --out DIR --review-root ROOT INPUTS
    hz-admit-wave predict BRANCH --after DIR [--after DIR ...] --core-repo GIT --worktree WT --out DIR
                          --review-root ROOT INPUTS
    hz-admit-wave bind    PREDICT_DIR --main SHA [--out DIR]
    hz-admit-wave wave    DIR DIR [DIR ...]
    INPUTS = (--selection SEL.json | --reviewed-packet PACKET) --junit LANE=FILE ... [--production PATH ...]
             [--members TEXT] [--notes FILE] [--collector PY] [--python PY] [--node-modules DIR] [--gate CMD]

preopen  Pre-open pair for BRANCH at the exact main SHA: resulting tree = git merge-tree(main, head); the
         cases of every selected lane are COLLECTED (collect_cases.py) on the head tree, which must reproduce
         the local focused JUnit (--junit), and on the resulting tree (a difference is recorded as
         TREE-SCANNING; the packet always uses the resulting tree's collection, never a carried digest);
         hz-focused-manifest builds the pair, a builder REVIEW.md is written, the pair is rebuilt with
         --review and checked with --verify. Prints the single read-only `hz-focused-admit prepare`.
predict  The same for a LATER head against the PREDICTED main after the earlier head(s) of --after (preopen or
         bound dirs at one main) squash-merge in the given order: predicted main = commit-tree(merge-tree(main,
         earlier head), parent main), chained. Tree-equality checks: each predicted squash tree equals the
         earlier packet's resulting tree. The packet binds the predicted commit, so it is NOT admissible and
         nothing to prepare is printed; it is for review while the earlier heads are in CI.
bind     After the earlier head(s) merged and the real main SHA is in --core-repo (this tool never fetches):
         rebinds the predicted packet to the real main only if main's first-parent chain back to the predicted
         parent has exactly one single-parent commit per earlier head, main's tree is byte-equal to the
         predicted main tree, and git merge-tree(real main, head) is byte-equal to the predicted resulting
         tree. Otherwise REFUSED: re-prepare with `preopen`. The rebuilt packet must equal the predicted one in
         every manifest field except base. Prints the single read-only prepare.
wave     Checks 2..20 preopen/bound dirs share one main, one review root and distinct heads and checkouts, and
         prints ONE batch `hz-focused-admit prepare --packets ... --core-repos ...`.

Every output directory must be new or empty and inside --review-root. Never applies, pushes, queues, fetches,
calls GitHub or writes the focused registry; the only Git writes are the predicted commit objects and the scratch refs
it creates in --core-repo (use a scratch clone). Inherited GIT_* variables are ignored.
The independent review of the scope and of the exact prepared JSON (--approved-sha256) stays mandatory.
Exit 0 ok, 1 refused, 2 usage. Structured events admit_wave.start / admit_wave.outcome go to stderr.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from . import ops
from steuermaschine.config import child_environment

PREPARE = ("sm", "head", "prepare")  # the command a built packet goes to next
BUILDER = "sm head packet"  # named in the title of the builder's REVIEW.md
DEFAULT_COLLECTOR = Path(__file__).with_name("collect_cases.py")
SHA = re.compile(r"[0-9a-f]{40}")
LANE = re.compile(r"[a-z][a-z0-9-]{0,30}")
RECORD = "wave.json"
PREDICTED_REFS = "refs/sm-wave/predicted"
LIVE_CORE = Path.home() / "Code/heinzel"  # the shared Core repository; predict writes objects, so never there
MAX_BATCH = 20  # hz_focused_slots.MAX_BATCH; the batch path re-checks it
# Hashed Git data, kept byte for byte: a predicted commit's identity and message and its scratch ref prefix decide its
# SHA, which recorded predict packets bind and by which bind and preopen recognise a predicted commit.
SIM_ENV = {"GIT_AUTHOR_NAME": "sm head packet", "GIT_AUTHOR_EMAIL": "sm-admit-wave@invalid",
           "GIT_COMMITTER_NAME": "sm head packet", "GIT_COMMITTER_EMAIL": "sm-admit-wave@invalid",
           "GIT_AUTHOR_DATE": "1700000000 +0000", "GIT_COMMITTER_DATE": "1700000000 +0000"}


class Refused(Exception):
    pass


def require(condition: object, message: str) -> None:
    if not condition:
        raise Refused(message)


def say(message: str) -> None:
    print(f"== {message}", file=sys.stderr)


def event(name: str, **fields: Any) -> None:
    print(json.dumps({"event": name, **fields}, sort_keys=True), file=sys.stderr)


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """No inherited GIT_DIR/GIT_INDEX_FILE/... (a pre-push hook may export them)."""
    return child_environment(drop_prefixes=("GIT_",)) | (extra or {})


def git(repo: Path, *args: str, check: bool = True, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=clean_env(extra),
                          timeout=300)
    if check and done.returncode != 0:
        raise Refused(f"git {' '.join(args[:3])} failed in {repo}: {done.stderr.strip()[-300:]}")
    return done


def out(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.strip()


def commit_of(repo: Path, rev: str) -> str:
    done = git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}", check=False)
    require(done.returncode == 0, f"{rev} is not a commit in {repo} (this tool never fetches)")
    return done.stdout.strip()


def exact_commit(repo: Path, sha: str, label: str) -> str:
    require(SHA.fullmatch(sha or ""), f"{label} must be a full 40-hex commit SHA, got {sha!r}")
    require(commit_of(repo, sha) == sha, f"{label} {sha} is not a commit in {repo}")
    return sha


def tree_of(repo: Path, commit: str) -> str:
    return out(repo, "rev-parse", f"{commit}^{{tree}}")


def merge_tree(repo: Path, base: str, head: str) -> str:
    done = git(repo, "merge-tree", "--write-tree", "--no-messages", base, head, check=False)
    require(done.returncode != 1, f"{head[:12]} conflicts with {base[:12]}: resolve and re-run")
    require(done.returncode == 0, f"git merge-tree failed: {done.stderr.strip()[-300:]}")
    return done.stdout.split("\n", 1)[0].strip()


def check_worktree(worktree: Path, head: str) -> None:
    done = git(worktree, "rev-parse", "HEAD", check=False)
    require(done.returncode == 0 and done.stdout.strip() == head, f"{worktree} is not at the head {head}")
    require(git(worktree, "status", "--porcelain").stdout.strip() == "", f"{worktree} is not clean")


def refuse_predicted(repo: Path, sha: str, known: set[str]) -> None:
    """A predicted commit (or anything built on one) is never a real main."""
    require(sha not in known, f"{sha[:12]} is a predicted commit, not a real main")
    merged = out(repo, "for-each-ref", "--format=%(objectname)", "--merged", sha, PREDICTED_REFS)
    require(not merged, f"{sha[:12]} is or descends from a predicted commit ({merged[:12]}), not a real main")
    identity = out(repo, "log", "-1", "--format=%ae%n%ce", sha).split()
    require(SIM_ENV["GIT_AUTHOR_EMAIL"] not in identity, f"{sha[:12]} was written by this tool's prediction, not a real main")


def same_repo(a: Path, b: Path) -> bool:
    if not b.exists():
        return False
    if a.resolve() == b.resolve():
        return True
    common = [git(r, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False).stdout.strip() for r in (a, b)]
    return bool(common[0]) and common[0] == common[1]


def check_junit(lane: str, path: Path) -> None:
    """The manifest only sees the synthetic collected XML, so the local run's outcome is checked here."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as error:
        raise Refused(f"{lane} JUnit {path} is not readable XML: {error}") from error
    cases = list(root.iter("testcase"))
    require(cases, f"{lane} JUnit {path} has no testcases")
    bad = [f"{c.get('classname')}::{c.get('name')} {child.tag}" for c in cases for child in c
           if child.tag in ("failure", "error", "skipped")]
    require(not bad, f"{lane} JUnit {path} is not a green run ({len(bad)} failed/errored/skipped): {'; '.join(bad[:3])}")


def toolchain(collector: Path, python: str | None) -> dict[str, str]:
    """What produced the collected cases; bind refuses when it changed."""
    version = sys.version.split()[0]
    if python:
        done = subprocess.run([python, "-c", "import sys; print(sys.version.split()[0])"], capture_output=True,
                              text=True, env=clean_env(), timeout=60)
        require(done.returncode == 0, f"cannot run --python {python}")
        version = done.stdout.strip()
    return {"collector_sha256": file_sha(collector), "python_version": version}


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise Refused(f"cannot read {label} {path}: {error}") from error


def fresh_dir(path: Path, review_root: Path) -> Path:
    root = review_root.expanduser().resolve(strict=True)
    path = path.expanduser().resolve()
    require(path.is_relative_to(root) and path != root, f"output {path} must be inside --review-root {root}")
    require(not path.exists() or (path.is_dir() and not any(path.iterdir())),
            f"output {path} exists and is not empty: use a new directory (reviewed artefacts are never overwritten)")
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_record(directory: Path) -> dict[str, Any]:
    directory = directory.expanduser().resolve()
    record = load_json(directory / RECORD, "wave record")
    require(isinstance(record, dict) and record.get("schema") == 1, f"{directory / RECORD} is not a wave record")
    packet = directory / "packet.json"
    require(packet.is_file() and file_sha(packet) == record.get("packet_sha256"),
            f"{packet} changed since it was built: re-prepare")
    record["dir"] = directory
    return record


# -- inputs ---------------------------------------------------------------------------------------------------

class Inputs:
    """Reviewed scope plus local evidence for one head (from argparse)."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.core = args.core_repo.expanduser().resolve()
        self.worktree = args.worktree.expanduser().resolve()
        self.review_root = args.review_root.expanduser().resolve()
        require(git(self.core, "rev-parse", "--git-dir", check=False).returncode == 0, f"{self.core} is not a Git repository")
        require(bool(args.selection) != bool(args.reviewed_packet), "give exactly one of --selection or --reviewed-packet")
        self.reviewed = None
        if args.reviewed_packet:
            self.reviewed = load_json(args.reviewed_packet.expanduser(), "reviewed packet")
            manifest = self.reviewed.get("manifest") or {}
            self.selection = manifest.get("selection")
            require(not args.production, "--production comes from the --reviewed-packet")
            self.production = sorted(set(manifest.get("production", {})) - set(manifest.get("changed", {})))
        else:
            self.selection = load_json(args.selection.expanduser(), "selection")
            if isinstance(self.selection, dict):  # an empty lane list means no such lane (sm head select writes "python": [])
                self.selection = {k: v for k, v in self.selection.items() if v != []}
            self.production = list(args.production)
        require(isinstance(self.selection, dict) and self.selection
                and all(LANE.fullmatch(k) and isinstance(v, list) and v and all(isinstance(t, str) for t in v)
                        for k, v in self.selection.items()), "selection must be a non-empty {lane: [test ids]}")
        self.junit: dict[str, Path] = {}
        for value in args.junit:
            lane, sep, path = value.partition("=")
            require(sep and lane and path and lane not in self.junit, f"--junit expects one LANE=FILE per lane: {value}")
            self.junit[lane] = Path(path).expanduser().resolve()
            require(self.junit[lane].is_file() and self.junit[lane].stat().st_size > 0, f"no local {lane} JUnit at {path}")
            check_junit(lane, self.junit[lane])
        require(set(self.junit) == set(self.selection),
                f"--junit lanes {sorted(self.junit)} != selected lanes {sorted(self.selection)}")
        self.collector = args.collector.expanduser().resolve()
        require(self.collector.is_file(), f"collector {self.collector} not found (--collector)")
        self.python = args.python or (str(self.worktree / ".venv/bin/python")
                                      if (self.worktree / ".venv/bin/python").exists() else None)
        nm = args.node_modules or (self.worktree / "web/node_modules")
        self.node_modules = nm.expanduser() if nm.exists() else None
        self.run_collect = getattr(args, "run_collect", None) or run_direct
        self.members = args.members or ""
        self.notes = args.notes.expanduser().read_text() if args.notes else ""


def run_direct(cmd: list[str]) -> subprocess.CompletedProcess:
    """One collection run, not under the run lock (the native commands pass a runner that takes the lock)."""
    return subprocess.run(cmd, capture_output=True, text=True, env=clean_env(), timeout=1800)


def collect_lanes(inp: Inputs, out_dir: Path, head_tree: str, result_tree: str) -> tuple[list[str], dict[str, Path]]:
    sel = out_dir / "sel.json"
    sel.write_text(json.dumps(inp.selection, indent=1, sort_keys=True) + "\n")
    lines, junit = [], {}
    work = Path(tempfile.mkdtemp(prefix="packet-collect-"))  # the trees are extracted here, never beside reviewed files
    try:
        lines, junit = _collect_lanes(inp, out_dir, head_tree, result_tree, sel, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    (out_dir / "collected.txt").write_text("\n".join(lines) + "\n")
    return lines, junit


def _collect_lanes(inp: Inputs, out_dir: Path, head_tree: str, result_tree: str, sel: Path, work: Path) -> tuple[list[str], dict[str, Path]]:
    lines, junit = [], {}
    for lane in sorted(inp.selection):
        for label, tree in (("pr", head_tree), ("main", result_tree)):
            cmd = [sys.executable, "-I", str(inp.collector), "collect", "--repo", str(inp.core), "--tree", tree,
                   "--selection", str(sel), "--lane", lane, "-o", str(out_dir / f"{lane}.{label}-collected.xml")]
            if inp.python:
                cmd += ["--python", inp.python]
            if inp.node_modules:
                cmd += ["--node-modules", str(inp.node_modules)]
            if inp.collector == DEFAULT_COLLECTOR:
                cmd += ["--work", str(work)]
            done = inp.run_collect(cmd)
            with (out_dir / "collect.err").open("a") as err:
                err.write(done.stdout + done.stderr)
            require(done.returncode == 0, f"collect {lane} on the {label} tree failed: {done.stderr.strip()[-300:]}")
        diff = out_dir / f"{lane}.case-diff.txt"
        done = subprocess.run([sys.executable, "-I", str(inp.collector), "compare", "--hosted", str(inp.junit[lane]),
                               "--pr", str(out_dir / f"{lane}.pr-collected.xml"),
                               "--result", str(out_dir / f"{lane}.main-collected.xml"), "-o", str(diff)],
                              capture_output=True, text=True, env=clean_env(), timeout=300)
        ids = diff.read_text().splitlines() if diff.exists() else []
        if done.returncode == 0:
            lines.append(f"{lane}: collected head tree == local focused JUnit; resulting tree identical")
        elif done.returncode == 3:
            lines.append(f"{lane}: collected head tree == local focused JUnit; TREE-SCANNING: resulting tree differs "
                         f"({len(ids)} ids), the packet uses the resulting tree's collected cases")
            lines += [f"  {line}" for line in ids]
        else:
            raise Refused(f"{lane}: the head-tree collection does not reproduce the local focused JUnit (see {diff})")
        junit[lane] = out_dir / f"{lane}.main-collected.xml"
    return lines, junit


def manifest(core: Path, head: str, base: str, inp_selection: Path, junit: dict[str, Path], production: list[str],
             output: Path, err: Path, review: tuple[str, Path] | None = None) -> dict[str, Any]:
    try:
        built = ops.build_packet(core, head, base, output, selection=inp_selection,
                                 junit=[f"{lane}={path}" for lane, path in sorted(junit.items())], production=production,
                                 review=review[0] if review else None, review_root=review[1] if review else None)
    except ops.PacketRefused as error:
        text = "\n".join([*error.summary, f"REFUSED: {error}"])
        err.write_text(text + "\n")
        raise Refused(f"manifest refused: {text.strip()[-400:]}") from error
    err.write_text(built.report())
    return load_json(output, "packet")


def verify(core: Path, packet: Path, junit: dict[str, Path], review_root: Path, err: Path) -> None:
    try:
        checked = ops.verify_packet(core, packet, junit=[f"{lane}={path}" for lane, path in sorted(junit.items())], review_root=review_root)
    except ops.PacketRefused as error:
        err.write_text("\n".join([*error.summary, f"REFUSED: {error}"]) + "\n")
        raise Refused(f"--verify failed: {error}") from error
    err.write_text(checked.report())
    require(checked.status == "byte_equal", f"--verify failed: {checked.report().strip()[-300:]}")


def check_packet(packet: dict[str, Any], head: str, base: str, tree: str, reviewed: dict | None) -> None:
    pr, main = packet.get("manifest", {}), packet.get("main_manifest", {})
    require(pr.get("head") == head and pr.get("base") == base and main.get("base") == base,
            "packet does not bind the requested head and base")
    require(pr.get("tree") == tree and main.get("tree") == tree, f"packet tree != resulting tree {tree}")
    if reviewed is not None:
        old = reviewed["manifest"]
        require(pr["selection"] == old["selection"] and sorted(pr["production"]) == sorted(old["production"])
                and pr["mode"] == old["mode"], "regenerated scope differs from the --reviewed-packet (selection, "
                "production or mode): a new scope needs a new review")


def review_text(title: str, facts: list[str], sections: list[tuple[str, str]], inp: Inputs) -> str:
    lines = [f"# {title} (builder-prepared by {BUILDER}; NOT the independent review)", ""]
    if inp.members:
        lines += [f"Members: {inp.members}", ""]
    lines += [f"- {fact}" for fact in facts]
    for heading, body in sections:
        lines += ["", f"## {heading}", "```", body.rstrip("\n"), "```"]
    if inp.notes:
        lines += ["", "## Notes for the independent reviewer", inp.notes.rstrip("\n")]
    return "\n".join(lines) + "\n"


def overlap_lines(core: Path, inp: Inputs, packet: dict[str, Any], base: str) -> str:
    lines = []
    if inp.reviewed is not None:
        old, new = inp.reviewed["manifest"]["production"], packet["manifest"]["production"]
        lines += [f"MERGED BLOB (main and head both edit; review the merged content): {p} {new[p]}"
                  for p in sorted(new) if old.get(p) != new[p]]
        old_base = inp.reviewed["manifest"].get("base")
        if old_base and SHA.fullmatch(old_base) and git(core, "cat-file", "-e", f"{old_base}^{{commit}}", check=False).returncode == 0:
            delta = set(out(core, "diff", "--name-only", "--no-renames", old_base, base).split())
            lines += [f"SELECTED TEST CHANGED ON MAIN: {p}" for p in sorted(delta & {t for ts in inp.selection.values() for t in ts})]
    return "\n".join(lines) or "none"


def build(inp: Inputs, kind: str, branch: str, head: str, base: str, result_tree: str, out_dir: Path,
          facts: list[str], extra_sections: list[tuple[str, str]], extra_record: dict[str, Any]) -> dict[str, Any]:
    check_worktree(inp.worktree, head)
    lines, junit = collect_lanes(inp, out_dir, tree_of(inp.core, head), result_tree)
    say("; ".join(lines)[:400])
    packet_path, sel = out_dir / "packet.json", out_dir / "sel.json"
    packet = manifest(inp.core, head, base, sel, junit, inp.production, packet_path, out_dir / "manifest.err")
    check_packet(packet, head, base, result_tree, inp.reviewed)
    rel = str((out_dir / "REVIEW.md").relative_to(inp.review_root))
    sections = [*extra_sections, ("Overlap with main", overlap_lines(inp.core, inp, packet, base)),
                ("Case collection", "\n".join(lines)), ("Manifest summary", (out_dir / "manifest.err").read_text())]
    (out_dir / "REVIEW.md").write_text(review_text(f"{kind} pair: {branch} head {head[:12]} onto {base[:12]}",
                                                   facts, sections, inp))
    with discard_on_refusal(out_dir):
        packet = manifest(inp.core, head, base, sel, junit, inp.production, packet_path, out_dir / "manifest.final.err",
                          (rel, inp.review_root))
        check_packet(packet, head, base, result_tree, inp.reviewed)
        verify(inp.core, packet_path, junit, inp.review_root, out_dir / "verify.err")
    record = dict(schema=1, kind=kind, branch=branch, head=head, main=base, main_tree=tree_of(inp.core, base),
                  tree=result_tree, core_repo=str(inp.core), worktree=str(inp.worktree),
                  review_root=str(inp.review_root), review=rel, lanes=sorted(junit),
                  packet_sha256=file_sha(packet_path), manifest_sha256=packet["review"]["manifest_sha256"],
                  collector=str(inp.collector), python=inp.python) | toolchain(inp.collector, inp.python) | extra_record
    (out_dir / RECORD).write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    say(f"{kind} packet {packet_path} sha256 {record['packet_sha256']} tree {result_tree}")
    return record


@contextlib.contextmanager
def discard_on_refusal(out_dir: Path):
    """A refused build leaves no REVIEW.md or packet.json that could be mistaken for a reviewable pair."""
    try:
        yield
    except BaseException:
        for name in ("REVIEW.md", "packet.json"):
            (out_dir / name).unlink(missing_ok=True)
        raise


def prepare_command(packet: Path, worktree: str, review_root: str, args: argparse.Namespace) -> str:
    cmd = [*PREPARE, str(packet), "--core-repo", worktree, "--review-root", review_root]
    if getattr(args, "reviewed_protected_producer", False):
        cmd.append("--reviewed-protected-producer")
    if getattr(args, "nonconsumers", None):
        cmd += ["--nonconsumers", str(args.nonconsumers.expanduser().resolve())]
    return shlex.join(cmd)


# -- subcommands ----------------------------------------------------------------------------------------------

def cmd_preopen(args: argparse.Namespace) -> dict[str, Any]:
    inp = Inputs(args)
    main = exact_commit(inp.core, args.main, "--main")
    refuse_predicted(inp.core, main, set())
    head = commit_of(inp.core, args.branch)
    out_dir = fresh_dir(args.out, inp.review_root)
    result = merge_tree(inp.core, main, head)
    say(f"main {main} head {head} resulting tree {result}")
    record = build(inp, "preopen", args.branch, head, main, result, out_dir,
                   [f"head `{head}`, base `{main}` (given main), tree `{result}` (git merge-tree)",
                    "cases COLLECTED on the resulting tree (no carried digest); the head-tree collection reproduces "
                    "the local focused JUnit"], [], {})
    print(prepare_command(out_dir / "packet.json", record["worktree"], record["review_root"], args))
    return record


def cmd_predict(args: argparse.Namespace) -> dict[str, Any]:
    inp = Inputs(args)
    live = getattr(args, "live_core", None) or LIVE_CORE
    require(not same_repo(inp.core, live), f"predict writes commit objects and refs: --core-repo must be a scratch "
            f"clone, not {live}")
    require(args.after, "predict needs --after DIR for each earlier head, in merge order")
    earlier = [read_record(d) for d in args.after]
    require(all(r["kind"] in ("preopen", "bound") for r in earlier),
            "--after takes preopen or bound dirs (a predicted base is not a real main)")
    main = earlier[0]["main"]
    require(all(r["main"] == main for r in earlier), "--after dirs bind different mains: one wave, one main")
    exact_commit(inp.core, main, "earlier main")
    refuse_predicted(inp.core, main, set())
    head = commit_of(inp.core, args.branch)
    require(head not in {r["head"] for r in earlier} and len({r["head"] for r in earlier}) == len(earlier),
            "the later head and every earlier head must be distinct")
    out_dir = fresh_dir(args.out, inp.review_root)
    chain, parent = [], main
    for record in earlier:
        exact_commit(inp.core, record["head"], "earlier head")
        squash_tree = merge_tree(inp.core, parent, record["head"])
        if parent == main:
            require(squash_tree == record["tree"], f"tree-equality: merge-tree(main, {record['head'][:12]}) "
                    f"{squash_tree} != its packet tree {record['tree']}")
        sim = git(inp.core, "commit-tree", squash_tree, "-p", parent, "-m",
                  f"sm head packet predicted squash of {record['head']} onto {parent}", extra=SIM_ENV).stdout.strip()
        require(SHA.fullmatch(sim), "commit-tree did not return a commit")
        git(inp.core, "update-ref", f"{PREDICTED_REFS}/{sim}", sim)
        chain.append(dict(head=record["head"], branch=record["branch"], dir=str(record["dir"]), parent=parent,
                          tree=squash_tree, predicted=sim))
        parent = sim
    predicted, predicted_tree = parent, chain[-1]["tree"]
    require(tree_of(inp.core, predicted) == predicted_tree, "tree-equality: predicted main tree mismatch")
    result = merge_tree(inp.core, predicted, head)
    say(f"predicted main {predicted} (tree {predicted_tree}) head {head} resulting tree {result}")
    chain_text = "\n".join(f"{c['predicted']} = squash of {c['branch']} {c['head']} onto {c['parent']}, tree {c['tree']}"
                           for c in chain)
    record = build(inp, "predict", args.branch, head, predicted, result, out_dir,
                   [f"head `{head}`, PREDICTED base `{predicted}` (not a real main; bind before prepare), "
                    f"tree `{result}`",
                    f"real main `{main}`; earlier heads merge in order: {', '.join(c['branch'] for c in chain)}",
                    "cases COLLECTED on the predicted resulting tree"],
                   [("Predicted main chain", chain_text)],
                   dict(parent_main=main, predicted_main=predicted, predicted_main_tree=predicted_tree, after=chain))
    say(f"PREDICTED only: not admissible; after the earlier heads merge run `{BUILDER} bind`")
    return record


def cmd_bind(args: argparse.Namespace) -> dict[str, Any]:
    pred = read_record(args.predict_dir)
    require(pred["kind"] == "predict", f"{pred['dir']} is a {pred['kind']} dir, not a predict dir")
    core, review_root = Path(pred["core_repo"]), Path(pred["review_root"])
    head, parent = pred["head"], pred["parent_main"]
    actual = exact_commit(core, args.main, "--main")
    redo = f"re-prepare with `{BUILDER} preopen`"
    refuse_predicted(core, actual, {pred["predicted_main"], *(c["predicted"] for c in pred["after"])})
    check_worktree(Path(pred["worktree"]), head)
    collector = Path(pred["collector"])
    require(collector.is_file() and toolchain(collector, pred["python"]) == {
        k: pred[k] for k in ("collector_sha256", "python_version")},
        f"the collector or the collection python changed since predict; {redo}")
    require(git(core, "merge-base", "--is-ancestor", parent, actual, check=False).returncode == 0,
            f"REFUSED: main {actual[:12]} does not descend from the predicted parent {parent[:12]}; {redo}")
    c, chain = actual, []
    for _ in pred["after"]:
        parents = out(core, "rev-list", "--parents", "-n1", c).split()[1:]
        require(len(parents) == 1, f"main commit {c[:12]} is not a single-parent squash; {redo}")
        chain.append(c)
        c = parents[0]
    require(c == parent, f"main {actual[:12]} is not exactly {len(pred['after'])} squash commit(s) on "
            f"{parent[:12]} (another commit merged); {redo}")
    actual_tree = tree_of(core, actual)
    require(actual_tree == pred["predicted_main_tree"], f"main tree {actual_tree} != predicted main tree "
            f"{pred['predicted_main_tree']} (the earlier head changed or merged differently); {redo}")
    result = merge_tree(core, actual, head)
    require(result == pred["tree"], f"resulting tree {result} != predicted resulting tree {pred['tree']}; {redo}")
    require(out(core, "merge-base", actual, head) == out(core, "merge-base", pred["predicted_main"], head),
            f"merge base differs from the prediction; {redo}")
    src = pred["dir"]
    out_dir = fresh_dir(args.out or src / f"bound-{actual[:12]}", review_root)
    junit = {}
    for lane in pred["lanes"]:
        target = out_dir / f"{lane}.main-collected.xml"
        shutil.copyfile(src / f"{lane}.main-collected.xml", target)
        junit[lane] = target
    shutil.copyfile(src / "sel.json", out_dir / "sel.json")
    old = load_json(src / "packet.json", "predicted packet")
    production = sorted(set(old["manifest"]["production"]) - set(old["manifest"]["changed"]))
    packet_path = out_dir / "packet.json"
    rel = str((out_dir / "REVIEW.md").relative_to(review_root))
    checks = "\n".join([f"real main {actual} first-parent chain to {parent}: {' '.join(chain)}",
                        f"main tree {actual_tree} == predicted main tree", f"merge-tree(main, head) {result} == predicted",
                        "every manifest field except base equals the predicted packet"])
    body = (src / "REVIEW.md").read_text() + f"\n## Bind to real main {actual}\n```\n{checks}\n```\n" \
        f"Predicted packet {pred['review'].replace('REVIEW.md', 'packet.json')} sha256 {pred['packet_sha256']}; " \
        "cases carried from the predicted resulting-tree collection (same tree).\n"
    with discard_on_refusal(out_dir):
        packet = manifest(core, head, actual, out_dir / "sel.json", junit, production, packet_path,
                          out_dir / "manifest.err")
        check_packet(packet, head, actual, result, None)
        for part in ("manifest", "main_manifest"):
            drift = sorted(k for k in set(packet[part]) | set(old[part])
                           if k != "base" and packet[part].get(k) != old[part].get(k))
            require(not drift, f"rebound {part} differs from the prediction in {drift}; {redo}")
        (out_dir / "REVIEW.md").write_text(body)
        packet = manifest(core, head, actual, out_dir / "sel.json", junit, production, packet_path,
                          out_dir / "manifest.final.err", (rel, review_root))
        check_packet(packet, head, actual, result, None)
        verify(core, packet_path, junit, review_root, out_dir / "verify.err")
    record = {k: v for k, v in pred.items() if k not in ("dir", "parent_main", "predicted_main", "predicted_main_tree", "after")}
    record |= dict(kind="bound", main=actual, main_tree=actual_tree, review=rel, packet_sha256=file_sha(packet_path),
                   manifest_sha256=packet["review"]["manifest_sha256"], predicted_dir=str(src),
                   predicted_packet_sha256=pred["packet_sha256"], predicted_main=pred["predicted_main"])
    (out_dir / RECORD).write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    say(f"BOUND {packet_path} sha256 {record['packet_sha256']} to main {actual}")
    print(prepare_command(packet_path, record["worktree"], record["review_root"], args))
    return record


def cmd_wave(args: argparse.Namespace) -> dict[str, Any]:
    records = [read_record(d) for d in args.dirs]
    require(2 <= len(records) <= MAX_BATCH, f"a wave needs 2..{MAX_BATCH} packet dirs; use the single prepare for one")
    require(all(r["kind"] in ("preopen", "bound") for r in records), "predict dirs are not admissible: bind them first")
    require(len({r["main"] for r in records}) == 1, "packets bind different mains: rebuild them at one main")
    require(len({r["review_root"] for r in records}) == 1, "packets use different review roots")
    require(len({r["head"] for r in records}) == len(records) and len({r["worktree"] for r in records}) == len(records),
            "wave members need distinct heads and distinct Core checkouts")
    for r in records:
        check_worktree(Path(r["worktree"]), r["head"])
    cmd = [*PREPARE, "--packets", *(str(r["dir"] / "packet.json") for r in records),
           "--core-repos", *(r["worktree"] for r in records), "--review-root", records[0]["review_root"]]
    if args.nonconsumers:
        cmd += ["--nonconsumers", str(args.nonconsumers.expanduser().resolve())]
    print(shlex.join(cmd))
    return {"main": records[0]["main"], "pairs": len(records)}


# -- CLI ------------------------------------------------------------------------------------------------------

def _inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("branch", help="branch or commit of the head in --core-repo")
    parser.add_argument("--core-repo", type=Path, required=True, help="Git repo with main and every head (scratch clone)")
    parser.add_argument("--worktree", type=Path, required=True, help="owned clean Core checkout at the head")
    parser.add_argument("--out", type=Path, required=True, help="new packet dir inside --review-root")
    parser.add_argument("--review-root", type=Path, required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--selection", type=Path, help="reviewed {lane: [test ids]} JSON")
    scope.add_argument("--reviewed-packet", type=Path, help="earlier reviewed packet: same selection, production, mode")
    parser.add_argument("--junit", action="append", default=[], metavar="LANE=FILE",
                        help="local focused JUnit of exactly this head, one per selected lane")
    parser.add_argument("--production", action="append", default=[], metavar="PATH", help="declared extra production file")
    parser.add_argument("--members", help="one line for REVIEW.md")
    parser.add_argument("--notes", type=Path, help="notes for the independent reviewer, appended to REVIEW.md")
    parser.add_argument("--collector", type=Path, default=DEFAULT_COLLECTOR, help="case collector (default: the one of this package)")
    parser.add_argument("--python", help="Core venv python for collection (default WORKTREE/.venv/bin/python)")
    parser.add_argument("--node-modules", type=Path, help="web/node_modules for the web lane (default WORKTREE/web/node_modules)")
    parser.add_argument("--nonconsumers", type=Path, help="passed through to the printed prepare command")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="packet", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="operation", required=True)
    pre = sub.add_parser("preopen", help="pre-open pair for BRANCH at an exact main SHA")
    _inputs(pre)
    pre.add_argument("--main", required=True, help="exact 40-hex main SHA the pair binds")
    pre.add_argument("--reviewed-protected-producer", action="store_true", help="passed through to the printed prepare")
    pred = sub.add_parser("predict", help="later head against the predicted main after --after heads merge")
    _inputs(pred)
    pred.add_argument("--after", type=Path, action="append", default=[], help="earlier preopen/bound dir, in merge order")
    bind = sub.add_parser("bind", help="rebind a predict dir to the real main if the trees are byte-equal")
    bind.add_argument("predict_dir", type=Path)
    bind.add_argument("--main", required=True, help="exact 40-hex real main SHA, present in the core repo")
    bind.add_argument("--out", type=Path, help="new dir (default PREDICT_DIR/bound-<main12>)")
    bind.add_argument("--nonconsumers", type=Path, help="passed through to the printed prepare command")
    wave = sub.add_parser("wave", help="print ONE batch prepare for 2..20 preopen/bound dirs at one main")
    wave.add_argument("dirs", type=Path, nargs="+")
    wave.add_argument("--nonconsumers", type=Path, help="passed through to the printed prepare command")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    event("admit_wave.start", operation=args.operation)
    try:
        result = {"preopen": cmd_preopen, "predict": cmd_predict, "bind": cmd_bind, "wave": cmd_wave}[args.operation](args)
    except (Refused, OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, Refused) else f"{type(error).__name__}: {error}"
        print(f"packet: REFUSED: {message}", file=sys.stderr)
        event("admit_wave.outcome", operation=args.operation, result="refused", reason=message[:500])
        return 1
    event("admit_wave.outcome", operation=args.operation, result="success",
          head=result.get("head"), main=result.get("main"), packet_sha256=result.get("packet_sha256"))
    return 0

