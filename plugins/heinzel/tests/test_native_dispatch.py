"""`sm ci one ...`, `sm head validate` and `sm ci window ...` through the whole command tree, on fakes: text and json,
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
from steuermaschine_heinzel.native import dispatch, window
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


def test_the_review_units_and_files_of_the_write_commands():
    by_path = {cmd.path: cmd for cmd in (fn.__sm_native__ for fn in (dispatch.ci_one_dispatch, dispatch.ci_one_rerun, window.head_validate,
                                                                     window.ci_window_run, window.ci_window_release))}
    assert by_path["ci one rerun"].review_file == by_path["ci one dispatch"].review_file == "ci-one-dispatch.md"
    assert by_path["ci one rerun"].code_digest() == by_path["ci one dispatch"].code_digest()
    for path in ("head validate", "ci window run", "ci window release"):
        names = {f.name for f in by_path[path].code_files()}
        assert by_path[path].review_file == "head-validate.md" and {"window.py", "dispatch.py", "focused.py", "rules.py", "lock.py", "refuse.py"} <= names, path


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


def test_validate_plan_and_usage(rt):
    plan = invoke(rt(), ["head", "validate", ".", "--dry-run", "--lanes", "web-1,cli"])
    assert plan.exit_code == 0 and "would push one throwaway child commit per lane" in plan.stdout and "Nothing was pushed" in plan.stdout
    assert f"web-1: {VBRANCH}" in plan.stdout and "cli: validation/feat-x-cli-aaaaaaaa" in plan.stdout
    doc = window.Validation.model_validate(json.loads(invoke(rt(), ["--format", "json", "head", "validate", ".", "--dry-run"]).stdout))
    assert doc.status == "plan" and [row.lane for row in doc.lanes] == ["web-1", "web-2"]
    assert invoke(rt(), ["head", "validate", ".", "--lanes", "nope"]).exit_code == 2
    assert invoke(rt(), ["head", "validate", ".", "--os", "windows"]).exit_code == 2
    runtime = rt()
    runtime.providers.vcs.b = "main"
    assert "never on main" in invoke(runtime, ["head", "validate", "."]).stderr
    runtime = rt()
    runtime.providers.vcs.dirt = " M file.py"
    assert "dirty tree" in invoke(runtime, ["head", "validate", "."]).stderr


@pytest.fixture
def lanes(monkeypatch):
    """The single-lane library answering lane by lane: each start gets the next run id, each run is green unless scripted."""
    world = {"started": [], "deleted": [], "conclusions": {}, "refuse": {}, "in_flight": 0, "tip_moved": False, "args": []}

    def cmd_dispatch(args):
        world["args"].append(args)
        if args.suite in world["refuse"]:
            why = world["refuse"][args.suite]
            raise why if isinstance(why, BaseException) else SystemExit(why)
        world["started"].append((args.branch, args.suite, args.os, args.throwaway))
        rid = len(world["started"])
        focused.out(f"dispatch e2e {args.suite} x1 on {args.os} @ {args.branch}: run {rid}")
        return wait(rid, None, False, args.suite) if args.wait else 0  # --serial: the library waits for the result

    def wait(rid, names, as_json, label, attempt=1):
        if world["conclusions"].get(rid) == "TERM":
            raise window.Terminated()
        if world["conclusions"].get(rid) == "UNREAD":
            raise focused.Unreadable(f"refusing: cannot read run {rid}: HTTP 404 after 4 reads")
        conclusion = world["conclusions"].get(rid, "success")
        focused.EDGE.result = {"run": rid, "status": "completed", "conclusion": conclusion, "faillog": "E   assert 1 == 2",
                               "jobs": [] if conclusion == "cancelled" else [{"name": "e2e", "conclusion": conclusion}]}
        return 0

    def delete_throwaway(branch, sha):
        if world["tip_moved"]:
            return False
        world["deleted"].append((branch, sha))
        return True

    monkeypatch.setattr(focused, "cmd_dispatch", cmd_dispatch)
    monkeypatch.setattr(focused, "wait", wait)
    monkeypatch.setattr(focused, "delete_throwaway", delete_throwaway)
    monkeypatch.setattr(window, "in_flight", lambda lanes=0, wants_arm=False: world["in_flight"])
    return world


def test_validate_pushes_a_throwaway_commit_through_the_run_lock_runs_every_lane_and_cleans_up(rt, lanes):
    runtime = rt(commands=remote())
    done = invoke(runtime, ["head", "validate", ".", "--max-wait", "0"])
    assert done.exit_code == 0 and "VALIDATED aaaaaaaaa: web-1, web-2 green on ubuntu-24.04. Next: sm head admit" in done.stdout, done
    assert lanes["started"] == [(VBRANCH, "web-1", "ubuntu-24.04", False), (V2, "web-2", "ubuntu-24.04", False)]
    assert all(args.wait_for_slot and not args.wait for args in lanes["args"])  # every lane waits for its slot, none blocks the next start
    git = runtime.providers.commands.git
    head = "a" * 40
    assert git[0][3:] == ["commit-tree", f"{head}^{{tree}}", "-p", head, "-m", "ci: hosted validation of aaaaaaaa (web-1)"]  # the head read at the start
    assert git[1][3:] == ["commit-tree", f"{head}^{{tree}}", "-p", head, "-m", "ci: hosted validation of aaaaaaaa (web-2)"]  # same tree, own commit
    # one atomic push of both lane branches, under the run lock
    assert git[2][-5:] == ["push", "--atomic", "origin", f"{commit('web-1')}:refs/heads/{VBRANCH}", f"{commit('web-2')}:refs/heads/{V2}"]
    assert lanes["deleted"] == [(VBRANCH, commit("web-1")), (V2, commit("web-2"))] and f"removed {VBRANCH}" in done.stdout and f"removed {V2}" in done.stdout
    assert window.the_window(runtime).status().active is None  # the window was given back


ALL = ["web-1", "web-2", "cli", "supervised"]


@pytest.mark.parametrize("serial", [False, True])
def test_four_lanes_run_on_four_refs_of_their_own_and_cleanup_removes_every_one(rt, lanes, serial):
    """Lanes on one ref share the focused workflow's concurrency group, and GitHub keeps one pending run per group."""
    runtime = rt(commands=remote())
    refs = [f"validation/feat-x-{lane}-aaaaaaaa" for lane in ALL]
    done = invoke(runtime, ["--format", "json", "head", "validate", ".", "--lanes", ",".join(ALL), *(["--serial"] if serial else [])])
    doc = window.Validation.model_validate(json.loads(done.stdout))
    assert done.exit_code == 0 and doc.status == "validated" and doc.validation_branches == refs, done
    assert [(row.lane, row.branch, row.commit, row.run) for row in doc.lanes] == [(lane, ref, commit(lane), n) for n, (lane, ref) in enumerate(zip(ALL, refs), 1)]
    assert [branch for branch, *_ in lanes["started"]] == refs and len({commit(lane) for lane in ALL} | {"a" * 40}) == 5  # never the head SHA
    assert all(args.wait is serial for args in lanes["args"])
    pushes = [argv for argv in runtime.providers.commands.git if "push" in argv]
    assert len(pushes) == 1 and pushes[0][-4:] == [f"{commit(lane)}:refs/heads/{ref}" for lane, ref in zip(ALL, refs)]
    assert lanes["deleted"] == [(ref, commit(lane)) for lane, ref in zip(ALL, refs)]
    assert window.the_window(runtime).status().active is None


def test_a_run_cancelled_while_queued_is_no_verdict_on_the_head(rt, lanes):
    lanes["conclusions"] = {2: "cancelled", 4: "cancelled"}  # a newer run in its concurrency group, or a cancel by hand (run ids go on)
    gh = remote()
    done = invoke(rt(commands=gh), ["head", "validate", "."])
    assert done.exit_code == 75 and "web-2: run 2 completed/cancelled  cancelled while queued, not by this command" in done.stdout, done
    assert "NOT VALIDATED" not in done.stdout and "Nothing is wrong with the head" in done.stdout and "assert 1 == 2" not in done.stdout
    assert not [call for call in gh.calls if "/cancel" in " ".join(call)] and len(lanes["deleted"]) == 2
    doc = window.Validation.model_validate(json.loads(invoke(rt(commands=remote()), ["--format", "json", "head", "validate", "."]).stdout))
    assert doc.status == "incomplete" and doc.lanes[1].cancelled == "queued" and doc.lanes[0].cancelled == ""
    lanes["conclusions"] = {5: "failure", 6: "cancelled"}  # a real failure next to it is still a verdict
    doc = window.Validation.model_validate(json.loads(invoke(rt(commands=remote()), ["--format", "json", "head", "validate", "."]).stdout))
    assert doc.status == "not_validated" and doc.exit_code == 1


def test_release_removes_every_lane_branch_a_gone_holder_recorded(rt, monkeypatch):
    import socket

    gh = remote()
    left = ["validation/fix-dead-web-1-bbbbbbbb", "validation/fix-dead-cli-bbbbbbbb"]
    for name in left:
        gh.on(f"git/ref/heads/{name}", {"ref": f"refs/heads/{name}", "object": {"type": "commit", "sha": "e" * 40}})
        gh.on(f"git/refs/heads/{name}", {})
    gh.on("actions/runs?status=", [{"total_count": 0, "workflow_runs": []}])
    runtime = rt(commands=gh)
    w = window.the_window(runtime)
    w.enter("fix/dead", "b" * 40, 2, 0, owner="gone", pid=999_999_999, host=socket.gethostname())
    w.note("gone", {"validation_branch": " ".join(left), "commit": "e" * 40})
    done = json.loads(invoke(runtime, ["--format", "json", "ci", "window", "release", "--remove"]).stdout)
    assert done["released"] and done["branch_removed"] == " ".join(left) and done["notes"] == [f"removed {name}" for name in left]
    assert [call[-1] for call in gh.calls if "DELETE" in call] == [f"repos/{REPO}/git/refs/heads/{name}" for name in left]


def test_a_red_lane_is_not_validated_and_the_branch_is_still_removed(rt, lanes):
    lanes["conclusions"][2] = "failure"
    done = invoke(rt(commands=remote()), ["--format", "json", "head", "validate", "."])
    doc = window.Validation.model_validate(json.loads(done.stdout))
    assert done.exit_code == 1 and doc.status == "not_validated" and doc.lanes[1].fail_log == "E   assert 1 == 2" and len(lanes["deleted"]) == 2
    lanes["conclusions"] = {4: "failure"}
    text = invoke(rt(commands=remote()), ["head", "validate", "."]).stdout
    assert "NOT VALIDATED: 1 of 2 lanes" in text and "      E   assert 1 == 2" in text


def test_a_lane_that_cannot_start_ends_the_set_cancels_the_started_runs_and_is_no_verdict_on_the_head(rt, lanes):
    lanes["refuse"]["web-2"] = "refusing: 19 active + 1 requested exceed the 20-job ceiling; main first"
    gh = remote()
    runtime = rt(commands=gh)
    done = invoke(runtime, ["head", "validate", "."])
    assert done.exit_code == 75 and "INCOMPLETE: not every lane could start" in done.stdout and "Nothing is wrong with the head" in done.stdout, done
    assert "web-1: run 1 cancelled" in done.stdout and "web-2: not started  19 active + 1 requested" in done.stdout and "NOT VALIDATED" not in done.stdout
    assert [call for call in gh.calls if "/cancel" in " ".join(call)] and lanes["deleted"] == [(VBRANCH, commit("web-1")), (V2, commit("web-2"))]
    assert window.the_window(runtime).status().active is None


def test_an_unreadable_inventory_after_started_lanes_refuses_the_next_lane_and_keeps_the_started_runs(rt, lanes):
    lanes["refuse"]["web-2"] = focused.Unreadable("refusing: incomplete GitHub workflow_runs inventory after 3 reads")
    gh = remote()
    runtime = rt(commands=gh)
    done = invoke(runtime, ["head", "validate", "."])
    assert done.exit_code == 75 and "could not be read; the started runs were kept and watched" in done.stdout, done
    assert "web-1: run 1 kept completed/success" in done.stdout and "web-2: not started  incomplete GitHub workflow_runs inventory" in done.stdout
    assert not [call for call in gh.calls if "/cancel" in " ".join(call)]  # a read error never cancels a started run
    assert len(lanes["deleted"]) == 2 and window.the_window(runtime).status().active is None  # removed only after the watch


def test_a_kept_lane_that_failed_is_a_verdict_even_when_the_next_lane_could_not_start(rt, lanes):
    lanes["refuse"]["web-2"] = focused.Unreadable("refusing: malformed GitHub workflow_runs inventory after 3 reads")
    lanes["conclusions"][1] = "failure"
    gh = remote()
    done = invoke(rt(commands=gh), ["--format", "json", "head", "validate", "."])
    doc = window.Validation.model_validate(json.loads(done.stdout))
    assert done.exit_code == 1 and doc.status == "not_validated" and doc.lanes[0].conclusion == "failure" and doc.lanes[1].run is None
    assert not [call for call in gh.calls if "/cancel" in " ".join(call)]


@pytest.mark.parametrize("serial", [False, True])
def test_a_lane_whose_green_run_cannot_be_read_is_incomplete_never_not_validated(rt, lanes, serial):
    """819m: run 2 succeeded on GitHub, but its read kept failing: no verdict on the head, exit 75."""
    lanes["conclusions"][2] = "UNREAD"
    gh = remote()
    done = invoke(rt(commands=gh), ["head", "validate", ".", *(["--serial"] if serial else [])])
    assert done.exit_code == 75 and "NOT VALIDATED" not in done.stdout, done
    assert "web-2: run 2 -/-  result unread: cannot read run 2: HTTP 404 after 4 reads" in done.stdout and "INCOMPLETE: GitHub did not answer" in done.stdout
    assert not [call for call in gh.calls if "/cancel" in " ".join(call)] and len(lanes["deleted"]) == 2
    lanes["conclusions"] = {3: "failure", 4: "UNREAD"}  # a real failure next to an unread lane is still a verdict
    doc = window.Validation.model_validate(json.loads(invoke(rt(commands=remote()), ["--format", "json", "head", "validate", ".", *(["--serial"] if serial else [])]).stdout))
    assert doc.status == "not_validated" and doc.lanes[1].unread and not doc.lanes[1].failed
    assert "NOT VALIDATED: 1 of 2 lanes" in "\n".join(doc.render())


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


def test_runs_in_flight_are_counted_from_githubs_inventory_and_a_full_set_waits_before_any_push(rt, tmp_path, monkeypatch):
    def run(rid, workflow="focused-job.yml", branch="validation/other"):
        return {"id": rid, "status": "in_progress", "head_branch": branch, "path": f".github/workflows/{workflow}@{branch}"}

    gh = remote()
    foreign = [run(1), run(2), run(3), run(4), run(9, "ci.yml", "fix/pr")]  # four focused runs this machine never started
    gh.on("actions/runs?status=in_progress", [{"total_count": 5, "workflow_runs": foreign}])
    gh.on("actions/runs?status=", [{"total_count": 0, "workflow_runs": []}])
    runtime = rt(commands=gh)
    done = invoke(runtime, ["head", "validate", ".", "--max-wait", "0"])
    assert done.exit_code == 75 and "4 runs in flight + 2 wanted > cap 5" in done.stderr and "Nothing was pushed" in done.stderr, done
    assert gh.git == [] and not [call for call in gh.calls if "POST" in call or "--method" in call]
    gh.on("actions/runs?status=in_progress", [{"total_count": 2, "workflow_runs": [run(1), run(7, "ci.yml", "release/v1")]}])
    held = invoke(runtime, ["head", "validate", ".", "--os", "macos-latest", "--max-wait", "0"])  # macOS lanes wait for the candidate; Linux lanes go on
    assert held.exit_code == 75 and "a release/* run is active" in held.stderr and "needs macOS" in held.stderr and gh.git == []
    gh.on("actions/runs?status=in_progress", [{"total_count": 9, "workflow_runs": [run(1)]}])  # a truncated listing
    unsure = invoke(runtime, ["head", "validate", ".", "--max-wait", "0"])
    assert unsure.exit_code == 1 and "cannot count the focused runs in flight" in unsure.stderr and gh.git == []
    gh.on("actions/runs?status=in_progress", [{"total_count": 0, "workflow_runs": []}])
    (tmp_path / "runs.pending").write_text("{}\n")
    paused = invoke(runtime, ["head", "validate", ".", "--max-wait", "0"])
    assert paused.exit_code == 1 and "reconcile the uncertain focused start" in paused.stderr and gh.git == []


def test_a_second_command_for_the_same_head_waits_and_leaves_the_first_ones_turn_and_branch_alone(rt, lanes):
    runtime = rt(commands=remote())
    first = window.the_window(runtime)
    first.alive = lambda turn: True
    assert first.enter("feat/x", "a" * 40, 2, 0, owner="the-first-command", pid=4242, host="here").state == "go"
    done = invoke(runtime, ["head", "validate", ".", "--max-wait", "0"])
    assert done.exit_code == 75 and "already being validated by another command (pid 4242 on here)" in done.stderr
    assert runtime.providers.commands.git == [] and lanes["started"] == [] and lanes["deleted"] == []
    assert window.the_window(runtime).status().active.owner == "the-first-command"  # still held by the first command


def test_a_second_head_waits_for_the_window_with_exit_75_and_pushes_nothing(rt, lanes):
    runtime = rt(commands=remote())
    window.the_window(runtime).enter("fix/other", "b" * 40, 2, in_flight=0, owner="other", pid=1, host="elsewhere")
    done = invoke(runtime, ["head", "validate", ".", "--max-wait", "0"])
    assert done.exit_code == 75 and "fix/other bbbbbbbbb holds the window" in done.stderr and "Nothing was pushed" in done.stderr
    assert runtime.providers.commands.git == [] and lanes["started"] == []
    status = invoke(runtime, ["ci", "window", "status"]).stdout
    assert "active   fix/other bbbbbbbbb" in status and "waiting  1. feat/x aaaaaaaaa (2 runs)" in status
    ran = invoke(runtime, ["ci", "window", "run", "a", "b", "--run"])
    assert ran.exit_code == 75 and "a: WAIT" in ran.stdout and "b: not started" in ran.stdout


def test_a_holder_whose_command_is_gone_is_passed_and_release_ends_a_turn_by_hand(rt, lanes):
    import socket

    runtime = rt(commands=remote())
    dead = dict(owner="gone", pid=999_999_999, host=socket.gethostname())
    w = window.the_window(runtime)
    w.enter("fix/dead", "b" * 40, 2, 0, **dead)
    w.note("gone", {"validation_branch": "validation/fix-dead-bbbbbbbb", "commit": "e" * 40})
    assert "ITS COMMAND IS GONE" in invoke(runtime, ["ci", "window", "status"]).stdout
    done = invoke(runtime, ["head", "validate", "."])
    assert done.exit_code == 0 and "held by a command that is gone (fix/dead bbbbbbbbb); it left validation/fix-dead-bbbbbbbb" in done.stdout
    assert "sm ci window release --remove-branch validation/fix-dead-bbbbbbbb" in done.stdout
    # release by hand: nobody, a live holder (refused without --force), a dead one
    assert invoke(runtime, ["ci", "window", "release"]).stdout == "nobody holds the window\n"
    live = dict(owner="live", pid=1, host=socket.gethostname())
    w.enter("fix/live", "c" * 40, 1, 0, **live)
    kept = json.loads(invoke(runtime, ["--format", "json", "ci", "window", "release"]).stdout)
    assert window.WindowRelease.model_validate(kept).released is False and "its command still runs" in kept["reason"]
    assert "(forced)" in invoke(runtime, ["ci", "window", "release", "--force"]).stdout
    w.enter("fix/dead", "b" * 40, 2, 0, **dead)
    w.note("gone", {"validation_branch": "validation/fix-dead-bbbbbbbb", "commit": "e" * 40})
    freed = invoke(runtime, ["ci", "window", "release"]).stdout
    assert "its command is gone" in freed and "it left validation/fix-dead-bbbbbbbb on the remote: sm ci window release --remove-branch" in freed


def test_remove_branch_deletes_a_leftover_validation_branch_only_when_no_run_is_active_on_it(rt, monkeypatch):
    gh = remote()
    left = "validation/fix-dead-bbbbbbbb"
    gh.on(f"git/ref/heads/{left}", {"ref": f"refs/heads/{left}", "object": {"type": "commit", "sha": "e" * 40}})
    gh.on(f"git/refs/heads/{left}", {})
    busy = {"id": 77, "status": "queued", "head_branch": left, "path": ".github/workflows/focused-job.yml@x"}
    gh.on("actions/runs?status=queued", [{"total_count": 1, "workflow_runs": [busy]}])
    gh.on("actions/runs?status=", [{"total_count": 0, "workflow_runs": []}])
    runtime = rt(commands=gh)
    refused = invoke(runtime, ["ci", "window", "release", "--remove-branch", left])
    assert refused.exit_code == 1 and "has active runs (77)" in refused.stderr and not [call for call in gh.calls if "DELETE" in call]
    gh.on("actions/runs?status=queued", [{"total_count": 0, "workflow_runs": []}])
    done = invoke(runtime, ["ci", "window", "release", "--remove-branch", left])
    assert done.exit_code == 0 and f"removed {left}" in done.stdout
    assert next(call for call in gh.calls if "DELETE" in call)[-1].endswith(f"git/refs/heads/{left}")
    assert invoke(runtime, ["ci", "window", "release", "--remove-branch", "fix/work"]).exit_code == 2  # never a work branch


def test_a_stopped_validation_cancels_its_runs_removes_its_branch_and_frees_the_window(rt, lanes):
    lanes["conclusions"][1] = "TERM"  # SIGTERM arrives while it waits for run 1
    gh = remote()
    runtime = rt(commands=gh)
    done = invoke(runtime, ["head", "validate", "."])
    assert done.exit_code == 143 and "stopped by a signal" in done.stderr and "the window is free" in done.stderr
    assert len([call for call in gh.calls if "/cancel" in " ".join(call)]) == 2 and [name for name, _ in lanes["deleted"]] == [VBRANCH, V2]
    assert window.the_window(runtime).status().active is None


def test_cleanup_reports_what_really_happened_and_is_armed_only_after_the_push(rt, lanes):
    lanes["tip_moved"] = True
    moved = invoke(rt(commands=remote()), ["head", "validate", "."]).stdout
    assert f"did NOT remove {VBRANCH}: it no longer points at {commit('web-1')[:9]}" in moved and f"removed {VBRANCH}" not in moved.replace("did NOT remove", "")
    lanes["tip_moved"] = False

    class NoPush(Commands):
        def passthrough(self, argv, cwd=None, env=None, timeout=None):
            self.git.append(list(argv))
            return 1

    gh = NoPush()
    gh.on("git/ref/heads/validation/feat-x-", 404)
    failed = invoke(rt(commands=gh), ["head", "validate", "."])
    assert failed.exit_code == 1 and f"push of {VBRANCH}, {V2} failed (exit 1)" in failed.stderr and "a ref the remote refused, or the pre-push hook" in failed.stderr
    assert lanes["started"][2:] == [] and lanes["deleted"] == []  # a branch this command did not push is never deleted


def test_an_existing_or_unreadable_validation_branch_refuses_and_the_window_plan_starts_nothing(rt, lanes):
    runtime = rt(commands=remote(exists={"ref": "x"}))
    done = invoke(runtime, ["head", "validate", "."])
    assert done.exit_code == 1 and "already exists on the remote" in done.stderr and f"sm ci window release --remove-branch {VBRANCH}" in done.stderr
    assert lanes["started"] == [] and lanes["deleted"] == [] and window.the_window(runtime).status().active is None
    unsure = invoke(rt(commands=remote(exists=502)), ["head", "validate", "."])
    assert unsure.exit_code == 1 and "cannot tell whether" in unsure.stderr and lanes["started"] == []  # only a clear 404 is "absent"
    plan = invoke(rt(), ["ci", "window", "run", "one", "two"])
    assert plan.exit_code == 0 and "1. sm head validate one --lanes web-1,web-2" in plan.stdout and "Nothing was started" in plan.stdout
    both = invoke(rt(commands=remote()), ["--format", "json", "ci", "window", "run", "one", "two", "--run"])
    assert [head["result"] for head in json.loads(both.stdout)["heads"]] == ["VALIDATED", "VALIDATED"]


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


def test_a_long_work_branch_keeps_one_ref_per_lane(rt):
    """acuk 1: a work branch of 100 characters cut the lane off, so every lane got the same ref and the push refused."""
    runtime = rt()
    runtime.providers.vcs.b = "feat/" + "very-long-topic-" * 6 + "end"
    doc = window.Validation.model_validate(json.loads(invoke(runtime, ["--format", "json", "head", "validate", ".", "--dry-run", "--lanes", ",".join(ALL)]).stdout))
    assert len(set(doc.validation_branches)) == 4 and all(ref.endswith(f"-{lane}-aaaaaaaa") for ref, lane in zip(doc.validation_branches, ALL, strict=True))
    assert all(len(ref) <= len("validation/") + 80 + 9 for ref in doc.validation_branches)
    assert window.lane_branch("feat/x", "web-1", "a" * 40) == VBRANCH  # a short name is unchanged


def test_serial_keeps_a_real_failure_when_a_later_lane_cannot_start(rt, lanes):
    """acuk 2: the finished red lane was 'cancelled' with the rest, and the verdict became INCOMPLETE."""
    lanes["conclusions"][1] = "failure"
    lanes["refuse"]["web-2"] = "refusing: 19 active + 1 requested exceed the 20-job ceiling; main first"
    gh = remote()
    done = invoke(rt(commands=gh), ["--format", "json", "head", "validate", ".", "--serial"])
    doc = window.Validation.model_validate(json.loads(done.stdout))
    assert done.exit_code == 1 and doc.status == "not_validated" and doc.lanes[0].conclusion == "failure" and doc.lanes[1].run is None, done
    assert not [call for call in gh.calls if "/cancel" in " ".join(call)]


def _term_here():
    import signal

    assert signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, signal.SIG_IGN, None), "the validation's SIGTERM handler must be installed"
    signal.raise_signal(signal.SIGTERM)


def test_a_stop_during_the_push_waits_for_it_and_removes_the_pushed_refs(rt, lanes):
    """acuk 3: SIGTERM in the middle of the push left both refs on the remote (the push landed, cleanup was not armed)."""

    class TermDuringPush(Commands):
        def passthrough(self, argv, cwd=None, env=None, timeout=None):
            self.git.append(list(argv))
            _term_here()
            return 0

    runtime = rt(commands=TermDuringPush())
    runtime.providers.commands.on("git/ref/heads/validation/feat-x-", 404)
    done = invoke(runtime, ["head", "validate", "."])
    assert done.exit_code == 143 and "stopped by a signal" in done.stderr, done
    assert lanes["started"] == [] and lanes["deleted"] == [(VBRANCH, commit("web-1")), (V2, commit("web-2"))]
    assert window.the_window(runtime).status().active is None


def test_a_stop_during_the_cleanup_lets_it_finish(rt, lanes, monkeypatch):
    real = focused.delete_throwaway

    def delete_and_term(branch, sha):
        if not lanes["deleted"]:
            _term_here()
        return real(branch, sha)

    monkeypatch.setattr(focused, "delete_throwaway", delete_and_term)
    runtime = rt(commands=remote())
    done = invoke(runtime, ["head", "validate", "."])
    assert done.exit_code == 0 and "VALIDATED" in done.stdout and "a stop signal came during the cleanup; the cleanup finished first" in done.stdout, done
    assert [name for name, _ in lanes["deleted"]] == [VBRANCH, V2] and window.the_window(runtime).status().active is None


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
