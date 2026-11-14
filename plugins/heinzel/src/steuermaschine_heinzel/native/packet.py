"""Native packet building: everything before `sm head prepare`.

`sm head manifest|size|floor`, `sm head cases collect|compare` and `sm head packet preopen|predict|bind|wave` here; the logic is the moved code in packet_lib (tests/packet_verbatim.py prints every
line that differs from the originals). No command writes outside this machine: a packet is a local file.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import typer

from steuermaschine.config import child_environment
from steuermaschine.engine import Refuse
from steuermaschine.github import Gh
from steuermaschine.helpdoc import H
from steuermaschine.native import Group, native, runtime
from steuermaschine.runlock import Request

from .lock import lock_of
from .packet_lib import admit_wave as aw
from .packet_lib import check_ops, ops, wave_ops
from .packet_lib import floor_check as fc
from .packet_lib import focused_manifest as fm

# The packet builders write local files (the packet, the collected cases) that admission binds: one review unit.
PACKET_REVIEW = "head-packet-writes.md"


def _refuse(error: ops.PacketRefused, next_cmd: str) -> NoReturn:
    for line in error.summary:
        runtime().say(line)
    raise Refuse(f"refused: {error}", next_cmd) from error


def _core(core_repo: Path | None) -> Path:
    path = core_repo or runtime().settings.get("core_repo_dir")
    if not path:
        raise Refuse("no Core repository: give --core-repo (or --core), or set core_repo_dir", "sm self config", code=2)
    return Path(str(path)).expanduser()


def _head_of(target: str, core: Path) -> str:
    """A PR number becomes its exact head (one GitHub read); a branch or commit is passed on."""
    if not target.isdigit() or fm.SHA.fullmatch(target):
        return target
    rt = runtime()
    data, why = Gh(rt.providers.commands, repo=str(rt.settings.get("repo") or "")).try_json(["pr", "view", target, "--json", "headRefOid"])
    head = (data or {}).get("headRefOid", "")
    if not fm.SHA.fullmatch(head):
        raise Refuse(f"cannot read the head of PR {target}: {why or 'no exact head in the answer'}", f"sm head manifest BRANCH --core-repo {core}")
    return str(head)


@native("head manifest", writes=True, review=PACKET_REVIEW, replaces=("hz-focused-manifest",), help=H(
    "Build one focused admission packet from Git objects, or regenerate a packet and compare bytes (--verify).",
    "Use when a single packet is built or checked by hand; `sm head packet preopen` calls the same code.",
    "writes the packet file given with --output (a local file); --verify reads only. Never fetches, never writes the registry.",
    "sm head manifest BRANCH --base MAIN_SHA --selection SEL.json --junit python=COLLECTED.xml --output PACKET.json",
    "sm head manifest --verify PACKET.json, then sm head prepare PACKET.json --core-repo WORKTREE --review-root ROOT",
    "TARGET is a branch, an exact commit or a PR number (one GitHub read for its head). The selection is derived from\n"
    "the base's own selector unless --selection gives a reviewed one. The JUnit files are not bound to the head by Git:\n"
    "confirm they come from a collection or run of exactly this head. Exit 1: refused, or --verify found a difference."))
def head_manifest(
    target: str | None = typer.Argument(None, metavar="[TARGET]", help="Branch, exact head commit or PR number; omit with --verify."),
    verify: Path | None = typer.Option(None, "--verify", metavar="PACKET", help="Regenerate this packet and compare bytes."),
    core_repo: Path | None = typer.Option(None, "--core-repo", help="Core repository holding base and head (default: setting core_repo_dir)."),
    base: str = typer.Option("origin/main", "--base", help="Base revision, read locally."),
    selection: Path | None = typer.Option(None, "--selection", help="Reviewed JSON {lane: [test ids]} instead of the derived selection."),
    junit: list[str] = typer.Option([], "--junit", metavar="LANE=FILE", help="JUnit case inventory of one selected lane; repeat per lane."),
    production: list[str] = typer.Option([], "--production", metavar="PATH", help="Covered production file beyond the changed ones; repeatable."),
    review: str | None = typer.Option(None, "--review", metavar="REL", help="Review file to bind, relative to --review-root."),
    review_root: Path | None = typer.Option(None, "--review-root", help="Root of the review path (--verify default: the packet's directory)."),
    layout: str = typer.Option("indent", "--layout", help="Packet file layout: indent, canonical or canonical-nl. --verify keeps the packet's own."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Where the packet is written (required unless --verify)."),
) -> ops.ManifestResult:
    core = _core(core_repo)
    if bool(target) == bool(verify):
        raise Refuse("give exactly one of TARGET or --verify PACKET", "sm head manifest --help", code=2)
    try:
        if verify:
            return ops.verify_packet(core, verify, junit=junit, review_root=review_root)
        if output is None:
            raise Refuse("a build needs --output PACKET.json", "sm head manifest --help", code=2)
        return ops.build_packet(core, _head_of(str(target), core), base, output, selection=selection, junit=junit, production=production,
                                review=review, review_root=review_root, layout=layout)
    except ops.PacketRefused as error:
        _refuse(error, "sm head manifest --help")


@native("head size", writes=False, replaces=("hz-wave-size",), help=H(
    "Size one admission wave: test ids per lane, production files, overlaps and merge conflicts.",
    "Use before building packets for more than one head, to see what they share and whether they conflict.",
    "nothing (read-only).",
    "sm head size fix/a ~/Code/heinzel-b:fix/b --base origin/main",
    "sm head packet preopen BRANCH --main SHA ... for each head that fits",
    "Each argument is a branch, optionally WORKTREE:BRANCH. No lane and no production count has a maximum (PRs of any\n"
    "size); with the base's whole-suite sentinel a web lane holding it counts as the one id web/ALL. Exit 1: a merge\n"
    "conflicts; the output names the branch to move out."))
def head_size(
    branches: list[str] = typer.Argument(..., metavar="[WORKTREE:]BRANCH...", help="The heads of the wave."),
    core: Path | None = typer.Option(None, "--core", help="Core repository for arguments without a worktree (default: setting core_repo_dir)."),
    base: str = typer.Option("origin/main", "--base", help="Base revision, read locally, never fetched."),
) -> ops.SizeResult:
    try:
        return ops.size_wave(branches, _core(core), base)
    except ops.PacketRefused as error:
        _refuse(error, "sm head size --help")


@native("head floor", writes=False, replaces=("hz-floor-check",), help=H(
    "Is this admitted head ready for the merge gate: open non-draft PR, focused plan, pins, focused check runs.",
    "Use after `sm head apply` and the push, before the PR enters the merge queue.",
    "nothing (read-only); three GitHub reads.",
    "sm head floor ~/Code/heinzel-fix-x",
    "sm queue add PR (when READY); gh pr ready PR when it is a draft",
    "TARGET is a worktree path or a branch name. Exit 1: NOT READY with every reason; refused when state is unreadable."))
def head_floor(
    target: str = typer.Argument(..., metavar="WORKTREE_OR_BRANCH", help="The worktree at the head, or its branch name."),
    core: Path | None = typer.Option(None, "--core", help="Core repository (default: setting core_repo_dir)."),
    repo: str | None = typer.Option(None, "--repo", metavar="OWNER/NAME", help="The GitHub repository (default: setting repo)."),
    base: str = typer.Option("origin/main", "--base", help="Base revision, read locally."),
) -> ops.FloorResult:
    rt = runtime()
    slug = str(repo or rt.settings.get("repo") or "")
    gh_client = Gh(rt.providers.commands, repo=slug)

    def git(cmd: list[str]) -> str:
        done = rt.providers.commands.run(cmd, timeout=120)
        if done.rc:
            raise fc.Unreadable(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed: {(done.err or '').strip()[:200]}")
        return str(done.out)

    def gh(cmd: list[str]) -> str:
        done = gh_client.run(cmd[1:], timeout=120)
        if done.rc:
            raise fc.Unreadable(f"GitHub read failed ({' '.join(cmd[1:3])}): {gh_client.why(done)}")
        return str(done.out)

    try:
        return ops.floor(target, _core(core), slug, base, git=git, gh=gh)
    except ops.PacketRefused as error:
        _refuse(error, "sm head floor --help")


# ----------------------------------------------------------------------------- head packet
PACKET = Group("head packet", H(
    "head packet: build the reviewed admission packet directory of one head (preopen, predict, bind) or of a wave.",
    "Use after the local focused run of the head is green and before `sm head prepare`.",
    "writes packet files under the review root; predict also writes predicted commits and scratch refs in the scratch clone.",
    "sm head packet preopen BRANCH --main SHA --core-repo GIT --worktree WT --out DIR --review-root ROOT --selection SEL.json --junit python=JUNIT.xml",
    "sm head prepare DIR/packet.json --core-repo WT --review-root ROOT"))

SELECTION = typer.Option(None, "--selection", help="Reviewed JSON {lane: [test ids]}; exactly one of this and --reviewed-packet.")
REVIEWED_PACKET = typer.Option(None, "--reviewed-packet", help="An earlier reviewed packet: same selection, production and mode.")
JUNIT = typer.Option([], "--junit", metavar="LANE=FILE", help="The local focused JUnit of exactly this head, one per selected lane.")
PRODUCTION = typer.Option([], "--production", metavar="PATH", help="Declared extra production file; repeatable.")
MEMBERS = typer.Option(None, "--members", help="One line for the builder's REVIEW.md.")
NOTES = typer.Option(None, "--notes", help="Notes for the independent reviewer, appended to REVIEW.md.")
PYTHON = typer.Option(None, "--python", help="Core venv python for the collection (default WORKTREE/.venv/bin/python).")
NODE_MODULES = typer.Option(None, "--node-modules", help="web/node_modules for the web lane (default WORKTREE/web/node_modules).")
NONCONSUMERS = typer.Option(None, "--nonconsumers", help="Reviewed nonconsumer receipts; passed on to the named prepare command.")
INPUTS_HELP = ("Every selected lane is COLLECTED on the head tree (it must reproduce the local JUnit) and on the resulting tree;\n"
               "the packet uses the resulting tree's cases. The collection runs under the run lock. The output directory must\n"
               "be new or empty and inside --review-root. Never applies, pushes, fetches or calls GitHub.")


def _gated(cmd: list[str]) -> SimpleNamespace:
    """One collection run under the run lock. Its output goes to the terminal's stderr side."""
    outcome = lock_of().run(Request(argv=cmd, environ=aw.clean_env(child_environment()), cwd=os.getcwd()))
    return SimpleNamespace(returncode=outcome.exit_code, stdout="", stderr=outcome.reason)


@native("head packet preopen", writes=True, review=PACKET_REVIEW, replaces=("hz-admit-wave",), help=H(
    "Build the pre-open packet of one head at an exact main: collected cases, the pair, the builder's REVIEW.md, verified.",
    "Use when the head merges cleanly onto the current main and its local focused run is green.",
    "writes packet files under the review root (sel.json, collected cases, packet.json, REVIEW.md, wave.json); nothing outside this machine.",
    "sm head packet preopen fix/x --main SHA --core-repo ~/Code/heinzel --worktree WT --out ROOT/x/m-SHA8 --review-root ROOT --selection SEL.json --junit python=J.xml",
    "an independent review of DIR/REVIEW.md, then the named command: sm head prepare DIR/packet.json --core-repo WT --review-root ROOT",
    INPUTS_HELP))
def head_packet_preopen(
    branch: str = typer.Argument(..., metavar="BRANCH", help="Branch or commit of the head in --core-repo."),
    main: str = typer.Option(..., "--main", help="The exact 40-hex main commit the pair binds."),
    core_repo: Path = typer.Option(..., "--core-repo", help="Git repository holding main and the head."),
    worktree: Path = typer.Option(..., "--worktree", help="The owned clean Core checkout at the head."),
    out: Path = typer.Option(..., "--out", help="New packet directory inside --review-root."),
    review_root: Path = typer.Option(..., "--review-root", help="Root the review path is relative to."),
    selection: Path | None = SELECTION, reviewed_packet: Path | None = REVIEWED_PACKET, junit: list[str] = JUNIT,
    production: list[str] = PRODUCTION, members: str | None = MEMBERS, notes: Path | None = NOTES, python: str | None = PYTHON,
    node_modules: Path | None = NODE_MODULES, nonconsumers: Path | None = NONCONSUMERS,
    reviewed_protected_producer: bool = typer.Option(False, "--reviewed-protected-producer", help="Passed on to the named prepare command."),
) -> wave_ops.PacketBuilt:
    try:
        return wave_ops.preopen(branch, main, reviewed_protected_producer=reviewed_protected_producer, core_repo=core_repo, worktree=worktree, out=out,
                                review_root=review_root, selection=selection, reviewed_packet=reviewed_packet, junit=junit, production=production,
                                members=members, notes=notes, python=python, node_modules=node_modules, nonconsumers=nonconsumers, collector=None,
                                run_collect=_gated)
    except ops.PacketRefused as error:
        _refuse(error, "sm head packet preopen --help")


@native("head packet predict", writes=True, review=PACKET_REVIEW, replaces=("hz-admit-wave",), help=H(
    "Build the packet of a later head against the PREDICTED main after the earlier head(s) merge; for review, not admissible.",
    "Use when a head depends on heads that are still in CI, to have its packet reviewed while they merge.",
    "writes packet files under the review root, and predicted commit objects and scratch refs in --core-repo (a scratch clone, never the shared repository).",
    "sm head packet predict fix/later --after ROOT/earlier/m-SHA8 --core-repo SCRATCH.git --worktree WT --out ROOT/later/predict --review-root ROOT "
    "--selection SEL.json --junit python=J.xml",
    "after the earlier heads merged: sm head packet bind ROOT/later/predict --main REAL_MAIN_SHA",
    "--after takes preopen or bound directories at one main, in merge order. The predicted main is the chained squash of\n"
    "the earlier heads; each predicted tree must equal the earlier packet's resulting tree. " + INPUTS_HELP))
def head_packet_predict(
    branch: str = typer.Argument(..., metavar="BRANCH", help="Branch or commit of the later head in --core-repo."),
    after: list[Path] = typer.Option(..., "--after", metavar="DIR", help="Packet directory of an earlier head; repeat in merge order."),
    core_repo: Path = typer.Option(..., "--core-repo", help="Scratch clone holding main and every head; predicted commits are written here."),
    worktree: Path = typer.Option(..., "--worktree", help="The owned clean Core checkout at the head."),
    out: Path = typer.Option(..., "--out", help="New packet directory inside --review-root."),
    review_root: Path = typer.Option(..., "--review-root", help="Root the review path is relative to."),
    selection: Path | None = SELECTION, reviewed_packet: Path | None = REVIEWED_PACKET, junit: list[str] = JUNIT,
    production: list[str] = PRODUCTION, members: str | None = MEMBERS, notes: Path | None = NOTES, python: str | None = PYTHON,
    node_modules: Path | None = NODE_MODULES,
) -> wave_ops.PacketBuilt:
    try:
        return wave_ops.predict(branch, after, live_core=_core(None), core_repo=core_repo, worktree=worktree, out=out, review_root=review_root,
                                selection=selection, reviewed_packet=reviewed_packet, junit=junit, production=production, members=members, notes=notes,
                                python=python, node_modules=node_modules, nonconsumers=None, collector=None, run_collect=_gated)
    except ops.PacketRefused as error:
        _refuse(error, "sm head packet predict --help")


@native("head packet bind", writes=True, review=PACKET_REVIEW, replaces=("hz-admit-wave",), help=H(
    "Rebind a predicted packet to the real main, only when main and the resulting tree are byte-equal to the prediction.",
    "Use after the earlier head(s) merged and the real main commit is in the scratch clone (this command never fetches).",
    "writes the bound packet directory under the review root (default PREDICT_DIR/bound-MAIN12); nothing outside this machine.",
    "sm head packet bind ROOT/later/predict --main REAL_MAIN_SHA",
    "an independent review, then the named command: sm head prepare BOUND_DIR/packet.json --core-repo WT --review-root ROOT",
    "Refuses unless main's first-parent chain to the predicted parent has exactly one single-parent commit per earlier\n"
    "head, main's tree equals the predicted main tree and merge-tree(main, head) equals the predicted resulting tree;\n"
    "then build again with `sm head packet preopen`. The cases are carried from the predicted collection (same tree)."))
def head_packet_bind(
    predict_dir: Path = typer.Argument(..., metavar="PREDICT_DIR", help="The directory `head packet predict` built."),
    main: str = typer.Option(..., "--main", help="The exact 40-hex real main commit, present in the predict directory's core repository."),
    out: Path | None = typer.Option(None, "--out", help="New directory (default PREDICT_DIR/bound-MAIN12)."),
    nonconsumers: Path | None = NONCONSUMERS,
) -> wave_ops.PacketBuilt:
    try:
        return wave_ops.bind(predict_dir, main, out=out, nonconsumers=nonconsumers)
    except ops.PacketRefused as error:
        _refuse(error, "sm head packet preopen --help")


@native("head packet wave", writes=False, replaces=("hz-admit-wave",), help=H(
    "Check that 2 to 20 packet directories form one wave: one main, one review root, distinct heads, clean checkouts.",
    "Use when several heads are to be admitted in one transaction.",
    "nothing (read-only).",
    "sm head packet wave ROOT/a/m-SHA8 ROOT/b/m-SHA8",
    "not ported yet: the one batch prepare for the wave; until then sm head prepare PACKET for a single packet",
    "Takes preopen and bound directories; a predict directory is not admissible. The result lists the packets and their\n"
    "checkouts in batch order, as the batch prepare will take them."))
def head_packet_wave(
    dirs: list[Path] = typer.Argument(..., metavar="DIR...", help="The packet directories, in the order of the batch."),
    nonconsumers: Path | None = NONCONSUMERS,
) -> wave_ops.WavePlan:
    try:
        return wave_ops.wave(dirs, nonconsumers=nonconsumers)
    except ops.PacketRefused as error:
        _refuse(error, "sm head size BRANCH...")


# ----------------------------------------------------------------------------- head cases
CASES = Group("head cases", H(
    "head cases: the test case inventory of a lane on an exact Git tree, and the rule that compares inventories.",
    "Use when a packet's cases are collected or checked by hand; `sm head packet preopen` does both for every lane.",
    "collect writes the JUnit file given with --output; compare writes the id list given with --output. Local files only.",
    "sm head cases collect --repo ~/Code/heinzel --tree TREE --selection SEL.json --lane python --output python.collected.xml",
    "sm head manifest BRANCH --base MAIN --selection SEL.json --junit python=python.collected.xml --output PACKET.json"))


@native("head cases collect", writes=True, review=PACKET_REVIEW, replaces=("collect_cases.py",), help=H(
    "List the test cases of one lane's selection on an exact Git tree, without running them, as the hosted job reports them.",
    "Use when a lane's cases are needed for a tree other than the one that was run (a resulting tree).",
    "writes the JUnit file given with --output (a local file); extracts the tree into a temporary directory and removes it.",
    "sm head cases collect --repo ~/Code/heinzel --tree TREE --selection SEL.json --lane python --output python.collected.xml",
    "sm head cases compare --hosted RUN.xml --pr HEAD_TREE.xml --result RESULTING_TREE.xml",
    "python and browser lanes: pytest --collect-only with the given interpreter. web lane: runs the selected Vitest files\n"
    "(the whole suite for web/ALL), because a listing leaves parametrized names unexpanded. Runs under the run lock."))
def head_cases_collect(
    repo: Path | None = typer.Option(None, "--repo", help="Git repository holding the tree (default: setting core_repo_dir)."),
    tree: str = typer.Option(..., "--tree", help="The exact tree (or commit) to collect on."),
    selection: Path = typer.Option(..., "--selection", help="JSON {lane: [test ids]}."),
    lane: str = typer.Option(..., "--lane", help="The lane of the selection to collect: python, python312, macos, browser or web."),
    output: Path = typer.Option(..., "--output", "-o", help="The JUnit file to write."),
    python: str | None = typer.Option(None, "--python", help="Interpreter with the checkout's test dependencies (default: this command's own)."),
    node_modules: Path | None = typer.Option(None, "--node-modules", help="web/node_modules for the web lane."),
) -> check_ops.CasesCollected:
    try:
        return check_ops.collect(_core(repo), tree, selection, lane, output, python=python, node_modules=node_modules, run=_gated)
    except ops.PacketRefused as error:
        _refuse(error, "sm head cases collect --help")


@native("head cases compare", writes=True, review=PACKET_REVIEW, replaces=("collect_cases.py",), help=H(
    "Compare three case inventories: the run's JUnit, the head-tree collection and the resulting-tree collection.",
    "Use after collecting a lane on both trees, before a packet carries cases for the resulting tree.",
    "writes the differing ids to the file given with --output (a local file); without --output nothing.",
    "sm head cases compare --hosted RUN.xml --pr HEAD_TREE.xml --result RESULTING_TREE.xml",
    "sm head manifest ... --junit LANE=RESULTING_TREE.xml (always the resulting tree's collection)",
    "Exit 0: identical. Exit 1: the head-tree collection does not reproduce the run (platform ids, runtime skips): refuse.\n"
    "Exit 3: a selected test scans the tree, so the resulting tree has other cases; the ids are listed."))
def head_cases_compare(
    hosted: Path = typer.Option(..., "--hosted", help="JUnit of the focused run of the head."),
    pr: Path = typer.Option(..., "--pr", help="JUnit collected on the head tree."),
    result: Path = typer.Option(..., "--result", help="JUnit collected on the resulting tree."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write the differing ids here."),
) -> check_ops.CasesCompared:
    try:
        return check_ops.compare(hosted, pr, result, output)
    except ops.PacketRefused as error:
        _refuse(error, "sm head cases compare --help")

