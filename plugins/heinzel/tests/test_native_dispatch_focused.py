import base64
import hashlib
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from types import SimpleNamespace

import pytest
from steuermaschine_heinzel.native.dispatch_lib import rules as c

from steuermaschine.defaults import FileStore, Subprocess

# Moved from the former single-lane tool's tests. Assertions are unchanged except where they named the old command.


def load(_name="focused"):
    """A fresh copy of the moved module per test, as the former tests loaded the script: module state never leaks."""
    import importlib.util

    spec = importlib.util.find_spec("steuermaschine_heinzel.native.dispatch_lib.focused")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.R = "example/core"
    return module


def job(name, concl="failure", status="completed", labels=("ubuntu-24.04",), id=1):
    return {"id": id, "name": name, "conclusion": concl, "status": status, "labels": list(labels)}


def test_main_and_release_refused_without_force():
    assert "refusing" in c.branch_refusal("main")
    assert "refusing" in c.branch_refusal("release/v0.9.28")
    assert c.branch_refusal("release/v0.9.28", force=True) == ""
    assert c.branch_refusal("fix/update-paths") == ""


def test_pick_run_is_newest_ci_run_for_the_head():
    runs = [{"databaseId": 1, "headSha": "a", "workflowName": "CI", "createdAt": "1"},
            {"databaseId": 2, "headSha": "a", "workflowName": "CI", "createdAt": "2"},
            {"databaseId": 3, "headSha": "a", "workflowName": "CodeQL", "createdAt": "3"},
            {"databaseId": 4, "headSha": "b", "workflowName": "CI", "createdAt": "4"}]
    assert c.pick_run(runs, "a")["databaseId"] == 2
    assert c.pick_run(runs, "z") is None


def test_match_job_exact_then_unique_substring():
    jobs = [job("End-to-end web-2 (macos-latest)", id=7), job("End-to-end web-2 (ubuntu-24.04)", "success", id=8), job("CI OK", id=9)]
    assert c.match_job(jobs, "End-to-end web-2 (macos-latest)")[0]["id"] == 7
    assert c.match_job(jobs, "web-2 (macos")[0]["id"] == 7
    j, err = c.match_job(jobs, "web-2")
    assert j is None and "matches 2" in err
    j, err = c.match_job(jobs, "nope")
    assert j is None and "web-2 (macos-latest)" in err and "CI OK" not in err.split("failing:")[1]


def test_capacity_rules():
    rel = [{"status": "in_progress", "headBranch": "release/v1"}]
    assert "needs macOS" in c.capacity_refusal(rel, [], 5, True)  # beside a candidate: no macOS run, and that outranks the focused limit
    assert "max 5" in c.capacity_refusal(rel, [], 5, False) and c.capacity_refusal(rel, [], 4, False) == ""  # a Linux run goes beside it
    assert c.capacity_refusal([], [], 4, False) == ""
    assert "max 5" in c.capacity_refusal([], [], 5, False)
    main_q = [{"status": "queued", "headBranch": "main"}]
    full = [job("x", status="queued")] * 20
    assert "ceiling" in c.capacity_refusal(main_q, full, 0, False)
    assert c.capacity_refusal([], full, 0, False) == "" and "queued behind 20" in c.capacity_note(full, False)
    mac = [job("m", status="in_progress", labels=("macos-latest",))] * 4
    assert "macOS/ARM" in c.capacity_refusal(main_q, mac, 0, True)
    assert c.capacity_refusal([], mac, 0, True) == "" and c.capacity_refusal(main_q, mac, 0, False) == ""
    assert "ceiling" in c.capacity_refusal(main_q, full[:19], 0, False, new_jobs=2)
    assert "macOS/ARM" in c.capacity_refusal(main_q, mac[:3], 0, True, new_arm_jobs=2)


def test_beside_a_candidate_linux_runs_go_within_the_job_cap_and_macos_runs_never():
    rel = [{"id": 7, "status": "in_progress", "headBranch": "release/v1"}]
    linux = [job("x", status="in_progress", labels=("ubuntu-24.04",))]
    assert c.capacity_refusal(rel, linux * 19, 0, False) == ""  # 19 active + 1 requested stay within the 20-job cap, main waiting or not
    full = c.capacity_refusal(rel, linux * 20, 0, False)
    assert "release/*" in full and "20 active + 1 requested jobs exceed the 20-job ceiling" in full and "candidate first" in full
    assert "exceed the 20-job ceiling" in c.capacity_refusal(rel, linux * 17, 0, False, new_jobs=4)  # a whole lane set counts
    assert c.capacity_refusal([], linux * 20, 0, False) == ""  # without a candidate (and main not waiting) the job queues as before
    for wanted in ({"wants_arm": True}, {"wants_arm": False, "new_arm_jobs": 1}):  # either way of asking for a macOS runner
        why = c.capacity_refusal(rel, [], 0, wanted.pop("wants_arm"), **wanted)
        assert "release/*" in why and "needs macOS" in why
    assert c.capacity_refusal(rel, [], 0, True, new_arm_jobs=0) == ""  # an ARM-labelled request with no macOS job is a Linux run
    done = [{"id": 7, "status": "completed", "headBranch": "release/v1"}]
    assert c.capacity_refusal(done, [], 0, True) == ""  # a finished candidate holds nothing


def test_the_caps_are_the_accounts_and_come_from_settings_not_from_constants():
    rel = [{"status": "queued", "headBranch": "release/v1"}]
    main_q = [{"status": "queued", "headBranch": "main"}]
    linux = [job("x", status="in_progress", labels=("ubuntu-24.04",))]
    mac = [job("m", status="in_progress", labels=("macos-latest",))]
    small, large = c.Caps(jobs=3, macos=1), c.Caps(jobs=60, macos=10)
    assert "exceed the 3-job ceiling" in c.capacity_refusal(rel, linux * 3, 0, False, caps=small)
    assert c.capacity_refusal(rel, linux * 40, 0, False, caps=large) == ""
    assert "exceed the 3-job ceiling" in c.capacity_refusal(main_q, linux * 3, 0, False, caps=small)
    assert "macOS/ARM jobs exceed 1" in c.capacity_refusal(main_q, mac, 0, True, caps=small)
    assert c.capacity_refusal(main_q, mac * 5, 0, True, caps=large) == ""
    assert "ceiling 3, ARM 1" in c.capacity_note(linux * 3, False, small) and c.capacity_note(linux * 3, False, large) == ""
    assert (c.Caps().jobs, c.Caps().macos) == (c.JOB_CEILING, c.ARM_LIMIT)


@pytest.fixture
def cli(tmp_path, monkeypatch):
    mod = load()
    mod.EDGE = mod.Edge(store=FileStore(tmp_path / "store"))
    monkeypatch.setattr(mod, "STATE", tmp_path / "runs.jsonl")
    monkeypatch.setattr(mod, "note", lambda *_: None)
    monkeypatch.setattr(mod, "gh", lambda *_a, **_kw: pytest.fail("unexpected GitHub call"))
    # The SHA guard has its own tests below; here the tip is used by nothing else.
    monkeypatch.setattr(mod, "remote_tip", lambda _branch: "a" * 40)
    monkeypatch.setattr(mod, "attachment_facts", lambda _sha: ([], set(), []))
    return mod


def run(rid, branch="fix/test", status="queued", workflow=c.WORKFLOW, attempt=1):
    return {"id": rid, "head_branch": branch, "path": f".github/workflows/{workflow}@{branch}",
            "head_sha": "a" * 40, "status": status, "run_attempt": attempt,
            "conclusion": "failure" if status == "completed" else None}


def test_admission_serializes_two_starts_at_four_active(cli, monkeypatch):
    started = []
    ready = Barrier(2)

    def snapshot(*_):
        observed = 4 + len(started)
        time.sleep(0.02)  # Give an unguarded peer time to observe the same free slot.
        return c.capacity_refusal([], [], observed, False)

    monkeypatch.setattr(cli, "_capacity", snapshot)

    def request():
        ready.wait()
        try:
            return cli.admit(lambda _: started.append("sent") or "started", False, False)
        except SystemExit as err:
            return str(err)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: request(), range(2)))
    assert started == ["sent"]
    assert "started" in results
    assert any("max 5" in outcome for outcome in results)


def test_admission_counts_hosted_runs_and_local_attempts_once(cli, monkeypatch):
    remote = [run(rid) for rid in range(1, 5)]
    monkeypatch.setattr(cli, "inventory", lambda endpoint, _: remote if "status=queued" in endpoint else [])
    monkeypatch.setattr(cli, "jobs_of", lambda _: [])
    monkeypatch.setattr(cli, "run_state", lambda rid: run(rid) if rid == 1 else run(
        rid, status="completed", workflow="ci.yml", attempt=1
    ))
    cli.record(1, "dispatch", "one", "fix/test", 1, 1, 0)
    assert cli._capacity(False) == ""
    cli.record(5, "rerun", "five", "fix/test", 2, 1, 0)
    assert "max 5" in cli._capacity(False)
    assert "already in flight" in cli._capacity(False, rerun_id=5)


@pytest.mark.parametrize("status", ["waiting", "in_progress"])
def test_active_candidate_prevents_start_without_polling_jobs(cli, monkeypatch, status):
    monkeypatch.setattr(cli, "inventory", lambda endpoint, _: [run(
        1, "release/v0.9.35", status, "ci.yml"
    )] if f"status={status}&" in endpoint else [])
    monkeypatch.setattr(cli, "jobs_of", lambda _: pytest.fail("polled jobs despite an active candidate"))
    with pytest.raises(SystemExit, match="release/"):
        cli.admit(lambda _: pytest.fail("started during candidate"), False, True)


@pytest.mark.parametrize(
    ("pool", "main_job_status", "used", "refusal"),
    [
        ("ubuntu-24.04", "queued", 20, "20 active + 1 requested"),
        ("macos-latest", "pending", 4, "4 active + 1 requested macOS/ARM"),
        ("ubuntu-24.04", "queued", 19, None),
        ("macos-latest", "waiting", 3, None),
        ("ubuntu-24.04", "in_progress", 20, None),
    ],
)
def test_partial_main_admission_uses_own_job_status(cli, monkeypatch, capsys, pool, main_job_status, used, refusal):
    main, pr = [run(1, "main", "in_progress", "ci.yml"), run(2, "fix/test", "in_progress", "ci.yml")]
    monkeypatch.setattr(cli, "inventory", lambda endpoint, _: [main, pr] if "status=in_progress&" in endpoint else [])
    inventories = {
        1: [job("running main", status="in_progress", labels=(pool,), id=10),
            job("next main", status=main_job_status, labels=(pool,), id=11)],
        2: [job("unrelated PR", status="queued", labels=(pool,), id=20 + i) for i in range(used - 2)],
    }
    monkeypatch.setattr(cli, "jobs_of", inventories.__getitem__)
    started = []

    def start(_):
        started.append(True)

    if refusal:
        with pytest.raises(SystemExit) as error:
            cli.admit(start, c.is_arm([pool]), False)
        assert refusal in str(error.value)
        assert "main first" in str(error.value)
        assert not started
        assert "no main CI waiting" not in capsys.readouterr().out
    else:
        cli.admit(start, c.is_arm([pool]), False)
        assert started == [True]
        if main_job_status == "in_progress":
            assert "no main CI waiting" in capsys.readouterr().out


@pytest.mark.parametrize("payload", [None, [], [{"total_count": 1, "workflow_runs": []}],
                                     [{"total_count": 1, "workflow_runs": [run(1, status="requested")]}]])
def test_uncertain_inventory_refuses_before_start(cli, monkeypatch, payload):
    monkeypatch.setattr(cli, "ghj", lambda *_: payload)
    with pytest.raises(SystemExit, match=r"inventory|uncertain"):
        cli.admit(lambda _: pytest.fail("started without evidence"), False, False)


def test_failed_inventory_api_refuses_before_start(cli, monkeypatch):
    def unavailable(*_):
        raise SystemExit("gh api: unavailable")

    monkeypatch.setattr(cli, "ghj", unavailable)
    with pytest.raises(SystemExit, match="unavailable"):
        cli.admit(lambda _: pytest.fail("started without evidence"), False, False)


def test_partial_job_inventory_refuses_before_start(cli, monkeypatch):
    def github(*args):
        endpoint = args[-1]
        if "status=queued" in endpoint:
            return [{"total_count": 1, "workflow_runs": [run(1)]}]
        if "/jobs?" in endpoint:
            return [{"total_count": 2, "jobs": [job("only one", status="queued")]}]
        return [{"total_count": 0, "workflow_runs": []}]

    monkeypatch.setattr(cli, "ghj", github)
    with pytest.raises(SystemExit, match="incomplete GitHub jobs"):
        cli.admit(lambda _: pytest.fail("started without job evidence"), False, False)


@pytest.mark.parametrize("receipt", [True, False])
def test_dispatch_binds_exact_workflow_and_requires_receipt(cli, monkeypatch, receipt):
    monkeypatch.setattr(cli, "_capacity", lambda *_: "")
    monkeypatch.setattr(cli, "gh", lambda *_a, **_kw: "a" * 40)
    calls = []
    args = SimpleNamespace(branch="validation/clock", force=False, suite="web-2", test=None, kind=None,
                           repeat=1, stress=False, os="ubuntu-24.04", wait=False, json=False, reviewed_head=None,
                           throwaway=False, cleanup=False)

    def github(*args):
        calls.append(args)
        return {"workflow_run_id": 123, "html_url": f"https://github.com/{cli.R}/actions/runs/123"} if receipt else None

    monkeypatch.setattr(cli, "ghj", github)
    pending = cli.STATE.with_suffix(".pending")
    if receipt:
        assert cli.cmd_dispatch(args) == 0
        assert not pending.exists()
        assert json.loads(cli.STATE.read_text())["run"] == 123
    else:
        with pytest.raises(SystemExit, match="no exact dispatch receipt"):
            cli.cmd_dispatch(args)
        assert json.loads(pending.read_text())["branch"] == "validation/clock"
        with pytest.raises(SystemExit, match="reconcile"):
            cli.cmd_dispatch(args)
    assert f"repos/{cli.R}/actions/workflows/{c.WORKFLOW}/dispatches" in calls[0]
    assert "ref=refs/heads/validation/clock" in calls[0] and "inputs[ref]=validation/clock" in calls[0]
    assert len(calls) == 1


@pytest.mark.parametrize(("error", "kept"), [
    ("gh api --method POST: gh: Unexpected inputs provided (HTTP 422)", False),
    ("gh api --method POST: gh: Not Found (HTTP 404)", False),
    ("gh api --method POST: gh: Server Error (HTTP 502)", True),
    ("refusing: GitHub API unavailable: gh timed out", True),
    ("gh api --method POST: unexpected EOF", True),
])
def test_dispatch_definite_refusal_clears_pending_ambiguous_keeps_it(cli, monkeypatch, error, kept):
    monkeypatch.setattr(cli, "_capacity", lambda *_: "")
    monkeypatch.setattr(cli, "gh", lambda *_a, **_kw: "a" * 40)
    args = SimpleNamespace(branch="validation/clock", force=False, suite="web-2", test=None, kind=None,
                           repeat=1, stress=False, os="ubuntu-24.04", wait=False, json=False, reviewed_head=None,
                           throwaway=False, cleanup=False)

    def github(*_args):
        raise SystemExit(error)

    monkeypatch.setattr(cli, "ghj", github)
    pending = cli.STATE.with_suffix(".pending")
    with pytest.raises(SystemExit, match="HTTP 502|unavailable|EOF" if kept else "GitHub refused the dispatch"):
        cli.cmd_dispatch(args)
    assert pending.exists() is kept
    if kept:
        assert json.loads(pending.read_text())["branch"] == "validation/clock"
    else:
        with pytest.raises(SystemExit, match="GitHub refused the dispatch"):
            cli.cmd_dispatch(args)  # the next start is not blocked by a stale marker


EXACT_BRANCH = "validation/paired-webkit-b3f1dda"
EXACT_HEAD = "a" * 40
EXACT_NODE = "tests/e2e/web_ui_test.py::test_pair_reopen[tab-webkit]"
RUNNER_BLOB = "b" * 40


@pytest.fixture
def exact_dispatch(cli, monkeypatch):
    args = SimpleNamespace(branch=EXACT_BRANCH, force=False, suite=None, test=[EXACT_NODE], kind=None,
                           repeat=10, stress=False, os="macos-latest", wait=False, json=False,
                           reviewed_head=EXACT_HEAD, throwaway=False, cleanup=False)
    pending = cli.STATE.with_suffix(".pending")
    raw = b"reviewed runner bytes\n"
    monkeypatch.setattr(cli, "EXACT_RUNNER_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(cli, "gh", lambda *_a, **_kw: "a" * 40)
    calls = []
    lock_held = False

    @contextmanager
    def lock():
        nonlocal lock_held
        assert not lock_held
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    def capacity(*_):
        assert lock_held
        calls.append("capacity")
        return ""

    monkeypatch.setattr(cli, "admission_lock", lock)
    monkeypatch.setattr(cli, "_capacity", capacity)
    workflow = f".github/workflows/{c.WORKFLOW}"
    repo = {"full_name": cli.R}
    responses = {
        "history": {"total_count": 0, "workflow_runs": []},
        "workflow": [{"type": "file", "path": workflow, "sha": cli.EXACT_WORKFLOW_BLOB}],
        "scripts": [{"type": "file", "path": cli.EXACT_RUNNER_PATH, "sha": RUNNER_BLOB}],
        "runner": {"sha": RUNNER_BLOB, "encoding": "base64", "content": base64.encodebytes(raw).decode("ascii")},
        "ref": {"ref": f"refs/heads/{EXACT_BRANCH}", "object": {"type": "commit", "sha": EXACT_HEAD}},
        "post": {"workflow_run_id": 123, "html_url": f"https://github.com/{cli.R}/actions/runs/123"},
        "run": {**run(123, EXACT_BRANCH), "event": "workflow_dispatch", "repository": repo, "head_repository": repo},
    }
    endpoints = {
        f"repos/{cli.R}/actions/workflows/{c.WORKFLOW}/runs?branch=validation%2Fpaired-webkit-b3f1dda&per_page=1": "history",
        f"repos/{cli.R}/contents/.github/workflows?ref={EXACT_HEAD}": "workflow",
        f"repos/{cli.R}/contents/scripts?ref={EXACT_HEAD}": "scripts",
        f"repos/{cli.R}/git/blobs/{RUNNER_BLOB}": "runner",
        f"repos/{cli.R}/git/ref/heads/{EXACT_BRANCH}": "ref",
        f"repos/{cli.R}/actions/runs/123": "run",
    }

    def github(*params):
        assert lock_held and params[0] == "api"
        if params[1:3] == ("--method", "POST"):
            key = "post"
            assert pending.exists()
            assert f"repos/{cli.R}/actions/workflows/{c.WORKFLOW}/dispatches" in params
            assert f"ref=refs/heads/{EXACT_BRANCH}" in params and f"inputs[ref]={EXACT_BRANCH}" in params
            assert f"inputs[selector]={EXACT_NODE}" in params and "inputs[repeat]=10" in params
        else:
            key = endpoints[params[1]]
            if key == "run":
                assert json.loads(pending.read_text())["run"] == 123
                assert not cli.STATE.exists()
        calls.append(key)
        result = responses[key]
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(cli, "ghj", github)
    return args, pending, responses, calls


def test_exact_repeat_verifies_pins_and_returned_run_before_recording(cli, exact_dispatch):
    args, pending, _, calls = exact_dispatch
    assert cli.cmd_dispatch(args) == 0
    assert calls == ["capacity", "history", "workflow", "scripts", "runner", "ref", "post", "run"]
    assert not pending.exists()
    receipt = json.loads(cli.STATE.read_text())
    assert (receipt["run"], receipt["branch"], receipt["head_sha"], receipt["attempt"]) == (
        123, EXACT_BRANCH, EXACT_HEAD, 1
    )
    with pytest.raises(SystemExit, match="fresh validation"):
        cli.cmd_dispatch(args)
    assert calls.count("post") == 1 and not pending.exists()


@pytest.mark.parametrize(
    ("key", "payload", "reason"),
    [
        ("history", {"total_count": 1, "workflow_runs": [{"id": 123}]}, "branch is used"),
        ("history", {"total_count": False, "workflow_runs": []}, "history is uncertain"),
        ("workflow", [{"type": "file", "path": ".github/workflows/focused-job.yml", "sha": "b" * 40}], "workflow blob"),
        ("workflow", [{"type": "symlink", "path": ".github/workflows/focused-job.yml", "sha": "b" * 40}], "unambiguous file"),
        ("runner", {"sha": RUNNER_BLOB, "encoding": "base64", "content": "!"}, "malformed"),
        ("runner", {"sha": RUNNER_BLOB, "encoding": "base64", "content": "Yg=="}, "runner digest"),
        ("ref", {"ref": f"refs/heads/{EXACT_BRANCH}", "object": {"type": "commit", "sha": "b" * 40}}, "--reviewed-head"),
    ],
)
def test_exact_repeat_refuses_unreviewed_or_reused_target_before_post(cli, exact_dispatch, key, payload, reason):
    args, pending, responses, calls = exact_dispatch
    responses[key] = payload
    with pytest.raises(SystemExit, match=reason):
        cli.cmd_dispatch(args)
    assert calls[-1] == key and "post" not in calls and not pending.exists()


@pytest.mark.parametrize(
    "changed",
    [
        {"head_sha": "b" * 40},
        {"head_branch": "validation/moved"},
        {"path": ".github/workflows/other.yml"},
        {"path": ".github/workflows/focused-job.yml@refs/heads/validation/other"},
        {"run_attempt": 2},
        {"event": "push"},
        {"head_repository": {"full_name": "other/heinzel"}},
        None,
    ],
)
def test_exact_repeat_keeps_pending_after_mismatched_or_unavailable_run(cli, exact_dispatch, changed):
    args, pending, responses, calls = exact_dispatch
    responses["run"] = SystemExit("run unavailable") if changed is None else {**responses["run"], **changed}
    with pytest.raises(SystemExit, match="reconcile"):
        cli.cmd_dispatch(args)
    receipt = json.loads(pending.read_text())
    assert (receipt["run"], receipt["head_sha"], receipt["selector"], receipt["repeat"]) == (
        123, EXACT_HEAD, EXACT_NODE, 10
    )
    assert not cli.STATE.exists()
    with pytest.raises(SystemExit, match="reconcile"):
        cli.cmd_dispatch(args)
    assert calls.count("post") == 1


def test_exact_repeat_keeps_intent_without_a_dispatch_receipt(cli, exact_dispatch):
    args, pending, responses, calls = exact_dispatch
    responses["post"] = None
    with pytest.raises(SystemExit, match="reconcile"):
        cli.cmd_dispatch(args)
    receipt = json.loads(pending.read_text())
    assert receipt["head_sha"] == EXACT_HEAD and receipt["branch"] == EXACT_BRANCH and "run" not in receipt
    assert "run" not in calls and not cli.STATE.exists()
    with pytest.raises(SystemExit, match="reconcile"):
        cli.cmd_dispatch(args)
    assert calls.count("post") == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"reviewed_head": None},
        {"reviewed_head": EXACT_HEAD[:8]},
        {"branch": "work/flakes-650-typer"},
        {"branch": "main", "force": True},
        {"test": [EXACT_NODE, "tests/e2e/web_ui_test.py::test_sibling"]},
        {"test": ["tests/e2e/web_ui_test.py::Case[one]::test_pair_reopen"]},
        {"kind": "vitest"},
        {"repeat": c.MAX_REPEAT + 1},
        {"repeat": 1},
    ],
)
def test_exact_repeat_shape_is_rejected_before_admission(cli, exact_dispatch, changes):
    args, pending, _, calls = exact_dispatch
    with pytest.raises(SystemExit):
        cli.cmd_dispatch(SimpleNamespace(**(vars(args) | changes)))
    assert not calls and not pending.exists()


def test_direct_rerun_cannot_target_other_workflows(cli, monkeypatch):
    monkeypatch.setattr(cli, "run_state", lambda rid: run(rid, status="completed", workflow="other.yml"))
    with pytest.raises(SystemExit, match="outside the CI workflow"):
        cli.cmd_rerun(SimpleNamespace(target="123456", force=False))


def test_selection_infers_kind_and_validates():
    assert c.selection("web-2") == ("e2e", "web-2", "")
    assert c.selection(None, ["tests/e2e/audio_visualizer_test.py::test_x"])[0] == "e2e"
    assert c.selection(None, ["tests/unit/ci_test.py"])[0] == "pytest"
    assert c.selection(None, ["web/src/splash/Splash.test.tsx"])[0] == "vitest"
    for bad in (["--deselect=x"], ["tests/../x"], ["tests/x.py;id"], ["a $(id)"]):
        assert c.selection(None, bad)[2]
    assert c.selection("web-2", ["tests/e2e/x.py"])[2]
    assert c.selection("nope")[2]
    assert c.selection(None, ["tests/unit/x.py"], kind="e2e")[2]


def test_verify_hint_names_the_failing_job_not_ci_ok():
    assert c.verify_hint(589, ["CI OK", "End-to-end web-2 (macos-latest)"]) .startswith('verify with: sm ci one rerun 589 --job "End-to-end web-2 (macos-latest)"')
    assert "--job" not in c.verify_hint(42380545800, ["run"])


def test_summarize_skips_skipped():
    assert c.summarize([job("a"), job("b", "skipped"), job("c", "success")], ["a", "b"]) == ["FAILURE   a"]


def test_verify_hint_skips_code_scanning_and_sonar():
    assert "--job" not in c.verify_hint(600, ["CodeQL", "SonarCloud Code Analysis"])
    assert '--job "Test (ubuntu-24.04, Python 3.12)"' in c.verify_hint(600, ["CodeQL", "Test (ubuntu-24.04, Python 3.12)"])


def test_rounds_follow_the_rule_of_three_with_a_cap():
    assert [c.rounds_for(c.parse_rate(p)) for p in ("1/3", "1/2", "1/20", "0.01", "33%")] == [9, 6, 60, 60, 10]
    assert c.rounds_for(None) == 10 and c.rounds_for(c.parse_rate("")) == 10


def test_repeat_refusal_and_tallies():
    assert c.repeat_refusal(10, None, "tests/e2e/x.py::t") == ""
    assert c.repeat_refusal(61, None, "tests/e2e/x.py::t") and c.repeat_refusal(0, None, "x")
    assert c.repeat_refusal(3, "web-2", "web-2") and c.repeat_refusal(3, None, "tests/x.py::t[a]")
    assert c.parse_tally("2026 x\tFOCUSED TALLY 7/10 green\nmore") == "7/10 green"
    assert c.parse_tally("FOCUSED TALLY 3/4 green (asked 9)") == "3/4 green (asked 9)"
    assert c.tally_line(["success", "failure", "success"]) == "2/3 green"
    assert "[7/10 green]" in c.status_line({"run": 1, "kind": "dispatch", "what": "x", "at": "2026-11-07T16:00:00", "tally": "7/10 green"})


# ---------------------------------------------------------------- SHA guard and --throwaway (RCA 2026-11-12, #790)

TIP = "c" * 40
NEW = "d" * 40


def test_sha_refusal_names_each_use_of_the_sha():
    assert c.sha_refusal(TIP, "validation/x", [(790, "e" * 40)], {"pull_request:" + "e" * 40}, ["validation/x"]) == []
    assert c.sha_refusal(TIP, "bisect/web-3", [], set(), ["bisect/web-3", "validation/y"]) == []
    lanes = ["rehearsal/aaaaaaaaaa-cli-ubuntu", "rehearsal/aaaaaaaaaa-web-2-macos"]  # the rehearsal: one commit, many lanes
    assert c.sha_refusal(TIP, lanes[0], [], set(), lanes) == []
    assert c.sha_refusal(TIP, lanes[0], [(790, TIP)], set(), lanes) == ["it is the head of open PR #790"]
    assert len(c.sha_refusal(TIP, lanes[0], [], set(), [*lanes, "main"])) == 1  # main's own tip is never a rehearsal
    pr = c.sha_refusal(TIP, "validation/x", [(790, TIP), (12, "e" * 40)], set(), [])
    assert pr == ["it is the head of open PR #790"]
    plan = c.sha_refusal(TIP, "validation/x", [], {"pull_request:" + TIP, "push-tree:" + TIP}, [])
    assert len(plan) == 1 and "pull_request:cccccccc" in plan[0]
    assert c.sha_refusal(TIP, "validation/x", [], {"push-tree:" + TIP}, []) == []  # a tree key is not a PR head
    own = c.sha_refusal(TIP, "fix/clock", [], set(), [])
    assert own == ["it is the tip of the non-validation branch fix/clock"]  # the API need not list it
    other = c.sha_refusal(TIP, "validation/x", [], set(), ["validation/x", "feat/chat-head"])
    assert other == ["it is the tip of the non-validation branch feat/chat-head"]
    text = c.sha_refusal_text(TIP, "fix/clock", own)
    assert "merge-gate and coverage-floor" in text and "--throwaway" in text and "fix/clock @ cccccccc" in text


def test_throwaway_branch_and_option_rules():
    assert c.throwaway_branch("fix/clock", TIP) == "validation/fix-clock-cccccccc"
    assert c.throwaway_branch("validation/clock", TIP) == "validation/clock-cccccccc"
    assert c.throwaway_branch("release/v0.9.40", TIP) == "validation/release-v0.9.40-cccccccc"
    assert c.REF.fullmatch(c.throwaway_branch("a/" * 90 + "b", TIP))
    assert "needs --throwaway and --wait" in c.throwaway_refusal(False, True, True, None)
    assert "needs --throwaway and --wait" in c.throwaway_refusal(True, True, False, None)
    assert "--reviewed-head" in c.throwaway_refusal(True, False, False, TIP)
    assert c.throwaway_refusal(True, True, True, None) == "" and c.throwaway_refusal(False, False, False, TIP) == ""


def dispatch_args(**changes):
    base = dict(branch="fix/clock", force=False, suite="web-2", test=None, kind=None, repeat=1, stress=False,
                os="ubuntu-24.04", wait=False, json=False, reviewed_head=None, throwaway=False, cleanup=False)
    return SimpleNamespace(**(base | changes))


@pytest.fixture
def guarded(cli, monkeypatch):
    """Dispatch with the real guard decision over fake remote facts; records every write."""
    facts = {"prs": [], "keys": set(), "tips": {}, "refs": {"fix/clock": TIP, "validation/clock": TIP}}
    posts, deletes = [], []
    monkeypatch.setattr(cli, "_capacity", lambda *_: "")
    monkeypatch.setattr(cli, "gh", lambda *params, **_kw: deletes.append(params) or "" if "DELETE" in params else "a" * 40)
    monkeypatch.setattr(cli, "remote_tip", lambda branch: facts["refs"][branch])
    monkeypatch.setattr(cli, "attachment_facts", lambda sha: (facts["prs"], facts["keys"], facts["tips"].get(sha, [])))

    def github(*params, **_kw):
        if params[1:3] == ("--method", "POST"):
            posts.append(params)
            return {"workflow_run_id": 123, "html_url": f"https://github.com/{cli.R}/actions/runs/123"}
        assert params[1].startswith(f"repos/{cli.R}/git/ref/heads/validation/")  # the throwaway target must be absent
        return None

    monkeypatch.setattr(cli, "ghj", github)
    return facts, posts, deletes


@pytest.mark.parametrize("case, branch, expected", [
    ("pr", "validation/clock", "head of open PR #790"),
    ("plan", "validation/clock", "focused plan registry holds pull_request:cccccccc"),
    ("other-tip", "validation/clock", "non-validation branch feat/chat-head"),
    ("own-tip", "fix/clock", "non-validation branch fix/clock"),
])
def test_dispatch_refuses_a_sha_that_is_or_will_be_a_pr_head(cli, guarded, capsys, case, branch, expected):
    facts, posts, _ = guarded
    if case == "pr":
        facts["prs"] = [(790, TIP)]
    elif case == "plan":
        facts["keys"] = {"pull_request:" + TIP}
    elif case == "other-tip":
        facts["tips"] = {TIP: ["validation/clock", "feat/chat-head"]}
    with pytest.raises(SystemExit) as stop:
        cli.cmd_dispatch(dispatch_args(branch=branch, force=True))  # --force never lifts the guard
    err = capsys.readouterr().err
    assert stop.value.code == 2 and expected in err and "--throwaway" in err and "BLOCKED" in err
    assert posts == [] and not cli.STATE.exists() and not cli.STATE.with_suffix(".pending").exists()


def test_dispatch_on_an_unused_validation_sha_starts(cli, guarded):
    facts, posts, _ = guarded
    facts["prs"], facts["keys"] = [(790, "e" * 40)], {"pull_request:" + "e" * 40}
    facts["tips"] = {TIP: ["validation/clock"]}
    assert cli.cmd_dispatch(dispatch_args(branch="validation/clock")) == 0
    assert len(posts) == 1 and "ref=refs/heads/validation/clock" in posts[0]


def test_dispatch_on_a_rehearsal_lane_starts_although_every_lane_shares_the_commit(cli, guarded):
    facts, posts, _ = guarded
    lanes = ["rehearsal/aaaaaaaaaa-cli-ubuntu", "rehearsal/aaaaaaaaaa-web-2-macos"]
    facts["refs"] = dict.fromkeys(lanes, TIP)
    facts["tips"] = {TIP: lanes}
    assert cli.cmd_dispatch(dispatch_args(branch=lanes[0], suite="cli", test=None)) == 0  # as the rehearsal calls it
    assert len(posts) == 1 and f"ref=refs/heads/{lanes[0]}" in posts[0]


def test_a_failed_cleanup_never_replaces_the_result_of_the_run(cli, guarded, monkeypatch, capsys):
    facts, _posts, _deletes = guarded
    fake_throwaway(cli, facts, monkeypatch, [])
    monkeypatch.setattr(cli, "wait", lambda rid, *_: 0)
    monkeypatch.setattr(cli, "delete_throwaway", lambda *a: (_ for _ in ()).throw(SystemExit("refusing: cannot read the tip")))
    assert cli.cmd_dispatch(dispatch_args(throwaway=True, cleanup=True, wait=True)) == 0
    assert "not deleted validation/fix-clock-cccccccc: refusing: cannot read the tip" in capsys.readouterr().err


def fake_throwaway(cli, facts, monkeypatch, made):
    def make(branch, sha):
        target = c.throwaway_branch(branch, sha)
        facts["refs"][target] = NEW
        made.append((branch, sha, target))
        return target, NEW
    monkeypatch.setattr(cli, "make_throwaway", make)


def test_throwaway_dispatches_the_validation_branch_and_keeps_it_without_cleanup(cli, guarded, monkeypatch):
    facts, posts, deletes = guarded
    facts["prs"] = [(790, TIP)]  # the source is a PR head: exactly the case --throwaway exists for
    made = []
    fake_throwaway(cli, facts, monkeypatch, made)
    assert cli.cmd_dispatch(dispatch_args(throwaway=True)) == 0
    assert made == [("fix/clock", TIP, "validation/fix-clock-cccccccc")]
    assert "ref=refs/heads/validation/fix-clock-cccccccc" in posts[0]
    assert "inputs[ref]=validation/fix-clock-cccccccc" in posts[0]
    assert json.loads(cli.STATE.read_text())["branch"] == "validation/fix-clock-cccccccc"
    assert deletes == []
    monkeypatch.setattr(cli, "wait", lambda *_: 0)
    facts["refs"].pop("validation/fix-clock-cccccccc")
    assert cli.cmd_dispatch(dispatch_args(branch="validation/clock", throwaway=True, wait=True)) == 0
    assert deletes == []  # --wait alone never deletes


@pytest.mark.parametrize("moved", [False, True])
def test_cleanup_deletes_only_its_own_unmoved_validation_branch_after_the_result(cli, guarded, monkeypatch, capsys, moved):
    facts, _posts, deletes = guarded
    made, order = [], []
    fake_throwaway(cli, facts, monkeypatch, made)

    def waited(rid, *_):
        order.append(("wait", rid, list(deletes)))
        if moved:
            facts["refs"]["validation/fix-clock-cccccccc"] = "f" * 40
        return 1  # a red result is still cleaned up; the exit code is the run's

    monkeypatch.setattr(cli, "wait", waited)
    assert cli.cmd_dispatch(dispatch_args(throwaway=True, cleanup=True, wait=True)) == 1
    assert order == [("wait", 123, [])]
    if moved:
        assert deletes == [] and "not deleting validation/fix-clock-cccccccc" in capsys.readouterr().err
    else:
        assert deletes == [("api", "--method", "DELETE", f"repos/{cli.R}/git/refs/heads/validation/fix-clock-cccccccc")]


def test_throwaway_sha_already_used_refuses_and_reports_the_branch(cli, guarded, monkeypatch, capsys):
    facts, posts, deletes = guarded
    fake_throwaway(cli, facts, monkeypatch, [])
    facts["prs"] = [(791, NEW)]
    with pytest.raises(SystemExit) as stop:
        cli.cmd_dispatch(dispatch_args(throwaway=True, cleanup=True, wait=True))
    assert stop.value.code == 2 and posts == [] and deletes == []
    assert "validation/fix-clock-cccccccc stays on the remote" in capsys.readouterr().err


def test_refused_start_keeps_the_throwaway_branch_and_says_so(cli, guarded, monkeypatch, capsys):
    facts, posts, deletes = guarded
    fake_throwaway(cli, facts, monkeypatch, [])
    monkeypatch.setattr(cli, "_capacity", lambda *_: "refusing: a release/* run is active")
    with pytest.raises(SystemExit, match="release"):
        cli.cmd_dispatch(dispatch_args(throwaway=True, cleanup=True, wait=True))
    assert posts == [] and deletes == [] and "stays on the remote" in capsys.readouterr().err


@pytest.mark.parametrize("changes, reason", [
    ({"cleanup": True}, "needs --throwaway and --wait"),
    ({"throwaway": True, "cleanup": True}, "needs --throwaway and --wait"),
    ({"throwaway": True, "reviewed_head": TIP}, "use one of them"),
])
def test_throwaway_option_shapes_are_rejected_before_any_remote_call(cli, monkeypatch, changes, reason):
    monkeypatch.setattr(cli, "remote_tip", lambda *_: pytest.fail("no remote read expected"))
    monkeypatch.setattr(cli, "ghj", lambda *_a, **_kw: pytest.fail("no remote read expected"))
    with pytest.raises(SystemExit, match=reason):
        cli.cmd_dispatch(dispatch_args(**changes))


def slot(registry):
    raw = json.dumps(registry).encode()
    import gzip
    return json.dumps({"codec": "gzip+base64", "bytes": len(raw), "data": base64.b64encode(gzip.compress(raw)).decode()})


def test_guard_reads_use_the_admission_decoder_and_fail_closed(tmp_path, monkeypatch):
    pytest.importorskip("steuermaschine_heinzel.native.admission_lib.focused_slots", reason="the admission area's decoder is not merged yet")
    cli = load()
    replies = {
        f"repos/{cli.R}/pulls?state=open&per_page=100": [[{"number": 790, "head": {"sha": TIP}}], [{"number": 7, "head": {"sha": NEW}}]],
        f"repos/{cli.R}/actions/variables?per_page=30": [{"total_count": 3, "variables": [
            {"name": "FOCUSED_CI_PLANS", "value": json.dumps({"pull_request:" + TIP: {}})},
            {"name": "FOCUSED_CI_PLANS_V2_1", "value": slot({"pull_request:" + NEW: {}, "push-tree:" + TIP: {}})},
            {"name": "SONAR_HOST", "value": "x"}]}],
        f"repos/{cli.R}/commits/{TIP}/branches-where-head": [{"name": "fix/clock"}, {"name": "validation/clock"}],
    }
    monkeypatch.setattr(cli, "ghj", lambda *params, **_kw: replies[params[-1]])
    assert cli.attachment_facts(TIP) == (
        [(790, TIP), (7, NEW)], {"pull_request:" + TIP, "pull_request:" + NEW, "push-tree:" + TIP}, ["fix/clock", "validation/clock"])
    with pytest.raises(SystemExit) as stop:
        cli.attachment_guard("validation/clock", TIP)
    assert stop.value.code == 2

    for key, bad, reason in [
        (f"repos/{cli.R}/pulls?state=open&per_page=100", None, "cannot read the open PRs"),
        (f"repos/{cli.R}/pulls?state=open&per_page=100", [[{"number": 1, "head": {"sha": "main"}}]], "malformed open PR list"),
        (f"repos/{cli.R}/actions/variables?per_page=30", None, "cannot read the focused plan registry"),
        (f"repos/{cli.R}/actions/variables?per_page=30", [{"total_count": 9, "variables": []}], "incomplete focused plan registry"),
        (f"repos/{cli.R}/actions/variables?per_page=30",
         [{"total_count": 1, "variables": [{"name": "FOCUSED_CI_PLANS_V2_0", "value": "{\"codec\": \"none\"}"}]}], "undecodable"),
        (f"repos/{cli.R}/actions/variables?per_page=30",
         [{"total_count": 1, "variables": [{"name": "FOCUSED_CI_PLANS", "value": None}]}], "undecodable"),
        (f"repos/{cli.R}/commits/{TIP}/branches-where-head", {"message": "No commit found"}, "cannot read the branches"),
    ]:
        good = replies[key]
        replies[key] = bad
        with pytest.raises(SystemExit, match=reason):
            cli.attachment_facts(TIP)
        replies[key] = good

    monkeypatch.setattr(cli, "ghj", lambda *_a, **_kw: {"ref": "refs/heads/fix/clock-2", "object": {"type": "commit", "sha": TIP}})
    with pytest.raises(SystemExit, match="cannot read the tip of fix/clock"):
        cli.remote_tip("fix/clock")  # a prefix match is not the branch


def scratch_git(*args, cwd):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_make_throwaway_pushes_one_empty_commit_from_a_temporary_worktree(tmp_path, monkeypatch):
    """Real git against a scratch remote: the source branch, the clone's HEAD and the caller's index stay untouched."""
    for name in subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True).stdout.split():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                       "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(key, value)
    cli = load()
    remote = tmp_path / cli.R
    remote.parent.mkdir(parents=True)
    seed = tmp_path / "seed"
    scratch_git("init", "-q", "-b", "main", str(seed), cwd=tmp_path)
    (seed / "f.txt").write_text("x\n")
    scratch_git("add", "f.txt", cwd=seed)
    scratch_git("commit", "-q", "-m", "feat: x", cwd=seed)
    scratch_git("branch", "fix/clock", cwd=seed)
    scratch_git("clone", "-q", "--bare", str(seed), str(remote) + ".git", cwd=tmp_path)
    clone = tmp_path / "clone.git"
    scratch_git("clone", "-q", "--bare", str(remote) + ".git", str(clone), cwd=tmp_path)
    tip = scratch_git("rev-parse", "refs/heads/fix/clock", cwd=remote.with_name(remote.name + ".git"))
    bare = remote.with_name(remote.name + ".git")

    def github(*params, **_kw):
        branch = params[1].split("/git/ref/heads/", 1)[1]
        done = subprocess.run(["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], cwd=bare, capture_output=True, text=True)
        return {"ref": f"refs/heads/{branch}", "object": {"type": "commit", "sha": done.stdout.strip()}} if done.returncode == 0 else None

    monkeypatch.setattr(cli, "ghj", github)
    monkeypatch.setenv("SM_TEST_ALLOW_LOCAL_PUSH", "git push")  # the push goes to a bare repository in tmp_path
    cli.EDGE = cli.Edge(commands=Subprocess())
    monkeypatch.setattr(cli, "CORE_CLONE", clone)
    monkeypatch.setattr(cli, "SCRATCH", tmp_path / "scratch")
    target, new = cli.make_throwaway("fix/clock", tip)
    assert target == f"validation/fix-clock-{tip[:8]}" and new != tip
    assert scratch_git("rev-parse", f"refs/heads/{target}", cwd=bare) == new
    assert scratch_git("rev-parse", f"{new}^", cwd=bare) == tip
    assert scratch_git("log", "-1", "--format=%s", new, cwd=bare) == "chore: validation only"
    assert scratch_git("diff", "--stat", tip, new, cwd=bare) == ""
    assert scratch_git("rev-parse", "refs/heads/fix/clock", cwd=bare) == tip
    assert list((tmp_path / "scratch").iterdir()) == []  # the temporary worktree is gone
    assert "throwaway-" not in scratch_git("worktree", "list", cwd=clone)
    with pytest.raises(SystemExit, match="already exists"):
        cli.make_throwaway("fix/clock", tip)
    monkeypatch.setattr(cli, "R", "someone/else")
    with pytest.raises(SystemExit, match="is not a clone of"):
        cli.make_throwaway("main", tip)
