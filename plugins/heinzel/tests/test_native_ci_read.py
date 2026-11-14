"""ci why and ci faillog, native. Fixtures in fixtures/ci: recorded 2026-11-12 23:08 with
`.venv/bin/python plugins/heinzel/tests/record_ci_fixtures.py 784 43186264208` and `... 585 42398276348` (the real run,
its failed-step log cut down to the error places, the check rollup of the pull request, and what the old cause and
log tools print for exactly these answers). No test here starts a process or reads the network."""

import json
import re
from pathlib import Path

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github import logs
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel.native import ci_read
from steuermaschine_heinzel.native.ci_lib import cause as c
from steuermaschine_heinzel.native.ci_lib import faillog
from steuermaschine_heinzel.native.queue_lib import known_failures
from steuermaschine_heinzel.plugin import plugin

FIXTURES = Path(__file__).parent / "fixtures" / "ci"
OLD = re.compile(r"(?<![\w./-])hz-[a-z]")
COVERAGE, WEB = 43186264208, 42398276348
HEAD = "4565406a0e0b4a6c4f0e7f1f2f1c7a1b2c3d4e5f"
ROLLUP = "pr view {pr} -R helmut-hoffer-von-ankershoffen/heinzel --json statusCheckRollup"
GREEN_MAIN = [{"databaseId": 1, "headSha": "f" * 40, "attempt": 1, "status": "completed", "conclusion": "success"}]


def text(name: str) -> str:
    return (FIXTURES / name).read_text()


def recorded(pr: int | None = None, run: int | None = None, check_runs: str = "") -> FakeGh:
    fake = FakeGh()
    for number in (COVERAGE, WEB):
        fake.on(f"run view {number} -R helmut-hoffer-von-ankershoffen/heinzel --json", text(f"run-{number}.json"))
        fake.on(f"run view {number} -R helmut-hoffer-von-ankershoffen/heinzel --log-failed", text(f"run-{number}.log"))
    if pr:
        fake.on(ROLLUP.format(pr=pr), text(f"pr-{pr}-rollup.json"))
        fake.on(f"pr view {pr} -R helmut-hoffer-von-ankershoffen/heinzel --json state", {"state": "OPEN", "mergeStateStatus": "BLOCKED", "headRefOid": HEAD, "title": "fix: a title"})
        fake.on("check-runs", check_runs)
        fake.on("run list", GREEN_MAIN)
    return fake


def runtime_for(tmp_path, commands, known: str = "", **settings):
    registry = tmp_path / "known-failures.toml"
    registry.write_text(known)
    table = {"legacy_state_dir": str(tmp_path / "state"), "ci_known_failures_file": str(registry), **settings}
    stack = stack_of(plugin)
    validated, _ = stack.validate({"heinzel": table}, Path("."))
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, validated, _providers=fake_providers(commands=commands))


KNOWN = '[[known]]\nmatch = "End-to-end web-2 (ubuntu"\nbead = "9kk"\nadded = 2026-11-08\nuntil = 2099-01-01\nnote = "job fails on the test above"\n'


# ------------------------------------------------------------------ ci why, through the whole tree
def test_why_for_a_run_names_the_hosted_only_gate_the_old_tool_called_unknown(tmp_path):
    fake = recorded()
    out = invoke(runtime_for(tmp_path, fake), ["ci", "why", str(COVERAGE)])
    assert out.exit_code == 0, out
    lines = out.stdout.splitlines()
    assert lines[0] == "CAUSE    run 43186264208: scoped coverage under the floor (a hosted-only gate) -> Focused tests (python)=UNKNOWN"
    assert lines[1] == "RUN      43186264208 CI attempt 1: completed failure"
    assert "  UNKNOWN   Focused tests (python)  step: Run the exact reviewed selection with fresh evidence" in lines
    assert "  AGGREGATE CI OK  step: Fail if any required job failed" in lines
    assert "      scripts.release_candidate.CandidateError: PR scoped coverage below the 90% report threshold" in lines
    assert lines[-2] == "NEXT     sm ci faillog 43186264208" and lines[-1].startswith("TRAP     Read every failure before the head changes")
    assert not fake.writes() and not OLD.search(out.stdout) and "^[" not in out.stdout
    # the old cause line for the same answers named both jobs UNKNOWN: the log does not name its steps
    assert text(f"run-{COVERAGE}-old-cause.txt").strip() == "run 43186264208: not all download, no rerun -> Focused tests (python)=UNKNOWN, CI OK=UNKNOWN"


def test_why_for_a_pull_request_reads_its_failing_run_and_lists_the_check_another_suite_posted(tmp_path):
    out = invoke(runtime_for(tmp_path, recorded(pr=784)), ["--format", "json", "ci", "why", "784"])
    assert out.exit_code == 0, out
    report = ci_read.CiWhy.model_validate_json(out.stdout)
    assert report.kind == "pr" and report.pull_request and report.pull_request.merge_state == "BLOCKED" and report.read_only
    assert [r.run for r in report.runs] == [COVERAGE] and sorted(set(report.runs[0].checks)) == ["CI OK", "Focused tests (python)"]
    assert [(j.name, j.cause) for j in report.runs[0].jobs] == [("Focused tests (python)", "UNKNOWN"), ("CI OK", "AGGREGATE")]
    assert report.gates == ["coverage-floor: FAILURE, posted by the check suite of CodeQL, not a job of the CI run; the coverage floor of the pull request"]
    assert report.next == f"sm ci faillog {COVERAGE}" and report.cause.startswith("PR 784: scoped coverage under the floor")
    text_out = invoke(runtime_for(tmp_path, recorded(pr=784)), ["ci", "why", "784"]).stdout.splitlines()
    assert text_out[1].startswith("         PR 784 OPEN 4565406 github=BLOCKED") and any(line.startswith("GATE     coverage-floor: FAILURE") for line in text_out)


def test_a_known_failure_gets_one_rerun_of_that_job(tmp_path):
    out = invoke(runtime_for(tmp_path, recorded(pr=585), KNOWN), ["ci", "why", "585"])
    lines = out.stdout.splitlines()
    assert lines[0] == "CAUSE    PR 585: KNOWN failure (bead 9kk) -> End-to-end web-2 (ubuntu-24.04)=TEST"
    assert "  TEST      End-to-end web-2 (ubuntu-24.04)  [KNOWN 9kk]  step: Run make test-e2e SUITE=web-2" in lines
    assert any(line.startswith("  TESTS    1 failing: tests/e2e/scheduled_connected_test.py::test_mounted_scheduled_connected_read[True-webkit]") for line in lines)
    assert lines[-2] == 'NEXT     sm ci one rerun 585 --job "End-to-end web-2 (ubuntu-24.04)" --wait' and lines[-1].startswith("TRAP     KNOWN is a reviewed registry entry")


def test_without_a_registry_entry_the_same_failure_is_a_test_failure_to_read(tmp_path):
    out = invoke(runtime_for(tmp_path, recorded(pr=585)), ["ci", "why", "585"])
    lines = out.stdout.splitlines()
    assert lines[0] == "CAUSE    PR 585: 1 failed job(s) (TEST) -> End-to-end web-2 (ubuntu-24.04)=TEST" and lines[-2] == f"NEXT     sm ci faillog {WEB}"


# ------------------------------------------------------------------ the decision
def job(name="Lint", cause="TEST", source="", known=""):
    return ci_read.FailedJob(name=name, cause=cause, source=source, known=known)


def run_of(*jobs, **over):
    return ci_read.RunFailure(**{"run": 900000, "status": "completed", "conclusion": "failure", "jobs": list(jobs), **over})


def test_every_download_is_one_rerun_and_the_aggregate_does_not_spoil_it():
    line, nxt, trap = ci_read.decide(7, "pr", [run_of(job("make setup", "DOWNLOAD", "uv"), job("CI OK", "AGGREGATE"))], [], [], None)
    assert line == "PR 7: every failed job is a tool DOWNLOAD -> make setup=DOWNLOAD(uv)" and nxt == "sm ci one rerun 7 --wait" and "never the fix" in trap


def test_an_unknown_cause_is_never_rerun():
    many = [job(f"job {i}", "TEST") for i in range(5)] + [job("Build", "UNKNOWN")]
    line, nxt, trap = ci_read.decide(900000, "run", [run_of(*many)], [], [], None)
    assert line.startswith("run 900000: 6 failed job(s), cause not identified for at least one -> job 0=TEST") and line.endswith("and 2 more")
    assert nxt == "sm ci faillog 900000" and trap == ci_read.TRAPS["unknown"]


def test_known_needs_every_failure_to_be_known():
    runs = [run_of(job("web-2", "TEST", known="9kk"), job("Lint", "TEST"))]
    assert ci_read.decide(7, "pr", runs, [], [], None)[1] == "sm ci faillog 900000"
    assert ci_read.decide(7, "pr", [run_of(job("web-2", "TEST"), known="7zk")], [], [], None)[0].startswith("PR 7: KNOWN failure (bead 7zk)")


def test_no_failed_job_cases_in_the_order_a_person_would_test_them():
    unread = ci_read.RunFailure(run=900000, unknown="HTTP 502")
    assert ci_read.decide(7, "pr", [unread], [], [], None)[:2] == ("PR 7: run 900000 could not be read: HTTP 502", "sm ci why 7")
    line, nxt, trap = ci_read.decide(7, "pr", [], ["merge-gate: FAILURE, posted by ..."], [], None)
    assert line == "PR 7: no CI job failed; red check(s) posted by another check suite: merge-gate" and nxt == "sm head sonar 7" and trap == ci_read.TRAPS["attached"]
    assert ci_read.decide(7, "pr", [], ["coverage-floor: FAILURE"], [], None)[1] == "sm why 7"
    assert ci_read.decide(7, "pr", [], [], ["Lint", "Build"], None) == ("PR 7: still running (2 check(s) pending), no failure yet", "sm ci watch 7", ci_read.TRAPS["pending"])
    cancelled = ci_read.RunFailure(run=900000, status="completed", conclusion="cancelled", cancelled_jobs=3)
    assert ci_read.decide(900000, "run", [cancelled], [], [], None) == ("run 900000: cancelled, nothing failed", "sm ci rerun --dry-run", ci_read.TRAPS["cancelled"])
    assert ci_read.decide(900000, "run", [ci_read.RunFailure(run=900000, status="completed", conclusion="success")], [], [], None) == ("run 900000: no failed jobs", "", "")


def test_a_green_blocked_pull_request_gets_the_other_suite_trap(tmp_path):
    green = "\n".join(json.dumps({"name": n, "status": "completed", "conclusion": "success", "completed_at": "2026-11-12T10:00:00Z"}) for n in ("CI OK", "merge-gate", "coverage-floor"))
    fake = recorded(pr=784, check_runs=green)
    fake.on(ROLLUP.format(pr=784), {"statusCheckRollup": [{"name": "CI OK", "status": "COMPLETED", "conclusion": "SUCCESS"}]})
    out = invoke(runtime_for(tmp_path, fake), ["ci", "why", "784"])
    lines = out.stdout.splitlines()
    assert lines[0] == "CAUSE    PR 784: CI is not the reason; GitHub mergeStateStatus BLOCKED" and lines[-2] == "NEXT     sm why 784"
    assert lines[-1].startswith("TRAP     BLOCKED with every check green: merge-gate and coverage-floor are attached by SHA to another check suite")


def test_a_run_in_flight_and_a_run_that_cannot_be_read(tmp_path):
    fake = FakeGh().on("run view 900001", {"status": "in_progress", "conclusion": "", "jobs": [{"name": "Lint", "conclusion": ""}], "workflowName": "CI", "attempt": 1})
    out = invoke(runtime_for(tmp_path, fake), ["ci", "why", "900001"])
    assert out.stdout.splitlines()[0] == "CAUSE    run 900001: still running (0 check(s) pending), no failure yet" and "NEXT     sm ci watch 900001" in out.stdout
    out = invoke(runtime_for(tmp_path, FakeGh().on("run view", 404)), ["ci", "why", "900002"])
    assert out.exit_code == 0 and out.stdout.splitlines()[0].startswith("CAUSE    run 900002: run 900002 could not be read: ") and "  UNKNOWN  " in out.stdout
    fake = FakeGh().on("--json", text(f"run-{WEB}.json")).on("--log-failed", 502)
    report = ci_read.CiWhy.model_validate_json(invoke(runtime_for(tmp_path, fake), ["--format", "json", "ci", "why", str(WEB)]).stdout)
    assert report.runs[0].unknown and report.runs[0].jobs[0].cause == "UNKNOWN" and report.trap == ci_read.TRAPS["unknown"]


def test_a_pull_request_whose_rollup_cannot_be_read_says_so(tmp_path):
    fake = recorded(pr=784)
    fake.on(ROLLUP.format(pr=784), 502)
    out = invoke(runtime_for(tmp_path, fake), ["ci", "why", "784"])
    assert out.exit_code == 0 and any(line.startswith("UNKNOWN  ") for line in out.stdout.splitlines())


# ------------------------------------------------------------------ ci faillog
def test_faillog_of_a_run_is_the_old_condensed_log_without_the_escape_carets(tmp_path):
    out = invoke(runtime_for(tmp_path, recorded()), ["ci", "faillog", str(WEB)])
    assert out.exit_code == 0, out
    lines = out.stdout.splitlines()
    assert lines[0] == f"## run {WEB}: run  (4426 bytes of failed steps; all of it: sm ci faillog {WEB} --raw)"
    old = [line.replace("^[", "") for line in text(f"run-{WEB}-old-faillog.txt").splitlines()]
    assert [line.rstrip() for line in lines[1:-1]] == [line.rstrip() for line in old]
    assert lines[-1] == f"verify with: sm ci one rerun {WEB} --wait   (hosted-only failure; do not push-and-wait for full CI)"


def test_faillog_of_a_pull_request_names_the_job_to_rerun_and_json_is_one_document(tmp_path):
    out = invoke(runtime_for(tmp_path, recorded(pr=585)), ["--format", "json", "ci", "faillog", "585", "-k", "2", "--tail", "3"])
    report = ci_read.FailLog.model_validate_json(out.stdout)
    assert report.next == 'sm ci one rerun 585 --job "End-to-end web-2 (ubuntu-24.04)" --wait' and report.runs[0].run == WEB and report.runs[0].log[-4] == "--- last 3 lines ---"
    out = invoke(runtime_for(tmp_path, recorded(pr=585)), ["ci", "faillog", "585", "--no-hint"])
    assert out.stdout.splitlines()[0].startswith(f"## run {WEB}: CI OK, End-to-end web-2 (ubuntu-24.04)  (") and "verify with" not in out.stdout


def test_faillog_raw_file_and_refusals(tmp_path):
    out = invoke(runtime_for(tmp_path, recorded()), ["ci", "faillog", str(WEB), "--raw"])
    assert out.stdout == text(f"run-{WEB}.log")
    log = tmp_path / "x.log"
    log.write_text("line one\nE   AssertionError: boom\nlast\n")
    out = invoke(runtime_for(tmp_path, FakeGh()), ["ci", "faillog", "--file", str(log), "--tail", "1"])
    assert out.stdout.splitlines() == ["[log]", "  line one", "  E   AssertionError: boom", "  last", "--- last 1 lines ---", "last"]
    for args, word in ((["ci", "faillog"], "give a pull request number"), (["ci", "faillog", "--file", str(tmp_path / "none")], "cannot read"),
                       (["ci", "faillog", "12", "--raw"], "--raw needs a run id"), (["ci", "faillog", "900009", "--raw"], "run 900009: ")):
        out = invoke(runtime_for(tmp_path, FakeGh()), args)
        assert out.exit_code == 1 and word in out.stderr, (args, out)


def test_faillog_without_a_failing_check_and_with_an_unreadable_log(tmp_path):
    fake = FakeGh().on("pr view", {"statusCheckRollup": [{"name": "Lint", "conclusion": "SUCCESS"}]})
    assert invoke(runtime_for(tmp_path, fake), ["ci", "faillog", "12"]).stdout.strip() == "#12: no failing check found"
    out = invoke(runtime_for(tmp_path, FakeGh().on("pr view", 502)), ["ci", "faillog", "12"])
    assert out.stdout.startswith("#12: no failing check found (")
    out = invoke(runtime_for(tmp_path, FakeGh().on("--log-failed", 502)), ["ci", "faillog", "900003"])
    assert out.stdout.splitlines()[1].startswith("UNKNOWN  ")


def test_the_reads_never_reach_the_classifier_or_the_keychain(tmp_path):
    import steuermaschine_heinzel.native.ci_lib as lib

    fake = recorded(pr=585)
    for args in (["ci", "faillog", str(WEB)], ["ci", "why", "585"]):
        assert invoke(runtime_for(tmp_path, fake), args).exit_code == 0
    assert {argv[0] for argv in fake.calls} == {"gh"} and "classifier" not in ci_read.CiSettings.model_fields
    assert not (Path(lib.__file__).parent / "jev.py").exists() and "jev" not in Path(ci_read.__file__).read_text().replace("ci classify", "")


# ------------------------------------------------------------------ the libraries (the assertions of the old tools' tests)
def test_download():
    assert c.classify("Install gitleaks", "curl: download failed HTTP 503")[0] == "DOWNLOAD"
    assert c.classify("Install the locked Electron package (pnpm)", "")[0] == "DOWNLOAD"
    assert c.classify("make setup", "uv: read timed out fetching wheel")[0] == "DOWNLOAD"


def test_test_and_unknown():
    assert c.classify("pytest", "FAILED tests/x.py::t - AssertionError")[0] == "TEST"
    assert c.classify("build", "boom")[0] == "UNKNOWN"


def test_electron_packaging_checksum_gateway():
    step = "Build and verify unsigned native package"
    log = (
        "2026-11-09T07:52:26.0827270Z HTTPError: Response code 504 (Gateway Time-out) for "
        "https://github.com/electron/electron/releases/download/v45.0.0/SHASUMS256.txt"
    )
    assert c.classify(step, log) == ("DOWNLOAD", "electron")
    assert c.classify(step, log.replace("github.com/", "example.com/")) == ("UNKNOWN", None)
    assert c.classify(step, log.replace("504", "403")) == ("UNKNOWN", None)
    assert c.classify(step, log + "\nAssertionError: invalid package identity") == ("TEST", None)


def test_verdict_rerun_only_all_download():
    assert c.verdict({"a": ("DOWNLOAD", "uv"), "b": ("DOWNLOAD", "gh")}, "9") == ("run 9: all DOWNLOAD -> a=DOWNLOAD(uv), b=DOWNLOAD(gh)", True)
    assert c.verdict({"a": ("DOWNLOAD", "uv"), "b": ("UNKNOWN", None)}, "9")[1] is False
    assert c.verdict({"a": ("DOWNLOAD", "uv"), "CI OK": ("AGGREGATE", None)}, "9")[1] is True and c.verdict({}, "9") == ("run 9: no failed jobs", False)


def test_docs_playwright_deps_timeout_is_not_pnpm_download():
    step = "Build and verify the product site or a standalone documentation site"
    timeout = (
        "subprocess.TimeoutExpired: Command '['/runner/.cache/uv/bin/python', "
        "'-m', 'playwright', 'install', 'chromium', 'webkit', '--with-deps']' timed out after 600 seconds"
    )
    preamble = "✓ pnpm 12.15.0 (installed)\nInstalling dependencies...\n"
    apt_stall = "2026-11-11T15:00:12Z Ign:2 https://archive.ubuntu.com/ubuntu noble/universe amd64 fonts-ipafont-gothic\n"
    log = preamble + apt_stall + timeout
    cause = c.classify(step, log)
    assert cause == ("BROWSER_SETUP_TIMEOUT", "apt")
    assert c.verdict({"Build the site": cause}, "9")[1] is False
    assert c.classify(step, preamble + timeout) == ("BROWSER_SETUP_TIMEOUT", "playwright")
    assert c.classify(step, apt_stall + preamble + timeout) == ("BROWSER_SETUP_TIMEOUT", "playwright")
    assert c.classify(step, preamble + apt_stall) == ("UNKNOWN", None)


def test_analyze_ignores_playwright_timeout_from_a_different_step(tmp_path):
    info = {"status": "completed", "jobs": [{
        "name": "Build the site", "conclusion": "failure", "steps": [
            {"name": "Install pnpm", "conclusion": "failure"},
        ],
    }]}
    log = (
        "Build the site\tEarlier step\tsubprocess.TimeoutExpired: Command '['/runner/bin/python', "
        "'-m', 'playwright', 'install', 'chromium', 'webkit', '--with-deps']' timed out after 600 seconds\n"
        "Build the site\tInstall pnpm\tcurl: download failed HTTP 503\n"
    )
    fake = FakeGh().on("--json", info).on("--log-failed", log)
    report = ci_read.CiWhy.model_validate_json(invoke(runtime_for(tmp_path, fake), ["--format", "json", "ci", "why", "43043098800"]).stdout)
    assert report.runs[0].verdict.endswith("Build the site=DOWNLOAD(pnpm)") and report.runs[0].rerun_is_next
    assert report.next == "sm ci one rerun 43043098800 --wait"


def test_a_job_without_log_lines_is_unknown_and_named_steps_are_not_mixed():
    jobs = [{"name": "A", "conclusion": "failure", "steps": [{"name": "s1", "conclusion": "failure"}]}, {"name": "B", "conclusion": "success"}]
    assert c.classify_jobs(jobs, {}) == {"A": ("UNKNOWN", None)}
    assert c.classify_jobs(jobs, {("A", "other step"): ["FAILED tests/x.py::t"]}) == {"A": ("UNKNOWN", None)}
    assert c.classify_jobs(jobs, {("A", "UNKNOWN STEP"): ["FAILED tests/x.py::t"]}) == {"A": ("TEST", None)}
    assert c.classify_jobs([{"name": "CI OK", "conclusion": "failure"}], {}) == {"CI OK": ("UNKNOWN", None)}  # alone it is not an aggregate of anything


def test_failing_test_names_from_three_runners():
    log = ("job\tstep\t2026-11-12T08:00:00.0Z FAILED tests/a_test.py::test_x - AssertionError\n FAIL  web/src/a.test.ts > renders\n"
           "  1) [webkit] › e2e/a.spec.ts:3:1 › opens (2.1s)\nnothing here\n")  # noqa: RUF001
    assert logs.failing_tests(log) == ["tests/a_test.py::test_x", "web/src/a.test.ts > renders", "[webkit] › e2e/a.spec.ts:3:1 › opens"]  # noqa: RUF001


def test_rollup_runs_attached_checks_and_the_job_to_rerun():
    rollup = [{"name": "Lint", "conclusion": "FAILURE", "detailsUrl": "https://github.com/o/r/actions/runs/900000/job/1"},
              {"name": "CI OK", "conclusion": "FAILURE", "detailsUrl": "https://github.com/o/r/actions/runs/900000/job/2"},
              {"name": "coverage-floor", "conclusion": "FAILURE", "detailsUrl": "https://github.com/o/r/runs/131075933689"},
              {"name": "Build", "conclusion": "SUCCESS", "detailsUrl": "https://github.com/o/r/actions/runs/900000/job/3"}]
    assert logs.failing_runs(rollup) == {"900000": ["Lint", "CI OK"]} and [x["name"] for x in logs.attached_checks(rollup)] == ["coverage-floor"]
    assert faillog.rerun_job(["CI OK", "merge-gate", "Lint"]) == "Lint" and faillog.rerun_job(["CI OK", "coverage-floor", "run"]) is None
    assert logs.condense([]) == ["(no error-looking line found)", "--- last 12 lines ---"]
    many = [("job / step", f"Error: boom {i}") if i % 3 == 0 else ("job / step", "fine") for i in range(200)]
    assert len([x for x in logs.condense(many, k=2, tail=1) if x.startswith("  ")]) <= 12


def test_known_registry_deadlines(tmp_path):
    from datetime import date

    path = tmp_path / "k.toml"
    path.write_text('[[known]]\nmatch = "Web-2"\nbead = "9kk"\nadded = 2026-11-08\n\n[[known]]\nmatch = "old"\nbead = "abc"\nuntil = "2026-11-01"\n\n'
                    '[[known]]\nmatch = "bad"\nbead = "not a bead id!"\nadded = 2026-11-08\n\n[[known]]\nmatch = "undated"\nbead = "xyz"\n')
    known = known_failures.load_known(str(path), date(2026, 11, 12))
    assert known == [("web-2", "9kk", "")] and known_failures.known_bead(["End-to-end web-2 (ubuntu)"], known) == "9kk" and known_failures.known_bead(["Lint"], known) is None
    assert known_failures.load_known(str(path), date(2026, 11, 16)) == [] and known_failures.load_known(str(tmp_path / "none")) == []
    import pytest

    with pytest.raises(ValueError):
        known_failures.known_entries(str(path), strict=True)
