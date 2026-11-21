"""`sm ci one ...` through the whole command tree, on fakes: text and json,
the review gate of the write commands, and the exact order of GitHub calls of one dispatch (a scripted transcript)."""

import hashlib
import json
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel.native import dispatch
from steuermaschine_heinzel.native.dispatch_lib import focused
from steuermaschine_heinzel.plugin import plugin

REPO = "example/core"
TIP = "c" * 40
WORKFLOW = "focused-job.yml"


class Commands(FakeGh):
    """The scripted gh, plus git and the gated push for the validation tests."""

    def __init__(self):
        super().__init__()
        self.git = []

    def run(self, argv, cwd=None, timeout=None, env=None):
        if argv[0] == "git":
            self.git.append(list(argv))
            return Result(0, child(argv[-1]) + "\n") if "commit-tree" in argv else Result(0, "")
        if argv[0] == "ps":
            return Result(0, "1\n")
        return super().run(argv, cwd, timeout, env)

    def passthrough(self, argv, cwd=None, env=None, timeout=None):
        self.git.append(list(argv))
        return 0


@pytest.fixture
def rt(tmp_path):
    def build(reviewed=True, commands=None):
        stack = stack_of(plugin)
        table = {"repo": REPO, "dispatch_state_file": str(tmp_path / "runs.jsonl"), "core_repo_dir": str(tmp_path / "clone"),
                 "dispatch_scratch_dir": str(tmp_path / "scratch"), "lock_directory": str(tmp_path / "locks"), "lock_history": str(tmp_path / "history.jsonl")}
        (tmp_path / "locks").mkdir(exist_ok=True)
        settings, _ = stack.validate({"heinzel": table}, tmp_path)
        providers = stack.providers(settings, fake_providers(commands=commands or Commands()))  # the plugin's extras, as in a real run
        return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=reviewed), stack, settings, _providers=providers)

    return build


@pytest.fixture
def registry(monkeypatch):
    """The plan registry read, answered empty: its decoder belongs to the admission area and has its own tests."""
    monkeypatch.setattr(focused, "plan_registry_keys", lambda: set())


def idle_github(gh: FakeGh) -> FakeGh:
    """A repository with the workflow on main and on the branch, nothing running, no PR on the tip."""
    gh.on(f"contents/.github/workflows/{WORKFLOW}", "a" * 40 + "\n")
    gh.on("git/ref/heads/validation/clock", {"ref": "refs/heads/validation/clock", "object": {"type": "commit", "sha": TIP}})
    gh.on("pulls?state=open", [[]])
    gh.on("actions/variables", [{"total_count": 0, "variables": []}])
    gh.on(f"commits/{TIP}/branches-where-head", [{"name": "validation/clock"}])
    gh.on("actions/runs?status=", [{"total_count": 0, "workflow_runs": []}])
    gh.on(f"workflows/{WORKFLOW}/dispatches", {"workflow_run_id": 123, "html_url": f"https://github.com/{REPO}/actions/runs/123"})
    return gh


def test_repeats_in_text_and_json(rt):
    assert invoke(rt(), ["ci", "one", "repeats", "--fail-rate", "1/3"]).stdout == "9\n"
    doc = json.loads(invoke(rt(), ["--format", "json", "ci", "one", "repeats"]).stdout)
    assert dispatch.Rounds.model_validate(doc).rounds == 10
    bad = invoke(rt(), ["ci", "one", "repeats", "--fail-rate", "often"])
    assert bad.exit_code == 2 and "is not a rate" in bad.stderr


def test_status_without_runs_and_with_a_paused_admission(rt, tmp_path):
    assert invoke(rt(), ["ci", "one", "status"]).stdout == "no focused run in the last 24 h (max 5 in flight)\n"
    (tmp_path / "runs.jsonl").write_text(json.dumps({"run": 9, "kind": "dispatch", "what": "dispatch e2e web-2", "branch": "validation/x", "attempt": 1,
                                                     "at": "2026-11-12T21:00:00", "t": 1_800_000_000.0, "result": "success", "tally": "3/3 green"}) + "\n")
    (tmp_path / "runs.pending").write_text("{}\n")
    doc = json.loads(invoke(rt(), ["--format", "json", "ci", "one", "status"]).stdout)
    status = dispatch.FocusedStatus.model_validate(doc)
    assert status.in_flight == 0 and status.runs[0].tally == "3/3 green" and status.paused.endswith("runs.pending")
    text = invoke(rt(), ["ci", "one", "status", "--branch", "validation/x"]).stdout
    assert "9 dispatch dispatch e2e web-2 [3/3 green] (since 21:00)" in text and "admission paused" in text




def test_one_dispatch_makes_exactly_these_github_calls_in_this_order(rt, registry):
    gh = idle_github(Commands())
    runtime = rt(commands=gh)
    done = invoke(runtime, ["--format", "json", "ci", "one", "dispatch", "validation/clock", "--suite", "web-2", "--os", "ubuntu-24.04"])
    assert done.exit_code == 0, done
    result = dispatch.FocusedRun.model_validate(json.loads(done.stdout))
    assert result.run == 123 and result.status == "started" and result.lines[-1] == "wait with: sm ci one watch 123"
    calls = [" ".join(call[1:]) for call in gh.calls]
    posts = [call for call in calls if "--method POST" in call]
    assert len(posts) == 1 and posts[0].endswith(
        f"repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches -f ref=refs/heads/validation/clock -f inputs[ref]=validation/clock "
        "-f inputs[os]=ubuntu-24.04 -f inputs[kind]=e2e -f inputs[selector]=web-2 -f inputs[repeat]=1 -f inputs[stress]=false")
    order = [next(i for i, call in enumerate(calls) if needle in call) for needle in
             ("contents/.github/workflows", "git/ref/heads/validation/clock", "pulls?state=open", "branches-where-head", "actions/runs?status=", "dispatches")]
    assert order == sorted(order)  # workflow present, tip, SHA guard, capacity, and only then the one write
    assert calls.index(posts[0]) == len(calls) - 1  # nothing is read or written after the start
    row = json.loads((Path(runtime.settings["dispatch_state_file"])).read_text())
    assert row["run"] == 123 and row["branch"] == "validation/clock" and row["result"] is None
    assert not Path(runtime.settings["dispatch_state_file"]).with_suffix(".pending").exists()
    assert any("ci one: dispatch e2e web-2" in line for page in runtime.providers.store.keys("heinzel.journal")
               for line in runtime.providers.store.load("heinzel.journal", page)["lines"])


def test_the_sha_guard_refuses_a_pr_head_with_exit_2_and_makes_no_write(rt, registry):
    gh = idle_github(Commands())
    gh.on("pulls?state=open", [[{"number": 790, "head": {"sha": TIP}}]])
    done = invoke(rt(commands=gh), ["ci", "one", "dispatch", "validation/clock", "--suite", "web-2"])
    assert done.exit_code == 2 and "it is the head of open PR #790" in done.stderr and "sm ci one dispatch validation/clock --throwaway" in done.stderr
    assert not [call for call in gh.calls if "POST" in call]
    usage = invoke(rt(commands=gh), ["ci", "one", "dispatch", "main", "--suite", "web-2"])
    assert usage.exit_code == 1 and "main is main or a release candidate" in usage.stderr
    assert invoke(rt(commands=gh), ["ci", "one", "dispatch", "x", "--suite", "web-2", "--os", "windows"]).exit_code == 2


@pytest.mark.parametrize("use, needle", [
    ("admitted", f"the focused plan registry holds pull_request:{TIP[:8]}"),
    ("pr-branch", "fix/clock"),
])
def test_the_sha_guard_refuses_an_admitted_head_even_with_force(rt, use, needle):
    """w7z1 (#790): an admitted head (a registry key, its PR not listed open yet) or the tip of a PR branch refuses, --force too.
    The registry is read for real here (no `registry` fixture): the decoder of the admission area finds the key."""
    gh = idle_github(Commands())
    if use == "admitted":
        gh.on("actions/variables", [{"total_count": 1, "variables": [{"name": "FOCUSED_CI_PLANS", "value": json.dumps({f"pull_request:{TIP}": {}})}]}])
    else:
        gh.on(f"commits/{TIP}/branches-where-head", [{"name": "validation/clock"}, {"name": "fix/clock"}])
    for extra in ([], ["--force"]):
        done = invoke(rt(commands=gh), ["ci", "one", "dispatch", "validation/clock", "--suite", "web-2", *extra])
        assert done.exit_code == 2 and "is or will be a PR head" in done.stderr and needle in done.stderr, done
    assert not [call for call in gh.calls if "POST" in call]


def test_rerun_refuses_a_run_outside_the_ci_workflow_before_any_write(rt):
    gh = Commands()
    gh.on("actions/runs/123456", {"id": 123456, "status": "completed", "conclusion": "failure", "run_attempt": 1, "head_branch": "fix/x",
                                  "head_sha": TIP, "path": ".github/workflows/other.yml"})
    done = invoke(rt(commands=gh), ["ci", "one", "rerun", "123456"])
    assert done.exit_code == 1 and "is outside the CI workflow" in done.stderr and len(gh.calls) == 1


VBRANCH = "validation/feat-x-web-1-aaaaaaaa"  # one throwaway ref per lane
V2 = "validation/feat-x-web-2-aaaaaaaa"


def child(message: str) -> str:
    """The fake commit-tree: a SHA of its own per commit message (per lane)."""
    return hashlib.sha1(message.encode()).hexdigest()


def commit(lane: str) -> str:
    return child(f"ci: hosted validation of aaaaaaaa ({lane})")


def remote(exists=404) -> Commands:
    """The scripted remote of a validation: the lane branches (absent by default) and the cancel endpoint."""
    gh = Commands()
    gh.on("git/ref/heads/validation/feat-x-", exists)
    gh.on("actions/runs/", {"id": 1, "status": "in_progress", "head_branch": VBRANCH, "path": ".github/workflows/focused-job.yml", "event": "workflow_dispatch"})
    gh.on("/cancel", {})
    return gh








ALL = ["web-1", "web-2", "cli", "supervised"]


















def test_a_run_read_is_retried_a_bounded_number_of_times(rt):
    gh = remote()
    view = {"status": "completed", "conclusion": "success", "url": "u", "attempt": 1}
    gh.on("run view 7 ", 404, 502, view)
    runtime = bound_inventory(rt, gh)
    assert focused.run_view(7) == view and runtime.providers.clock.slept == [2.0, 5.0]
    gh.on("run view 8 ", 404)
    runtime = bound_inventory(rt, gh)
    with pytest.raises(focused.Unreadable, match=r"cannot read run 8: .* after 4 reads$"):
        focused.run_view(8)
    assert runtime.providers.clock.slept == [2.0, 5.0, 10.0] and len([call for call in gh.calls if "run view 8" in " ".join(call)]) == 4
    gh.on("run view 9 ", {"status": "completed"})  # no attempt: malformed, read again, then refused
    bound_inventory(rt, gh)
    with pytest.raises(focused.Unreadable, match="malformed answer for run 9"):
        focused.run_view(9)


def runs_page(total, *ids):
    return [{"total_count": total, "workflow_runs": [{"id": rid, "status": "queued", "head_branch": "x", "path": ".github/workflows/ci.yml@x"} for rid in ids]}]


def bound_inventory(rt, gh):
    runtime = rt(commands=gh)
    dispatch.bound(runtime, [], [])
    return runtime


@pytest.mark.parametrize("first", [runs_page(68, *range(1, 68)),  # counted after a run was queued, listed before
                                   runs_page(66, *range(1, 68)),  # a run left the status between the count and the list
                                   [*runs_page(101, *range(1, 101)), *runs_page(101, 100)]])  # a page shifted: an item twice, one missing
def test_an_inconsistent_inventory_read_is_read_again_and_a_consistent_one_is_used(rt, first):
    gh = remote()
    gh.on("actions/runs?status=queued", first, runs_page(67, *range(1, 68)))
    runtime = bound_inventory(rt, gh)
    items = focused.inventory("runs?status=queued&per_page=100", "workflow_runs")
    assert [run["id"] for run in items] == list(range(1, 68)) and runtime.providers.clock.slept == [2.0]
    assert len([call for call in gh.calls if "status=queued" in " ".join(call)]) == 2 and gh.writes() == []


@pytest.mark.parametrize("answer", [runs_page(9, 1), "not json", 502])
def test_an_inventory_that_stays_unreadable_refuses_after_three_bounded_reads(rt, answer):
    gh = remote()
    gh.on("actions/runs?status=queued", answer)
    runtime = bound_inventory(rt, gh)
    with pytest.raises(focused.Unreadable, match=r"after 3 reads$"):
        focused.inventory("runs?status=queued&per_page=100", "workflow_runs")
    assert len([call for call in gh.calls if "status=queued" in " ".join(call)]) == 3 and runtime.providers.clock.slept == [2.0, 5.0]


















def test_a_log_that_cannot_be_read_in_time_does_not_fail_a_finished_run(monkeypatch):
    def timed_out(*args, **kwargs):
        raise SystemExit("refusing: GitHub API unavailable: gh timed out after 300s")

    monkeypatch.setattr(focused, "gh", timed_out)
    assert focused.tally_of(123) == ""


def test_without_the_registry_decoder_a_dispatch_fails_closed(rt, monkeypatch):
    import builtins

    real = builtins.__import__

    def no_decoder(name, globals=None, locals=None, fromlist=(), level=0):
        if "admission_lib" in name or (fromlist and "focused_slots" in fromlist):
            raise ImportError(name)
        return real(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", no_decoder)
    gh = idle_github(Commands())
    done = invoke(rt(commands=gh), ["ci", "one", "dispatch", "validation/clock", "--suite", "web-2"])
    assert done.exit_code == 1 and "the SHA guard cannot be decided" in done.stderr and not [call for call in gh.calls if "POST" in call]






def _term_here():
    import signal

    assert signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, signal.SIG_IGN, None), "the validation's SIGTERM handler must be installed"
    signal.raise_signal(signal.SIGTERM)






def test_the_account_caps_come_from_the_settings_and_reach_the_capacity_rule(rt, tmp_path):
    assert dispatch.caps({}) == dispatch.c.Caps() and dispatch.caps(None) == dispatch.c.Caps()
    assert dispatch.caps({"ci_job_cap": 40, "ci_macos_cap": 0}) == dispatch.c.Caps(jobs=40, macos=0)
    runtime = rt(commands=remote())
    assert (runtime.settings.get("ci_job_cap"), runtime.settings.get("ci_macos_cap")) == (dispatch.c.JOB_CEILING, dispatch.c.ARM_LIMIT)  # the defaults of the table
    dispatch.bound(runtime, [], [])
    assert dispatch.c.Caps() == focused.CAPS
    focused.bind(focused.EDGE, focused.R, focused.STATE, focused.CORE_CLONE, focused.SCRATCH, dispatch.c.Caps(jobs=2, macos=1))
    try:
        assert focused.CAPS.jobs == 2
    finally:
        focused.bind(focused.EDGE, focused.R, focused.STATE, focused.CORE_CLONE, focused.SCRATCH)
    assert dispatch.c.Caps() == focused.CAPS
