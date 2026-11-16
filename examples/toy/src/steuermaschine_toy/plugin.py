"""A second plugin, to prove the core is reusable: a plain project whose pipeline is PR -> CI green -> merge -> tag.

No admission, no review stop, no queue. The forge and CI are a simulation kept in one JSON file (setting `world`),
so the quick start in the README runs without any account; `sm sim ...` plays the part of the outside world.
A real project replaces `SimForge`/`SimCI` with providers for its code host and keeps the pipeline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from pydantic import BaseModel, ConfigDict, Field

from steuermaschine import API_VERSION, PROG, USAGE, native
from steuermaschine.engine import Ctx, Gate, Pipeline, Refuse, Step, Wait, identify_head
from steuermaschine.helpdoc import H
from steuermaschine.hookspecs import hookimpl
from steuermaschine.models import Model
from steuermaschine.protocols import PR, Check, CIStatus


class World:
    """The simulated outside: PRs, CI results, tags."""

    def __init__(self, path: str):
        self.path = Path(path)

    def read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"prs": [], "ci": {}, "tags": {}, "remote": {}}

    def write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")


class SimForge:
    def __init__(self, world: World):
        self.w = world

    def pr_for_branch(self, branch):
        return next((PR(**p) for p in self.w.read()["prs"] if p["branch"] == branch and p["state"] == "open"), None)

    def open_pr(self, branch, title, body):
        data = self.w.read()
        pr = {"number": len(data["prs"]) + 1, "branch": branch, "head": data["remote"].get(branch, ""), "state": "open", "url": ""}
        data["prs"].append(pr)
        self.w.write(data)
        return PR(**pr)

    def checks(self, sha):
        state = self.w.read()["ci"].get(sha, "pending")
        return [Check("ci", state)]

    def active_runs(self):
        return []

    def merge(self, pr, head):
        data = self.w.read()
        row = next(p for p in data["prs"] if p["number"] == pr)
        if row["head"] != head:
            raise RuntimeError(f"PR #{pr} is at {row['head'][:9]}, not {head[:9]}")
        row["state"] = "merged"
        self.w.write(data)

    def tag(self, name, sha):
        data = self.w.read()
        data["tags"][name] = sha
        self.w.write(data)

    def get_variable(self, name):
        return None

    def set_variable(self, name, value):
        raise NotImplementedError


class SimCI:
    def __init__(self, world: World):
        self.w = world

    def status(self, sha):
        state = self.w.read()["ci"].get(sha, "pending")
        return CIStatus(state, ("unit tests",) if state == "red" else ())

    def failures(self, sha):
        return list(self.status(sha).failing)


class SimVcs:
    """Wraps the real git provider; only `push` and `remote_head` go to the simulated remote."""

    def __init__(self, inner, world: World):
        self.inner, self.w = inner, world

    def head(self, worktree):
        return self.inner.head(worktree)

    def branch(self, worktree):
        return self.inner.branch(worktree)

    def base(self, worktree):
        try:
            return self.inner.base(worktree)
        except RuntimeError:
            return "0" * 40  # a toy repository has no remote

    def dirty(self, worktree):
        return self.inner.dirty(worktree)

    def remote_head(self, worktree, branch):
        return self.w.read()["remote"].get(branch)

    def push(self, worktree, branch):
        data = self.w.read()
        data["remote"][branch] = self.head(worktree)
        self.w.write(data)


# ----------------------------------------------------------------------------- the pipeline
def _facts(ctx: Ctx):
    f = ctx.subject.facts
    return f["worktree"], f["branch"], f["head"]


def push_probe(ctx):
    wt, branch, head = _facts(ctx)
    return f"remote {branch} is at {head[:9]}" if ctx.providers.vcs.remote_head(wt, branch) == head else None


def push_run(ctx):
    wt, branch, head = _facts(ctx)
    remote = ctx.providers.vcs.remote_head(wt, branch)
    if remote and remote != head and not ctx.providers.forge.pr_for_branch(branch):
        raise Refuse(f"the remote branch {branch} is at {remote[:9]}: no force push")
    ctx.providers.vcs.push(wt, branch)
    return f"pushed {branch} at {head[:9]}"


def pr_probe(ctx):
    _, branch, head = _facts(ctx)
    pr = ctx.providers.forge.pr_for_branch(branch)
    if pr and pr.head == head:
        ctx.data["pr"] = pr.number
        return f"PR #{pr.number}"
    return None


def pr_run(ctx):
    _, branch, _ = _facts(ctx)
    pr = ctx.providers.forge.open_pr(branch, ctx.inputs.get("title", branch), "")
    ctx.data["pr"] = pr.number
    return f"opened PR #{pr.number}"


def ci_run(ctx):
    _, _, head = _facts(ctx)
    status = ctx.providers.ci.status(head)
    if status.state == "green":
        return "CI green"
    if status.state == "red":
        raise Refuse(f"CI is red: {', '.join(status.failing)}. Fix it in a new commit; a new head gets a new run", f"{PROG} head status")
    raise Wait("CI is running", "CI on " + head[:9])


def merge_run(ctx):
    _, _, head = _facts(ctx)
    ctx.providers.forge.merge(ctx.data["pr"], head)
    return f"merged PR #{ctx.data['pr']}"


def merge_probe(ctx):
    world = ctx.providers.extra["world"].read()
    return f"PR #{ctx.data.get('pr')} is merged" if any(p["number"] == ctx.data.get("pr") and p["state"] == "merged" for p in world["prs"]) else None


def tag_run(ctx):
    _, _, head = _facts(ctx)
    name = ctx.inputs.get("tag") or f"build-{head[:7]}"
    ctx.providers.forge.tag(name, head)
    return f"tagged {name}"


def pipeline() -> Pipeline:
    return Pipeline(
        subject="head", verb="ship", purpose="Ship one head: push, open the PR, wait for green CI, merge, tag.",
        when="Use when a head is committed in a worktree and should go out.", identify=identify_head,
        inputs={"title": "PR title (default: the branch name).", "tag": "Tag to set after the merge (default: build-<sha7>)."},
        steps=[
            Step("push", push_run, "pushes the branch", external=True, probe=push_probe, plan=lambda c: f"git push origin HEAD:refs/heads/{c.subject.facts['branch']}", binds=("head",)),
            Step("pr", pr_run, "opens a pull request", external=True, probe=pr_probe, plan=lambda c: f"open a PR for {c.subject.facts['branch']}", binds=("head",)),
            Step("ci", ci_run, binds=("head",), trap="A red CI is never rerun here: read the failures, fix once, ship the new head."),
            Step("merge", merge_run, "merges the pull request", external=True, probe=merge_probe, plan=lambda c: f"merge PR #{c.data.get('pr')}", gates=("ci-green",)),
            Step("tag", tag_run, "creates a tag", external=True, plan=lambda c: f"tag {c.inputs.get('tag') or 'build-' + c.subject.facts['head'][:7]}"),
        ])


class ToySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    world: Path = Field(Path(".sim/world.json"), description="JSON file that holds the simulated forge and CI.")


class Toy:
    """The plugin: an object whose @hookimpl functions the core calls (see `sm self hooks`)."""

    name = "toy"

    @hookimpl
    def sm_api_version(self):
        return API_VERSION

    @hookimpl
    def sm_settings(self):
        return ToySettings

    @hookimpl
    def sm_forge(self, settings):
        return SimForge(World(settings["world"]))

    @hookimpl
    def sm_ci(self, settings):
        return SimCI(World(settings["world"]))

    @hookimpl
    def sm_vcs(self, settings, base):
        return SimVcs(base.vcs, World(settings["world"]))

    @hookimpl
    def sm_provider_extras(self, settings):
        return {"world": World(settings["world"])}

    @hookimpl
    def sm_pipelines(self, settings):
        return [pipeline()]

    @hookimpl
    def sm_gates(self, settings):
        def green(ctx):
            status = ctx.providers.ci.status(ctx.subject.facts["head"])
            return status.state == "green", f"CI is {status.state}"

        return [Gate("ci-green", "CI of the exact head is green at the moment of the merge.", green)]

    @hookimpl
    def sm_traps(self, settings):
        return {"head.ship.merge": "Merge only the head CI saw: a push after green voids it."}

    @hookimpl
    def sm_native(self, settings):
        from . import reset  # `sim reset`: a write command (it takes the writers' lock)

        return native.collect(sys.modules[__name__], reset)


SIM = native.Group("sim", H(
    "sim: play the outside world of the toy project (CI results).", "Use when the quick start asks for a CI result.",
    "the simulated world file only.", f"{PROG} sim ci green .", f"{PROG} head ship . --live"))


class SimResult(Model):
    """The simulated CI result of one head."""

    head: str
    ci: str

    def render(self) -> list[str]:
        return [f"CI for {self.head[:9]} is now {self.ci}"]


@native.native("sim ci", help=H("Set the simulated CI result for the head of a worktree.", "Use when a run waits for CI.",
                                "nothing real (only the simulated world file of the toy).", f"{PROG} sim ci green .", f"{PROG} head ship . --live"))
def sim_ci(state: str = typer.Argument(..., metavar="green|red|pending", help="The CI result to play."),
           worktree: str = typer.Argument(".", help="The worktree whose head gets the result.")) -> SimResult:
    if state not in ("green", "red", "pending"):
        raise Refuse("state is one of green, red, pending", code=USAGE)
    p = native.runtime().providers
    head = p.vcs.head(str(Path(worktree).expanduser().resolve()))
    world = p.extra["world"]
    data = world.read()
    data["ci"][head] = state
    world.write(data)
    return SimResult(head=head, ci=state)


plugin = Toy()
