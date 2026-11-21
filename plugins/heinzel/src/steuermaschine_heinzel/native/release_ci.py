"""Releases run in GitHub Actions: `sm release VERSION` dispatches Core's workflow "Release" and watches it, `sm release
status` reads its runs, and the release train's launchd tick only follows an accepted release on the Mini.

Core's `release-chain.yml` owns the whole chain (cut, candidate, merge, tag, publish, PyPI, compose, acceptance, marker,
Latest); it completes with the Mini switched off. The Mini follows: when a release with an accepted `acceptance.json`
is Latest and the Mini has not followed it yet, the tick runs `sm mini update --minimum X` and then the iOS upload
(the command at path IOS, once the iOS builder has added it). The local step runner stays behind the hidden `--local` for one release.

Review unit of every command here: this file, `release.py` (the local fallback), `mini.py` (the follow step) and
`release_lib/binding.py` (repository and state paths).
"""

from __future__ import annotations

import json
import re
from typing import Any

import typer
from pydantic import Field

from steuermaschine.engine import Refuse
from steuermaschine.github import Gh
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import call, native, runtime

from . import mini
from .go import world
from .release_lib.binding import core_repo

WORKFLOW = "release-chain.yml"
EXACT = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
POLL = 30
FIND = 90  # seconds to find the run a dispatch created
IOS = "release ios"  # the iOS upload command; until it exists the follower reports the step as unavailable
IOS_ATTEMPTS = 3


def client() -> Gh:
    rt = runtime()
    return Gh(rt.providers.commands, repo=core_repo(), clock=rt.providers.clock)


def vkey(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


# ----------------------------------------------------------------------------- the chain's runs
class ChainJob(Model):
    name: str
    status: str
    conclusion: str = ""


class ChainRun(Model):
    """One run of Core's workflow "Release", as dispatched or read."""

    mode: str = Field("ci", description="ci: the workflow in GitHub Actions; local: the hidden fallback ran the local step runner.")
    version: str = ""
    dry_run: bool = False
    run_id: int | None = None
    url: str = ""
    title: str = ""
    status: str = ""
    conclusion: str = ""
    jobs: list[ChainJob] = []
    exit: int = 0
    lines: list[str] = []

    def render(self) -> list[str]:
        return self.lines

    @property
    def exit_code(self) -> int:
        return self.exit


def job_line(job: dict[str, Any]) -> str:
    state = job.get("conclusion") or job.get("status") or "?"
    return f"{job.get('name', '?')}: {state}"


def read_run(gh: Gh, run_id: int) -> dict[str, Any]:
    data, why = gh.try_json(["run", "view", str(run_id), "-R", core_repo(), "--json", "databaseId,status,conclusion,displayTitle,url,jobs,event"])
    if not isinstance(data, dict):
        raise Refuse(f"cannot read the Release run {run_id}: {why}", f"gh run view {run_id} -R {core_repo()}", code=1)
    return data


def chain_model(data: dict[str, Any], lines: list[str], exit_code: int, version: str = "", dry: bool = False) -> ChainRun:
    jobs = [ChainJob(name=str(j.get("name", "")), status=str(j.get("status", "")), conclusion=str(j.get("conclusion") or "")) for j in data.get("jobs") or []]
    return ChainRun(version=version, dry_run=dry, run_id=data.get("databaseId"), url=str(data.get("url", "")), title=str(data.get("displayTitle", "")),
                    status=str(data.get("status", "")), conclusion=str(data.get("conclusion") or ""), jobs=jobs, exit=exit_code, lines=lines)


def dispatch(gh: Gh, version: str, intro: str, dry: bool) -> int:
    """Dispatch the workflow on main and return the id of the run it created (the newest dispatch run created since)."""
    rt = runtime()
    before, _ = gh.try_json(["run", "list", "-R", core_repo(), "--workflow", WORKFLOW, "--event", "workflow_dispatch", "-L", "20", "--json", "databaseId"])
    seen = {r.get("databaseId") for r in before or [] if isinstance(r, dict)}
    args = ["workflow", "run", WORKFLOW, "-R", core_repo(), "--ref", "main", "-f", f"version={version}", "-f", f"intro={intro}", "-f", f"dry_run={str(dry).lower()}"]
    done = gh.run(args)
    if done.rc:
        raise Refuse(f"dispatching {WORKFLOW} failed: {gh.why(done)}", "sm release status", code=1)
    waited = 0
    while waited <= FIND:
        rows, _ = gh.try_json(["run", "list", "-R", core_repo(), "--workflow", WORKFLOW, "--event", "workflow_dispatch", "-L", "20", "--json", "databaseId,displayTitle"])
        fresh = [r for r in rows or [] if isinstance(r, dict) and r.get("databaseId") not in seen]
        if fresh:
            return int(max(r["databaseId"] for r in fresh))
        rt.providers.clock.sleep(5)
        waited += 5
    raise Refuse(f"{WORKFLOW} was dispatched, but no new run appeared within {FIND} s; nothing is guessed", "sm release status", code=1)


def watch(gh: Gh, run_id: int, minutes: int) -> tuple[dict[str, Any], list[str]]:
    """Poll the run until it completes or `minutes` pass; every job change is shown once."""
    rt = runtime()
    lines: list[str] = []
    shown: dict[str, str] = {}
    waited = 0
    while True:
        data = read_run(gh, run_id)
        for job in data.get("jobs") or []:
            line = job_line(job)
            if shown.get(str(job.get("name"))) != line:
                shown[str(job.get("name"))] = line
                rt.say(line)
                lines.append(line)
        if data.get("status") == "completed" or waited >= minutes * 60:
            return data, lines
        rt.providers.clock.sleep(POLL)
        waited += POLL


@native("release run", writes=True, help=H(
    "Release one Core version in GitHub Actions: dispatch the workflow \"Release\" and watch it to its verdict.",
    "Use when a release is due, or to resume one that stopped: the workflow continues the release in flight.",
    "dispatches Core's release-chain.yml on main; the workflow cuts, merges as the owner, tags, publishes, uploads to PyPI, "
    "composes, accepts, writes acceptance.json and marks the release Latest. Nothing runs on this machine.",
    "sm release 0.9.54", "sm release status",
    "`sm release VERSION` is short for `sm release run VERSION`. VERSION: X.Y.Z; `next` resumes the release in flight\n"
    "or cuts the next patch. --dry-run prepares and checks the cut and the tail checks, pushes and publishes nothing.\n"
    "--no-watch returns after the dispatch. Exit 0: the run succeeded (ACCEPTED, or the dry run passed); 1: it failed\n"
    "(dispatch again to resume after the fix); 75: still running after --timeout (same command again watches a new\n"
    "dispatch, which waits for the running one). The Mini follows an accepted release by itself (`sm daemon train`)."))
def release_run(
    version: str = typer.Argument(..., metavar="VERSION", help="X.Y.Z, or `next`."),
    intro: str = typer.Option("", "--intro", help="The release introduction; default: from the user-facing commit subjects."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Prepare and check, push and publish nothing."),
    no_watch: bool = typer.Option(False, "--no-watch", help="Return after the dispatch."),
    timeout: int = typer.Option(300, "--timeout", metavar="MINUTES", help="Minutes to watch (default 300)."),
    local: bool = typer.Option(False, "--local", hidden=True, help="Fallback for one release: the local step runner (sm release resume) instead of CI."),
) -> ChainRun:
    if version in ("next", "patch"):
        version = "next"
    elif not EXACT.fullmatch(version):
        raise Refuse(f"{version!r} is not X.Y.Z or `next`", "sm release run --help", code=2)
    if local:
        return run_local(version, intro, dry_run)
    gh = client()
    if version != "next" and not dry_run:  # a mistyped version refuses before anything is dispatched
        from .release_drive import guard

        rows, _ = gh.try_json(["release", "list", "-R", core_repo(), "-L", "10", "--json", "tagName"])
        guard(version, rows if isinstance(rows, list) else None, "")
    wanted = "" if version == "next" else version
    run_id = dispatch(gh, wanted, intro, dry_run)
    runtime().say(f"dispatched {WORKFLOW}: run {run_id} (https://github.com/{core_repo()}/actions/runs/{run_id})")
    if no_watch:
        data = read_run(gh, run_id)
        return chain_model(data, [f"Release run {run_id}: {data.get('status')}; watch with: sm release status"], 0, wanted, dry_run)
    data, lines = watch(gh, run_id, timeout)
    if data.get("status") != "completed":
        return chain_model(data, [*lines, f"Release run {run_id} still {data.get('status')} after {timeout} min: sm release status"], 75, wanted, dry_run)
    good = data.get("conclusion") == "success"
    verdict = (f"{'DRY RUN OK' if dry_run else 'RELEASED'} (run {run_id})" if good
               else f"Release run {run_id} ended {data.get('conclusion')}: gh run view {run_id} --log-failed; dispatch again after the fix")
    return chain_model(data, [*lines, verdict], 0 if good else 1, wanted, dry_run)


def run_local(version: str, intro: str, dry_run: bool) -> ChainRun:
    """The hidden fallback: the local step runner, exactly as the release train started it."""
    if dry_run or not EXACT.fullmatch(version):
        raise Refuse("--local takes an exact X.Y.Z and no --dry-run", "sm release resume --help", code=2)
    from .release import release_resume

    done = call(release_resume.__sm_native__, version=version, intro=intro, snapshot_acceptance=True, candidate_gate=True, live=True)
    return ChainRun(mode="local", version=version, exit=done.exit, lines=[*done.render()])


# ----------------------------------------------------------------------------- status
def chain_status(gh: Gh, limit: int = 5) -> list[str]:
    """Lines on the newest runs of the workflow "Release": the running one with its jobs, else the last finished one."""
    rows, why = gh.try_json(["run", "list", "-R", core_repo(), "--workflow", WORKFLOW, "-L", str(limit), "--json", "databaseId,status,conclusion,displayTitle,event,createdAt"])
    if not isinstance(rows, list):
        return [f"CI release chain: unreadable ({why})"]
    rows = [r for r in rows if isinstance(r, dict) and r.get("event") != "pull_request"]
    if not rows:
        return ["CI release chain: no run yet (sm release VERSION dispatches one)"]
    active = [r for r in rows if r.get("status") != "completed"]
    lines = []
    for row in active or rows[:1]:
        head = f"CI release chain: run {row['databaseId']} {row.get('displayTitle', '')}: {row.get('conclusion') or row.get('status')}"
        lines.append(head)
        data, _ = gh.try_json(["run", "view", str(row["databaseId"]), "-R", core_repo(), "--json", "jobs"])
        for job in (data or {}).get("jobs") or []:
            if job.get("status") != "completed" or job.get("conclusion") not in ("success", "skipped"):
                lines.append(f"  {job_line(job)}")
    return lines


# ----------------------------------------------------------------------------- the Mini follows
class Follow(Model):
    """One tick of the Mini's follower: the newest accepted release made live here, then its iOS build."""

    version: str = ""
    live: bool = False
    ios: str = Field("", description="done, failed, unavailable (the iOS upload command does not exist yet) or empty.")
    exit: int = 0
    lines: list[str] = []

    def render(self) -> list[str]:
        return self.lines

    @property
    def exit_code(self) -> int:
        return self.exit


def accepted_latest(gh: Gh) -> tuple[str | None, str]:
    """(the version of the Latest Core release when its acceptance.json says accepted, why not)."""
    tag, why = gh.try_json(["api", f"repos/{core_repo()}/releases/latest"])
    name = tag.get("tag_name", "") if isinstance(tag, dict) else ""
    if not name.startswith("v") or not EXACT.fullmatch(name[1:]):
        return None, f"no readable Latest release ({why or name})"
    done = gh.run(["release", "download", name, "-R", core_repo(), "-p", "acceptance.json", "-O", "-"])
    try:
        marker = json.loads(done.out) if not done.rc else {}
    except ValueError:
        marker = {}
    if marker.get("project") != "heinzel" or marker.get("version") != name[1:] or marker.get("accepted") is not True:
        return None, f"{name} has no accepted acceptance.json"
    return name[1:], ""


FOLLOW_KIND, FOLLOW_KEY = "release.follow", "core"  # the store record of what the Mini followed


def follow(dry_run: bool) -> Follow:
    gh = client()
    version, why = accepted_latest(gh)
    if version is None:
        return Follow(lines=[f"follow: nothing to follow: {why}"])
    store = runtime().providers.store
    state = store.load(FOLLOW_KIND, FOLLOW_KEY) or {}
    if state.get("version") != version:
        if state.get("version") and EXACT.fullmatch(str(state["version"])) and vkey(str(state["version"])) > vkey(version):
            return Follow(version=version, lines=[f"follow: the Mini followed v{state['version']} already, newer than Latest v{version}"])
        state = {"version": version, "live": False, "ios": "", "ios_attempts": 0}
    if state.get("live") and state.get("ios") == "done":
        return Follow(version=version, live=True, ios="done", lines=[f"follow: v{version} is live here and on TestFlight"])
    if dry_run:
        todo = [step for step, done in (("sm mini update", state.get("live")), ("iOS upload", state.get("ios") == "done")) if not done]
        return Follow(version=version, live=bool(state.get("live")), ios=str(state.get("ios", "")), lines=[f"dry-run: would follow v{version}: {', '.join(todo)}"])
    lines: list[str] = []
    if not state.get("live"):
        proof = mini.update(world(), version)
        lines += proof.render()
        state["live"] = proof.exit_code == 0
        store.save(FOLLOW_KIND, FOLLOW_KEY, state)
        if not state["live"]:
            return Follow(version=version, exit=1, lines=[*lines, f"follow: v{version} is not live yet; the next tick tries again"])
    cmd = next((c for c in runtime().commands() if getattr(c, "path", "") == IOS), None)
    if cmd is None:
        state["ios"] = "unavailable"
        lines.append(f"follow: v{version} live; the iOS upload command does not exist yet, so no iOS build (the next tick looks again)")
    elif int(state.get("ios_attempts") or 0) >= IOS_ATTEMPTS:
        lines.append(f"follow: the iOS upload of {version} failed {IOS_ATTEMPTS} times; run it by hand after the fix")
    else:
        state["ios_attempts"] = int(state.get("ios_attempts") or 0) + 1
        result = call(cmd, version=version)
        ok = int(getattr(result, "exit_code", 0) or 0) == 0
        state["ios"] = "done" if ok else "failed"
        lines += list(result.render()) if hasattr(result, "render") else []
        lines.append(f"follow: iOS upload of {version}: {'done' if ok else 'failed'}")
    store.save(FOLLOW_KIND, FOLLOW_KEY, state)
    return Follow(version=version, live=True, ios=str(state["ios"]), exit=0 if state["ios"] != "failed" else 1, lines=lines)

