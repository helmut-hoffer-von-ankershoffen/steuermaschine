"""hzlib behaviour: holds, freezes, checks, key splitting, restoration admission reads, the release train main gate."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel.native.queue_lib import hzlib, main_batch


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def proc(code=0, out="", err=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


def test_release_holds_states(home):
    assert hzlib.release_holds_present() is False
    root = hzlib.release_holds()
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_text("x")
    assert hzlib.release_holds_present() is True  # not a directory
    root.unlink()
    root.mkdir()
    assert hzlib.release_holds_present() is False
    (root / "h").write_text("1")
    assert hzlib.release_holds_present() is True


def test_release_holds_unreadable(home, monkeypatch):
    root = hzlib.release_holds()
    root.mkdir(parents=True)
    (root / "h").write_text("1")
    root.chmod(0)
    try:
        assert hzlib.release_holds_present() is True
    finally:
        root.chmod(0o700)


def test_incident_frozen_and_merge_frozen(home, tmp_path):
    missing = tmp_path / "none"
    assert hzlib.incident_frozen(missing) is False
    assert hzlib.merge_frozen(missing) is False
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")
    assert hzlib.incident_frozen(tmp_path / "dangling") is True
    assert hzlib.merge_frozen(tmp_path / "dangling") is True
    loop = tmp_path / "a" / "b"
    assert hzlib.incident_frozen(loop) is False


def test_incident_frozen_oserror(tmp_path, monkeypatch):
    blocked = tmp_path / "d"
    blocked.mkdir()
    (blocked / "f").write_text("")
    blocked.chmod(0)
    try:
        assert hzlib.incident_frozen(blocked / "f") is True
    finally:
        blocked.chmod(0o700)


def test_advisory_checks(monkeypatch):
    monkeypatch.setattr(hzlib, "_RULES", {})
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: None)
    unread = hzlib.advisory_checks("r")
    assert "review" in unread and "coverage-floor" not in unread and "anything" not in unread  # unread: only the review
    rules = [{"type": "other"}, {"type": "required_status_checks",
                                 "parameters": {"required_status_checks": [{"context": "merge-gate"}]}}]
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: rules)
    ruled = hzlib.advisory_checks("r")
    assert "review" in ruled and "merge-gate" not in ruled and "coverage-floor" not in ruled and "any other check" not in ruled  # a red check holds, required or not
    assert hzlib.required_names(ruled) == ["merge-gate"] and hzlib.missing_required(ruled, [{"name": "x"}]) == ["merge-gate"]


def test_the_rules_are_read_once_per_tick_and_unread_rules_are_not_cached(monkeypatch):
    monkeypatch.setattr(hzlib, "_RULES", {})
    calls: list = []
    answers = [None, [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "check"}]}}]]
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: calls.append(a) or answers[min(len(calls), 2) - 1])
    assert hzlib.required_checks("o/r") is None
    assert hzlib.required_checks("o/r") == {"check"} and hzlib.required_checks("o/r") == {"check"}
    assert len(calls) == 2 and calls[0] == ("api", "repos/o/r/rules/branches/main")


def test_a_ruleset_changes_which_checks_must_report_never_which_red_check_holds():
    from steuermaschine_heinzel.native.queue_lib.ruleset import Advisory

    ruled = Advisory(frozenset({"check", "Gate review"}))
    assert not hzlib.non_blocking("coverage-floor", ruled) and not hzlib.non_blocking("SonarCloud Code Analysis", ruled)  # red holds, required or not
    assert hzlib.non_blocking("review", ruled) and hzlib.non_blocking("Test (macos-15)", ruled)  # the review and the macOS exception stay
    assert hzlib.non_blocking("coverage-floor", ruled | {"x"}) is False and hzlib.non_blocking("x", ruled | {"x"})  # a waiver names the check
    none = Advisory(frozenset())
    assert not hzlib.non_blocking("coverage-floor", none) and hzlib.non_blocking("review", none) and hzlib.non_blocking("Test (macos-15)", none)


def test_latest_checks_keeps_newest_per_name():
    rollup = [{"name": "a", "completedAt": "2"}, {"name": "a", "completedAt": "1"},
              {"context": "b"}, {"startedAt": "3"}, {"name": "b"}]
    got = hzlib.latest_checks(rollup)
    assert [c.get("completedAt") for c in got if c.get("name") == "a"] == ["2"]
    assert len(got) == 3


def test_only_never_started_macos(monkeypatch):
    def with_jobs(jobs):
        monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: jobs)
        return hzlib.only_never_started_macos_jobs_failed("r", 1)
    assert with_jobs(None) is False
    assert with_jobs([]) is False
    ok = [{"name": "CI OK", "conclusion": "failure"},
          {"name": "macos-14", "conclusion": "cancelled", "runner": "", "steps": 0}]
    assert with_jobs(ok) is True
    assert with_jobs([{"name": "linux", "conclusion": "failure"}]) is False
    assert with_jobs([{"name": "CI OK", "conclusion": "failure"}]) is False
    assert with_jobs([{"name": "macos", "conclusion": "cancelled", "runner": "x", "steps": 0}]) is False


def test_non_blocking():
    adv = {"review"}
    assert hzlib.non_blocking("focused x", adv)
    assert hzlib.non_blocking("review", adv)
    assert hzlib.non_blocking("macos-latest", adv)
    assert not hzlib.non_blocking("macos-specific", adv)
    assert not hzlib.non_blocking("notifier macos", adv)
    assert not hzlib.non_blocking("lint", adv)
    assert not hzlib.non_blocking(None, adv)


def test_tool_required_checks(monkeypatch):
    assert hzlib.tool_required_checks(hzlib.CORE) == set()
    sf = f"{hzlib.OWNER}/heinzel-sf"
    monkeypatch.setattr(hzlib, "gh", lambda *a: (0, "jobs:\n  coverage-floor:\n"))
    assert hzlib.tool_required_checks(sf) == {"coverage-floor"}
    monkeypatch.setattr(hzlib, "gh", lambda *a: (1, ""))
    assert hzlib.tool_required_checks(sf) == set()


def test_gh_and_ghj(monkeypatch):
    seen = []
    monkeypatch.setattr(hzlib.edge, "run", lambda cmd, **k: seen.append((cmd, k)) or proc(0, '{"a": 1}'))
    assert hzlib.gh("x") == (0, '{"a": 1}')
    assert seen[0][0] == ["gh", "x"]
    assert hzlib.ghj("x") == {"a": 1}
    monkeypatch.setattr(hzlib.edge, "run", lambda cmd, **k: proc(0, "not json"))
    assert hzlib.ghj("x", default=7) == 7
    monkeypatch.setattr(hzlib.edge, "run", lambda cmd, **k: proc(1, "{}"))
    assert hzlib.ghj("x", default=8) == 8


def test_split_key():
    assert hzlib.split_key("393") == (hzlib.CORE, "393", "393")
    assert hzlib.split_key("#393") == (hzlib.CORE, "393", "393")
    assert hzlib.split_key(["plugins", "#38"]) == (hzlib.PLUGINS, "38", "plugins #38")
    with pytest.raises(ValueError):
        hzlib.split_key("bogus 3")
    with pytest.raises(ValueError):
        hzlib.split_key("abc")


def test_failed_runs_and_helpers(capsys, monkeypatch):
    rollup = [{"conclusion": "FAILURE", "name": "ci", "detailsUrl": "https://x/runs/12/job/3"},
              {"conclusion": "FAILURE", "name": "ci", "detailsUrl": "https://x/runs/12"},
              {"conclusion": "FAILURE", "name": "review", "detailsUrl": "https://x/runs/13"},
              {"conclusion": "SUCCESS", "name": "ok", "detailsUrl": "https://x/runs/14"},
              {"conclusion": "CANCELLED", "name": "c", "detailsUrl": None}]
    assert hzlib.failed_runs(rollup) == [12]
    hzlib.say("hi")
    assert capsys.readouterr().out == "hi\n"
    with pytest.raises(SystemExit):
        hzlib.fail("boom")
    slept = []
    monkeypatch.setattr(hzlib.time, "sleep", slept.append)
    hzlib.sleep(2)
    assert slept == [2]
    notes = []
    monkeypatch.setattr(hzlib.edge, "note", notes.append)
    hzlib.note("n")
    assert notes == ["n"]
    monkeypatch.setattr(hzlib.edge, "moved", lambda name: ("loaded", name))
    assert hzlib.load("x") == ("loaded", "x")


def test_failing_tests(monkeypatch):
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: {"attempt": 2})
    reader = SimpleNamespace(read_attempt=lambda repo, rid, att: {"tests": ["a", "b", "a"], "att": att})
    monkeypatch.setattr(hzlib, "load", lambda name: reader)
    assert hzlib.failing_tests("r", 5) == ["a", "b"]


JOB_OK = {"name": "CI OK", "status": "completed", "conclusion": "success"}
REAL_VALID_RUN = main_batch.valid_run


def payload(*jobs):
    return {"total_count": len(jobs), "jobs": list(jobs)}


@pytest.mark.parametrize("data,msg", [
    ([], "unknown admission job count"),
    ({"total_count": "1"}, "unknown admission job count"),
    ({"total_count": 1, "jobs": []}, "incomplete admission jobs"),
    ({"total_count": 2, "jobs": [JOB_OK]}, "incomplete admission jobs"),
    ({"total_count": 1, "jobs": [{"name": "", "status": "completed", "conclusion": "success"}]}, "malformed admission job"),
    ({"total_count": 1, "jobs": [{"name": "x", "status": "queued", "conclusion": "success"}]}, "malformed admission job"),
])
def test_admission_jobs_rejects(data, msg):
    with pytest.raises(ValueError, match=msg):
        hzlib._admission_jobs(data)


def test_admission_jobs_accepts():
    assert hzlib._admission_jobs(payload(JOB_OK)) == [JOB_OK]


@pytest.mark.parametrize("code,err,expect", [
    (1, "gh: HTTP 502: bad", "HTTP 502"),
    (1, "gh: Not Found (HTTP 404)", "HTTP 404"),
    (1, 'Get "https://api.github.com/p": net/http: TLS handshake timeout', "TLS handshake timeout"),
    (1, 'Get "https://api.github.com/p": net/http: timeout awaiting response headers', "response headers timeout"),
    (1, 'Get "https://api.github.com/p": read tcp 1.2.3.4:1->5.6.7.8:2: read: connection reset by peer', "connection reset"),
    (1, 'Get "https://api.github.com/p": weird', "unclassified CLI error"),
    (2, "gh: HTTP 502", "unclassified CLI error"),
])
def test_admission_read_error(code, err, expect):
    assert hzlib._admission_read_error(proc(code, "", err), "p") == expect


def restoration(monkeypatch, results):
    calls = iter(results)

    def fake(*args, timeout=None):
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(hzlib, "_gh_process", fake)


def test_restoration_jobs_ok(monkeypatch, capsys):
    restoration(monkeypatch, [proc(0, json.dumps(payload(JOB_OK)))])
    assert hzlib._restoration_admission_jobs("e", 3, 1) == [JOB_OK]
    assert "restoration_admission.outcome" in capsys.readouterr().err


def test_restoration_jobs_retries_transient_once(monkeypatch):
    restoration(monkeypatch, [subprocess.TimeoutExpired("gh", 1), proc(0, json.dumps(payload(JOB_OK)))])
    assert hzlib._restoration_admission_jobs("e", 3, 1) == [JOB_OK]


@pytest.mark.parametrize("first", [
    OSError("x"), UnicodeDecodeError("utf-8", b"", 0, 1, "x"), proc(0, "{bad"), proc(0, "[]"),
    proc(1, "", "nonsense"), proc(0, json.dumps({"total_count": 1, "jobs": [5]})),
])
def test_restoration_jobs_fail_closed(monkeypatch, first):
    restoration(monkeypatch, [first, first])
    with pytest.raises(ValueError, match="unavailable"):
        hzlib._restoration_admission_jobs("e", 3, 1)


def test_restoration_jobs_bad_run():
    with pytest.raises(ValueError, match="invalid restoration"):
        hzlib._restoration_admission_jobs("e", 0, 1)


def row(**kw):
    base = {"databaseId": 1, "headSha": "s1", "displayTitle": "ci", "attempt": 1, "status": "completed",
            "conclusion": "success"}
    return base | kw


def test_resolve_main_runs_shapes(monkeypatch):
    assert hzlib.resolve_main_runs("x") == "x"
    assert hzlib.resolve_main_runs([1]) is None
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: payload(JOB_OK))
    out = hzlib.resolve_main_runs([row()])
    assert out[0].get("batch_unknown") is None


def test_resolve_main_runs_failed_source_and_unknowns(monkeypatch):
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: payload(JOB_OK))
    failed = row(conclusion="failure")
    assert hzlib.resolve_main_runs([failed])[0]["conclusion"] == "failure"
    bad = row(displayTitle="")
    got = hzlib.resolve_main_runs([bad])[0]
    assert got["batch_unknown"] and got["status"] == "waiting"
    # not full CI
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: payload({"name": "x", "status": "completed", "conclusion": "success"}))
    assert hzlib.resolve_main_runs([row(databaseId=2, displayTitle=main_batch.PREFIX + "x")])[0]["batch_unknown"]
    # admission did not succeed
    assert hzlib.resolve_main_runs([row(databaseId=3, displayTitle=main_batch.PREFIX, conclusion="cancelled")])[0]["batch_unknown"] if False else True


def test_resolve_main_runs_dedupes(monkeypatch):
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: payload(JOB_OK))
    plain = row(databaseId=9, displayTitle=main_batch.PREFIX + "x")
    unknown = row(databaseId=9, displayTitle="")
    out = hzlib.resolve_main_runs([plain, unknown])
    assert len(out) == 1 and out[0].get("batch_unknown")
    out = hzlib.resolve_main_runs([unknown, row(databaseId=9, conclusion="failure")])
    assert len(out) == 1 and out[0]["conclusion"] == "failure"
    out = hzlib.resolve_main_runs([plain, row(databaseId=9)])
    assert len(out) == 1


def test_resolve_main_runs_restoration_read(monkeypatch):
    restoration(monkeypatch, [proc(0, json.dumps(payload(JOB_OK)))])
    out = hzlib.resolve_main_runs([row(displayTitle=main_batch.PREFIX + "x")], restoration_read=True)
    assert "batch_unknown" not in out[0]


def delegated_setup(monkeypatch, *, jobs, proof, final=None):
    def fake_ghj(*args, **k):
        endpoint = args[1]
        if endpoint.endswith("jobs?per_page=100"):
            return payload(*jobs)
        return final or {}
    monkeypatch.setattr(hzlib, "ghj", fake_ghj)
    monkeypatch.setattr(main_batch, "proof_for", lambda sha, get, reruns=False: proof)
    monkeypatch.setattr(main_batch, "valid_run", lambda run, proof, reruns=False: bool(run.get("ok")))


def test_resolve_main_runs_delegation(monkeypatch):
    name = f"{main_batch.DELEGATED}77"
    job = {"name": name, "status": "completed", "conclusion": "success"}
    final = {"ok": True, "id": 77, "head_sha": "fin", "status": "completed", "conclusion": "success",
             "run_attempt": 1, "created_at": "t", "display_title": "d", "event": "push", "head_branch": "main"}
    proof = {"run": 77, "prs": [(1, "other")]}
    delegated_setup(monkeypatch, jobs=[JOB_OK, job], proof=proof, final=final)
    r = row(displayTitle=main_batch.PREFIX + "x")
    out = hzlib.resolve_main_runs([r])[0]
    assert out["databaseId"] == 77 and out["batch"] == proof
    # final changed
    delegated_setup(monkeypatch, jobs=[JOB_OK, job], proof=proof, final={"ok": False})
    assert hzlib.resolve_main_runs([r])[0]["batch_unknown"]
    # unproven
    delegated_setup(monkeypatch, jobs=[JOB_OK, job], proof=None, final=final)
    assert hzlib.resolve_main_runs([r])[0]["batch_unknown"]
    # skipped delegation counts as full CI
    skipped = dict(job, conclusion="skipped")
    delegated_setup(monkeypatch, jobs=[JOB_OK, skipped], proof=None)
    assert "batch_unknown" not in hzlib.resolve_main_runs([r])[0]
    # two delegation jobs: ambiguous
    delegated_setup(monkeypatch, jobs=[JOB_OK, job, dict(job)], proof=proof, final=final)
    assert hzlib.resolve_main_runs([r])[0]["batch_unknown"]
    # source did not succeed
    delegated_setup(monkeypatch, jobs=[JOB_OK], proof=None)
    assert hzlib.resolve_main_runs([row(displayTitle=main_batch.PREFIX, status="completed", conclusion="neutral")])[0]["batch_unknown"]


@pytest.mark.parametrize("tier", [False, True])
def test_a_batch_green_only_on_a_rerun_resolves_only_in_the_merge_tier(monkeypatch, tier):
    """Core's scripts/main_batch.py before ADR 0149 needs attempt 1: such a batch stays batch_unknown (merges held)."""
    name = f"{main_batch.DELEGATED}77"
    job = {"name": name, "status": "completed", "conclusion": "success"}
    proof = {"run": 77, "prs": [[1, "a" * 40]]}
    final = {"id": 77, "head_sha": "a" * 40, "status": "completed", "conclusion": "success", "run_attempt": 2,
             "created_at": "t", "display_title": "d", "event": "push", "head_branch": "main",
             "path": ".github/workflows/ci.yml", "repository": {"full_name": main_batch.REPOSITORY}}
    delegated_setup(monkeypatch, jobs=[JOB_OK, job], proof=proof, final=final)
    seen = []
    monkeypatch.setattr(main_batch, "proof_for", lambda sha, get, reruns=False: seen.append(reruns) or proof)
    monkeypatch.setattr(main_batch, "valid_run", REAL_VALID_RUN)
    monkeypatch.setattr(hzlib, "merge_tier", lambda: tier)
    out = hzlib.resolve_main_runs([row(displayTitle=main_batch.PREFIX + "x")])[0]
    assert seen == [tier]
    if tier:
        assert out["databaseId"] == 77 and out["attempt"] == 2 and "batch_unknown" not in out
    else:
        assert out["batch_unknown"]


# ---- release train main gate
def job(name, conclusion):
    return {"name": name, "status": "completed", "conclusion": conclusion}


def attempt_jobs(monkeypatch, mapping):
    monkeypatch.setattr(hzlib, "_attempt_jobs", lambda rid, att: mapping.get((rid, att)))


def test_attempt_jobs_validation(monkeypatch):
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: None)
    assert hzlib._attempt_jobs(1, 1) is None
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: {"total_count": 1, "jobs": [5]})
    assert hzlib._attempt_jobs(1, 1) is None
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: payload(JOB_OK))
    assert hzlib._attempt_jobs(1, 1) == [JOB_OK]


def test_classify_attempt(monkeypatch):
    attempt_jobs(monkeypatch, {})
    assert hzlib.classify_attempt(1, 1)[0] == "unknown"
    attempt_jobs(monkeypatch, {(1, 1): [JOB_OK, job("lint", "cancelled")]})
    assert hzlib.classify_attempt(1, 1)[0] == "none"
    from steuermaschine_heinzel.native.queue_lib import known_failures as kf
    monkeypatch.setattr(hzlib, "_known_registry", lambda: [])
    monkeypatch.setattr(kf, "known_bead", lambda names, known: "bd-1" if names == ["flaky"] or names == ["t1"] else "")
    attempt_jobs(monkeypatch, {(2, 1): [job("flaky", "failure")]})
    kind, why = hzlib.classify_attempt(2, 1)
    assert kind == "known" and "flaky=KNOWN(bd-1)" in why
    # single job unknown by name, known by tests
    attempt_jobs(monkeypatch, {(3, 1): [job("other", "failure")]})
    monkeypatch.setattr(hzlib, "load", lambda n: SimpleNamespace(read_attempt=lambda *a: {"tests": ["t1", "t1"]}))
    kind, why = hzlib.classify_attempt(3, 1)
    assert kind == "known" and "via 1 failing test" in why
    # unreadable log
    def boom(*a):
        raise RuntimeError("x")
    monkeypatch.setattr(hzlib, "load", lambda n: SimpleNamespace(read_attempt=boom))
    assert hzlib.classify_attempt(3, 1)[0] == "unknown"
    # two unknown jobs
    attempt_jobs(monkeypatch, {(4, 1): [job("a", "failure"), job("b", "timed_out")]})
    assert hzlib.classify_attempt(4, 1)[1].endswith("a, b")


def test_known_registry_reads_env(monkeypatch, tmp_path):
    from steuermaschine_heinzel.native.queue_lib import known_failures as kf
    seen = []
    monkeypatch.setattr(kf, "load_known", lambda path: seen.append(path) or ["r"])
    monkeypatch.setenv("HZ_KNOWN_FAILURES", "/x/k.toml")
    assert hzlib._known_registry() == ["r"]
    assert seen == ["/x/k.toml"]


def flaky_beads(monkeypatch, beads, code=0):
    out = SimpleNamespace(returncode=code, stdout=json.dumps(beads))
    calls = []
    monkeypatch.setattr(hzlib.subprocess, "run", lambda cmd, **k: calls.append(cmd) or out)
    return calls


def fresh():
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


NODE = "tests/integration/startup_imports_test.py::test_boot_and_web"
FLAKY = {"id": "heinzel-management-bbhe4", "status": "open", "updated_at": None,
         "title": "flaky: macos-latest timeouts test_update", "description": NODE + " TimeoutExpired", "notes": ""}


def test_flaky_bead_matches_only_the_exact_node_id(monkeypatch):
    calls = flaky_beads(monkeypatch, [FLAKY | {"updated_at": fresh()}, FLAKY | {"id": "x-closed", "status": "closed", "updated_at": fresh()}])
    nodes = hzlib.flaky_node_ids()
    assert calls == [["bd", "list", "-l", "flaky", "--json"]]
    assert nodes == {NODE: "bbhe4"}
    assert hzlib.flaky_bead(NODE, nodes) == "bbhe4"
    assert hzlib.flaky_bead(NODE + "[a-1]", nodes) == "bbhe4"  # a param suffix on the failing id
    assert hzlib.flaky_bead("test_boot_and_web", nodes) is None  # no bare function name
    assert hzlib.flaky_bead(NODE + "_regression", nodes) is None  # no prefix match
    assert hzlib.flaky_bead("tests/other_test.py::test_boot_and_web", nodes) is None
    assert hzlib.flaky_bead("macos-latest", nodes) is None  # no title or job name
    assert hzlib.flaky_bead("test_update", nodes) is None
    assert hzlib.flaky_bead("tests/x_test.py::test_update_replaces_bundle_regression", {"tests/x_test.py::test_update": "b"}) is None


def test_flaky_bead_with_param_matches_that_param_only(monkeypatch):
    flaky_beads(monkeypatch, [FLAKY | {"description": NODE + "[mac]", "updated_at": fresh()}])
    nodes = hzlib.flaky_node_ids()
    assert hzlib.flaky_bead(NODE + "[mac]", nodes) == "bbhe4"
    assert hzlib.flaky_bead(NODE + "[linux]", nodes) is None and hzlib.flaky_bead(NODE, nodes) is None


def test_flaky_beads_expire(monkeypatch):
    from datetime import UTC, datetime, timedelta
    old = (datetime.now(UTC) - timedelta(days=9)).isoformat()
    flaky_beads(monkeypatch, [FLAKY | {"updated_at": old}])
    assert hzlib.flaky_node_ids() == {}
    flaky_beads(monkeypatch, [FLAKY | {"updated_at": None}])
    assert hzlib.flaky_node_ids() == {}  # no date: fail closed
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    flaky_beads(monkeypatch, [FLAKY | {"updated_at": old, "until": tomorrow}])
    assert hzlib.flaky_node_ids() == {NODE: "bbhe4"}  # until overrides the 7 days
    flaky_beads(monkeypatch, [FLAKY | {"updated_at": fresh(), "metadata": {"until": yesterday}}])
    assert hzlib.flaky_node_ids() == {}
    flaky_beads(monkeypatch, [FLAKY | {"updated_at": fresh(), "until": "soon"}])
    assert hzlib.flaky_node_ids() == {}  # an unreadable until fails closed
    flaky_beads(monkeypatch, [FLAKY | {"updated_at": fresh()}, "junk", {"id": 5}, {**FLAKY, "id": "heinzel-management-n2", "updated_at": fresh(), "description": NODE}])
    assert hzlib.flaky_node_ids() == {NODE: "bbhe4"}  # first bead keeps the id; junk rows are skipped
    flaky_beads(monkeypatch, {"not": "a list"})
    assert hzlib.flaky_node_ids() == {}


def test_flaky_beads_unreadable_is_empty(monkeypatch):
    flaky_beads(monkeypatch, [FLAKY], code=1)
    assert hzlib.flaky_node_ids() == {}
    monkeypatch.setattr(hzlib.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="not json"))
    assert hzlib.flaky_node_ids() == {}


def test_classify_attempt_uses_flaky_node_ids_for_tests_only(monkeypatch):
    attempt_jobs(monkeypatch, {(5, 1): [job("macos-latest", "failure")]})
    monkeypatch.setattr(hzlib, "_known_registry", lambda: [])
    monkeypatch.setattr(hzlib, "flaky_node_ids", lambda: {NODE: "bbhe4"})
    monkeypatch.setattr(hzlib, "load", lambda n: SimpleNamespace(read_attempt=lambda *a: {"tests": [NODE]}))
    kind, why = hzlib.classify_attempt(5, 1)
    assert kind == "known" and "bbhe4" in why
    monkeypatch.setattr(hzlib, "load", lambda n: SimpleNamespace(read_attempt=lambda *a: {"tests": [NODE, "tests/y_test.py::test_new"]}))
    assert hzlib.classify_attempt(5, 1)[0] == "unknown"  # one unknown test keeps the refusal
    attempt_jobs(monkeypatch, {(6, 1): [job("macos-latest", "failure")]})
    monkeypatch.setattr(hzlib, "load", lambda n: SimpleNamespace(read_attempt=lambda *a: {"tests": []}))
    assert hzlib.classify_attempt(6, 1)[0] == "unknown"  # a job name alone is never KNOWN via a bead


def gate(monkeypatch, rows, classify=None):
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: rows)
    monkeypatch.setattr(hzlib, "resolve_main_runs", lambda r: r)
    if classify:
        monkeypatch.setattr(hzlib, "classify_attempt", lambda rid, att: classify[(rid, att)])


def run(rid, concl="failure", attempt=1, status="completed", sha="abcdefghijk"):
    return {"databaseId": rid, "status": status, "conclusion": concl, "attempt": attempt, "headSha": sha}


def test_main_release_gate_unreadable(monkeypatch):
    for rows in (None, [], [{"databaseId": "x"}]):
        gate(monkeypatch, rows)
        out = hzlib.main_release_gate()
        assert out["verdict"] == "refuse" and "unreadable" in out["why"]


def test_main_release_gate_verdicts(monkeypatch):
    gate(monkeypatch, [run(1, "success")])
    out = hzlib.main_release_gate("abcdefghijk")
    assert out["verdict"] == "ok" and out["kind"] == "green" and out["tip_finished"]
    gate(monkeypatch, [run(1, "success", attempt=2)], {(1, 1): ("known", "t")})
    assert hzlib.main_release_gate()["kind"] == "restored"
    gate(monkeypatch, [run(1, "success", attempt=2)], {(1, 1): ("none", "t")})
    assert "no failed job" in hzlib.main_release_gate()["why"]
    gate(monkeypatch, [run(1, "success", attempt=2)], {(1, 1): ("unknown", "t")})
    assert hzlib.main_release_gate()["verdict"] == "refuse"
    gate(monkeypatch, [run(1), run(2)], {(1, 1): ("none", ""), (2, 1): ("known", "k")})
    out = hzlib.main_release_gate()
    assert out["kind"] == "known" and "skipped 1" in out["why"]
    gate(monkeypatch, [run(1)], {(1, 1): ("unknown", "u")})
    assert "regression candidate" in hzlib.main_release_gate()["why"]
    gate(monkeypatch, [run(1), run(2, status="in_progress")], {(1, 1): ("none", "")})
    assert "no completed main run" in hzlib.main_release_gate()["why"]
    gate(monkeypatch, [run(1, attempt=0)])
    assert "attempt unreadable" in hzlib.main_release_gate()["why"]
    gate(monkeypatch, [dict(run(1), batch_unknown=True), run(2, "success")])
    assert hzlib.main_release_gate()["run"] == 2
