"""Native packet building: `sm head manifest`, the pair packet a release cut reads (a local file; nothing is written outside this machine)."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import typer

from steuermaschine.engine import Refuse
from steuermaschine.github import Gh
from steuermaschine.helpdoc import H
from steuermaschine.native import native, runtime

from .packet_lib import ops
from .packet_lib import focused_manifest as fm


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


@native("head manifest", writes=True, replaces=("hz-focused-manifest",), help=H(
    "Build one focused admission packet from Git objects, or regenerate a packet and compare bytes (--verify).",
    "Use when a single packet is built or checked by hand; the release cut reads its pair packet.",
    "writes the packet file given with --output (a local file); --verify reads only. Never fetches, never writes the registry.",
    "sm head manifest BRANCH --base MAIN_SHA --selection SEL.json --junit python=COLLECTED.xml --output PACKET.json",
    "sm head manifest --verify PACKET.json, then sm release cut VERSION",
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
