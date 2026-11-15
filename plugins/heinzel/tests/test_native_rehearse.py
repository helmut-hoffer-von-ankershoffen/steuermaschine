"""`sm release rehearse ...` and `sm daemon rehearse` through the whole command tree, on fakes: text and json, the review
gate of the write commands, the guards, and the exact external calls of start, step and cancel in order (a scripted
GitHub that keeps branches and runs, the real single-lane library and the real run lock policy in between)."""

import json
import re
import shutil
from pathlib import Path

import pytest
import rehearse_plugins_kit as kit

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeClock, MemoryStore, fake_providers, invoke
from steuermaschine_heinzel import native as native_pkg
from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native import rehearse
from steuermaschine_heinzel.native.dispatch_lib import focused
from steuermaschine_heinzel.native.rehearse_lib import legacy, others
from steuermaschine_heinzel.native.rehearse_lib import rehearsal as lib
from steuermaschine_heinzel.plugin import HeinzelSettings, plugin

REPO = "example/core"
MAIN, COMMIT, TREE = "a" * 40, "b" * 40, "c" * 40
CLI, WEB = f"rehearsal/{MAIN[:10]}-cli-ubuntu", f"rehearsal/{MAIN[:10]}-web-1-ubuntu"
TWO = ["--lanes", "cli,web-1", "--os", "ubuntu-24.04"]
PLUGINS_MAIN = "1" * 40


class World(FakeGh):
    """GitHub as the rehearsal and the single-lane library see it (branches, runs, pull requests), plus the local
    commands: the process table, git, make, and the gated check. Every call is recorded, in order."""

    def __init__(self, work: Path):
        super().__init__()
        self.work, self.main = work, MAIN
        self.refs: dict[str, str] = {}
        self.runs: dict[int, dict] = {}
        self.pulls: list = []
        self.others_active: list = []  # active runs that are not the rehearsal's
        self.processes, self.ps_code = "  1 /sbin/launchd\n", 0
        self.next_run, self.down = 100, False
        self.local: list[list[str]] = []
        self.checks: dict[str, int] = {}
        self.edge_runs = [{"id": 7, "status": "completed", "conclusion": "success", "head_sha": MAIN}]
        self.core = kit.CORE_0939  # what main's edge wheel requires
        self.plugins = {"heinzel-example": kit.CORE_0939, "heinzel-github": kit.CORE_0939}  # plugins main: each plugin and the core its lock was made with
        self.ranges: dict[str, str] = {}

    # ------------------------------------------------------------------ local commands
    def run(self, argv, cwd=None, timeout=None, env=None):
        argv = [str(a) for a in argv]
        if argv[0] == "gh":
            return self.github(argv)
        self.local.append(argv)
        if argv[0] == "ps":
            return Result(self.ps_code, self.processes) if "pid=,command=" in argv else Result(0, "1\n")
        if argv[:3] == ["git", "-C", argv[2]] and "rev-parse" in argv:
            return Result(0, PLUGINS_MAIN + "\n")
        if "worktree" in argv and "add" in argv:  # plugins main, checked out: one directory per plugin with its lock
            tree = Path(argv[-2])
            tree.mkdir(parents=True)
            for name, locked in self.plugins.items():
                kit.plugin(tree, name, locked, self.ranges.get(name, ">=0.9.1,<0.10"))
            return Result(0, "")
        if "worktree" in argv and "remove" in argv:
            shutil.rmtree(argv[-1])
            return Result(0, "")
        if argv[0] == "uv":
            return kit.lock_check(argv[argv.index("-c") + 1], argv[argv.index("-c") + 2:], Path(str(cwd)))
        return Result(0, "")

    def passthrough(self, argv, cwd=None, env=None, timeout=None):
        self.local.append([str(a) for a in argv])
        return self.checks.get(next((a for a in argv if a.startswith("NAME=")), ""), 0)

    # ------------------------------------------------------------------ GitHub
    def github(self, argv):
        self.calls.append(argv)
        if self.down:
            return Result(1, "", "gh: Server Error (HTTP 502)")
        found = self.answer(argv)
        if isinstance(found, int):
            return Result(1, "", f"gh: Not Found (HTTP {found})")
        return Result(0, found if isinstance(found, str) else json.dumps(found), "")

    def answer(self, argv):
        if argv[1:3] == ["run", "download"]:
            kit.wheel(Path(argv[argv.index("--dir") + 1]) / "dist/heinzel-0.9.40.dev1-py3-none-any.whl", "0.9.40.dev1", self.core)
            return ""
        path = next(a for a in argv if a.startswith(f"repos/{REPO}/")).removeprefix(f"repos/{REPO}/")
        method = next((argv[i + 1] for i, a in enumerate(argv) if a in ("--method", "-X")), "GET")
        fields = dict(a.split("=", 1) for a in argv if "=" in a and not a.startswith("repos/"))
        slurp = "--slurp" in argv
        if path.startswith("pulls?state=open"):
            return [self.pulls] if slurp else self.pulls
        if path.startswith("actions/runs?status="):
            status = path.split("status=")[1].split("&")[0]
            rows = [r for r in [*self.runs.values(), *self.others_active] if r.get("status") == status]
            return [{"total_count": len(rows), "workflow_runs": rows}] if slurp else {"workflow_runs": rows}
        if path == f"git/commits/{self.main}":
            return {"tree": {"sha": TREE}}
        if path == "git/commits" and method == "POST":
            return {"sha": COMMIT}
        if path == f"commits/{COMMIT}/pulls":
            return []
        if path == f"commits/{COMMIT}/branches-where-head":
            return [{"name": name} for name, sha in self.refs.items() if sha == COMMIT]
        if path == "git/refs" and method == "POST":
            self.refs[fields["ref"].removeprefix("refs/heads/")] = fields["sha"]
            return {}
        if path.startswith("git/refs/heads/") and method == "DELETE":
            del self.refs[path.removeprefix("git/refs/heads/")]
            return ""
        if path.startswith("git/ref/heads/"):
            name = path.removeprefix("git/ref/heads/")
            sha = self.main if name == "main" else self.refs.get(name)
            return 404 if sha is None else {"ref": f"refs/heads/{name}", "object": {"type": "commit", "sha": sha}}
        if path.startswith("contents/.github/workflows/focused-job.yml"):
            return "d" * 40 + "\n"
        if path.startswith("actions/workflows/edge.yml/runs"):
            return {"workflow_runs": self.edge_runs}
        if path.endswith("/dispatches") and method == "POST":
            self.next_run += 1
            branch = fields["ref"].removeprefix("refs/heads/")
            self.runs[self.next_run] = {"id": self.next_run, "status": "in_progress", "conclusion": None, "run_attempt": 1, "head_branch": branch, "head_sha": self.refs[branch],
                                        "path": f".github/workflows/focused-job.yml@{branch}", "html_url": f"https://github.com/{REPO}/actions/runs/{self.next_run}",
                                        "event": "workflow_dispatch", "name": "focused job"}
            return {"workflow_run_id": self.next_run, "html_url": f"https://github.com/{REPO}/actions/runs/{self.next_run}"}
        if m := re.fullmatch(r"actions/runs/(\d+)/jobs\?.*", path):
            return [{"total_count": 1, "jobs": [{"id": int(m[1]) * 10, "name": "focused", "status": self.runs[int(m[1])]["status"], "labels": ["ubuntu-24.04"]}]}]
        if (m := re.fullmatch(r"actions/runs/(\d+)/cancel", path)) and method == "POST":
            self.runs[int(m[1])].update(status="completed", conclusion="cancelled")
            return {}
        if m := re.fullmatch(r"actions/runs/(\d+)", path):
            return self.runs[int(m[1])]
        if path.startswith("compare/"):
            return {"commits": [{"commit": {"message": "feat(web): chat head (#791)\n\nbody"}}]}
        raise AssertionError(" ".join(argv))

    # ------------------------------------------------------------------ reading the record
    def finish(self, failing=()):
        for rid, run in self.runs.items():
            if run["status"] != "completed":
                run.update(status="completed", conclusion="failure" if rid in failing else "success")

    def written(self) -> list[str]:
        """Every call that changes something on GitHub, in order, without the repository prefix."""
        lines = [" ".join(call[1:]).replace(f"repos/{REPO}/", "") for call in self.calls]
        return [line for line in lines if any(mark in line for mark in ("--method POST", "--method DELETE", "-X POST", "-X DELETE"))]


def dispatch_of(branch: str, lane: str) -> str:
    return (f"api --method POST -H X-GitHub-Api-Version: 2026-03-10 actions/workflows/focused-job.yml/dispatches -f ref=refs/heads/{branch} -f inputs[ref]={branch} "
            f"-f inputs[os]=ubuntu-24.04 -f inputs[kind]=e2e -f inputs[selector]={lane} -f inputs[repeat]=1 -f inputs[stress]=false")


STARTED = [f"api git/commits --method POST -f message=chore: candidate rehearsal of {MAIN[:10]} -f tree={TREE} -f parents[]={MAIN}",
           f"api git/refs --method POST -f ref=refs/heads/{CLI} -f sha={COMMIT}", f"api git/refs --method POST -f ref=refs/heads/{WEB} -f sha={COMMIT}",
           dispatch_of(CLI, "cli"), dispatch_of(WEB, "web-1")]
DELETED = [f"api git/refs/heads/{CLI} --method DELETE", f"api git/refs/heads/{WEB} --method DELETE"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # a sandbox: nothing touches the real home
    monkeypatch.setattr("os.getloadavg", lambda: (1.0, 1.0, 1.0))  # the machine's real load must not decide these tests
    monkeypatch.setattr(focused, "plan_registry_keys", lambda: set())  # the registry decoder belongs to the admission area and has its own tests
    if rehearse not in native_pkg.MODULES:  # until the area is registered
        monkeypatch.setattr(native_pkg, "MODULES", [*native_pkg.MODULES, rehearse])
    (tmp_path / "legacy").mkdir()
    (tmp_path / "locks").mkdir()
    return World(tmp_path / "work")


@pytest.fixture
def rt(tmp_path, monkeypatch, world):
    def build(reviewed=True, store=None, legacy_dir=True):
        stack = stack_of(plugin)
        table = {"repo": REPO, "dispatch_state_file": str(tmp_path / "runs.jsonl"), "core_repo_dir": str(tmp_path / "clone"),
                 "dispatch_scratch_dir": str(tmp_path / "scratch"), "lock_directory": str(tmp_path / "locks"), "lock_history": str(tmp_path / "history.jsonl"),
                 "lock_allow_full_log": str(tmp_path / "allow-full.log"), "legacy_state_dir": str(tmp_path / "legacy")}
        mine = {"rehearse_scratch_dir": str(tmp_path / "work"), "rehearse_plugins_clone": str(tmp_path / "plugins")}
        if "rehearse_scratch_dir" in HeinzelSettings.model_fields:  # the mixin is registered: its fields are settings
            table.update(mine)
        else:  # not yet: the edge falls back to the mixin's defaults
            for name, value in mine.items():
                monkeypatch.setattr(rehearse.RehearseSettings.model_fields[name], "default", Path(value))
        if not legacy_dir:  # the default: no legacy_state_dir, the project's state directory holds the queue
            table.pop("legacy_state_dir")
        settings, _ = stack.validate({"heinzel": table}, tmp_path)
        providers = fake_providers(commands=world, store=store or build.store, clock=build.clock)
        return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=reviewed), stack, settings, _providers=providers)

    build.store, build.clock = MemoryStore(), FakeClock()  # one store and one clock for every runtime of a test: the state of one machine
    return build


def queue(tmp_path, *items):
    (tmp_path / "legacy" / legacy.QUEUE_STATE).write_text(json.dumps({"items": [{"key": key, "status": status} for key, status in items]}))


def state(rt) -> dict:
    return rt.store.load(lib.KIND, lib.KEY) or {}


# ----------------------------------------------------------------------------- the read commands
def test_plan_in_text_and_json_changes_nothing(rt, world, tmp_path):
    queue(tmp_path, ("393", "merged"), ("650", "needs work"))
    plan = invoke(rt(), ["release", "rehearse", "plan", "--consumers"])
    assert plan.exit_code == 0 and plan.stdout.splitlines() == [
        "8 focused-job runs: cli (ubuntu-24.04), supervised (ubuntu-24.04), web-1 (ubuntu-24.04), web-2 (ubuntu-24.04), cli (macos-latest), "
        "supervised (macos-latest), web-1 (macos-latest), web-2 (macos-latest)", "Consumer lock checks of every plugin on plugins main: always",
        "Full Consumer checks (make check per plugin) on this machine: yes", "no guard refuses a start now"]
    world.pulls = [{"head": {"ref": "release/v0.9.40"}}]
    doc = rehearse.RehearsalPlan.model_validate(json.loads(invoke(rt(), ["--format", "json", "release", "rehearse", "plan", *TWO]).stdout))
    assert [(row.lane, row.operating_system) for row in doc.runs] == [("cli", "ubuntu-24.04"), ("web-1", "ubuntu-24.04")] and not doc.consumers
    assert doc.refusal == "refusing: a release is in flight (release/v0.9.40)"
    assert world.written() == [] and state(rt) == {}
    usage = invoke(rt(), ["release", "rehearse", "plan", "--lanes", "nope"])
    assert usage.exit_code == 2 and "choose from cli, supervised, web-1, web-2" in usage.stderr


def test_status_in_text_and_json_and_exit_1_when_the_last_verdict_is_red(rt, world):
    assert invoke(rt(), ["release", "rehearse", "status"]).stdout == "no rehearsal yet\n"
    last = {"main": MAIN, "commit": COMMIT, "started": 1_800_000_000, "finished": 1_800_000_600, "verdict": "red", "yielded": "", "consumers": None,
            "lanes": [{"lane": "cli", "os": "ubuntu-24.04", "branch": CLI, "run": 101, "status": "done", "result": "failure", "url": "https://x/101"}],
            "failures": ["End-to-end cli (ubuntu-24.04): failure https://x/101"], "suspects": ["feat(web): chat head (#791)"]}
    rt.store.save(lib.KIND, lib.KEY, {"last": last, "last_green": "d" * 40})
    red = invoke(rt(), ["release", "rehearse", "status", "--line"])
    assert red.exit_code == 1 and red.stdout == (f"REHEARSAL RED main {MAIN[:10]}: End-to-end cli (ubuntu-24.04): failure https://x/101 | "
                                                 "merges since last green: feat(web): chat head (#791)\n")
    done = invoke(rt(), ["--format", "json", "release", "rehearse", "status"])
    doc = rehearse.RehearsalStatus.model_validate(json.loads(done.stdout))
    assert done.exit_code == 1 and doc.last.verdict == "red" and doc.last.lanes[0].operating_system == "ubuntu-24.04" and doc.last_green == "d" * 40
    assert doc.last.started.startswith("2027-01-15T") and doc.current is None and world.calls == []  # reads the store only
    assert rehearse.last_verdict(rt.store)["verdict"] == "red" and rehearse.open_rehearsal(rt.store) is None
    rt.store.save(lib.KIND, lib.KEY, {"last": last, "current": {**last, "verdict": ""}})  # a red verdict does not count while a new rehearsal is open
    assert invoke(rt(), ["release", "rehearse", "status"]).exit_code == 0 and rehearse.open_rehearsal(rt.store)["main"] == MAIN


# ----------------------------------------------------------------------------- start, step, cancel: the exact calls
def test_start_then_step_make_exactly_these_external_calls_in_this_order(rt, world, tmp_path):
    queue(tmp_path, ("393", "merged"))
    done = invoke(rt(), ["release", "rehearse", "start", *TWO])
    assert done.exit_code == 0 and done.stdout == f"REHEARSAL running main {MAIN[:10]}: 0/2 lanes done\n", done
    assert world.written() == STARTED and world.refs == {CLI: COMMIT, WEB: COMMIT}  # one commit, one branch per lane, one run per lane; nothing else
    lines = [" ".join(call[1:]).replace(f"repos/{REPO}/", "") for call in world.calls]
    order = [next(i for i, line in enumerate(lines) if needle in line) for needle in
             ("pulls?state=open", "actions/runs?status=queued", "git/ref/heads/main", f"git/commits/{MAIN}", "git/commits --method POST", f"commits/{COMMIT}/pulls",
              f"git/refs --method POST -f ref=refs/heads/{CLI}", f"git/refs --method POST -f ref=refs/heads/{WEB}", "contents/.github/workflows/focused-job.yml",
              f"commits/{COMMIT}/branches-where-head", "--slurp actions/runs?status=", dispatch_of(CLI, "cli"))]
    assert order == sorted(order)  # the guards, main, the throwaway commit and its PR check, the branches, then the single-lane guards, then the run
    assert world.local[0] == ["ps", "-ww", "-axo", "pid=,command="]  # the admission guard is asked first
    current = state(rt)["current"]
    assert [(x["run"], x["status"]) for x in current["lanes"]] == [(101, "running"), (102, "running")] and current["commit"] == COMMIT
    journal = [line for page in rt.store.keys("heinzel.journal") for line in rt.store.load("heinzel.journal", page)["lines"]]
    assert any(f"rehearsal of main {MAIN[:10]} started: 2 lanes on throwaway commit {COMMIT[:10]}" in line for line in journal)

    before = len(world.written())
    assert invoke(rt(), ["release", "rehearse", "step"]).stdout == f"REHEARSAL running main {MAIN[:10]}: 0/2 lanes done\n"
    assert len(world.written()) == before  # a step with nothing to do writes nothing
    world.finish()
    green = invoke(rt(), ["--format", "json", "release", "rehearse", "step"])
    doc = rehearse.RehearsalRun.model_validate(json.loads(green.stdout))
    assert green.exit_code == 0 and doc.message == f"rehearsal green: REHEARSAL GREEN main {MAIN[:10]}" and doc.current is None and doc.last.verdict == "green"
    assert world.written() == [*STARTED, *DELETED] and world.refs == {}
    assert state(rt)["last_green"] == MAIN and rehearse.last_verdict(rt.store)["verdict"] == "green"
    assert "hz-" not in done.stdout + green.stdout + "".join(journal)


def test_start_in_json_and_a_red_lane_exits_1_with_the_run_and_the_merges(rt, world):
    rt.store.save(lib.KIND, lib.KEY, {"last_green": "d" * 40})
    done = invoke(rt(), ["--format", "json", "release", "rehearse", "start", "--lanes", "web-2", "--os", "ubuntu-24.04"])
    doc = rehearse.RehearsalRun.model_validate(json.loads(done.stdout))
    assert done.exit_code == 0 and doc.action == "start" and doc.current.lanes[0].run == 101 and doc.current.lanes[0].status == "running"
    world.finish(failing={101})
    red = invoke(rt(), ["release", "rehearse", "step"])
    assert red.exit_code == 1 and red.stdout == (f"rehearsal red: REHEARSAL RED main {MAIN[:10]}: End-to-end web-2 (ubuntu-24.04): failure "
                                                 f"https://github.com/{REPO}/actions/runs/101 | merges since last green: feat(web): chat head (#791)\n")
    assert state(rt)["last_green"] == "d" * 40 and world.refs == {}
    assert invoke(rt(), ["release", "rehearse", "step"]).stdout == "no rehearsal is open\n"


def test_cancel_cancels_the_rehearsals_own_runs_and_deletes_its_branches_in_this_order(rt, world):
    invoke(rt(), ["release", "rehearse", "start", *TWO])
    world.others_active = [{"id": 999, "status": "queued", "head_branch": "fix/x", "path": ".github/workflows/ci.yml"}]
    done = invoke(rt(), ["release", "rehearse", "cancel"])
    assert done.exit_code == 0 and done.stdout == "rehearsal incomplete\n", done
    assert world.written() == [*STARTED, "api -X POST actions/runs/101/cancel", "api -X POST actions/runs/102/cancel", *DELETED]
    last = state(rt)["last"]
    assert last["verdict"] == "incomplete" and last["yielded"] == "cancelled by `sm release rehearse cancel`" and [x["result"] for x in last["lanes"]] == ["cancelled"] * 2
    again = invoke(rt(), ["--format", "json", "release", "rehearse", "cancel"])
    assert rehearse.RehearsalRun.model_validate(json.loads(again.stdout)).message == "no rehearsal is open" and again.exit_code == 0


def test_cancel_leaves_a_branch_that_moved_off_the_throwaway_commit(rt, world):
    invoke(rt(), ["release", "rehearse", "start", *TWO])
    world.refs[WEB] = "f" * 40
    assert invoke(rt(), ["release", "rehearse", "cancel"]).exit_code == 0
    assert world.refs == {WEB: "f" * 40} and world.written()[-1] == DELETED[0]


def test_the_capacity_of_the_single_lane_library_leaves_lanes_pending(rt, world):
    done = invoke(rt(), ["release", "rehearse", "start"])
    assert done.exit_code == 0 and "0/8 lanes done; waiting: refusing: 5 focused runs already in flight (max 5)" in done.stdout, done
    assert len([line for line in world.written() if "dispatches" in line]) == 5 and len(world.refs) == 8
    assert [x["status"] for x in state(rt)["current"]["lanes"]].count("pending") == 3


# ----------------------------------------------------------------------------- the guards
@pytest.mark.parametrize("status", ["queued", "ready", "waiting"])
def test_start_waits_with_exit_75_while_the_merge_queue_holds_a_pr_and_writes_nothing(rt, world, tmp_path, status):
    queue(tmp_path, ("795", status), ("393", "merged"))
    done = invoke(rt(), ["release", "rehearse", "start", *TWO])
    assert done.exit_code == 75 and f"queued PRs wait for admission or merge: 795 ({status})" in done.stderr
    assert world.written() == [] and world.calls == [] and state(rt) == {}


def test_a_waiting_queue_refuses_the_rehearsal_with_default_settings(rt, world):
    runtime = rt(legacy_dir=False)
    target = Path(runtime.project.home) / legacy.QUEUE_STATE  # the queue loop's file in the project's state directory
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"items": [{"key": "795", "status": "waiting"}]}))
    done = invoke(runtime, ["release", "rehearse", "start", *TWO])
    assert target == lib.QUEUE
    assert done.exit_code == 75 and "queued PRs wait for admission or merge: 795 (waiting)" in done.stderr
    assert world.written() == [] and world.calls == []


def test_an_unreadable_queue_or_process_table_or_github_refuses(rt, world, tmp_path):
    (tmp_path / "legacy" / legacy.QUEUE_STATE).write_text("{not json")
    assert "cannot read the merge queue state" in invoke(rt(), ["release", "rehearse", "start"]).stderr
    (tmp_path / "legacy" / legacy.QUEUE_STATE).write_text(json.dumps({"items": "795"}))
    assert "cannot read the merge queue state" in invoke(rt(), ["release", "rehearse", "start"]).stderr
    (tmp_path / "legacy" / legacy.QUEUE_STATE).unlink()
    world.ps_code = 1
    done = invoke(rt(), ["release", "rehearse", "start"])
    assert done.exit_code == 75 and "a focused admission prepare or apply is running" in done.stderr
    world.ps_code, world.down = 0, True
    done = invoke(rt(), ["release", "rehearse", "start"])
    assert done.exit_code == 75 and "cannot read GitHub's open PRs and active runs" in done.stderr
    assert world.written() == [] and state(rt) == {}


@pytest.mark.parametrize("row", ["812 /opt/homebrew/bin/python3 /Users/x/.local/bin/sm head apply /tmp/p/packet.json --approved abc",
                                 "812 sm head prepare /tmp/p/packet.json --core-repo /tmp/core --review-root /tmp", "812 /usr/bin/sm --format json head admit /tmp/wt",
                                 "812 sm release admit 0.9.41", "812 /x/venv/bin/python /x/venv/bin/steuermaschine --format=json head apply /tmp/p/packet.json",
                                 "812 sm --format json head prepare /tmp/p/packet.json", "812 uv run --script /tools/xx-focused-admit prepare /tmp/p/packet.json",
                                 "812 python /tools/xx_focused_admit.py apply /tmp/p/packet.json"])
def test_a_running_admission_refuses_the_start(rt, world, row):
    world.processes += row + "\n"
    done = invoke(rt(), ["release", "rehearse", "start", *TWO])
    assert done.exit_code == 75 and "a focused admission prepare or apply is running" in done.stderr and world.calls == []


def test_commands_that_are_no_admission_do_not_refuse(world):
    world.processes += ("812 sm head hash /tmp/p/prepare.out\n813 sm head blockers --core-repo /tmp/core\n814 /tools/xx-focused-admit blockers --core-repo /tmp/core\n"
                        "815 sm go\n816 sm release rehearse start\n817 vim notes-on-admission.md\n818 sm head preflight /tmp/wt\n")
    assert others.admission_running(world) is False


def test_a_pr_entering_the_queue_mid_flight_cancels_the_rehearsals_own_lanes_only(rt, world, tmp_path):
    invoke(rt(), ["release", "rehearse", "start", *TWO])
    world.runs[101].update(status="completed", conclusion="success")
    world.others_active = [{"id": 999, "status": "in_progress", "head_branch": "fix/x", "head_sha": "9" * 40}]
    queue(tmp_path, ("796", "queued"), ("393", "merged"))
    done = invoke(rt(), ["release", "rehearse", "step"])
    assert done.exit_code == 0 and done.stdout.startswith(f"rehearsal incomplete: REHEARSAL INCOMPLETE main {MAIN[:10]}: gave way mid-flight (the merge queue holds 796 (queued))")
    assert world.written() == [*STARTED, "api -X POST actions/runs/102/cancel", *DELETED]
    last = rehearse.last_verdict(rt.store)
    assert last["preempted"] is True and [x["result"] for x in last["lanes"]] == ["success", "cancelled"] and "last_green" not in state(rt)
    doc = rehearse.RehearsalStatus.model_validate(json.loads(invoke(rt(), ["--format", "json", "release", "rehearse", "status"]).stdout))
    assert doc.last.preempted and doc.last.verdict == "incomplete"


def test_a_second_rehearsal_command_waits_with_exit_75(rt, world):
    with rt.store.lock(lib.LOCK):
        done = invoke(rt(), ["release", "rehearse", "start", *TWO])
        tick = invoke(rt(), ["--format", "json", "daemon", "rehearse"])
    assert done.exit_code == 75 and "another rehearsal command is working" in done.stderr and world.calls == []
    assert tick.exit_code == 75 and rehearse.RehearsalRun.model_validate(json.loads(tick.stdout)).ran is False


# ----------------------------------------------------------------------------- the tick
def test_one_tick_starts_when_main_moved_then_steps_and_waits_for_the_interval(rt, world):
    first = invoke(rt(), ["daemon", "rehearse", *TWO])
    assert first.exit_code == 0 and first.stdout == f"REHEARSAL running main {MAIN[:10]}: 0/2 lanes done\n" and world.written() == STARTED
    world.finish()
    second = invoke(rt(), ["--format", "json", "daemon", "rehearse", *TWO])
    doc = rehearse.RehearsalRun.model_validate(json.loads(second.stdout))
    assert doc.action == "tick" and doc.ran and doc.message.startswith("rehearsal green") and world.written() == [*STARTED, *DELETED]
    calls = len(world.calls)
    quiet = invoke(rt(), ["daemon", "rehearse", *TWO])  # the last start is younger than the interval: nothing is read, nothing is said
    assert quiet.exit_code == 0 and quiet.stdout.strip() == "" and len(world.calls) == calls
    rt.clock.t += 29 * 60
    assert invoke(rt(), ["daemon", "rehearse", "--every", "0", *TWO]).stdout.strip() == ""  # --every has a floor of 30 minutes
    rt.clock.t += 60
    same = invoke(rt(), ["daemon", "rehearse", "--every", "0", *TWO])
    assert same.exit_code == 0 and same.stdout == f"main is still {MAIN[:10]}: nothing new to rehearse\n" and world.written() == [*STARTED, *DELETED]


def test_a_tick_that_meets_a_guard_or_an_error_returns_its_result_and_never_raises(rt, world, tmp_path):
    queue(tmp_path, ("795", "queued"))
    held = invoke(rt(), ["--format", "json", "daemon", "rehearse"])
    doc = rehearse.RehearsalRun.model_validate(json.loads(held.stdout))  # a result document, not an error: a loop goes on
    assert held.exit_code == 75 and doc.code == 75 and not doc.ran and "queued PRs wait for admission or merge: 795 (queued)" in doc.message
    queue(tmp_path)
    invoke(rt(), ["daemon", "rehearse", *TWO])
    world.runs[101]["head_sha"] = "e" * 40
    wrong = invoke(rt(), ["daemon", "rehearse"])
    assert wrong.exit_code == 1 and wrong.stdout == "the rehearsal stopped: run 101 is not on the throwaway commit\n"
    by_hand = invoke(rt(), ["release", "rehearse", "step"])
    assert by_hand.exit_code == 1 and "run 101 is not on the throwaway commit" in by_hand.stderr


# ----------------------------------------------------------------------------- the Consumer checks
def gated_checks(world) -> list[list[str]]:
    return [argv for argv in world.local if argv[0] == "sh"]


def lock_runs(world) -> list[list[str]]:
    return [argv for argv in world.local if argv[0] == "uv"]


STALE = "heinzel-github: Core dependencies changed; relock only this consumer. Run: make lock NAME=heinzel-github CORE=<compatible-core-wheel>"


def test_consumers_check_every_lock_on_plugins_main_then_each_plugin_under_the_run_lock(rt, world, tmp_path):
    world.checks = {"NAME=heinzel-github": 2}
    done = invoke(rt(), ["release", "rehearse", "consumers", "--full", "--allow-full"])
    work = tmp_path / "work" / f"{MAIN[:10]}-1800000000"
    wheel = work / "core/dist/heinzel-0.9.40.dev1-py3-none-any.whl"
    assert done.exit_code == 1 and done.stdout.splitlines() == [
        f"Consumer checks done: plugins main {PLUGINS_MAIN[:10]}, core wheel {wheel.name} (as core 0.9.40)", "  locks ok: heinzel-example, heinzel-github",
        "  passed: heinzel-example", "  FAILED: heinzel-github", f"  logs: {work}"], done
    tree, clone = str(work / "plugins"), str(tmp_path / "plugins")
    assert [argv[:6] if argv[0] == "uv" else argv for argv in world.local if argv[0] in ("git", "make", "uv") and argv[:3] != ["git", "rev-parse", "HEAD^{tree}"]] == [
        ["git", "-C", clone, "fetch", "--quiet", "origin", "main"], ["git", "-C", clone, "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
        ["git", "-C", clone, "worktree", "add", "--detach", tree, PLUGINS_MAIN], ["uv", "run", "--quiet", "--frozen", "--no-build", "--group"],
        ["git", "-C", clone, "worktree", "remove", "--force", tree]]  # plugins MAIN, not a commit core pins; no `make plan` filters anyone out
    assert lock_runs(world)[0][-1] == str(wheel) and lock_runs(world)[0][-3] == "-c"
    assert gated_checks(world) == [["sh", "-c", 'exec "$@" >"$0" 2>&1', str(work / f"{name}.log"), "make", "-C", tree, "check", f"NAME={name}", f"CORE={wheel}"]
                                   for name in ("heinzel-example", "heinzel-github")]
    assert "make -C" in (tmp_path / "allow-full.log").read_text() and world.written() == []  # the override is logged; GitHub is only read
    assert not (work / "core").exists() and (work / "heinzel-github.log").parent.is_dir()  # the downloaded wheel is removed, the logs stay
    world.checks, world.local = {}, []
    rt.clock.t += 60
    only = invoke(rt(), ["--format", "json", "release", "rehearse", "consumers", "--only", "heinzel-github", "--full", "--allow-full"])
    doc = rehearse.ConsumerChecks.model_validate(json.loads(only.stdout))
    assert only.exit_code == 0 and (doc.status, doc.passed, doc.failed, doc.plugins_main, doc.locked) == ("done", ["heinzel-github"], [], PLUGINS_MAIN, ["heinzel-github"])
    assert len(gated_checks(world)) == 1


def test_the_lock_check_needs_no_allow_full_and_a_refused_make_check_is_unavailable(rt, world):
    locks = invoke(rt(), ["release", "rehearse", "consumers"])
    assert locks.exit_code == 0 and "locks ok: heinzel-example, heinzel-github" in locks.stdout and gated_checks(world) == []  # cheap, local, never the run lock
    world.local = []
    done = invoke(rt(), ["--format", "json", "release", "rehearse", "consumers", "--full"])
    doc = rehearse.ConsumerChecks.model_validate(json.loads(done.stdout))
    assert done.exit_code == 1 and doc.status == "unavailable" and "--allow-full" in doc.detail and doc.failed == [] and doc.locked == ["heinzel-example", "heinzel-github"]
    assert gated_checks(world) == [] and world.local[-1][3:6] == ["worktree", "remove", "--force"]  # nothing ran; its worktree is removed all the same
    world.edge_runs = []
    none = invoke(rt(), ["release", "rehearse", "consumers"])
    assert none.exit_code == 1 and f"Consumer checks unavailable: no green Edge build of main {MAIN[:10]} yet" in none.stdout


def test_a_core_extra_that_adds_a_dependency_makes_a_stale_consumer_lock_red(rt, world):
    """yluq / 0.9.40: core added pyjwt[crypto] through an extra; plugins main still held locks made against 0.9.39, so
    compose's validate_lock refused the candidate. The rehearsal, without any option, must say red before the cut."""
    world.core = kit.CORE_0940
    world.plugins["heinzel-example"] = kit.CORE_0940  # this one was relocked; heinzel-github was not
    started = invoke(rt(), ["release", "rehearse", "start", "--lanes", "cli", "--os", "ubuntu-24.04"])
    consumers = state(rt)["current"]["consumers"]
    assert started.exit_code == 0 and consumers["stale"] == [STALE] and consumers["locked"] == ["heinzel-example"] and consumers["plugins"] == PLUGINS_MAIN
    assert gated_checks(world) == [] and not consumers["full"]
    world.finish()
    red = invoke(rt(), ["release", "rehearse", "step"])
    assert red.exit_code == 1 and red.stdout == f"rehearsal red: REHEARSAL RED main {MAIN[:10]}: Consumer lock ({STALE})\n"
    assert rehearse.rehearsal_of(state(rt)["last"]).consumers.stale == [STALE] and "last_green" not in state(rt)
    alone = invoke(rt(), ["release", "rehearse", "consumers"])
    assert alone.exit_code == 1 and f"  STALE LOCK: {STALE}" in alone.stdout.splitlines()


def test_a_consumer_that_does_not_allow_the_candidate_is_red_not_skipped(rt, world):
    world.ranges["heinzel-github"] = ">=0.9.1,<0.9.40"
    done = invoke(rt(), ["--format", "json", "release", "rehearse", "consumers"])
    doc = rehearse.ConsumerChecks.model_validate(json.loads(done.stdout))
    assert done.exit_code == 1 and doc.stale == ["heinzel-github: heinzel-github does not allow core 0.9.40"] and doc.locked == ["heinzel-example"]


def test_a_plugins_main_without_consumers_or_a_lock_check_that_cannot_run_is_never_green(rt, world):
    world.plugins = {}
    empty = invoke(rt(), ["release", "rehearse", "start", "--lanes", "cli", "--os", "ubuntu-24.04"])
    assert empty.exit_code == 0 and state(rt)["current"]["consumers"]["stale"] == [f"no consumer on plugins main {PLUGINS_MAIN[:10]}"]
    world.finish()
    assert invoke(rt(), ["release", "rehearse", "step"]).exit_code == 1 and "Consumer lock (no consumer on plugins main" in state(rt)["last"]["failures"][0]
    world.plugins = {"heinzel-example": kit.CORE_0939}
    real = world.run
    world.run = lambda argv, cwd=None, timeout=None, env=None: Result(1, "", "error: Failed to spawn: `python`") if argv[0] == "uv" else real(argv, cwd, timeout, env)
    rt.clock.t += 3600
    invoke(rt(), ["release", "rehearse", "start", "--lanes", "cli", "--os", "ubuntu-24.04"])
    world.finish()
    out = invoke(rt(), ["release", "rehearse", "step"])
    assert out.exit_code == 0 and "REHEARSAL INCOMPLETE" in out.stdout and "the lock check of plugins main did not run: error: Failed to spawn" in out.stdout


def test_without_an_edge_build_yet_the_lock_check_waits_for_the_next_step_while_lanes_run(rt, world):
    world.edge_runs = []
    invoke(rt(), ["release", "rehearse", "start", "--lanes", "cli", "--os", "ubuntu-24.04"])
    consumers = state(rt)["current"]["consumers"]
    assert consumers["status"] == "pending" and "no green Edge build" in consumers["detail"]
    world.edge_runs = [{"id": 7, "status": "completed", "conclusion": "success", "head_sha": MAIN}]
    world.finish()
    green = invoke(rt(), ["release", "rehearse", "step"])
    assert green.exit_code == 0 and green.stdout.startswith("rehearsal green") and "detail" not in state(rt)["last"]["consumers"]
    assert state(rt)["last"]["consumers"]["locked"] == ["heinzel-example", "heinzel-github"]


def test_a_rehearsal_with_consumers_is_red_when_a_plugin_check_fails(rt, world):
    world.checks = {"NAME=heinzel-example": 1}
    started = invoke(rt(), ["release", "rehearse", "start", "--consumers", "--allow-full", "--lanes", "cli", "--os", "ubuntu-24.04"])
    assert started.exit_code == 0 and state(rt)["current"]["consumers"]["failed"] == ["heinzel-example"]
    world.finish()
    red = invoke(rt(), ["release", "rehearse", "step"])
    assert red.exit_code == 1 and "Consumer (heinzel-example)" in red.stdout and len(gated_checks(world)) == 2
    assert rehearse.rehearsal_of(state(rt)["last"]).consumers.failed == ["heinzel-example"]


def test_a_candidate_starting_mid_flight_preempts_the_rehearsal_which_cancels_only_its_own_lanes(rt, world):
    invoke(rt(), ["release", "rehearse", "start", *TWO])
    world.runs[101].update(status="completed", conclusion="success")
    world.others_active = [{"id": 999, "status": "in_progress", "head_branch": "release/v0.9.41", "head_sha": "9" * 40}]
    done = invoke(rt(), ["release", "rehearse", "step"])
    assert done.exit_code == 0 and done.stdout.startswith(f"rehearsal incomplete: REHEARSAL INCOMPLETE main {MAIN[:10]}: gave way mid-flight (a release started (release/v0.9.41))")
    assert world.written() == [*STARTED, "api -X POST actions/runs/102/cancel", *DELETED]  # never run 999, never a branch that is not its own
    assert rehearse.last_verdict(rt.store)["preempted"] is True


# ----------------------------------------------------------------------------- the binding
def test_the_binding_reads_settings(rt, world, tmp_path):
    runtime = rt()
    rehearse.bound(rehearse.NativeRuntime(runtime.providers, runtime.settings, runtime.project))
    bound_to = {"repo": lib.R, "work": lib.WORK, "plugins": lib.PLUGINS, "max_open": lib.MAX_OPEN_MINUTES}
    assert bound_to == {"repo": REPO, "work": tmp_path / "work", "plugins": tmp_path / "plugins", "max_open": 180}
    assert lib.queue_path() == tmp_path / "legacy" / legacy.QUEUE_STATE
    assert others.queue_file({"queue_state_file": str(tmp_path / "q.json")}) == tmp_path / "q.json"
    assert others.queue_file({}) == statedir.root() / legacy.QUEUE_STATE and others.queue_file({}).is_absolute()  # never None/queue.json
    assert others.queue_file({}, tmp_path / "home") == tmp_path / "home" / legacy.QUEUE_STATE
    lib.bind(lib.EDGE, REPO, lib.PLUGINS, lib.WORK, lib.QUEUE, 600)
    assert lib.MAX_OPEN_MINUTES == 180  # a setting may lower the bound, never raise it
    lib.bind(lib.EDGE, REPO, lib.PLUGINS, lib.WORK, lib.QUEUE, 60)
    assert lib.MAX_OPEN_MINUTES == 60


def test_a_cancel_github_does_not_confirm_is_said_and_never_repeated(rt, world):
    invoke(rt(), ["release", "rehearse", "start", "--lanes", "cli", "--os", "ubuntu-24.04"])
    real = world.answer
    world.answer = lambda argv: 500 if argv[-1].endswith("/cancel") else real(argv)
    done = invoke(rt(), ["release", "rehearse", "cancel"])
    assert done.exit_code == 0 and "rehearsal: cancel of run 101: unknown" in done.stderr
    assert [line for line in world.written() if "cancel" in line] == ["api -X POST actions/runs/101/cancel"]


def test_the_sha_guard_of_the_single_lane_library_holds_for_a_rehearsal_lane(rt, world):
    world.pulls = [{"number": 790, "head": {"ref": "fix/x", "sha": COMMIT}}]  # GitHub's own check of the new commit did not see it yet
    done = invoke(rt(), ["release", "rehearse", "start", "--lanes", "cli", "--os", "ubuntu-24.04"])
    assert done.exit_code == 0 and "0/1 lanes done; waiting: " in done.stdout and "it is the head of open PR #790" in done.stderr
    assert [line for line in world.written() if "dispatches" in line] == [] and state(rt)["current"]["lanes"][0]["status"] == "pending"


def test_github_down_stops_the_consumer_checks_and_an_unreadable_state_stops_status(rt, world):
    world.down = True
    done = invoke(rt(), ["release", "rehearse", "consumers", "--allow-full"])
    assert done.exit_code == 1 and "the Consumer checks stopped: GitHub API failed: git/ref/heads/main" in done.stderr and world.local == []
    rt.store.save(lib.KIND, lib.KEY, {"last": {"verdict": "red"}})
    bad = invoke(rt(), ["release", "rehearse", "status"])
    assert bad.exit_code == 1 and "the rehearsal state cannot be read" in bad.stderr


def test_every_command_has_help_in_contract_form_and_names_no_former_tool(world):
    from steuermaschine.helpdoc import missing
    from steuermaschine.native import collect

    found = collect(rehearse)
    assert sorted(c.path for c in found if hasattr(c, "fn")) == ["daemon rehearse", "release rehearse cancel", "release rehearse consumers", "release rehearse plan",
                                                                 "release rehearse start", "release rehearse status", "release rehearse step"]
    for command in found:
        assert missing(command.help) == "" and "hz-" not in command.help and "hz_" not in command.help, command.path
    from steuermaschine.reviewgraph import reach

    writes = [c for c in found if getattr(c, "writes", False)]
    assert {c.path for c in writes} == {"daemon rehearse", "release rehearse cancel", "release rehearse consumers", "release rehearse start", "release rehearse step"}
    units = [{name.rsplit("/", 1)[-1] for name in unit.files} for unit in reach(writes)]
    assert len(units) == len(writes)
    assert all({"rehearse.py", "rehearsal.py", "focused.py", "rules.py", "dispatch.py", "lock.py", "refuse.py"} <= unit for unit in units)
    assert next(c for c in found if c.path == "daemon rehearse").tick
