"""`sm head junit`: the one sanctioned full Vitest run of a head whose selection is the whole suite (bead bf3i).

A head that touches a shared Vitest file (the copy standards test, for one) selects `web/ALL`; its admission packet
then needs a JUnit file of the whole suite (`--junit web=FILE`), and `sm gate run` refuses a Vitest run without a
file filter unless the coordinator grants `--allow-full`. This command checks that the head's selection really is
`web/ALL` and runs exactly that one JUnit run under the run lock with the full-suite refusal waived; the waiver is
logged to the gate's allow-full log with the reason. Any other selection is refused: focused files go through
`sm gate run -- ... vitest run FILE`.
"""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import typer

from steuermaschine.config import child_environment
from steuermaschine.engine import Refuse, Wait
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime
from steuermaschine.runlock import Outcome, Request

from .. import state as statedir
from . import lock

WHOLE_SUITE = ({"web/ALL"}, {"ALL"})


class JunitRun(Model):
    """The full Vitest JUnit run of one head."""

    worktree: str
    output: str
    exit: int
    cases: int = 0  # <testcase> elements in the written file
    waived: str = ""  # the logged reason the full-suite refusal was waived

    @property
    def exit_code(self) -> int:
        return self.exit

    def render(self) -> list[str]:
        if self.exit:
            return [f"the full Vitest run exited {self.exit}; {self.output} is not a green JUnit of the head. Fix the failures in ONE commit, then run this again"]
        return [f"wrote {self.output}: {self.cases} Vitest cases (the whole suite, web/ALL); logged: {self.waived}",
                f"Next: {self.output} is a local record; sm head admit takes no junit input any more"]


def whole_suite(selection: Path) -> bool:
    try:
        data = json.loads(selection.read_text())
    except (OSError, ValueError) as error:
        raise Refuse(f"cannot read the selection {selection}: {error}", "sm head select --repo WORKTREE --base origin/main", code=2) from None
    web = data.get("web") if isinstance(data, dict) else None
    return isinstance(web, list) and set(web) in WHOLE_SUITE


def gated(request: Request) -> Outcome:
    """The run under the run lock, the full-suite refusal waived (a seam for the tests)."""
    return lock.lock_of(allow_full=True).run(request)


def count_cases(path: Path) -> int:
    try:
        return sum(1 for _ in ET.parse(path).iter("testcase"))  # the file this run just wrote
    except (OSError, ET.ParseError):
        return 0


@native("head junit", writes=True, help=H(
    "Run the whole Vitest suite of a head once, as JUnit, when its selection is web/ALL; the full-suite refusal is waived and logged.",
    "Use when `sm head select` gave the head `web: [\"web/ALL\"]` and its packet needs the full Vitest JUnit.",
    "writes the JUnit file given with --output (a local file the packet binds); one line in the gate's allow-full log.",
    "sm head junit ~/Code/heinzel-ui-asks --sel SEL.json --output ~/Code/packets/ui-asks/web.xml",
    "sm head admit WORKTREE",
    "Refuses (exit 2) for any other web selection: focused Vitest files run with `sm gate run -- ... vitest run FILE`.\n"
    "Runs WORKTREE/web/node_modules/.bin/vitest with --maxWorkers=2 under the run lock (exit 75 when the lock gave up)."))
def head_junit(worktree: Path = typer.Argument(..., metavar="WORKTREE", help="The Core worktree at the head (web/node_modules installed)."),
               sel: Path = typer.Option(..., "--sel", metavar="SEL.json", help="The head's selection from `sm head select`; its web lane must be web/ALL."),
               output: Path = typer.Option(..., "--output", "-o", metavar="FILE", help="The JUnit file to write.")) -> JunitRun:
    rt = runtime()
    wt = worktree.expanduser().resolve()
    if not whole_suite(sel.expanduser()):
        raise Refuse(f"the selection {sel} does not select the whole Vitest suite (web: [\"web/ALL\"]): run its files focused instead",
                     "sm gate run -- pnpm --dir web exec vitest run FILE --maxWorkers=2", code=2)
    vitest = wt / "web" / "node_modules" / ".bin" / "vitest"
    if not vitest.exists():
        raise Refuse(f"no {vitest}: install the web dependencies of the worktree first", f"sm head junit {wt} --sel {sel} --output {output}", code=2)
    out = output.expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = [str(vitest), "run", "--maxWorkers=2", "--reporter=default", "--reporter=junit", f"--outputFile.junit={out}"]
    waived = f"web/ALL selection {sel.expanduser().resolve()}: one full Vitest JUnit run for the packet"
    configured = lock.setting(rt.settings, "lock_allow_full_log")
    log = Path(os.path.expanduser(str(configured))) if configured is not None else statedir.path(lock.DEFAULT_FILES["lock_allow_full_log"])
    lock.log_allow_full(log, [*argv, f"# {waived}"], rt.providers.clock, str(wt))
    done = gated(Request(argv=argv, environ=child_environment(), cwd=str(wt / "web"), full=True))
    if done.status == "gave_up":
        raise Wait(f"the run lock did not admit the full Vitest run: {done.reason}", "the run lock (sm gate status)")
    if done.status == "refused":
        raise Refuse(f"the run lock refused the full Vitest run: {done.reason}", "sm gate status")
    return JunitRun(worktree=str(wt), output=str(out), exit=done.exit_code, cases=count_cases(out) if done.exit_code == 0 else 0, waived=waived)
