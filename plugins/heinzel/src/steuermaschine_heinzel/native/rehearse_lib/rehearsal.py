"""The release rehearsal: the candidate's e2e lanes and plugin Consumer checks against main's tip, without a release.

Moved from the former rehearsal tool with its decisions unchanged. What changed is how it reaches the world: GitHub
through the one `Gh` client on the `Commands` provider (never retried), time through the `Clock`, its state and its
lock through the `Store`, the hosted lanes through the single-lane library in process, the Consumer checks through
the run lock, the journal through the store, and every path and the repository through settings (`bind`).

  start      makes ONE throwaway commit on top of main's tip (same tree, never a PR head) and one rehearsal/* branch per
             lane and OS, then dispatches core's focused-job.yml on each through the single-lane library (its caps
             apply; lanes it refuses stay pending for the next `step`).
  step       dispatch pending lanes, read finished runs, write the verdict once all are done
  tick       `step` when a rehearsal is open, else `start --if-moved` when main moved and the last start is old enough
  consumers  Consumer checks only: main's edge wheel against every plugin on plugins MAIN
  cancel     stop the open rehearsal: cancel its runs, delete its rehearsal/* branches

Guards (start, and every dispatch in step): refuses while a release is in flight (an open release/* PR or an active
release/* run), while the merge queue holds a PR that is not merged or needs work (a dispatched run blocks focused
admission while it is active), while a focused admission prepare or apply runs on this machine, and while GitHub or
the queue state cannot be read. A rehearsal that meets a guard midway yields: pending lanes are dropped and the
verdict is `incomplete`. When a PR enters the merge queue, a focused admission prepare or apply starts, or a release
(candidate) starts while lanes run, the next `step` (the scheduled tick) stops dispatching and cancels the rehearsal's
own running lanes as well, so admission or the candidate gets its idle window: verdict `incomplete` with `preempted`,
which the alarm reader prints as INFO only (expected, no WARN).
A rehearsal still open after 180 minutes is closed the same way and its own still-active runs are cancelled, so it
never blocks admission for longer. It gates nothing and is no CI OK or release evidence.
Result: the store, kind `heinzel.rehearse.state`, with `last.verdict` green|red|incomplete, the failed lanes with
their run URLs and the merges since the last green rehearsal; one journal line per verdict.
Cost: 8 hosted jobs, about 25 Linux and 36 macOS runner minutes. Lanes run `make test-e2e SUITE=<lane>` exactly as
the candidate does, so they stop at the first failure while core's Makefile keeps `-x`.
Consumer checks, part of EVERY rehearsal: the wheel core's Edge workflow built from main's tip, against the tip of
plugins MAIN (what compose installs, not the commit core's CI pins). For every plugin there the plugins repository's
own `validate_lock` runs, as compose runs it for the candidate: a stale lock (core's dependencies changed), changed
inputs or a core range that does not admit the release this wheel rehearses (its .dev suffix dropped) is red, and so
is a plugins main with no consumer at all. Cheap and local (seconds), no run lock. With `consumers` (--consumers) each
plugin's `make check` runs as well, under the run lock (about 10 minutes, needs --allow-full).

The two guards that look at other areas (the merge queue, a running admission) are in `others.py`.
"""
import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from steuermaschine.defaults import SystemClock
from steuermaschine.github import Gh, GitHub

from . import others

KIND, KEY = "heinzel.rehearse.state", "rehearsal"  # the one document: current, last, last_green
LOCK = "heinzel.rehearse"  # one rehearsal command at a time on this machine


class Edge:
    """How the functions below reach the world. `bind` sets it for one command; a test sets what it needs."""

    def __init__(self, commands: Any = None, clock: Any = None, store: Any = None, journal: Any = None, lane: Any = None, gated: Any = None, say: Any = None) -> None:
        self.commands, self.clock, self.store = commands, clock or SystemClock(), store
        self.journal = journal or (lambda text: None)
        self.lane = lane  # (branch, lane, os) -> (exit code, what it printed, what it said when it refused)
        self.gated = gated  # (argv, cwd, log) -> (exit code, why the run lock refused it or "")
        self.say = say or (lambda text: None)


EDGE = Edge()
R = ""  # OWNER/NAME of the core repository (setting `repo`)
LANES = ("cli", "supervised", "web-1", "web-2")
OSES = ("ubuntu-24.04", "macos-latest")
ACTIVE = ("queued", "in_progress", "waiting", "requested", "pending")
SETTLED = ("merged", "needs work")  # merge queue statuses that wait for nothing
MAX_OPEN_LIMIT = 180  # a setting may lower the bound below, never raise it
MAX_OPEN_MINUTES = MAX_OPEN_LIMIT
PLUGINS = Path()  # a clone of the plugins repository (setting `rehearse_plugins_clone`)
WORK = Path()  # wheel, temporary worktree and logs of the Consumer checks (setting `rehearse_scratch_dir`)
QUEUE = Path()  # the merge queue's state file (see `others.queue_file`)
SHA = re.compile(r"[0-9a-f]{40}")
MIN_EVERY_MINUTES = 30  # the tick never starts rehearsals closer together than this, whatever --every says
# The plugins repository's own lock check, run in its worktree of plugins main with its own tooling: `validate_lock`
# for every plugin, exactly as compose calls it for a candidate wheel. Only the wheel's version is read as the release
# it rehearses (an Edge wheel is X.Y.Z.devN, which no plugin's range admits); its dependencies are compared as built.
LOCK_CHECK = """
import json, sys
from packaging.version import Version
from heinzel_plugin_tools import cli
built = cli.core_metadata
def released(source):
    found = built(source)
    return {**found, "version": Version(found["version"]).base_version}
cli.core_metadata = released
verdicts = {}
for project in cli.projects():
    try:
        cli.validate_lock(project, sys.argv[1])
        verdicts[project.name] = ""
    except Exception as err:
        verdicts[project.name] = str(err) or type(err).__name__
print(json.dumps({"version": released(sys.argv[1])["version"], "consumers": verdicts}))
"""
LOCK_ARGV = ("uv", "run", "--quiet", "--frozen", "--no-build", "--group", "tooling", "python", "-c", LOCK_CHECK)


def bind(edge: Edge, repo: str, plugins_clone: Path, scratch: Path, queue: Path, max_open_minutes: int = MAX_OPEN_LIMIT) -> None:
    global EDGE, R, PLUGINS, WORK, QUEUE, MAX_OPEN_MINUTES
    EDGE, R, PLUGINS, WORK, QUEUE = edge, repo, plugins_clone, scratch, queue
    MAX_OPEN_MINUTES = max(1, min(MAX_OPEN_LIMIT, int(max_open_minutes)))


def queue_path():
    return QUEUE


def run(argv, **kw):
    done = EDGE.commands.run([str(word) for word in argv], cwd=str(kw["cwd"]) if kw.get("cwd") else None, timeout=kw.get("timeout"))
    return SimpleNamespace(returncode=done.rc, stdout=done.out, stderr=done.err)


def gh(*args):
    """One `gh` call through the one client. Never retried: a write that may have happened is not repeated."""
    done = Gh(EDGE.commands, repo=R, timeout=120).run(list(args), retry=False)
    return done.rc, done.out


def api(path: str, *args: str) -> Any:
    """One GitHub API read or write as JSON; any failure is an exception, never a guess."""
    code, out = gh("api", f"repos/{R}/{path}", *args)
    if code != 0:
        raise RuntimeError(f"GitHub API failed: {path}")
    return json.loads(out) if out.strip() else None


def note(text):
    EDGE.journal(text)


def load() -> dict[str, Any]:
    return EDGE.store.load(KIND, KEY) or {}


def save(state):
    EDGE.store.save(KIND, KEY, state)


def queue_waiting(items):
    """The queued PRs that still wait for admission or merge."""
    return [f"{i.get('key') or i.get('pr')} ({i.get('status')})" for i in items if i.get("status") not in SETTLED]


def release_in_flight(pulls, runs):
    heads = [p["head"]["ref"] for p in pulls if p["head"]["ref"].startswith("release/")]
    heads += [r["head_branch"] for r in runs if (r.get("head_branch") or "").startswith("release/")]
    return sorted(set(heads))


def admission_running():
    """A focused admission prepare or apply runs on this machine: it needs a window with no unknown active run."""
    return others.admission_running(EDGE.commands)  # unreadable refuses


def refusal() -> str:
    """Why no rehearsal run may start now ('' when it may). Unreadable state refuses."""
    if admission_running():
        return "refusing: a focused admission prepare or apply is running"
    try:
        waiting = queue_waiting(others.queue_items(queue_path()))
    except FileNotFoundError:
        waiting = []
    except (OSError, ValueError, AttributeError):
        return "refusing: cannot read the merge queue state"
    if waiting:
        return f"refusing: queued PRs wait for admission or merge: {', '.join(waiting[:5])}"
    try:
        pulls = api("pulls?state=open&per_page=100")
        runs = [r for s in ACTIVE for r in api(f"actions/runs?status={s}&per_page=100")["workflow_runs"]]
    except (RuntimeError, ValueError, KeyError, TypeError):
        return "refusing: cannot read GitHub's open PRs and active runs"
    if heads := release_in_flight(pulls, runs):
        return f"refusing: a release is in flight ({', '.join(heads)})"
    return ""


def preempted():
    """Why a rehearsal whose lanes already run must give way now ('' when it need not): a focused admission started
    on this machine, a PR entered the merge queue, or a release (candidate) started: an open release/* PR or an active
    release/* run. An unreadable queue file or GitHub cancels nothing (the dispatch guard already stops new lanes then)."""
    if admission_running():
        return "a focused admission prepare or apply started"
    try:
        waiting = queue_waiting(others.queue_items(queue_path()))
    except (OSError, ValueError, AttributeError):
        waiting = []
    if waiting:
        return f"the merge queue holds {', '.join(waiting[:5])}"
    try:
        pulls = api("pulls?state=open&per_page=100")
        runs = [r for s in ACTIVE for r in api(f"actions/runs?status={s}&per_page=100")["workflow_runs"]]
    except (RuntimeError, ValueError, KeyError, TypeError):
        return ""
    heads = release_in_flight(pulls, runs)
    return f"a release started ({', '.join(heads)})" if heads else ""


def cancel_run(run):
    """Cancel one hosted run of this rehearsal. Never retried. What GitHub answered is said; the lane counts as
    cancelled either way, as before."""
    done = GitHub(EDGE.commands, repo=R, timeout=120).cancel_run(str(run), live=True)
    if done.state not in ("cancelled", "not_cancellable"):
        EDGE.say(f"rehearsal: cancel of run {run}: {done.state}")


def stop_lanes(current):
    """Cancel this rehearsal's own running lanes (each was just refreshed: it is on our throwaway commit) and drop
    the pending ones. Never touches any other run."""
    for lane in current["lanes"]:
        if lane["status"] == "running":
            cancel_run(lane["run"])
            lane.update(status="done", result="cancelled")
        elif lane["status"] == "pending":
            lane["status"] = "dropped"


def short(runner):
    return "macos" if runner.startswith("macos") else "ubuntu"


def lanes_for(main: str, lanes: list[str], oses: list[str]) -> list[dict[str, Any]]:
    return [{"lane": lane, "os": runner, "branch": f"rehearsal/{main[:10]}-{lane}-{short(runner)}", "run": None,
             "status": "pending", "result": None} for runner in oses for lane in lanes]


def throwaway_commit(main):
    """A new commit with main's tree and main as its parent. It is no PR's head and is never merged."""
    tree = api(f"git/commits/{main}")["tree"]["sha"]
    commit = api("git/commits", "--method", "POST", "-f", f"message=chore: candidate rehearsal of {main[:10]}",
                 "-f", f"tree={tree}", "-f", f"parents[]={main}")["sha"]
    if not SHA.fullmatch(commit) or commit == main or api(f"commits/{commit}/pulls"):
        raise RuntimeError("the throwaway commit is not new or belongs to a pull request")
    return commit


def start(lanes, oses, consumers, if_moved=False):
    state = load()
    if state.get("current"):
        return "refusing: a rehearsal is open (`sm release rehearse step` or `cancel`)"
    if why := refusal():
        return why
    main = api("git/ref/heads/main")["object"]["sha"]
    if if_moved and state.get("last", {}).get("main") == main:
        return f"main is still {main[:10]}: nothing new to rehearse"
    commit = throwaway_commit(main)
    current = {"main": main, "commit": commit, "started": int(EDGE.clock.now()), "lanes": lanes_for(main, lanes, oses),
               "consumers": {"status": "pending", "full": bool(consumers)}, "yielded": ""}
    state["current"] = current
    save(state)  # before any branch exists, so `cancel` can always clean up
    for lane in current["lanes"]:
        api("git/refs", "--method", "POST", "-f", f"ref=refs/heads/{lane['branch']}", "-f", f"sha={commit}")
    note(f"rehearsal of main {main[:10]} started: {len(current['lanes'])} lanes on throwaway commit {commit[:10]}")
    return ""


def dispatch(lane):
    """Start one lane through the single-lane library, which owns the hosted capacity rules. Returns the reason when it refuses."""
    code, out, err = EDGE.lane(lane["branch"], lane["lane"], lane["os"])
    found = re.search(r": run (\d+)\s*$", out, re.M)
    if code != 0 or not found:
        return (err or out).strip().splitlines()[-1:] or ["the single-lane start gave no run"]
    lane.update(run=int(found.group(1)), status="running")
    return []


def refresh(lane, commit):
    seen = api(f"actions/runs/{lane['run']}")
    if seen["head_sha"] != commit:
        raise RuntimeError(f"run {lane['run']} is not on the throwaway commit")
    lane["url"] = seen["html_url"]
    if seen["status"] == "completed":
        lane.update(status="done", result=seen["conclusion"])


def verdict(current):
    """green only when every lane and requested Consumer check ran and passed; red on any failure; a cancelled or
    dropped lane proves nothing either way."""
    failures = [f"End-to-end {x['lane']} ({x['os']}): {x['result']} {x.get('url', '')}".strip()
                for x in current["lanes"] if x["status"] == "done" and x["result"] not in ("success", "cancelled")]
    consumers = current.get("consumers") or {}
    failures += [f"Consumer lock ({row})" for row in consumers.get("stale", [])]
    failures += [f"Consumer ({name})" for name in consumers.get("failed", [])]
    if failures:
        return "red", failures
    whole = all(x["result"] == "success" for x in current["lanes"]) and consumers.get("status") in (None, "done")
    return ("green" if whole and not current.get("yielded") else "incomplete"), []


def suspects(state, main):
    """First lines of the merges on main since the last green rehearsal: where a new failure came from."""
    base = state.get("last_green")
    if not base or base == main:
        return []
    try:
        return [c["commit"]["message"].splitlines()[0] for c in api(f"compare/{base}...{main}")["commits"]][-20:]
    except (RuntimeError, KeyError, ValueError, TypeError):
        return []


def delete_branches(current):
    for lane in current["lanes"]:
        try:
            if api(f"git/ref/heads/{lane['branch']}")["object"]["sha"] == current["commit"]:
                api(f"git/refs/heads/{lane['branch']}", "--method", "DELETE")
        except (RuntimeError, KeyError, TypeError):
            continue  # already gone, or not ours any more: leave it


def finish(state):
    current = state.pop("current")
    colour, failures = verdict(current)
    last = {**current, "finished": int(EDGE.clock.now()), "verdict": colour, "failures": failures}
    last["suspects"] = suspects(state, current["main"]) if colour == "red" else []
    if colour == "green":
        state["last_green"] = current["main"]
    state["last"] = last
    save(state)
    delete_branches(current)
    note(line(state))
    return colour


def step(with_consumers=True):
    state = load()
    current = state.get("current")
    if not current:
        return "no rehearsal is open"
    for lane in current["lanes"]:
        if lane["status"] == "running":
            refresh(lane, current["commit"])
    if any(x["status"] == "running" for x in current["lanes"]) and not current["yielded"] and (why := preempted()):
        current["yielded"] = f"gave way mid-flight ({why}): its own running lanes were cancelled"
        current["preempted"] = True
        stop_lanes(current)
        save(state)
    age = int((EDGE.clock.now() - current["started"]) / 60)
    if age > MAX_OPEN_MINUTES and not current["yielded"] and any(x["status"] in ("pending", "running") for x in current["lanes"]):
        current["yielded"] = f"still open after {age} min: closed, its active runs cancelled"
        stop_lanes(current)  # refreshed above: every running lane is on our throwaway commit
        save(state)
    pending = [x for x in current["lanes"] if x["status"] == "pending"]
    if pending and not current["yielded"]:
        if why := refusal():
            current["yielded"] = why
            for lane in pending:
                lane["status"] = "dropped"
        else:
            for lane in pending:
                if why := dispatch(lane):
                    current["waiting"] = why[0]
                    break
                save(state)
    for lane in current["lanes"]:
        if lane["status"] == "running":
            refresh(lane, current["commit"])
    save(state)
    consumers = current.get("consumers")
    open_lanes = [x for x in current["lanes"] if x["status"] in ("pending", "running")]
    if with_consumers and consumers and consumers["status"] == "pending" and not current["yielded"]:
        found = run_consumers(current["main"], full=bool(consumers.get("full", True)))
        if found["status"] == "unavailable" and not found.get("stale") and open_lanes and not found.get("final"):
            consumers["detail"] = found["detail"]  # no Edge build yet: the next step tries again while lanes run
        else:
            consumers.pop("detail", None)
            consumers.update({key: value for key, value in found.items() if key != "final"}, full=consumers.get("full", True))
        save(state)
    if open_lanes or (consumers and consumers["status"] == "pending" and not current["yielded"]):
        return line(state)
    return f"rehearsal {finish(state)}: {line(state)}"


def plugins_main() -> str:
    """The commit of plugins MAIN, fetched into the clone: what compose installs for a candidate."""
    git = ["git", "-C", str(PLUGINS)]
    if run([*git, "fetch", "--quiet", "origin", "main"]).returncode:
        raise RuntimeError("cannot fetch plugins main")
    found = run([*git, "rev-parse", "--verify", "FETCH_HEAD^{commit}"])
    sha = found.stdout.strip()
    if found.returncode or not SHA.fullmatch(sha):
        raise RuntimeError("cannot read the commit of plugins main")
    return sha


def lock_check(tree: Path, wheel: Path) -> tuple[str, dict[str, str]]:
    """The plugins repository's `validate_lock` for every plugin on its main. -> (release judged, {name: why or ""})."""
    done = run([*LOCK_ARGV, str(wheel)], cwd=tree, timeout=600)
    try:
        found = json.loads(done.stdout.strip().splitlines()[-1]) if done.returncode == 0 else None
        verdicts = found["consumers"] if found else None
        if not isinstance(verdicts, dict):
            raise ValueError
    except (ValueError, IndexError, KeyError, TypeError):
        last = (done.stderr or done.stdout).strip().splitlines()[-1:] or [f"exit {done.returncode}"]
        raise RuntimeError(f"the lock check of plugins main did not run: {last[0]}") from None
    return str(found["version"]), {str(name): str(why) for name, why in verdicts.items()}


def edge_wheel(main, work):
    runs = api(f"actions/workflows/edge.yml/runs?head_sha={main}&per_page=10")["workflow_runs"]
    built = [r["id"] for r in runs if r["status"] == "completed" and r["conclusion"] == "success" and r["head_sha"] == main]
    if not built:
        raise RuntimeError(f"no green Edge build of main {main[:10]} yet (main CI red or still running)")
    code, _ = gh("run", "download", str(built[0]), "--repo", R, "--name", "edge-release", "--dir", str(work / "core"))
    wheels = sorted((work / "core").glob("dist/heinzel-*.whl"))
    if code != 0 or len(wheels) != 1:
        raise RuntimeError("the Edge artifact does not hold exactly one core wheel")
    return wheels[0]


def gated(argv, cwd, log):
    """One heavy local command under the run lock, its output in `log`. -> (exit code, why the lock refused it or "")."""
    return EDGE.gated(argv, cwd, log)


def run_consumers(main: str, only: str | None = None, full: bool = False) -> dict[str, Any]:
    """The candidate's Consumer checks on this machine, against plugins MAIN: compose's `validate_lock` for EVERY plugin
    (red on any refusal, and on none at all), then with `full` each plugin's `make check` under the run lock."""
    work = WORK / f"{main[:10]}-{int(EDGE.clock.now())}"
    tree = work / "plugins"
    added = False
    try:
        try:
            wheel = edge_wheel(main, work)
            plugins = plugins_main()
            if run(["git", "-C", str(PLUGINS), "worktree", "add", "--detach", str(tree), plugins]).returncode:
                raise RuntimeError(f"cannot check out plugins main {plugins[:10]}")
            added = True
        except (RuntimeError, KeyError, TypeError, ValueError, OSError) as err:
            return {"status": "unavailable", "detail": str(err), "failed": [], "stale": []}
        found = {"plugins": plugins, "wheel": wheel.name, "failed": [], "passed": []}
        try:
            release, verdicts = lock_check(tree, wheel)
        except RuntimeError as err:
            return {**found, "status": "unavailable", "detail": str(err), "stale": [], "final": True}
        verdicts = {name: why for name, why in verdicts.items() if only in (None, name)}
        stale = [f"{name}: {why}" for name, why in sorted(verdicts.items()) if why]
        if not verdicts:
            stale = [f"no consumer{f' named {only}' if only else ''} on plugins main {plugins[:10]}"]
        found.update(release=release, stale=stale, locked=sorted(name for name, why in verdicts.items() if not why))
        if not full:
            return {**found, "status": "done"}
        for name in found["locked"]:  # a stale lock fails its check anyway: it is red already
            code, refused = gated(["make", "check", f"NAME={name}", f"CORE={wheel}"], tree, work / f"{name}.log")
            if code and refused:
                # A plugin's `make check NAME=...` is one consumer, but the run lock reads it as core's full suite. Running
                # it needs the caller's --allow-full, which the scheduled tick passes; never set here.
                return {**found, "status": "unavailable", "detail": "the run lock refuses `make check` as a full suite; pass --allow-full, as the scheduled tick does",
                        "failed": [], "passed": [], "final": True}
            (found["failed"] if code else found["passed"]).append(name)
        return {**found, "status": "done", "logs": str(work)}
    finally:
        if added:
            run(["git", "-C", str(PLUGINS), "worktree", "remove", "--force", str(tree)])
        shutil.rmtree(work / "core", ignore_errors=True)  # the downloaded wheel; the logs stay


def cancel() -> str:
    state = load()
    current = state.get("current")
    if not current:
        return "no rehearsal is open"
    stop_lanes(current)
    current["yielded"] = current["yielded"] or "cancelled by `sm release rehearse cancel`"
    save(state)
    return f"rehearsal {finish(state)}"


def line(state: dict[str, Any]) -> str:
    """One line for the digest and the alarm reader; empty when there is nothing to say."""
    if current := state.get("current"):
        done = sum(x["status"] == "done" for x in current["lanes"])
        wait = f"; waiting: {current['waiting']}" if current.get("waiting") and done < len(current["lanes"]) else ""
        return f"REHEARSAL running main {current['main'][:10]}: {done}/{len(current['lanes'])} lanes done{wait}"
    if not (last := state.get("last")):
        return ""
    text = f"REHEARSAL {last['verdict'].upper()} main {last['main'][:10]}"
    if last["failures"]:
        text += ": " + "; ".join(last["failures"])
    if last.get("suspects"):
        text += f" | merges since last green: {'; '.join(last['suspects'])}"
    if last["verdict"] == "incomplete":
        detail = last.get("yielded") or (last.get("consumers") or {}).get("detail") or "not every lane ran"
        text += f": {detail}"
    return text


def due(state, every_minutes, now):
    return now - state.get("last", {}).get("started", 0) >= max(MIN_EVERY_MINUTES, every_minutes) * 60


def chosen(value: str, allowed: tuple[str, ...]) -> list[str]:
    picked = [v for v in value.split(",") if v]
    if not picked or any(v not in allowed for v in picked):
        raise SystemExit(f"choose from {', '.join(allowed)}")
    return picked


def status_code(state=None) -> int:
    """1 when the last verdict is red and no rehearsal is open: the exit code of `status`, `start`, `step` and the tick."""
    state = load() if state is None else state
    return int(state.get("last", {}).get("verdict") == "red" and not state.get("current"))


def command(cmd: str, lanes: list[str] | None = None, oses: list[str] | None = None, consumers: bool = False, if_moved: bool = False,
            every: int = 120) -> tuple[str, int]:
    """`start`, `step` and `tick` in the former order, under the caller's lock. -> (what to say, exit code): 75 when a
    guard refused the start, 1 when the last verdict is red and nothing is open."""
    if cmd == "tick" and not load().get("current"):
        if not due(load(), every, EDGE.clock.now()):
            return "", 0
        if_moved = True
    if cmd in ("start", "tick") and not load().get("current") and (why := start(lanes, oses, consumers, if_moved)):
        return why, 75 if why.startswith("refusing") else 0
    return step(), status_code()
