"""The private adapter consumes reviewed public evidence without changing any live release state."""

import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import policy_kit as kit  # noqa: E402
from steuermaschine_heinzel.native.queue_lib import release_candidate
from steuermaschine_heinzel.native.release_lib import candidate_policy as policy

HEAD, TREE = "a" * 40, "b" * 40
NAMES = policy.api_check_runs(kit.policy())
REAL_AT_COMMIT_API = policy.core_policy.at_commit_api  # the policy's required contexts but the aggregate


@pytest.fixture(autouse=True)
def reviewed_policy(monkeypatch):
    """Core's release policy at the release head: read through at_commit_api, replaced by the kit's old shape."""
    seen = []
    monkeypatch.setattr(policy.core_policy, "at_commit_api", lambda get, sha: seen.append(sha) or kit.policy(commit=sha))
    return seen


def test_adapter_passes_frozen_head_tree_and_reviewed_pins_to_the_public_reader(monkeypatch, reviewed_policy):
    calls = []
    def ghj(*args, **kwargs):
        calls.append(args)
        if args[1].endswith("pulls/17"):
            return {"head": {"sha": HEAD}}
        if args[1].endswith("commits/" + HEAD):
            return {"commit": {"tree": {"sha": TREE}}}
        raise AssertionError(args)
    monkeypatch.setattr(policy.hzlib, "ghj", ghj)
    monkeypatch.setattr(release_candidate, "discover", lambda get, head: 99 if head == HEAD else pytest.fail("wrong head"))
    def verify(get, download, expected, pins):
        assert expected == release_candidate.ExpectedCandidate(99, 17, HEAD, TREE)
        assert pins.commit == HEAD and pins.sources == kit.manifest()["sources"]
        return {"run_id": 99, "run_attempt": 1, "head": HEAD, "tree": TREE}
    monkeypatch.setattr(release_candidate, "verify", verify)
    assert policy.verify_pr(17)["run_id"] == 99
    assert len(calls) == 2 and reviewed_policy == [HEAD]  # the policy of the exact release head, nothing else


def test_verify_pr_refuses_a_release_head_without_a_reviewed_policy(monkeypatch):
    """The real reader: the policy comes from the contents API at the release head; none there refuses."""
    monkeypatch.setattr(policy.core_policy, "at_commit_api", REAL_AT_COMMIT_API)
    answers = {"pulls/17": {"head": {"sha": HEAD}}, f"commits/{HEAD}": {"commit": {"tree": {"sha": TREE}}}}

    def api(path):
        if path not in answers:
            raise release_candidate.CandidateError("candidate API evidence unavailable")
        return answers[path]

    monkeypatch.setattr(policy, "_api", api)
    monkeypatch.setattr(release_candidate, "discover", lambda *_: pytest.fail("discovery before the policy"))
    with pytest.raises(release_candidate.CandidateError, match="unavailable"):
        policy.verify_pr(17)
    answers.update(kit.contents_routes(HEAD, kit.files("new")))
    monkeypatch.setattr(release_candidate, "discover", lambda get, head: 99)
    seen = {}
    monkeypatch.setattr(release_candidate, "verify", lambda get, download, expected, pins: seen.update(pins=pins) or {})
    policy.verify_pr(17)
    assert seen["pins"].commit == HEAD and len(seen["pins"].test_jobs) == 4
    answers.update(kit.contents_routes(HEAD, kit.files("new", workflow_blob=b"tampered\n", policy=kit.files("new")[kit.PATH])))
    with pytest.raises(release_candidate.CandidateError, match="does not describe its own"):
        policy.verify_pr(17)


def test_unavailable_candidate_api_does_not_dispatch_or_substitute_evidence(monkeypatch):
    monkeypatch.setattr(policy.hzlib, "ghj", lambda *args, **kwargs: None)
    with pytest.raises(release_candidate.CandidateError, match="unavailable"):
        policy.verify_pr(17)


def test_bad_head_stops_before_any_run_discovery(monkeypatch):
    monkeypatch.setattr(policy.hzlib, "ghj", lambda *args, **kwargs: {"head": {"sha": "unknown"}})
    monkeypatch.setattr(release_candidate, "discover", lambda *args: pytest.fail("discovery after malformed head"))
    with pytest.raises(release_candidate.CandidateError, match="exact source head"):
        policy.verify_pr(17)


def test_candidate_artifact_download_failure_is_not_accepted(monkeypatch):
    monkeypatch.setattr(policy.hzlib, "ghj", lambda *args, **kwargs: {"head": {"sha": HEAD}, "commit": {"tree": {"sha": TREE}}})
    monkeypatch.setattr(release_candidate, "discover", lambda *args: 99)
    monkeypatch.setattr(policy.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=b""))
    monkeypatch.setattr(release_candidate, "verify", lambda get, download, *args: download(1))
    with pytest.raises(release_candidate.CandidateError, match="artifact unavailable"):
        policy.verify_pr(17)


# --- owner waiver (v0.9.29): one KNOWN flaky job, green on the attempt-2 rerun of failed jobs only ------------------
JOB = "Test (macos-latest, Python 3.14, integration)"
WAIVER = {"job": JOB, "bead": "0bb", "owner_quote": "Release it. 0.23s are not a reason to spend hours of CI time!"}


def _job(name, attempt, conclusion, runner=7):
    return {"name": name, "run_attempt": attempt, "head_sha": HEAD, "status": "completed", "conclusion": conclusion,
            "runner_id": runner, "steps": [{"n": 1}], "id": hash((name, attempt)) & 0xFFFF}


def _world(attempt=2, current="success", one=None, two=None):
    one = one if one is not None else [_job("Build", 1, "success"), _job(JOB, 1, "failure"), _job("CI OK", 1, "failure")]
    two = two if two is not None else [_job("Build", 2, "success", 0), _job(JOB, 2, "success"), _job("CI OK", 2, "success")]
    pages = {
        "actions/workflows/ci.yml/runs?head_sha=" + HEAD + "&per_page=100&page=1": {"total_count": 1, "workflow_runs": [
            {"id": 99, "head_sha": HEAD, "run_attempt": attempt, "status": "completed", "conclusion": current}]},
        "actions/runs/99/attempts/1": {"id": 99, "run_attempt": 1, "head_sha": HEAD, "status": "completed", "conclusion": "failure"},
        "actions/runs/99/attempts/1/jobs?per_page=100&page=1": {"total_count": len(one), "jobs": one},
        "actions/runs/99/attempts/2/jobs?per_page=100&page=1": {"total_count": len(two), "jobs": two},
    }
    return lambda path: pages[path]


KNOWN = lambda run, attempt: ("known", f"{JOB}=KNOWN(0bb) via 1 failing test(s)")  # noqa: E731


def test_waiver_shows_attempt_one_with_only_the_waived_jobs_passing():
    reader, record = policy.waived_view(_world(), HEAD, WAIVER, KNOWN, aggregate="CI OK")
    assert reader("actions/runs/99")["run_attempt"] == 1 and reader("actions/runs/99")["conclusion"] == "success"
    listing = reader("actions/workflows/ci.yml/runs?head_sha=" + HEAD + "&per_page=100&page=1")
    assert release_candidate.discover(reader, HEAD) == 99 and listing["workflow_runs"][0]["run_attempt"] == 1
    jobs = {j["name"]: j for j in reader("actions/runs/99/attempts/1/jobs?per_page=100&page=1")["jobs"]}
    assert all(j["conclusion"] == "success" and j["run_attempt"] == 1 for j in jobs.values())
    assert record["bead"] == "0bb" and record["attempt1_failed"] == ["CI OK", JOB] and record["run_attempt"] == 2


@pytest.mark.parametrize("why, kwargs, waiver, classify, match", [
    ("unknown flake", {}, WAIVER, lambda r, a: ("unknown", "UNKNOWN"), "KNOWN failure"),
    ("other bead", {}, WAIVER | {"bead": "9kk"}, KNOWN, "KNOWN failure"),
    ("another job failed too", {"one": [_job("Build", 1, "failure"), _job(JOB, 1, "failure"), _job("CI OK", 1, "failure")]},
     WAIVER, KNOWN, "not exactly the waived job"),
    ("rerun still red", {"current": "failure"}, WAIVER, KNOWN, "attempt 2 is not green"),
    ("third attempt", {"attempt": 3}, WAIVER, KNOWN, "exactly attempt 2"),
    ("first attempt", {"attempt": 1}, WAIVER, KNOWN, "exactly attempt 2"),
    ("rerun never ran", {"two": [_job("Build", 2, "success"), _job(JOB, 2, "success", 0), _job("CI OK", 2, "success")]},
     WAIVER, KNOWN, "never ran on a runner"),
    ("aggregate only", {}, WAIVER | {"job": "CI OK"}, KNOWN, "single named job"),
    ("no owner words", {}, WAIVER | {"owner_quote": ""}, KNOWN, "owner's own words"),
])
def test_waiver_refuses_everything_else(why, kwargs, waiver, classify, match):
    with pytest.raises(release_candidate.CandidateError, match=match):
        policy.waived_view(_world(**kwargs), HEAD, waiver, classify, aggregate="CI OK")


def test_without_a_waiver_a_rerun_candidate_is_still_refused():
    with pytest.raises(release_candidate.CandidateError, match="rerun candidate"):
        release_candidate.discover(_world(), HEAD)


# merge-gate.yml POSTs merge-gate/coverage-floor check runs; GitHub may list them as jobs of the CI run (v0.9.32).
RUN = 42480597872
REPO_URL = f"https://github.com/{release_candidate.REPOSITORY}"


def _real(name: str, ident: int, conclusion: str) -> dict:
    ran = conclusion == "success"
    return {"id": ident, "run_id": RUN, "run_attempt": 1, "name": name, "status": "completed", "conclusion": conclusion,
            "html_url": f"{REPO_URL}/actions/runs/{RUN}/job/{ident}", "labels": ["ubuntu-24.04"],
            "runner_id": 7 if ran else None, "runner_name": "GitHub Actions 7" if ran else None,
            "steps": [{"name": "Set up job"}] if ran else []}


def _api_check(name: str, ident: int) -> dict:
    return {"id": ident, "run_id": RUN, "run_attempt": 1, "name": name, "status": "completed", "conclusion": "success",
            "html_url": f"{REPO_URL}/runs/{ident}", "check_run_url": f"https://api.github.com/check-runs/{ident}",
            "labels": [], "runner_id": None, "runner_name": None, "steps": []}


def _candidate_jobs() -> list[dict]:
    inventory = kit.expected_jobs("pull_request")
    return [_real(name, 1000 + i, want) for i, (name, want) in enumerate(sorted(inventory.items()))]


def _pages(jobs: list[dict], per_page: int = 100):
    def get(path: str):
        prefix = f"actions/runs/{RUN}/attempts/1/jobs?per_page=100&page="
        assert path.startswith(prefix), path
        page = int(path[len(prefix):])
        return {"total_count": len(jobs), "jobs": jobs[(page - 1) * per_page: page * per_page]}
    return get


def _inventory_ok(get) -> bool:
    """The reviewed reader's exact inventory rule (release_candidate.validate)."""
    jobs = release_candidate.job_inventory(get, RUN)
    inventory = kit.expected_jobs("pull_request")
    return len(jobs) == len(inventory) and {job.get("name") for job in jobs} == set(inventory)


def test_api_check_runs_listed_as_candidate_jobs_no_longer_refuse_the_inventory():
    jobs = _candidate_jobs() + [_api_check("merge-gate", 5001), _api_check("coverage-floor", 5002)]
    assert len(jobs) == 71
    assert not _inventory_ok(_pages(jobs))  # the old behaviour: "missing, extra or short-matrix candidate jobs"
    assert _inventory_ok(policy.without_api_check_runs(_pages(jobs), NAMES))
    assert _inventory_ok(policy.without_api_check_runs(_pages(jobs, per_page=30), NAMES))  # raw pagination still exhausted
    assert _inventory_ok(policy.without_api_check_runs(_pages(_candidate_jobs()), NAMES))  # 0.9.31 shape unchanged


@pytest.mark.parametrize("why, mutate", [
    ("other API check run name", lambda jobs: jobs + [_api_check("Sonar", 5001)]),
    ("merge-gate that acquired a runner", lambda jobs: jobs + [_api_check("merge-gate", 5001) | {"runner_id": 9}]),
    ("merge-gate with steps", lambda jobs: jobs + [_api_check("merge-gate", 5001) | {"steps": [{"name": "x"}]}]),
    ("merge-gate with labels", lambda jobs: jobs + [_api_check("merge-gate", 5001) | {"labels": ["ubuntu-24.04"]}]),
    ("merge-gate that is a real job of the run", lambda jobs: jobs + [_real("merge-gate", 5001, "skipped")]),
    ("merge-gate of another run", lambda jobs: jobs + [_api_check("merge-gate", 5001) | {"run_id": RUN + 1}]),
    ("missing job hidden behind API check runs", lambda jobs: jobs[1:] + [_api_check("merge-gate", 5001)]),
    ("short matrix", lambda jobs: [j for j in jobs if "Python 3.15" not in j["name"]] + [_api_check("coverage-floor", 5002)]),
    ("renamed matrix cell", lambda jobs: [j | {"name": "merge-gate"} if j["name"] == "Build" else j for j in jobs]),
])
def test_everything_else_still_refuses_the_inventory(why, mutate):
    assert not _inventory_ok(policy.without_api_check_runs(_pages(mutate(_candidate_jobs())), NAMES))


def test_filtered_reader_keeps_raw_integrity_checks_and_passes_other_paths():
    jobs = _candidate_jobs() + [_api_check("merge-gate", 1000)]  # duplicate ID of a real job
    with pytest.raises(release_candidate.CandidateError, match="duplicate"):
        policy.without_api_check_runs(_pages(jobs), NAMES)(f"actions/runs/{RUN}/attempts/1/jobs?per_page=100&page=1")
    with pytest.raises(release_candidate.CandidateError, match="pagination"):
        policy.without_api_check_runs(_pages(jobs), NAMES)(f"actions/runs/{RUN}/attempts/1/jobs?per_page=100&page=2")
    assert policy.without_api_check_runs(lambda path: {"path": path}, NAMES)("pulls/17") == {"path": "pulls/17"}


def test_verify_pr_reads_jobs_through_the_api_check_run_filter(monkeypatch):
    jobs = _pages(_candidate_jobs() + [_api_check("merge-gate", 5001)])
    monkeypatch.setattr(policy, "_api", lambda path: jobs(path) if "/jobs?" in path else
                        ({"head": {"sha": HEAD}} if path.startswith("pulls/") else {"commit": {"tree": {"sha": TREE}}}))
    monkeypatch.setattr(release_candidate, "discover", lambda get, head: RUN)
    seen = {}
    monkeypatch.setattr(release_candidate, "verify", lambda get, download, expected, pins: seen.update(ok=_inventory_ok(get)))
    policy.verify_pr(17)
    assert seen["ok"]


@pytest.mark.parametrize("variant", ["reviewed", "stale_workflow", "wrong_workflow", "wrong_recorder"])
def test_source_repin_checks_actual_executed_bytes_before_candidate_validation(monkeypatch, variant):
    """New workflow passes the source boundary; stale/wrong inputs fail closed."""
    import io
    import json
    import zipfile

    sources = {kit.WORKFLOW: kit.workflow_bytes(), kit.RECORDER: kit.recorder_bytes()}
    pins = dict(kit.manifest()["sources"])
    if variant == "stale_workflow":
        pins[kit.WORKFLOW] = "d1c7e3bc603901954ffaa3616bb1e1c003bc2ec82677c70cf4276eba26a4548c"
    if variant == "wrong_workflow":
        sources[kit.WORKFLOW] += b"\n# unexpected change\n"
    if variant == "wrong_recorder":
        sources[kit.RECORDER] += b"\n# unexpected change\n"
    pins = kit.policy(commit=HEAD, sources=pins)
    proof = {key: HEAD for key in ("source_commit", "head", "base", "tree")}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(release_candidate.PROOF_FILE, json.dumps(proof))
    artifact = {"id": 1, "name": release_candidate.ARTIFACT, "expired": False,
                "workflow_run": {"id": 99, "head_sha": HEAD}}
    def get(path):
        if path == "actions/runs/99":
            return {"status": "completed"}
        if path == "actions/runs/99/artifacts?per_page=100":
            return {"total_count": 1, "artifacts": [artifact]}
        return {}
    monkeypatch.setattr(release_candidate, "job_inventory", lambda *_: [])
    monkeypatch.setattr(release_candidate, "source_bytes", lambda _get, path, _ref, _pinned: sources[path])
    reached = []
    monkeypatch.setattr(release_candidate, "validate", lambda *_: reached.append(True) or {"source_boundary": "passed"})
    expected = release_candidate.ExpectedCandidate(99, 17, HEAD, TREE)
    if variant == "reviewed":
        assert release_candidate.verify(get, lambda _: buffer.getvalue(), expected, pins) == {"source_boundary": "passed"}
        assert reached == [True]
    else:
        with pytest.raises(release_candidate.CandidateError, match="executed CI source differs"):
            release_candidate.verify(get, lambda _: buffer.getvalue(), expected, pins)
        assert reached == []
