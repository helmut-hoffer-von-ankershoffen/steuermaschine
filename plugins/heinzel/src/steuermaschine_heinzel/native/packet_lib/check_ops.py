"""Typed entry of the case collection: the inventory of a lane on an exact tree, and the rule that compares inventories.

Collecting loads the head's test modules, so it runs in the collector's own isolated interpreter, under the run lock;
this module builds that child's argument list and reads the JUnit file it wrote. `compare` is pure.
"""

from __future__ import annotations

import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

from steuermaschine.models import Model

from . import admit_wave as aw
from . import collect_cases as cc
from .ops import PacketRefused

Runner = Callable[[list[str]], Any]  # one child run under the run lock: argv -> an object with returncode and stderr


# ----------------------------------------------------------------------------- head cases
class CasesCollected(Model):
    """The test cases of one lane's selection on one exact tree, as the hosted focused job reports them."""

    lane: str
    tree: str
    cases: int
    digest: str  # the case digest a packet binds for this lane
    output: str  # the JUnit file written

    def render(self) -> list[str]:
        return [f"{self.lane} {self.tree[:12]} {self.cases} cases digest {self.digest}", f"wrote {self.output}"]


class CasesCompared(Model):
    """The admission rule for collected cases: the head tree reproduces the run; the resulting tree may differ."""

    status: str  # identical | tree_scanning | not_reproduced
    lines: list[str] = []
    output: str = ""

    @property
    def exit_code(self) -> int:
        return {"identical": 0, "not_reproduced": 1, "tree_scanning": 3}[self.status]

    def render(self) -> list[str]:
        head = {"identical": "identical: the head-tree collection reproduces the run and the resulting tree has the same cases",
                "tree_scanning": f"TREE-SCANNING: the resulting tree differs in {len(self.lines)} id(s); a packet must use the resulting tree's collection",
                "not_reproduced": f"REFUSED: the head-tree collection does not reproduce the run ({len(self.lines)} id(s) differ)"}[self.status]
        return [head, *self.lines]


def collect(repo: Path, tree: str, selection: Path, lane: str, output: Path, *, python: str | None, node_modules: Path | None, run: Runner) -> CasesCollected:
    """One collection run of the moved collector, in its own isolated interpreter, through `run`."""
    work = Path(tempfile.mkdtemp(prefix="cases-collect-"))
    cmd = [sys.executable, "-I", str(aw.DEFAULT_COLLECTOR), "collect", "--repo", str(Path(repo).expanduser()), "--tree", tree,
           "--selection", str(selection), "--lane", lane, "-o", str(output), "--work", str(work)]
    cmd += ["--python", python] if python else []
    cmd += ["--node-modules", str(Path(node_modules).expanduser())] if node_modules else []
    try:
        done = run(cmd)
    finally:
        aw.shutil.rmtree(work, ignore_errors=True)
    if done.returncode != 0:
        raise PacketRefused(f"collecting {lane} on {tree[:12]} failed (exit {done.returncode}); the collector's own lines are above. {done.stderr or ''}".strip())
    try:
        cases = cc.read_junit(Path(output))
    except (OSError, ET.ParseError) as error:
        raise PacketRefused(f"the collection wrote no readable JUnit file at {output}: {error}") from error
    return CasesCollected(lane=lane, tree=tree, cases=len(cases), digest=cc.digest(cases), output=str(output))


def compare(hosted: Path, pr: Path, result: Path, output: Path | None = None) -> CasesCompared:
    try:
        code, lines = cc.compare(cc.read_junit(Path(hosted)), cc.read_junit(Path(pr)), cc.read_junit(Path(result)))
        if output:
            Path(output).write_text("\n".join(lines) + ("\n" if lines else ""))
    except (OSError, ValueError, ET.ParseError) as error:
        raise PacketRefused(str(error)) from error
    return CasesCompared(status={0: "identical", 1: "not_reproduced", 3: "tree_scanning"}[code], lines=lines, output=str(output or ""))
