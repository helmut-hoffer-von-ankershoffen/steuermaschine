"""Bounded measurements preserve run identity and disclose incomplete evidence.

Moved with the throughput module: the tests below the `tool` fixture are the former tests, byte for byte (check:
`python3 plugins/heinzel/tests/coord_verbatim.py`); the fixture hands them the moved module instead of loading a script."""
# ruff: noqa: E501, SIM, RUF, UP, B, I001, F811

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel.native.velocity_lib import samples


@pytest.fixture
def tool():
    return lambda name: samples


def check(name="CI OK", **extra):
    return {"name": name, "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-11-02T21:27:42Z", **extra}


def pr(number=466):
    return {"number": number, "title": "fix(ci): verify plugin row", "state": "MERGED", "createdAt": "2026-11-02T21:02:39Z", "mergedAt": "2026-11-02T21:35:48Z", "headRefName": f"fix/sample-{number}", "headRefOid": "a" * 40, "baseRepository": {"nameWithOwner": "helmut-hoffer-von-ankershoffen/heinzel", "databaseId": 1}, "headRepository": {"nameWithOwner": "helmut-hoffer-von-ankershoffen/heinzel", "databaseId": 1}, "mergeCommit": {"oid": "b" * 40}, "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": [check()], "pageInfo": {"hasNextPage": False}}}}}]}}


def run(number=466, run_id=100, attempt=1, status="completed"):
    return {"id": run_id, "run_attempt": attempt, "status": status, "conclusion": "success" if status == "completed" else None, "event": "pull_request", "head_branch": f"fix/sample-{number}", "head_sha": "a" * 40, "repository": {"full_name": "helmut-hoffer-von-ankershoffen/heinzel", "id": 1}, "head_repository": {"full_name": "helmut-hoffer-von-ankershoffen/heinzel", "id": 1}, "pull_requests": [{"number": number, "base": {"repo": {"id": 1}}}], "path": ".github/workflows/ci.yml", "created_at": "2026-11-02T21:02:42Z", "run_started_at": "2026-11-02T21:02:42Z", "updated_at": "2026-11-02T21:27:43Z"}


def job(name="Test (macos-latest)", **extra):
    return {"id": 1, "run_id": 100, "run_attempt": 1, "name": name, "status": "completed", "conclusion": "success", "runner_name": "runner", "created_at": "2026-11-02T21:03:08Z", "started_at": "2026-11-02T21:21:28Z", "completed_at": "2026-11-02T21:25:53Z", "steps": [{}], **extra}


def payload_jobs():
    return {"total_count": 3, "jobs": [job(), job("skipped", conclusion="skipped", runner_name=None), job("cancelled macos", conclusion="cancelled", runner_name=None, steps=[])]}


class FakeGh:
    def __init__(self, prs=None, runs=None, jobs=None):
        self.prs = prs or {466: pr()}
        self.runs = runs or {(100, 1): run()}
        self.jobs = jobs or payload_jobs()
        self.calls = []

    def __call__(self, args, **_):
        self.calls.append(args)
        assert "--paginate" not in args
        path = args[2]
        if path == "graphql":
            data = {"data": {"repository": {f"p{n}": p for n, p in self.prs.items()}}}
        elif "workflows/ci.yml/runs?" in path:
            data = {"total_count": 1, "workflow_runs": [{"run_attempt": 1}]}
        else:
            bits = path.split("/")
            key = (int(bits[bits.index("runs") + 1]), int(bits[bits.index("attempts") + 1].split("?")[0]))
            data = ({**self.jobs, "jobs": [{**j, "run_id": key[0], "run_attempt": key[1]} for j in self.jobs["jobs"]]} if "/jobs?" in path else self.runs[key])
        return SimpleNamespace(returncode=0, stdout=json.dumps(data))


def test_completed_attempt_cache_is_reused_without_reading_latest_attempt(tool, tmp_path):
    v = tool("hz-velocity")
    gh = FakeGh()
    api = v.Api(tmp_path, gh)
    assert api.attempt(v.CORE, 100, 1)["run_attempt"] == 1
    assert api.attempt(v.CORE, 100, 1, jobs=True)["total_count"] == 3
    assert api.calls == 2
    api2 = v.Api(tmp_path, lambda *a, **kw: pytest.fail("cached completed attempt must need no GitHub call"))
    assert api2.attempt(v.CORE, 100, 1)["id"] == 100
    assert api2.attempt(v.CORE, 100, 1, jobs=True)["total_count"] == 3
    assert api2.calls == 0 and api2.cache_hits == 2


def test_pending_attempt_is_neither_cached_nor_given_final_metrics(tool, tmp_path):
    v = tool("hz-velocity")
    gh = FakeGh(runs={(100, 1): run(status="queued")})
    api = v.Api(tmp_path, gh)
    result = v.collect(api, v.CORE, [(466, 100, 1)])
    assert result["samples"][0]["pending"]
    assert "attempt_elapsed_proxy_s" not in result["samples"][0]
    assert not list(tmp_path.rglob("run.json"))
    assert api.calls == 2


def test_run_attempt_identity_must_match_before_cache_or_measurement(tool, tmp_path):
    v = tool("hz-velocity")
    gh = FakeGh(runs={(100, 1): run(attempt=2)})
    api = v.Api(tmp_path, gh)
    with pytest.raises(v.Unavailable, match="identity mismatch"):
        api.attempt(v.CORE, 100, 1)
    assert not list(tmp_path.rglob("run.json"))


def test_hard_api_budget_stops_before_sixteenth_process(tool, tmp_path):
    v = tool("hz-velocity")
    gh = FakeGh()
    api = v.Api(tmp_path, gh)
    for _ in range(15):
        api.read("graphql")
    with pytest.raises(v.Unavailable, match="budget"):
        api.read("graphql")
    assert len(gh.calls) == 15


def test_job_queue_runtime_are_separate_and_skips_and_unstarted_have_no_runtime(tool):
    v = tool("hz-velocity")
    result = v.job_metrics(payload_jobs())
    assert result["job_sample_complete"]
    assert result["executed_jobs_observed"] == result["macos_executed_jobs_observed"] == 1
    assert result["never_started_jobs_observed"] == 1
    assert result["job_queue_tail_max_s"] == result["job_queue_sum_s"] == 1100
    assert result["job_runtime_sum_s"] == 265
    incomplete = payload_jobs() | {"total_count": 101}
    assert not v.job_metrics(incomplete)["job_sample_complete"]


def test_three_ci_samples_plus_release_are_bounded_and_cached(tool, tmp_path):
    v = tool("hz-velocity")
    prs = {n: pr(n) for n in (466, 468, 472)}
    prs[472] |= {"title": "chore(release): v0.9.14", "mergedAt": "2026-11-02T23:35:43Z"}
    runs = {(n, 1): run(n, n) for n in prs}
    runs[(900, 1)] = run(run_id=900) | {"path": ".github/workflows/acceptance.yml", "event": "workflow_dispatch", "display_title": "Acceptance 0.9.14", "head_sha": "b" * 40, "created_at": "2026-11-02T23:50:25Z", "updated_at": "2026-11-03T00:08:01Z"}
    gh = FakeGh(prs, runs)
    api = v.Api(tmp_path, gh)
    result = v.collect(api, v.CORE, [(n, n, 1) for n in prs], (472, 900, 1))
    assert api.calls == 11
    assert result["summary"]["primary_completed_successful_ci_samples"] == 3
    assert result["summary"]["primary_median_attempt_elapsed_proxy_s"] == 1501
    assert result["summary"]["tenfold_improvement_established"] is False
    assert result["release"]["merge_to_acceptance_workflow_success_s"] == 1938
    assert not result["release"]["accepted_artifact_verdict_verified"]
    api2 = v.Api(tmp_path, gh)
    again = v.collect(api2, v.CORE, [(n, n, 1) for n in prs], (472, 900, 1))
    assert api2.calls == 4
    assert again["summary"] == result["summary"]
    assert "contexts(first:100)" in gh.calls[0][-1]


@pytest.mark.parametrize("mutation", ["wrong_branch", "wrong_workflow", "wrong_event"])
def test_ci_selection_cannot_silently_measure_another_branch_or_workflow(tool, tmp_path, mutation):
    v = tool("hz-velocity")
    metadata = run()
    metadata[{"wrong_branch": "head_branch", "wrong_workflow": "path", "wrong_event": "event"}[mutation]] = "other"
    result = v.collect(v.Api(tmp_path, FakeGh(runs={(100, 1): metadata})), v.CORE, [(466, 100, 1)])
    assert "does not match" in result["samples"][0]["missing"]
    assert result["summary"]["primary_completed_successful_ci_samples"] == 0


def test_check_readiness_is_only_proxy_and_only_for_sampled_current_head(tool, tmp_path):
    v = tool("hz-velocity")
    p = pr()
    contexts = p["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
    contexts["nodes"] += [check("review", status="QUEUED", conclusion=None), check("Acceptance (macos)", completedAt="2026-11-02T21:35:05Z")]
    gh = FakeGh(prs={466: p})
    row = v.collect(v.Api(tmp_path, gh), v.CORE, [(466, 100, 1)])["samples"][0]
    assert row["check_ready_proxy_to_merge_s"] == 43
    p["headRefOid"] = "c" * 40
    row = v.collect(v.Api(tmp_path, gh), v.CORE, [(466, 100, 1)])["samples"][0]
    assert row["check_ready_proxy_at"] is None
    contexts["pageInfo"]["hasNextPage"] = True
    assert v.check_ready(p) is None


@pytest.mark.parametrize("status,conclusion", [("QUEUED", None), ("COMPLETED", "FAILURE"), ("COMPLETED", "CANCELLED")])
def test_red_or_pending_nonadvisory_check_never_becomes_ready(tool, status, conclusion):
    v = tool("hz-velocity")
    p = pr()
    p["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"] = [check(status=status, conclusion=conclusion)]
    assert v.check_ready(p) is None


def test_eligibility_event_retains_first_observation_and_is_bound_to_head(tool, tmp_path):
    v = tool("hz-velocity")
    first = v.record_eligible(tmp_path, v.CORE, 466, "a" * 40, at="2026-11-02T21:30:00Z")
    assert v.record_eligible(tmp_path, v.CORE, 466, "a" * 40, at="2026-11-02T21:31:00Z") == first
    assert v.eligible_observation(tmp_path, v.CORE, pr()) == first
    changed = pr() | {"headRefOid": "c" * 40}
    assert v.eligible_observation(tmp_path, v.CORE, changed) is None
    assert v.seconds("2026-11-03T00:00:00Z", "2026-11-02T21:35:48Z") is None
    assert len((tmp_path / "eligibility.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("field,value", [("display_title", "Acceptance 0.9.15"), ("path", ".github/workflows/ci.yml"), ("head_sha", "d" * 40), ("created_at", "2026-11-02T20:00:00Z")])
def test_release_measurement_requires_versioned_acceptance_of_same_merge(tool, tmp_path, field, value):
    v = tool("hz-velocity")
    release_pr = pr(472) | {"title": "chore(release): v0.9.14"}
    acceptance = run(run_id=900) | {"path": ".github/workflows/acceptance.yml", "event": "workflow_dispatch", "display_title": "Acceptance 0.9.14", "head_sha": "b" * 40, "created_at": "2026-11-02T23:50:25Z", "updated_at": "2026-11-03T00:08:01Z", field: value}
    gh = FakeGh(prs={466: pr(), 472: release_pr}, runs={(100, 1): run(), (900, 1): acceptance})
    result = v.collect(v.Api(tmp_path, gh), v.CORE, [(466, 100, 1)], (472, 900, 1))
    assert "identity does not match" in result["release"]["missing"]
    assert "merge_to_acceptance_workflow_success_s" not in result["release"]


@pytest.mark.parametrize("text", ["1:2", "0:2:1", "1:2:0", "1:2:latest", "1:2:1/../../x"])
def test_samples_require_positive_immutable_attempt_identity(tool, text):
    v = tool("hz-velocity")
    with pytest.raises(v.argparse.ArgumentTypeError):
        v.selection(text)


@pytest.mark.parametrize("part", ["./heinzel", "../heinzel", "owner/.", "owner/.."])
def test_repository_dot_components_cannot_escape_cache(tool, tmp_path, part):
    v = tool("hz-velocity")
    api = v.Api(tmp_path, lambda *a, **kw: pytest.fail("invalid repo must not call GitHub"))
    with pytest.raises(ValueError, match="invalid"):
        api.attempt(part, 100, 1)
    assert not v.valid_repo(part)


@pytest.mark.parametrize("mutation", ["pending", "run_id", "attempt"])
def test_cached_run_payload_is_revalidated_not_just_its_envelope(tool, tmp_path, mutation):
    v = tool("hz-velocity")
    gh = FakeGh()
    api = v.Api(tmp_path, gh)
    api.attempt(v.CORE, 100, 1)
    target = tmp_path / v.CORE / "100/1/run.json"
    cached = json.loads(target.read_text())
    cached["data"][{"pending": "status", "run_id": "id", "attempt": "run_attempt"}[mutation]] = {"pending": "queued", "run_id": 101, "attempt": 2}[mutation]
    target.write_text(json.dumps(cached))
    api.attempt(v.CORE, 100, 1)
    assert api.calls == 2
    assert api.cache_hits == 0


@pytest.mark.parametrize("envelope", [[], None, "not an envelope", 42])
def test_non_object_cache_envelope_is_a_miss_and_is_replaced(tool, tmp_path, envelope):
    v = tool("hz-velocity")
    folder = tmp_path / v.CORE / "100/1"
    folder.mkdir(parents=True)
    target = folder / "run.json"
    target.write_text(json.dumps(envelope))
    api = v.Api(tmp_path, FakeGh())
    assert api.attempt(v.CORE, 100, 1)["id"] == 100
    assert api.calls == 1 and api.cache_hits == 0
    assert json.loads(target.read_text())["data"]["id"] == 100


@pytest.mark.parametrize("mutation", ["pending", "run_id", "attempt", "missing_id"])
def test_cached_job_payload_identity_and_completion_are_revalidated(tool, tmp_path, mutation):
    v = tool("hz-velocity")
    api = v.Api(tmp_path, FakeGh())
    api.attempt(v.CORE, 100, 1, jobs=True)
    target = tmp_path / v.CORE / "100/1/jobs.json"
    cached = json.loads(target.read_text())
    cached["data"]["jobs"][0][{"pending": "status", "run_id": "run_id", "attempt": "run_attempt", "missing_id": "id"}[mutation]] = {"pending": "in_progress", "run_id": 999, "attempt": 3, "missing_id": None}[mutation]
    target.write_text(json.dumps(cached))
    calls = api.calls
    assert api.attempt(v.CORE, 100, 1, jobs=True)["jobs"][0]["run_id"] == 100
    assert api.calls == calls + 1


def test_cache_file_symlink_never_overwrites_its_target(tool, tmp_path):
    v = tool("hz-velocity")
    folder = tmp_path / "cache" / v.CORE / "100/1"
    folder.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (folder / "run.json").symlink_to(victim)
    api = v.Api(tmp_path / "cache", FakeGh())
    assert api.attempt(v.CORE, 100, 1)["id"] == 100
    assert victim.read_text() == "keep"
    assert not (folder / "run.json").is_symlink()


def test_cache_directory_and_journal_symlinks_are_rejected(tool, tmp_path):
    v = tool("hz-velocity")
    cache, outside = tmp_path / "cache", tmp_path / "outside"
    cache.mkdir()
    outside.mkdir()
    (cache / "helmut-hoffer-von-ankershoffen").symlink_to(outside, target_is_directory=True)
    api = v.Api(cache, lambda *a, **kw: pytest.fail("symlink directory must be rejected before GitHub"))
    with pytest.raises(OSError):
        api.attempt(v.CORE, 100, 1)
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (cache / "eligibility.jsonl").symlink_to(victim)
    with pytest.raises(OSError):
        v.record_eligible(cache, v.CORE, 466, "a" * 40)
    assert victim.read_text() == "keep"


def test_concurrent_cache_writers_use_exclusive_unique_temporary_files(tool, tmp_path):
    v = tool("hz-velocity")
    def write(index):
        with v.directory(tmp_path, ("repo",)) as folder:
            v.write_json_at(folder, "data.json", {"writer": index})
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(20)))
    assert json.loads((tmp_path / "repo/data.json").read_text())["writer"] in range(20)
    assert list((tmp_path / "repo").iterdir()) == [tmp_path / "repo/data.json"]


def test_precreated_temp_symlink_is_not_followed(tool, tmp_path, monkeypatch):
    v = tool("hz-velocity")
    victim = tmp_path / "victim"
    victim.write_text("keep")
    monkeypatch.setattr(v.secrets, "token_hex", lambda _: "fixed")
    (tmp_path / ".velocity-fixed").symlink_to(victim)
    with v.directory(tmp_path) as folder, pytest.raises(FileExistsError):
        v.write_json_at(folder, "run.json", {})
    assert victim.read_text() == "keep"


def test_missing_pr_association_is_separate_proxy_not_primary_cohort(tool, tmp_path):
    v = tool("hz-velocity")
    gh = FakeGh(runs={(100, 1): run() | {"pull_requests": []}})
    result = v.collect(v.Api(tmp_path, gh), v.CORE, [(466, 100, 1)])
    assert result["samples"][0]["pr_association_verified"] is False
    assert result["summary"]["primary_completed_successful_ci_samples"] == 0
    assert result["summary"]["association_unavailable_proxy_samples"] == 1
    assert "unavailable" in result["samples"][0]["binding"]


@pytest.mark.parametrize("mutation", ["other_pr", "fork", "wrong_base"])
def test_foreign_pr_or_repository_cannot_enter_proxy_or_primary_cohort(tool, tmp_path, mutation):
    v = tool("hz-velocity")
    metadata = run()
    if mutation == "other_pr":
        metadata["pull_requests"][0]["number"] = 999
    elif mutation == "wrong_base":
        metadata["pull_requests"][0]["base"]["repo"] = {"id": 2, "full_name": "other/repo"}
    else:
        metadata["pull_requests"] = []
        metadata["head_repository"] = {"full_name": "other/heinzel"}
    result = v.collect(v.Api(tmp_path, FakeGh(runs={(100, 1): metadata})), v.CORE, [(466, 100, 1)])
    assert "missing" in result["samples"][0]
    assert result["summary"]["primary_completed_successful_ci_samples"] == result["summary"]["association_unavailable_proxy_samples"] == 0


@pytest.mark.parametrize("samples", [[(466, 100, 1), (466, 100, 1)], [(466, 100, 1), (468, 100, 1)]])
def test_duplicate_run_attempt_cannot_skew_median_or_get_assigned_to_two_prs(tool, tmp_path, samples):
    v = tool("hz-velocity")
    api = v.Api(tmp_path, lambda *a, **kw: pytest.fail("duplicates must fail before GitHub"))
    with pytest.raises(ValueError, match="duplicate"):
        v.collect(api, v.CORE, samples)


def test_second_attempt_uses_attempt_start_and_labels_original_run_lifetime(tool, tmp_path):
    v = tool("hz-velocity")
    metadata = run(attempt=2) | {"created_at": "2026-11-01T21:02:42Z", "run_started_at": "2026-11-02T21:26:43Z"}
    result = v.collect(v.Api(tmp_path, FakeGh(runs={(100, 2): metadata})), v.CORE, [(466, 100, 2)])
    row = result["samples"][0]
    assert row["attempt_elapsed_proxy_s"] == 60
    assert row["run_lifetime_proxy_s"] == 87901
    assert result["summary"]["primary_median_attempt_elapsed_proxy_s"] == 60
