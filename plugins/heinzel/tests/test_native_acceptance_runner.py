"""An alternate runner needs exact reviewed source and complete independent VM proof."""

import base64
import hashlib
import json
import sys
from types import SimpleNamespace

import policy_kit as kit
import pytest

from steuermaschine_heinzel.native.queue_lib import core_policy
from steuermaschine_heinzel.native.queue_lib.release_candidate import CandidateError
from steuermaschine_heinzel.native.release_lib import acceptance_runner as alt
from steuermaschine_heinzel.native.release_lib import runner

HEAD, TAG = "a" * 40, "b" * 40
LABEL = "macos-15"
WORKFLOW, HARNESS, OLD = b"reviewed workflow", b"reviewed harness", b"original tagged workflow"
SNAPSHOT = {"version": "0.9.55", "tag": "v0.9.55", "commit": TAG, "wheel_sha256": "c" * 64, "profile_sha256": "d" * 64}


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def contract():
    return {"schema": 1, "workflow": ".github/workflows/acceptance.yml", "workflow_sha256": sha(WORKFLOW),
            "harness": "scripts/acceptance/run_acceptance.py", "harness_sha256": sha(HARNESS), "tagged_workflows": [sha(OLD)],
            "runners": {LABEL: {"os": "macOS", "image_os": "macos15", "architecture": "arm64", "python": "3.15"}},
            "shards": ["public", "update", "plugins", "browser"],
            "jobs": {"pins": "Acceptance pins", "shard": "Acceptance shard ({runner}, {shard})", "linux_runner": "ubuntu-24.04",
                     "macos_logical_runner": "macos-latest", "linux_result": "Acceptance (ubuntu-24.04)", "macos_result": "Acceptance (macos-latest)"}}


@pytest.fixture
def bound(contract, monkeypatch):
    monkeypatch.setattr(alt.core_facts, "at", lambda commit: SimpleNamespace(repository=runner.CORE, acceptance=contract))
    return {"version": "0.9.55", "app_version": "0.9.55", "snapshot": SNAPSHOT, "ref": "main", "commit": HEAD,
            "runner": {"label": LABEL, "head": HEAD, "policy": contract}}


@pytest.fixture
def source(contract, monkeypatch):
    blobs = {(HEAD, contract["workflow"]): WORKFLOW, (HEAD, contract["harness"]): HARNESS, (TAG, contract["workflow"]): OLD}
    calls = []
    def read(*args, **kwargs):
        path = args[1].split("/contents/", 1)[1]
        name, commit = path.split("?ref=")
        calls.append((commit, name))
        value = blobs.get((commit, name))
        return {"type": "file", "encoding": "base64", "content": base64.b64encode(value).decode()} if value else None
    monkeypatch.setattr(alt.core_facts, "at", lambda commit: SimpleNamespace(repository=runner.CORE, acceptance=contract))
    return SimpleNamespace(CORE=runner.CORE, hzlib=SimpleNamespace(ghj=read), blobs=blobs, calls=calls)


def test_reads_reviewed_main_and_original_tagged_source(source, contract):
    binding = alt.bind(source.CORE, source.hzlib.ghj, SNAPSHOT, HEAD, HEAD, LABEL)
    assert binding == {"label": LABEL, "head": HEAD, "policy": contract}
    assert set(source.calls) == {(HEAD, contract["workflow"]), (HEAD, contract["harness"]), (TAG, contract["workflow"])}
    assert alt.supports_snapshot(source, HEAD)
    assert not alt.supports_snapshot(source, HEAD, "f" * 64)


def test_changed_generated_source_cannot_enable_a_new_tag_default(source, contract):
    source.blobs[HEAD, contract["harness"]] = b"unreviewed"
    assert not alt.supports_snapshot(source, HEAD)


def test_runner_identity_uses_explicit_reviewed_policy(source, bound, monkeypatch):
    monkeypatch.setattr(runner, "state", lambda: {"snapshot": SNAPSHOT})
    monkeypatch.setattr(runner.hzlib, "gh", lambda *args: (0, HEAD))
    monkeypatch.setattr(runner.hzlib, "ghj", source.hzlib.ghj)
    monkeypatch.setattr(runner.release_snapshot, "workflow_supports_snapshot", lambda *args: pytest.fail("alternate must use paired policy"))
    assert runner.acceptance_identity("0.9.55", SNAPSHOT, "main", LABEL, HEAD) == bound


@pytest.mark.parametrize("wrong", ["expected", "workflow", "harness", "tag", "absent", "runner", "shards"])
def test_unreviewed_or_mismatched_source_refuses_before_dispatch(source, contract, monkeypatch, wrong):
    expected = TAG if wrong == "expected" else HEAD
    if wrong in {"workflow", "harness"}:
        source.blobs[HEAD, contract[wrong]] = b"changed"
    elif wrong == "tag":
        source.blobs[TAG, contract["workflow"]] = b"changed"
    elif wrong == "absent":
        monkeypatch.setattr(alt.core_facts, "at", lambda commit: SimpleNamespace(repository=runner.CORE, acceptance=None))
    elif wrong == "runner":
        contract["runners"] = {"macos-15-intel": contract["runners"][LABEL]}
    elif wrong == "shards":
        contract["shards"].pop()
    with pytest.raises(CandidateError):
        alt.bind(source.CORE, source.hzlib.ghj, SNAPSHOT, HEAD, expected, LABEL)


def test_existing_manifest_without_optional_acceptance_still_reads(contract):
    data = kit.manifest("new")
    assert core_policy.parse(kit.encode(data), HEAD).acceptance is None
    data["acceptance"] = contract
    assert core_policy.parse(kit.encode(data), HEAD).acceptance == contract
    data["acceptance"] = []
    with pytest.raises(CandidateError, match="acceptance malformed"):
        core_policy.parse(kit.encode(data), HEAD)


@pytest.fixture
def hosted(bound):
    repo = {"id": 12, "full_name": runner.CORE}
    run = {"id": 112, "run_attempt": 1, "head_sha": HEAD, "head_branch": "main", "path": bound["runner"]["policy"]["workflow"],
           "event": "workflow_dispatch", "display_title": "Acceptance 0.9.55 release-new", "repository": repo, "head_repository": repo}
    state = {"acceptance_identity": bound, "acceptance_marker": "release-new", "acceptance_attempts": [{"acceptance_run_id": 111}]}
    return state, run


@pytest.mark.parametrize(("key", "value"), [("head_branch", "v0.9.55"), ("head_sha", TAG), ("run_attempt", 2),
                                           ("event", "push"), ("path", "other.yml"), ("display_title", "Acceptance 0.9.55 wrong")])
def test_wrong_run_cannot_be_adopted(hosted, key, value):
    state, run = hosted
    alt.validate_run(state, run, runner.CORE, 112)
    run[key] = value
    with pytest.raises(CandidateError):
        alt.validate_run(state, run, runner.CORE, 112)


def test_archived_run_cannot_be_reused(hosted):
    state, run = hosted
    state["acceptance_attempts"].append({"acceptance_run_id": 112})
    with pytest.raises(CandidateError, match="reuse"):
        alt.validate_run(state, run, runner.CORE, 112)


def test_receipt_cannot_substitute_a_different_policy(hosted):
    state, run = hosted
    forged = json.loads(json.dumps(state["acceptance_identity"]["runner"]["policy"]))
    forged["runners"][LABEL]["python"] = "3.14"
    state["acceptance_identity"]["runner"]["policy"] = forged
    with pytest.raises(CandidateError, match="reviewed source"):
        alt.validate_run(state, run, runner.CORE, 112)


@pytest.fixture
def jobs(bound):
    policy = bound["runner"]["policy"]
    rows = []
    def add(name, label, steps):
        ident = len(rows) + 1
        rows.append({"id": ident, "run_id": 112, "run_attempt": 1, "head_sha": HEAD, "name": name,
                     "status": "completed", "conclusion": "success", "runner_id": ident, "runner_group_id": 0,
                     "runner_group_name": "GitHub Actions", "runner_name": f"GitHub Actions {ident}", "labels": [label],
                     "started_at": "2026-11-25T12:00:00Z", "completed_at": "2026-11-25T12:01:00Z",
                     "steps": [{"name": name, "status": "completed", "conclusion": "success"} for name in steps]})
    add(policy["jobs"]["pins"], "ubuntu-24.04", ["Pin the release bytes once for every shard"])
    for logical, actual in (("ubuntu-24.04", "ubuntu-24.04"), ("macos-latest", LABEL)):
        for name in policy["shards"]:
            add(policy["jobs"]["shard"].format(runner=logical, shard=name), actual, ["Run the acceptance harness", "Upload the shard receipt"])
    for os_name in ("linux", "macos"):
        add(policy["jobs"][f"{os_name}_result"], "ubuntu-24.04", ["Reduce the four shard receipts of this OS", "Upload the receipt"])
    return rows


@pytest.mark.parametrize("damage", [None, "missing", "wrong_label", "same_vm", "failed", "empty_steps", "wrong_head", "rerun"])
def test_all_eight_real_vms_and_workloads_are_required(bound, jobs, monkeypatch, damage):
    monkeypatch.setattr(alt.remote, "paged", lambda *args: jobs)
    if damage == "missing":
        jobs.pop()
    elif damage == "wrong_label":
        jobs[5]["labels"] = ["macos-15-intel"]
    elif damage == "same_vm":
        jobs[6]["runner_id"] = jobs[5]["runner_id"]
    elif damage == "failed":
        jobs[5]["conclusion"] = "failure"
    elif damage == "empty_steps":
        jobs[5]["steps"] = []
    elif damage == "wrong_head":
        jobs[5]["head_sha"] = TAG
    elif damage == "rerun":
        jobs[5]["run_attempt"] = 2
    if damage:
        with pytest.raises((CandidateError, alt.remote.InvalidCapacity)):
            alt.validate_jobs(bound, 112)
    else:
        alt.validate_jobs(bound, 112)


@pytest.fixture
def result(bound):
    config = bound["runner"]["policy"]
    return {"harness_sha256": config["harness_sha256"], "ci": {"runner_arch": "ARM64"},
            "host": {name: {"runner_label": LABEL, "machine": "arm64", "macos": "15.8.2", "image_os": "macos15",
                            "image_version": "20261125.1.0", "python_version": "3.15.1"} for name in config["shards"]}}


@pytest.mark.parametrize(("key", "value"), [("runner_label", "macos-latest"), ("machine", "x86_64"), ("macos", "26.1.0"),
                                           ("image_os", "macos26"), ("image_version", ""), ("python_version", "3.14.1")])
def test_each_shard_needs_native_image_and_python(bound, result, key, value):
    assert alt.receipt_matches(result, bound, "macOS")
    result["host"]["browser"][key] = value
    assert not alt.receipt_matches(result, bound, "macOS")


def test_harness_or_incomplete_host_map_refused(bound, result):
    result["harness_sha256"] = "f" * 64
    assert not alt.receipt_matches(result, bound, "macOS")
    result["harness_sha256"] = bound["runner"]["policy"]["harness_sha256"]
    result["host"].pop("update")
    assert not alt.receipt_matches(result, bound, "macOS")
    assert alt.receipt_matches({}, {"ref": "v0.9.55"}, "macOS")  # Existing tagged proof is unchanged.


@pytest.mark.parametrize(("too_large", "main_moved"), [(False, False), (True, False), (False, True)])
def test_alternate_replacement_archives_receipt_and_dispatches_once(tmp_path, monkeypatch, bound, too_large, main_moved):
    state = {"version": "0.9.55", "finished": True, "accepted": False, "acceptance_snapshot_mode": True,
             "acceptance_identity": {key: value for key, value in bound.items() if key != "runner"} | {"ref": "v0.9.55", "commit": TAG},
             "acceptance_run_id": 111, "acceptance_run_attempt": 1, "acceptance_marker": "release-old", "snapshot": SNAPSHOT}
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(state))
    path.chmod(0o600)
    original = path.read_bytes()
    found = [{"databaseId": 111, "status": "completed"}]
    calls = []
    def remember(**fields):
        state.update(fields)
        path.write_text(json.dumps(state))
    def dispatch(*args, **kwargs):
        calls.append((args, kwargs))
        found[0] = {"databaseId": 112, "status": "completed"}
    monkeypatch.setattr(runner, "state", lambda: state)
    monkeypatch.setattr(runner, "remember", remember)
    monkeypatch.setattr(runner, "paths", lambda: (None, path))
    monkeypatch.setattr(runner, "rerun_rejection", lambda *args: None)
    monkeypatch.setattr(runner, "verdict", lambda *args: (False, ["original rejection"]))
    monkeypatch.setattr(runner, "ours", lambda *args: found[0] if not state.get("runner_pending") else None)
    def identity(*args):
        state["runner_pending"] = True
        return bound
    monkeypatch.setattr(runner, "acceptance_identity", identity)
    monkeypatch.setattr(runner.hzlib, "gh", lambda *args: (0, TAG if main_moved else HEAD))
    def step(name, done, act, timeout):
        act()
        state.pop("runner_pending", None)
    monkeypatch.setattr(runner, "step", step)
    monkeypatch.setattr(runner, "dispatch", dispatch)
    if too_large:
        monkeypatch.setattr(runner.release_rejection, "LIMIT", len(original) + 1)
        with pytest.raises(runner.Stop, match="receipt size limit"):
            runner.acceptance_step("0.9.55", 0, SNAPSHOT, "main", 111, runner_label=LABEL, reviewed_head=HEAD)
        assert path.read_bytes() == original and not calls
        return
    if main_moved:
        with pytest.raises(runner.Stop, match="no request sent, original receipt retained"):
            runner.acceptance_step("0.9.55", 0, SNAPSHOT, "main", 111, runner_label=LABEL, reviewed_head=HEAD)
        assert not calls and path.read_bytes() == original
        assert state["acceptance_identity"]["commit"] == TAG and state["finished"] is True
        return
    assert runner.acceptance_step("0.9.55", 0, SNAPSHOT, "main", 111, runner_label=LABEL, reviewed_head=HEAD) == 112
    assert len(calls) == 1 and calls[0][1]["macos_runner"] == LABEL
    assert calls[0][1]["expected_wheel_sha256"] == SNAPSHOT["wheel_sha256"]
    assert base64.b64decode(state["acceptance_attempts"][0]["receipt_base64"]) == original
    assert runner.acceptance_step("0.9.55", 0, SNAPSHOT, "main", 111, runner_label=LABEL, reviewed_head=HEAD) == 112
    assert len(calls) == 1 and state["accepted"] is False
    with pytest.raises(runner.Stop, match="changed"):
        runner.acceptance_step("0.9.55", 0, SNAPSHOT, "main", 111, runner_label="macos-26", reviewed_head=HEAD)


@pytest.mark.parametrize("extra", [[], ["--acceptance-head", HEAD], ["--acceptance-head", HEAD, "--acceptance-ref", "v0.9.55"],
                                  ["--acceptance-head", HEAD, "--acceptance-ref", "main", "--snapshot-acceptance"]])
def test_cli_requires_complete_explicit_replacement_identity(monkeypatch, extra):
    monkeypatch.setattr(runner, "tail_receipt", lambda *args: pytest.fail("invalid request must not read state"))
    with pytest.raises(SystemExit) as error:
        runner.main(["0.9.55", "--acceptance-runner", LABEL, *extra])
    assert error.value.code == 2


def test_cli_forwards_explicit_choice_without_dispatching(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(runner, "tail_receipt", lambda *args: tmp_path / "absent.json")
    for name in ("acquire_global_lock", "free_global_lock", "free_tail_lock", "rejection_guard"):
        monkeypatch.setattr(runner, name, lambda *args: None)
    monkeypatch.setattr(runner, "state", lambda: {})
    monkeypatch.setattr(runner, "release_core", lambda *args, **kwargs: seen.append((args, kwargs)) or 0)
    assert runner.main(["0.9.55", "--acceptance-runner", LABEL, "--acceptance-head", HEAD,
                        "--acceptance-ref", "main", "--snapshot-acceptance", "--replace-acceptance-run", "111"]) == 0
    assert seen[0][1]["runner_label"] == LABEL and seen[0][1]["reviewed_head"] == HEAD


def test_alternate_option_cannot_start_a_new_release(monkeypatch):
    monkeypatch.setattr(runner, "state", lambda: {})
    monkeypatch.setattr(runner, "pick_flow", lambda *args: pytest.fail("release prelude must not run"))
    with pytest.raises(runner.Stop, match="existing exact published snapshot"):
        runner.release_core("0.9.56", "", 0, snapshot_acceptance=True, harness_ref="main", replace_run=111,
                            runner_label=LABEL, reviewed_head=HEAD)


@pytest.fixture
def native_route(tmp_path, monkeypatch, source):
    from test_native_release_writes import make

    from steuermaschine.github.testing import FakeGh
    from steuermaschine_heinzel.native import release as area

    rt = make(tmp_path, monkeypatch, FakeGh())
    receipt = tmp_path / "tail.json"
    original = {"version": "0.9.55", "finished": True, "accepted": False, "acceptance_snapshot_mode": True,
                "acceptance_identity": {"version": "0.9.55", "app_version": "0.9.55", "snapshot": SNAPSHOT,
                                        "ref": "v0.9.55", "commit": TAG},
                "acceptance_run_id": 111, "acceptance_run_attempt": 1, "acceptance_marker": "release-old", "snapshot": SNAPSHOT}
    receipt.write_text(json.dumps(original))
    receipt.chmod(0o600)
    phase = {"head": HEAD, "no_send": True, "reads": 0}
    posts, modules = [], []
    factory = area.runner

    def prepare():
        module = factory()  # Actual native loader: fresh, intentionally absent from sys.modules.
        modules.append(module)
        module.state = lambda: json.loads(receipt.read_bytes())
        def remember(**fields):
            receipt.write_text(json.dumps(module.state() | fields))
        module.remember = remember
        module.paths = lambda: (None, receipt)
        module.tail_receipt = lambda version: receipt
        for name in ("route_to_tail", "free_tail_lock", "free_global_lock", "acquire_global_lock", "rejection_guard", "release_snapshot_freeze"):
            setattr(module, name, lambda *args: None)
        module.record_waiver = lambda *args: pytest.fail("waiver side effect on alternate acceptance path")
        module.rerun_rejection = lambda *args: None
        module.verdict = lambda *args: (False, ["original rejection"])
        def ours(*args):
            found = module.state().get("acceptance_run_id")
            return {"databaseId": found, "status": "completed"} if found else None
        module.ours = ours
        module.hzlib.ghj = source.hzlib.ghj
        def gh(*args):
            if args[0] == "api":
                phase["reads"] += 1
                moved = phase["no_send"] and phase["reads"] % 2 == 0
                return (1, "") if moved and phase.get("read_failure") else (0, TAG if moved else phase["head"])
            assert args[:3] == ("workflow", "run", "acceptance.yml")
            posts.append(args)
            assert module.state()["acceptance_requested"] is True
            if phase.get("ambiguous"):
                return 1, ""
            remember(acceptance_run_id=112, acceptance_run_attempt=1)
            return 0, ""
        module.hzlib.gh = gh
        def release_core(version, intro, timeout, snapshot, ref, replaced, **options):
            module.acceptance_step(version, 0, SNAPSHOT, ref, replaced,
                                   runner_label=options["runner_label"], reviewed_head=options["reviewed_head"])
            return 0
        module.release_core = release_core  # Only omit the already-published release prelude, not binding/dispatch.
        return module
    monkeypatch.setattr(area, "runner", prepare)
    monkeypatch.setattr(area.release_stages, "open_window", lambda *args: False)
    monkeypatch.setattr(area.release_stages, "close_window", lambda *args: None)
    return SimpleNamespace(rt=rt, receipt=receipt, phase=phase, posts=posts, modules=modules)


def native_args(head=HEAD):
    return ["release", "resume", "0.9.55", "--snapshot-acceptance", "--acceptance-ref", "main",
            "--replace-acceptance-run", "111", "--acceptance-head", head, "--acceptance-runner", LABEL]


@pytest.mark.parametrize("read_failure", [False, True])
def test_real_native_fresh_runner_no_send_can_resume_with_new_reviewed_head(native_route, source, read_failure):
    from steuermaschine.testing import invoke

    before = native_route.receipt.read_bytes()
    native_route.phase["read_failure"] = read_failure
    first = invoke(native_route.rt, native_args())
    assert first.exit_code == 1 and "no request sent, original receipt retained" in first.stdout + first.stderr
    assert native_route.receipt.read_bytes() == before and native_route.posts == []
    newer = "e" * 40
    source.blobs.update({(newer, path): raw for (head, path), raw in list(source.blobs.items()) if head == HEAD})
    native_route.phase.update(head=newer, no_send=False)
    second = invoke(native_route.rt, native_args(newer))
    assert second.exit_code == 0, second.stdout + second.stderr
    state = json.loads(native_route.receipt.read_bytes())
    assert state["acceptance_identity"]["commit"] == newer and len(native_route.posts) == 1
    assert base64.b64decode(state["acceptance_attempts"][0]["receipt_base64"]) == before
    assert invoke(native_route.rt, native_args(newer)).exit_code == 0
    assert len(native_route.posts) == 1
    assert len({module.__name__ for module in native_route.modules}) == 3
    assert all(module.__name__ not in sys.modules for module in native_route.modules)
    assert all(module.hzlib.sleep == native_route.rt.providers.clock.sleep for module in native_route.modules)


def test_native_ambiguous_dispatch_is_never_repeated_by_a_fresh_invocation(native_route):
    from steuermaschine.testing import invoke

    native_route.phase.update(no_send=False, ambiguous=True)
    first = invoke(native_route.rt, native_args())
    assert first.exit_code == 1 and "dispatching acceptance.yml failed" in first.stdout + first.stderr
    pending = native_route.receipt.read_bytes()
    assert json.loads(pending)["acceptance_requested"] is True and len(native_route.posts) == 1
    native_route.phase["ambiguous"] = False
    second = invoke(native_route.rt, native_args())
    assert second.exit_code == 1 and "timed out" in second.stdout + second.stderr
    assert native_route.receipt.read_bytes() == pending and len(native_route.posts) == 1


@pytest.mark.parametrize("waiver", [["--waive-job", "Tests=bead"], ["--owner-quote", "quote"],
                                   ["--waive-job", "Tests=bead", "--owner-quote", "quote"],
                                   ["--waive-job", ""], ["--owner-quote", ""]])
def test_native_alternate_rejects_waivers_before_side_effects(native_route, waiver):
    from steuermaschine.testing import invoke

    before = native_route.receipt.read_bytes()
    response = invoke(native_route.rt, [*native_args(), *waiver])
    assert response.exit_code == 2
    assert native_route.receipt.read_bytes() == before and not native_route.posts
    assert native_route.phase["reads"] == 0
