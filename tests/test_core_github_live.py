"""Opt-in, read-only checks against the real GitHub and Sonar. Skipped unless SM_LIVE_TESTS=1.

    SM_LIVE_TESTS=1 SM_LIVE_GITHUB_REPO=OWNER/NAME [SM_LIVE_PR=12] [SM_LIVE_SONAR_PROJECT=KEY] uv run pytest tests/test_core_github_live.py

Needs an authenticated `gh`. Nothing here writes: no --live, no variable write, no PR.
"""

import os

import pytest

from steuermaschine.defaults import Subprocess
from steuermaschine.github import GitHub
from steuermaschine.github.models import is_unknown
from steuermaschine.github.sonar import Sonar

REPO = os.environ.get("SM_LIVE_GITHUB_REPO", "")
pytestmark = pytest.mark.skipif(os.environ.get("SM_LIVE_TESTS") != "1" or not REPO, reason="live tests are opt-in: SM_LIVE_TESTS=1 and SM_LIVE_GITHUB_REPO=OWNER/NAME")


@pytest.fixture(scope="module")
def github():
    return GitHub(Subprocess(), REPO)


@pytest.fixture(scope="module")
def pr(github):
    if number := os.environ.get("SM_LIVE_PR"):
        return github.pr(number)
    found = github.open_prs(limit=1)
    assert found.state == "ok"
    if not found.pulls:
        pytest.skip("the repository has no open pull request; set SM_LIVE_PR")
    return found.pulls[0]


def test_pull_request_by_number_branch_and_commit(github, pr):
    assert pr.state in ("open", "merged", "closed") and len(pr.head) == 40 and pr.base
    assert github.pr(pr.head).number == pr.number
    assert github.pr("9999999").state.startswith("unknown: ")
    assert github.pr("no-such-branch-zz9").state == "none"


def test_checks_and_why(github, pr):
    rollup = github.pr_checks(pr.number)
    assert rollup.state in ("green", "red", "pending", "none") and not is_unknown(rollup.required_source)
    assert all(c.state in ("green", "red", "pending", "skipped") for c in rollup.checks)
    why = github.why(pr.number)
    assert why.mergeable in ("yes", "no") or is_unknown(why.mergeable)
    assert why.mergeable != "no" or why.reasons


def test_branch_tip_runs_jobs_and_log_tail(github, pr):
    tip = github.branch_tip(pr.base)
    assert tip.state == "found" and len(tip.sha) == 40 and github.branch_tip("no-such-branch-zz9").state == "missing"
    assert github.active(limit=5).state == "ok"
    done = github.runs(status="completed", limit=1)
    assert done.state == "ok"
    if done.runs:
        run = done.runs[0]
        assert github.run(run.id).id == run.id and github.cancel_run(run.id).state == "not_cancellable"  # dry, and a finished run is never cancelled
        jobs = github.jobs(run.id)
        assert jobs.state == "ok"
        if jobs.jobs:
            assert github.log_tail(jobs.jobs[0].id, 5).state == "ok" or jobs.jobs[0].conclusion in ("skipped", "cancelled")


def test_commit_status_and_variables_are_read_only(github, pr):
    assert github.status(pr.head).state in ("green", "red", "pending")
    assert github.variable("NO_SUCH_VARIABLE_ZZ9").state == "missing"
    assert github.write_variable("NO_SUCH_VARIABLE_ZZ9", "x", live=True).state.startswith("refused: ")


def test_sonar(pr):
    key = os.environ.get("SM_LIVE_SONAR_PROJECT")
    if not key:
        pytest.skip("set SM_LIVE_SONAR_PROJECT")
    report = Sonar(key).pr_quality(pr.number, issues=3)
    assert report.gate in ("passed", "failed", "none") or is_unknown(report.gate)
