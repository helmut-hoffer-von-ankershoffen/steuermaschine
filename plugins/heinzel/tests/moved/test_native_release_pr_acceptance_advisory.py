"""hz-merge-train: published-build Acceptance dry runs are advisory on a release PR only after an absorbed NOT ACCEPTED release."""
import json

import pytest

CORE = "helmut-hoffer-von-ankershoffen/heinzel"
HEAD = "e10b2190a0304822a8076c1c57b103eb0e09eca5"
pytestmark = pytest.mark.usefixtures("native_receipts")


@pytest.fixture(autouse=True)
def isolated_release_holds(tmp_path, monkeypatch):
    from steuermaschine_heinzel.native.queue_lib import hzlib

    monkeypatch.setattr(hzlib, "release_holds", lambda: tmp_path / "release-holds")


def acceptance(os_name, conclusion="FAILURE", run=42480597939):
    return {"name": f"Acceptance ({os_name})", "status": "COMPLETED", "conclusion": conclusion, "workflowName": "Acceptance",
            "detailsUrl": f"https://github.com/{CORE}/actions/runs/{run}/job/1"}


def check(name, conclusion="SUCCESS"):
    return {"name": name, "status": "COMPLETED", "conclusion": conclusion}


class FakeGh:
    def __init__(self, rollup, branch="release/v0.9.32", event="pull_request"):
        self.view = {"state": "OPEN", "isDraft": False, "headRefOid": HEAD, "headRefName": branch,
                     "mergeStateStatus": "UNSTABLE", "statusCheckRollup": rollup}
        self.event, self.calls = event, []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("pr", "view"):
            fields = args[args.index("--json") + 1].split(",")
            return 0, json.dumps({k: v for k, v in self.view.items() if k in fields})
        if args[:1] == ("api",) and "/actions/runs/" in args[1]:
            return 0, f"{self.event}\t{HEAD}\n"
        return 0, ""


def receipt(tmp_path, version="0.9.31", **data):
    tmp_path.joinpath(f"v{version}.json").write_text(json.dumps({"version": version, "finished": True, **data}))


ABSORBED = {"accepted": False, "absorbed_by": "0.9.32", "absorb_reason": "fixed in 0.9.32 by #643"}
GREEN = [check("CI OK"), check("merge-gate"), check("coverage-floor")]


def run(tool, monkeypatch, tmp_path, fake):
    module = tool("hz-merge-train")
    monkeypatch.setattr(module, "gh", fake)
    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    monkeypatch.setattr(module, "FREEZE", tmp_path / "no-freeze")
    monkeypatch.setattr(module, "RELEASE_DIR", tmp_path)
    monkeypatch.setenv("HZ_INTERVENTIONS_LOG", str(tmp_path / "interventions.log"))
    merged, _ = module.train(CORE, "674")
    log = tmp_path / "interventions.log"
    return merged, (log.read_text() if log.exists() else "")


def test_advisory_when_previous_release_absorbed(tool, monkeypatch, tmp_path, capsys):
    import time

    from steuermaschine_heinzel.native.queue_lib import hzlib

    # The ruleset of main as read from GitHub: it requires the three aggregates, not the Acceptance dry runs (unread rules waive nothing).
    monkeypatch.setattr(hzlib, "_RULES", {CORE: (time.monotonic(), frozenset({"CI OK", "merge-gate", "coverage-floor"}))})
    receipt(tmp_path, **ABSORBED)
    fake = FakeGh([*GREEN, acceptance("ubuntu-24.04"), acceptance("macos-latest")])
    merged, log = run(tool, monkeypatch, tmp_path, fake)
    assert merged
    out = capsys.readouterr().out
    assert "ADVISORY on release PR release/v0.9.32" in out and "Acceptance (ubuntu-24.04)" in out
    assert log.count("merge train: #674 ADVISORY") == 1 and "absorbed_by 0.9.32" in log and "#643" in log


def test_blocking_when_previous_release_accepted(tool, monkeypatch, tmp_path):
    receipt(tmp_path, accepted=True)
    fake = FakeGh([*GREEN, acceptance("ubuntu-24.04")])
    merged, log = run(tool, monkeypatch, tmp_path, fake)
    assert not merged and log == ""


def test_blocking_when_not_accepted_but_not_absorbed_by_this_release(tool, monkeypatch, tmp_path):
    receipt(tmp_path, accepted=False, absorbed_by="0.9.33")
    merged, log = run(tool, monkeypatch, tmp_path, FakeGh([*GREEN, acceptance("ubuntu-24.04")]))
    assert not merged and log == ""


def test_blocking_on_non_release_pr(tool, monkeypatch, tmp_path):
    receipt(tmp_path, **ABSORBED)
    fake = FakeGh([*GREEN, acceptance("ubuntu-24.04")], branch="fix/acceptance-runner")
    merged, log = run(tool, monkeypatch, tmp_path, fake)
    assert not merged and log == ""


def test_blocking_when_acceptance_run_was_dispatched_with_the_candidate(tool, monkeypatch, tmp_path):
    receipt(tmp_path, **ABSORBED)
    fake = FakeGh([*GREEN, acceptance("ubuntu-24.04")], event="workflow_dispatch")
    merged, log = run(tool, monkeypatch, tmp_path, fake)
    assert not merged and log == ""


@pytest.mark.parametrize("required", ["CI OK", "merge-gate", "coverage-floor", "unit (ubuntu-24.04)"])
def test_required_and_candidate_checks_never_advisory(tool, monkeypatch, tmp_path, required):
    receipt(tmp_path, **ABSORBED)
    rollup = [c for c in GREEN if c["name"] != required]
    failed = {**acceptance("x"), "name": required}  # even dressed as an Acceptance-workflow check
    fake = FakeGh([*rollup, failed, acceptance("ubuntu-24.04")])
    merged, _ = run(tool, monkeypatch, tmp_path, fake)
    assert not merged
