"""Typed entry of the packet directory builders: preopen, predict, bind, wave.

The steps are those of the moved `admit_wave` (its `cmd_*` functions); this module gives them typed arguments and
typed results. A build writes local files only: the packet directory under the review root, and for predict the
predicted commit objects and scratch refs in the given scratch clone.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

from steuermaschine.models import Model

from . import admit_wave as aw
from .ops import PacketRefused

ERRORS = (aw.Refused, OSError, KeyError, TypeError, ValueError, aw.subprocess.SubprocessError)  # type: ignore[attr-defined]  # as the moved entry caught them
Runner = Callable[[list[str]], Any]  # one collection run: argv -> an object with returncode, stdout, stderr


class PacketBuilt(Model):
    """One packet directory: the pair, the builder's review file and the record of how it was built."""

    kind: str  # preopen | predict | bound
    admissible: bool  # False for predict: its base is a predicted commit
    directory: str
    branch: str
    head: str
    main: str  # the base the packet binds; for predict the predicted commit
    tree: str
    packet_path: str
    packet_sha256: str
    manifest_sha256: str
    review_path: str
    lanes: list[str]
    collection: list[str] = []  # per lane: whether the head-tree and resulting-tree collections agree
    prepare_command: str = ""
    parent_main: str = ""  # predict: the real main the prediction starts from
    predicted_main: str = ""

    def render(self) -> list[str]:
        lines = [f"{self.kind} packet {self.packet_path}", f"sha256 {self.packet_sha256}",
                 f"head {self.head[:12]} base {self.main[:12]} tree {self.tree[:12]} lanes {', '.join(self.lanes)}", *self.collection,
                 f"builder review {self.review_path} (not the independent review)"]
        if self.admissible:
            return [*lines, self.prepare_command]
        return [*lines, f"PREDICTED only, not admissible. After the earlier heads merged: sm head packet bind {self.directory} --main REAL_MAIN_SHA"]


class WavePlan(Model):
    """Several packet directories that form one wave at one main: what one batch prepare takes."""

    main: str
    pairs: int
    packets: list[str]
    core_repos: list[str]
    review_root: str
    nonconsumers: str = ""
    batch_prepare_ported: bool = False  # the batch form of `head prepare` (--packets, --core-repos) is not native yet

    def render(self) -> list[str]:
        lines = [f"one wave: {self.pairs} packets at main {self.main[:12]}, one review root, distinct heads, clean checkouts"]
        lines += [f"  {packet}   checkout {repo}" for packet, repo in zip(self.packets, self.core_repos, strict=True)]
        return [*lines, "NOT PORTED YET: the one batch prepare for these packets (head prepare with --packets and --core-repos). "
                "There is no command to run for the wave as a whole until it exists."]


def _call(command: Callable[[argparse.Namespace], dict[str, Any]], args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    printed = io.StringIO()
    try:
        with contextlib.redirect_stdout(printed):
            record = command(args)
    except ERRORS as error:
        raise PacketRefused(str(error) if isinstance(error, aw.Refused) else f"{type(error).__name__}: {error}") from error
    return record, printed.getvalue().strip()


def _built(record: dict[str, Any], directory: Path, command: str) -> PacketBuilt:
    collected = directory / "collected.txt"
    return PacketBuilt(
        kind=record["kind"], admissible=record["kind"] != "predict", directory=str(directory), branch=record["branch"], head=record["head"],
        main=record["main"], tree=record["tree"], packet_path=str(directory / "packet.json"), packet_sha256=record["packet_sha256"],
        manifest_sha256=record["manifest_sha256"], review_path=str(directory / "REVIEW.md"), lanes=list(record["lanes"]),
        collection=collected.read_text().splitlines() if collected.is_file() else [], prepare_command=command,
        parent_main=record.get("parent_main", ""), predicted_main=record.get("predicted_main", ""))


def _inputs(branch: str, *, core_repo: Path, worktree: Path, out: Path, review_root: Path, selection: Path | None, reviewed_packet: Path | None,
            junit: list[str], production: list[str], members: str | None, notes: Path | None, python: str | None, node_modules: Path | None,
            nonconsumers: Path | None, collector: Path | None, run_collect: Runner | None) -> argparse.Namespace:
    return argparse.Namespace(branch=branch, core_repo=Path(core_repo), worktree=Path(worktree), out=Path(out), review_root=Path(review_root),
                              selection=Path(selection) if selection else None, reviewed_packet=Path(reviewed_packet) if reviewed_packet else None,
                              junit=list(junit), production=list(production), members=members, notes=Path(notes) if notes else None,
                              collector=Path(collector) if collector else aw.DEFAULT_COLLECTOR, python=python,
                              node_modules=Path(node_modules) if node_modules else None,
                              nonconsumers=Path(nonconsumers) if nonconsumers else None, run_collect=run_collect)


def preopen(branch: str, main: str, *, reviewed_protected_producer: bool = False, **inputs: Any) -> PacketBuilt:
    """The pre-open pair for one head at an exact main."""
    args = _inputs(branch, **inputs)
    args.main, args.reviewed_protected_producer = main, reviewed_protected_producer
    record, command = _call(aw.cmd_preopen, args)
    return _built(record, args.out.expanduser().resolve(), command)


def predict(branch: str, after: list[Path], *, live_core: Path | None = None, **inputs: Any) -> PacketBuilt:
    """The pair of a later head against the predicted main after the earlier heads squash-merge."""
    args = _inputs(branch, **inputs)
    args.after, args.live_core = [Path(d) for d in after], live_core
    record, _ = _call(aw.cmd_predict, args)
    return _built(record, args.out.expanduser().resolve(), "")


def bind(predict_dir: Path, main: str, *, out: Path | None = None, nonconsumers: Path | None = None) -> PacketBuilt:
    """Rebind a predicted packet to the real main when the trees are byte-equal."""
    args = argparse.Namespace(predict_dir=Path(predict_dir), main=main, out=Path(out) if out else None,
                              nonconsumers=Path(nonconsumers) if nonconsumers else None)
    record, command = _call(aw.cmd_bind, args)
    directory = (args.out.expanduser().resolve() if args.out else Path(predict_dir).expanduser().resolve() / f"bound-{main[:12]}")
    return _built(record, directory, command)


def wave(dirs: list[Path], *, nonconsumers: Path | None = None) -> WavePlan:
    """Check that 2..20 admissible packet directories form one wave at one main."""
    args = argparse.Namespace(dirs=[Path(d) for d in dirs], nonconsumers=Path(nonconsumers) if nonconsumers else None)
    record, command = _call(aw.cmd_wave, args)
    words = shlex.split(command)  # the argument lists the moved check assembled for the batch form
    packets, repos, root = words.index("--packets"), words.index("--core-repos"), words.index("--review-root")
    return WavePlan(main=record["main"], pairs=record["pairs"], packets=words[packets + 1:repos], core_repos=words[repos + 1:root], review_root=words[root + 1],
                    nonconsumers=words[words.index("--nonconsumers") + 1] if "--nonconsumers" in words else "")
