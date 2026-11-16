"""The merge tier's ledgers (Core ADR 0149): flaky beads from the flake ledgers, and the main proof status.

`sm ci flake-ledger` reads the merge gate's `flake-ledger-{run}-{attempt}` artifacts and files or updates one `flaky`
bead per job that passed only on a rerun. `sm queue main-proof` runs main's own `scripts/focused_gate.py` in its
main mode (FOCUSED_MAIN_BASE) on the newest green main push, posts the commit status `hz/main-proof` when it accepts
the run (Core's last_green_main counts a main commit as green only with it), and files the run's own flake ledger
(`passed_on_rerun`) the same way. Both act only when the Core at main has the merge tier; both are daemon ticks.
"""

from __future__ import annotations

import contextlib
import functools
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from steuermaschine.config import child_environment
from steuermaschine.engine import Refuse, Wait
from steuermaschine.github import Gh
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime
from steuermaschine.protocols import Busy

from . import merge_tier
from .queue_lib import core_facts
from .tasks_lib import beads

SEEN = "heinzel.merge_tier.flakes"  # store kind: one record per harvested ledger (artifact name or main run)
JUDGE = "heinzel.merge_tier.judge"  # store lock: one main judge at a time
FILING = "heinzel.merge_tier.filing"  # store lock: one run at a time files flaky beads (tick and a manual call)
FILING_PAUSE, JUDGE_PATIENCE = 5.0, 120.0  # seconds: between two tries for the filing lock; how long the main judge waits for it
PROOFS = "heinzel.merge_tier.main_proof"  # store kind: one record per main commit the judge accepted
GREEN_RUNS = 30
SLOTS = ("FOCUSED_CI_PLANS", *(f"FOCUSED_CI_PLANS_V2_{i}" for i in range(4)))


class Filed(Model):
    """One flaky bead filed (new) or noted (existing) for one ledger line."""

    job: str
    bead: str
    action: str  # added | noted
    line: str


class Harvest(Model):
    """What one harvest of the flake ledgers did."""

    state: str  # harvested | not-merge-tier | nothing
    ledgers: list[str] = []
    filed: list[Filed] = []
    notes: list[str] = []

    def render(self) -> list[str]:
        if self.state == "not-merge-tier":
            return ["flake ledgers: the Core at main has no merge tier; nothing to harvest"]
        lines = [f"flake ledgers: {len(self.ledgers)} new, {len(self.filed)} flaky bead(s) filed or noted"]
        lines += [f"  {f.action:<6} {f.bead:<10} {f.job}" for f in self.filed]
        return lines + [f"  note: {n}" for n in self.notes]


class MainProof(Model):
    """What one main-proof tick did for the newest main commit."""

    state: str  # posted | already | waiting | ready (--check: the judge can run) | not-applicable | refused
    sha: str = ""
    run: int | None = None
    base: str = ""
    full: bool | None = None
    reason: str = ""
    filed: list[Filed] = []

    @property
    def exit_code(self) -> int:
        return 1 if self.state == "refused" else 0

    def render(self) -> list[str]:
        about = [f"run {self.run}", *([f"base {self.base[:9]}"] if self.base else [])] if self.run else []
        head = f"main proof {self.sha[:9] or '-'}: {self.state}" + (f" ({', '.join(about)})" if about else "")
        lines = [head + (f": {self.reason}" if self.reason else "")]
        if self.full is not None:
            lines.append(f"  the judge saw {'the FULL set' if self.full else 'a focused selection'}")
        return lines + [f"  {f.action:<6} {f.bead:<10} {f.job}" for f in self.filed]


def file_flakes(rt: Any, text: str) -> list[Filed]:
    """One `flaky: JOB` bead per job of the ledger lines: a new bead, or a note on the open one with that title."""
    tracker = beads.Tracker(rt)
    open_by_title = {str(i.get("title")): str(i.get("id")) for i in tracker.jl(None) if i.get("status") != "closed"}
    done = []
    for flake in merge_tier.flakes(text):
        title = f"flaky: {flake.job}"
        if bead := open_by_title.get(title):
            tracker.note(bead, flake.line)
            done.append(Filed(job=flake.job, bead=bead.removeprefix(tracker.prefix), action="noted", line=flake.line))
            continue
        added = tracker.add(title, "2", labels="flaky", description=flake.line, kind="bug")
        open_by_title[title] = tracker.prefix + added.bead
        done.append(Filed(job=flake.job, bead=added.bead, action="added", line=flake.line))
    return done


def file_once(rt: Any, key: str, text: Callable[[], str], record: dict[str, Any], patience: float = 0) -> list[Filed] | None:
    """File the flakes of one ledger (KEY) exactly once: under the filing lock, re-checked inside it. None: already
    filed. Another run holding the lock is a wait, never a second `flaky: JOB` bead; PATIENCE seconds are waited
    for it first (the main judge has minutes of work behind it, a harvest tick comes back anyway)."""
    store, waited = rt.providers.store, 0.0
    while True:
        with contextlib.ExitStack() as held:
            try:
                held.enter_context(store.lock(FILING))
            except Busy:
                if waited >= patience:
                    raise Wait("another run is filing flaky beads right now", "that run to finish (the next tick files the rest)") from None
            else:
                if store.load(SEEN, key):
                    return None
                filed = file_flakes(rt, text())
                store.save(SEEN, key, {**record, "at": rt.providers.clock.now()})
                return filed
        rt.providers.clock.sleep(FILING_PAUSE)
        waited += FILING_PAUSE


def _gh(rt: Any) -> Gh:
    return Gh(rt.providers.commands, repo=str(rt.settings.get("repo") or ""), clock=rt.providers.clock)


def _merge_tier(rt: Any, gh: Gh) -> str | None:
    """main's focused_plan.py when the merge-tier rules apply, else None."""
    mode = str(rt.settings.get("merge_tier_mode") or "auto")
    if mode == "off":
        return None
    plan = merge_tier.main_plan(gh)
    if mode == "on":
        return plan or ""
    return plan if plan and merge_tier.capable(plan) else None


def _ledger_text(rt: Any, gh: Gh, run: int, name: str) -> str:
    """The text files of one artifact, downloaded into a scratch directory."""
    with tempfile.TemporaryDirectory(prefix="sm-flake-ledger-") as scratch:
        repo, _why = gh.repo()
        done = rt.providers.commands.run(["gh", "run", "download", str(run), "-R", repo, "-n", name, "-D", scratch], timeout=120)
        if done.rc:
            raise Refuse(f"cannot download {name} of run {run}: {Gh.why(done)}", f"sm ci why {run}")
        return "\n".join(p.read_text(errors="replace") for p in sorted(Path(scratch).rglob("*")) if p.is_file())


@native("ci flake-ledger", writes=True, tick=True, help=H(
    "File one flaky bead per job that passed only on a rerun, from the merge gate's flake ledgers.",
    "Use at every tick (or as a daemon with --loop): a pass after a rerun is a flake, never hidden.",
    "creates or notes beads labelled flaky (bd create, bd update --append-notes); remembers each ledger it read.",
    "sm ci flake-ledger",
    "sm beads tasks ls all",
    "Reads the newest merge gate runs and their artifacts flake-ledger-RUN-ATTEMPT (one line per job: passed on rerun:\n"
    "JOB (attempt N failure, run R, head H)). Acts only when the Core at main has the merge tier (merge_tier_mode)."))
def ci_flake_ledger(runs: int = typer.Option(20, "--runs", min=1, max=100, help="How many of the newest merge gate runs to read.")) -> Harvest:
    rt: Any = runtime()
    gh = _gh(rt)
    if _merge_tier(rt, gh) is None:
        return Harvest(state="not-merge-tier")
    rows, why, _more = gh.pages(f"actions/workflows/{core_facts.judge_file()}/runs", {"status": "completed"}, key="workflow_runs", limit=runs)
    if why:
        raise Refuse(f"cannot list the merge gate runs: {why}", "sm ci flake-ledger")
    store, found, filed = rt.providers.store, [], []
    for row in rows:
        artifacts, why = gh.api(f"actions/runs/{row.get('id')}/artifacts", {"per_page": 100})
        if why:
            raise Refuse(f"cannot list the artifacts of run {row.get('id')}: {why}", "sm ci flake-ledger")
        for artifact in (artifacts or {}).get("artifacts", []):
            name = str(artifact.get("name") or "")
            if not merge_tier.FLAKE_ARTIFACT.fullmatch(name) or artifact.get("expired") or store.load(SEEN, name):
                continue
            done = file_once(rt, name, functools.partial(_ledger_text, rt, gh, int(row["id"]), name), {"run": row["id"]})
            if done is not None:
                filed += done
                found.append(name)
    return Harvest(state="harvested" if found else "nothing", ledgers=found, filed=filed)


# ----------------------------------------------------------------------------- the main proof
def _accepted(gh: Gh, sha: str) -> bool:
    rows, _why = gh.api(f"commits/{sha}/statuses", {"per_page": 100})
    return any(isinstance(r, dict) and r.get("context") == merge_tier.MAIN_PROOF_CONTEXT and r.get("state") == "success" for r in rows or [])


def _ci_ok(gh: Gh, run: int) -> bool:
    jobs, _why = gh.api(f"actions/runs/{run}/jobs", {"filter": "latest", "per_page": 100})
    return any(j.get("name") == core_facts.aggregate() and j.get("conclusion") == "success" for j in (jobs or {}).get("jobs", []))


def _ancestor(rt: Any, core: str, older: str, newer: str) -> bool:
    return rt.providers.commands.run(["git", "-C", core, "merge-base", "--is-ancestor", older, newer], timeout=60).rc == 0


def last_green(rt: Any, gh: Gh, core: str, head: str) -> str | None:
    """Core focused_plan.last_green_main: the newest main commit before HEAD whose push run passed the aggregate (latest
    attempt) and that carries the hz/main-proof status."""
    runs, _why = gh.api(f"actions/workflows/{core_facts.ci_file()}/runs", {"branch": "main", "event": "push", "status": "success", "per_page": GREEN_RUNS})
    for run in (runs or {}).get("workflow_runs", []):
        sha = str(run.get("head_sha") or "")
        if len(sha) != 40 or sha == head or not _ancestor(rt, core, sha, head):
            continue
        if _ci_ok(gh, int(run["id"])) and _accepted(gh, sha):
            return sha
    return None


def _push_run(gh: Gh, sha: str) -> dict[str, Any] | None:
    runs, _why = gh.api(f"actions/workflows/{core_facts.ci_file()}/runs", {"branch": "main", "event": "push", "head_sha": sha, "per_page": 10})
    found = [r for r in (runs or {}).get("workflow_runs", []) if r.get("head_sha") == sha]
    return found[0] if found else None


def _judge(rt: Any, gh: Gh, core: str, sha: str, run: int, base: str) -> dict[str, Any]:
    """main's own focused_gate.py in its main mode, in a scratch clone of main; its JSON proof, or a refusal."""
    commands = rt.providers.commands
    repo, _why = gh.repo()
    token = commands.run(["gh", "auth", "token"], timeout=30)
    if token.rc or not token.out.strip():
        raise Refuse(f"no GitHub token for the judge: {Gh.why(token)}", "gh auth status")
    plans = {}
    for name in SLOTS:
        data, _why = gh.api(f"actions/variables/{name}")
        plans[name] = str((data or {}).get("value") or "")
    with tempfile.TemporaryDirectory(prefix="sm-main-proof-") as scratch:
        clone = str(Path(scratch) / "core")
        for argv in (["git", "clone", "--quiet", "--shared", "--no-checkout", "--", core, clone],
                     ["git", "-C", clone, "remote", "set-url", "origin", f"https://github.com/{repo}.git"],
                     ["git", "-C", clone, "checkout", "--quiet", "--detach", sha]):
            done = commands.run(argv, timeout=300)
            if done.rc:
                raise Refuse(f"cannot prepare main {sha[:9]} for the judge: {argv[1:4]} exit {done.rc}", "sm queue main-proof")
        env = child_environment(drop_prefixes=("GIT_",), PR_HEAD_SHA=sha, GITHUB_TOKEN=token.out.strip(), CI_RUN_ID=str(run),
                                FOCUSED_MAIN_BASE=base, FLAKE_LEDGER=str(Path(scratch) / "flake-ledger.txt"), **plans)
        done = commands.run(["uv", "run", "--no-build", "--frozen", "--quiet", "--script", "scripts/focused_gate.py"], cwd=clone, env=env, timeout=1800)
    if done.rc:
        said = " / ".join((done.err or done.out).strip().splitlines()[-2:])[:300]
        raise Refuse(f"the main judge refused run {run} of {sha[:9]}: {said}", f"sm ci why {run}")
    lines = [line for line in done.out.splitlines() if line.startswith("{")]
    try:
        proof = json.loads(lines[-1])
    except (IndexError, ValueError):
        raise Refuse(f"the main judge printed no proof for run {run}", f"sm ci why {run}") from None
    if proof.get("type") != "focused-main/v1" or proof.get("head") != sha or proof.get("run") != run:
        raise Refuse(f"the main judge's proof does not bind main {sha[:9]} and run {run}", f"sm ci why {run}")
    return dict(proof)


@native("queue main-proof", writes=True, tick=True, help=H(
    "Judge the newest green main push with main's own merge gate (main mode) and post the hz/main-proof status.",
    "Use at every tick (or as a daemon with --loop): until a main commit carries the status, every main push runs the full set.",
    "posts one commit status (hz/main-proof, success) on the main commit the judge accepted; files flaky beads for its reruns.",
    "sm queue main-proof",
    "sm go",
    "The base is the last main commit that carries the status and whose push run passed the aggregate (none yet: the commit's parent, and\n"
    "the judge requires the full set). A main run that is not finished or not green waits; a cancelled run replaced by a newer\n"
    "push is not red, the newer one is judged. Needs the setting core_repo_dir (a clone of the Core, the objects the judge reads).\n"
    "--check only answers, in a few reads, whether the judge can run (state ready): the queue's loop asks it at every tick\n"
    "and starts the judge detached, once per main commit. One judge at a time: a second one waits (exit 75)."))
def queue_main_proof(sha: str | None = typer.Option(None, "--sha", metavar="SHA", help="Judge this main commit instead of the tip (it must still be the tip)."),
                     dry_run: bool = typer.Option(False, "--dry-run", help="Find the run and the base; judge and post nothing."),
                     check: bool = typer.Option(False, "--check", help="Only say whether the judge's inputs are complete (main's push run finished green with the aggregate, "
                                                                       "its commit in the Core clone): state ready. Reads only; finds no base, judges and posts nothing.")) -> MainProof:
    rt: Any = runtime()
    gh = _gh(rt)
    plan = _merge_tier(rt, gh)
    if plan is None or (plan and not merge_tier.proof_capable(plan)):
        return MainProof(state="not-applicable", reason="the Core at main has no main proof (hz/main-proof)")
    tip, why = gh.api("git/ref/heads/main")
    head = str((tip or {}).get("object", {}).get("sha") or "")
    if why or len(head) != 40:
        raise Refuse(f"cannot read main: {why or 'no exact commit'}", "sm queue main-proof")
    if sha and sha != head:
        return MainProof(state="refused", sha=sha, reason=f"main moved to {head[:9]}: the judge proves only the current main")
    if _accepted(gh, head):
        return MainProof(state="already", sha=head)
    run = _push_run(gh, head)
    if run is None or run.get("status") != "completed":
        return MainProof(state="waiting", sha=head, reason="its push run has not finished" if run else "no push run yet")
    if run.get("conclusion") != "success" or not _ci_ok(gh, int(run["id"])):
        return MainProof(state="waiting", sha=head, run=int(run["id"]),
                         reason=f"its push run is {run.get('conclusion')}: a rerun of a red main run is a valid repair (sm ci why {run['id']})")
    core = str(rt.settings.get("core_repo_dir") or "")
    if core in ("", "."):  # an empty path setting reads as ".": never the current directory
        raise Refuse("no Core clone: set core_repo_dir", "sm self config")
    core = str(Path(core).expanduser())
    if check:
        if rt.providers.commands.run(["git", "-C", core, "cat-file", "-e", f"{head}^{{commit}}"], timeout=60).rc != 0:
            return MainProof(state="waiting", sha=head, run=int(run["id"]),
                             reason="main's commit is not in the Core clone (core_repo_dir) yet: fetch main there, the judge reads its objects")
        return MainProof(state="ready", sha=head, run=int(run["id"]), reason=f"its push run is green with {core_facts.aggregate()}: the judge can run")
    base = last_green(rt, gh, core, head)
    if base is None:
        parent = rt.providers.commands.run(["git", "-C", core, "rev-parse", "--verify", f"{head}^1"], timeout=60)
        base = parent.out.strip() if parent.rc == 0 else ""
        if len(base) != 40:
            raise Refuse(f"main {head[:9]} is not in {core} (fetch it): no base for the bootstrap proof", f"git -C {core} fetch origin main")
    if dry_run:
        return MainProof(state="waiting", sha=head, run=int(run["id"]), base=base, reason="dry run: judge and post nothing")
    try:
        with rt.providers.store.lock(JUDGE):  # the queue tick starts this detached; a manual call may run at the same time
            return _prove(rt, gh, core, head, run, base)
    except Busy:
        raise Wait(f"another main proof runs (main {head[:9]})", "that run to finish") from None


def _prove(rt: Any, gh: Gh, core: str, head: str, run: dict[str, Any], base: str) -> MainProof:
    """Judge main, file its rerun flakes, post hz/main-proof (under the judge lock)."""
    proof = _judge(rt, gh, core, head, int(run["id"]), base)
    filed = []  # before the status: a tracker failure refuses here and the next tick judges and files again
    key = f"main-{run['id']}-{proof.get('attempt')}"
    if proof.get("passed_on_rerun"):
        filed = file_once(rt, key, lambda: "\n".join(str(line) for line in proof["passed_on_rerun"]), {"run": run["id"]}, JUDGE_PATIENCE) or []
    _posted, why = gh.api(f"statuses/{head}", method="POST", fields={
        "state": "success", "context": merge_tier.MAIN_PROOF_CONTEXT, "target_url": str(run.get("html_url") or ""),
        "description": f"merge gate (main) accepted run {run['id']}, base {base[:9]}" + (", full set" if proof.get("full") else "")})
    if why:
        raise Refuse(f"the judge accepted main {head[:9]}, but the status was not posted: {why}", "sm queue main-proof")
    rt.providers.store.save(PROOFS, head, {"run": run["id"], "base": base, "proof": proof, "at": rt.providers.clock.now()})
    return MainProof(state="posted", sha=head, run=int(run["id"]), base=base, full=bool(proof.get("full")), filed=filed)
