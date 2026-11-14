"""Shared by the test_native_ci_ops_*.py files: a runtime with the area's module in the command tree, and a scripted
world (gh, ps, sonar, bd) that records every external call in order."""

import json
import re
from pathlib import Path

import steuermaschine_heinzel.native as native_pkg
from steuermaschine_heinzel.native import ci_ops
from steuermaschine_heinzel.plugin import plugin

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import fake_providers

REPO = "example/core"


def ok(data) -> Result:
    return Result(0, data if isinstance(data, str) else json.dumps(data), "")


class World:
    """A `Commands` provider. GitHub is a small model (PRs, runs, jobs); every argv is recorded in `.calls`."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.prs: dict[str, tuple[str, str]] = {}  # number -> (STATE, head branch), for `pr view`
        self.open_branches: list[str] = []  # for `pr list --json headRefName`
        self.open_prs: list[dict] = []  # for `pr list --json number,...`
        self.runs: dict[str, list[dict]] = {}  # status -> rows of `run list --status`
        self.all_runs: list[dict] = []  # rows of `run list` without --status
        self.main_queued: list[dict] = []  # rows of `run list --status queued --branch main`
        self.jobs: dict[str, object] = {}  # run id -> the jobs document; None: the read fails
        self.default_jobs: object = {"jobs": [{"labels": ["macos-latest"], "status": "queued"}]}
        self.fail: set[str] = set()  # needles of calls that fail
        self.cancelled: list[str] = []
        self.rerun: list[list[str]] = []
        self.other = None  # for non-gh commands: a function (argv, cwd, timeout) -> Result
        self.rules: dict[str, object] = {}  # needle in the gh command line -> answer; asked before the model, longest needle first

    def on(self, needle: str, answer) -> None:
        """Answer every gh call whose line contains `needle`: a Result, data (printed as JSON, text as it is), or a function of the argv."""
        self.rules[needle] = answer

    def lines(self) -> list[str]:
        return [" ".join(call) for call in self.calls]

    def gh_lines(self) -> list[str]:
        return [" ".join(call[1:]) for call in self.calls if call[0] == "gh"]

    def passthrough(self, argv, cwd=None, env=None, timeout=None):
        return self.run(argv, cwd=cwd, timeout=timeout, env=env).rc

    def run(self, argv, cwd=None, timeout=None, env=None):
        a = [str(x) for x in argv]
        self.calls.append(a)
        line = " ".join(a[1:])
        if a[0] != "gh":
            if a[0] == "ps":
                return Result(0, "the tick of the coordinator\n", "")
            return self.other(a, cwd, timeout) if self.other else Result(127, "", f"no fake for {a[0]}")
        if any(needle in line for needle in self.fail):
            return Result(1, "", "gh: Server Error (HTTP 500)")
        for needle in sorted(self.rules, key=len, reverse=True):
            if needle in line:
                answer = self.rules[needle]
                answer = answer(a) if callable(answer) else answer
                return answer if isinstance(answer, Result) else ok(answer)
        if a[1:3] == ["pr", "view"]:
            info = self.prs.get(a[3])
            return ok({"state": info[0], "headRefName": info[1]}) if info else Result(1, "", "GraphQL: Could not resolve to a PullRequest")
        if a[1:3] == ["pr", "list"]:
            return ok(self.open_prs if "number" in a[a.index("--json") + 1] else [{"headRefName": b} for b in self.open_branches])
        if a[1:3] == ["run", "list"]:
            if "--branch" in a:
                return ok(self.main_queued)
            return ok(self.runs.get(a[a.index("--status") + 1], [])) if "--status" in a else ok(self.all_runs)
        if a[1:3] == ["run", "rerun"]:
            self.rerun.append(a[3:])
            return ok("")
        if a[1] == "api":
            url = next(x for x in a[2:] if x.startswith("repos/"))
            found = re.search(r"actions/runs/(\d+)/cancel$", url)
            if found and "POST" in a:
                self.cancelled.append(found.group(1))
                return ok({})
            found = re.search(r"actions/runs/(\d+)/jobs", url)
            if found:
                doc = self.jobs.get(found.group(1), self.default_jobs)
                return Result(1, "", "gh: Not Found (HTTP 404)") if doc is None else ok(doc)
            found = re.search(r"actions/runs/(\d+)$", url)
            if found:
                return ok({"id": int(found.group(1)), "status": "queued", "name": "CI", "head_branch": "b", "event": "pull_request"})
        return Result(1, "", f"no fake answer for: gh {line}")


def runtime(tmp_path, monkeypatch, commands, reviewed=True, **over):
    """The whole command tree with this area's module in it, on fake providers. `reviewed=False`: the review gate is on."""
    monkeypatch.setattr(native_pkg, "MODULES", [*[m for m in native_pkg.MODULES if m is not ci_ops], ci_ops])
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {"repo": REPO}}, tmp_path)
    # the area's settings, set directly: they work the same before and after the mixin is in the plugin's table
    settings.update({"ci_ops_legacy_deferred_file": str(tmp_path / "former-deferred.txt"), "ci_ops_queue_state_file": str(tmp_path / "queue.json"),
                     "ci_ops_sonar_closures_file": str(tmp_path / "closures.toml"), "ci_ops_beads_dir": str(tmp_path / "beads"),
                     "ci_ops_bead_prefix": "tracker-", "ci_ops_starved_minutes": 15, "ci_ops_rejudge_after_close": False, **over})
    providers = fake_providers(commands=commands, tracker=None)
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=reviewed), stack, settings, _providers=providers)
