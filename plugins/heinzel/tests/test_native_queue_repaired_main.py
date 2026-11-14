"""`sm queue main-rerun` after `sm head prepare --repair-main`: the missing-plan failure of exactly that run is KNOWN
(repaired-plan), but it is never rerun: Core's focused jobs refuse attempt 2, so the repair ends in a fresh attempt-1
push run (a fix-forward merge). A green focused attempt 2, should one exist, still restores merges and clears the pin.
Replays run 43452895000 at f6e3b02b on a scripted `gh`."""

import json

import pytest
from test_native_queue_main_commands import CORE, gh, nothing_real, rt  # noqa: F401  (fixtures)

from steuermaschine.native import NativeRuntime, using
from steuermaschine.testing import invoke
from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native import queue as queue_area
from steuermaschine_heinzel.native import queue_main as area
from steuermaschine_heinzel.native.admission_lib import repair_main as r

RUN, SHA, PATHS, OK = 43452895000, "f6e3b02b55394065c98fc29847128a7931897dcb", 131979590277, 131980761147


def jobs(attempt, *, paths="failure", ok="failure", lane="skipped"):
    return {"total_count": 4, "jobs": [
        {"id": 1 + attempt * 10, "name": "Secret scan", "status": "completed", "conclusion": "success"},
        {"id": 2 + attempt * 10, "name": "Focused tests (python)", "status": "completed", "conclusion": lane},
        {"id": PATHS if attempt == 1 else PATHS + 1, "name": "Changed paths", "status": "completed", "conclusion": paths},
        {"id": OK if attempt == 1 else OK + 1, "name": "CI OK", "status": "completed", "conclusion": ok}]}


def receipt(tmp_path, run=RUN, jobs_=None):
    root = statedir.root() / r.RECEIPTS
    root.mkdir(parents=True, exist_ok=True)
    proof = {"mode": r.MODE, "main": SHA, "pr": 803,
             "live": {"run": {"run": run, "attempt": 1, "jobs": jobs_ or {r.JOB: PATHS, r.AGGREGATE: OK}}}}
    (root / f"head-{'2' * 40}-{'5' * 32}.json").write_text(json.dumps(
        {"head": "2" * 40, "registry_changed": True, "transaction_sha256": "7" * 64, "repair_main": proof}))


def script(gh, attempt=1, conclusion="failure"):  # noqa: F811
    gh.on("run list -R " + CORE + " --workflow ci.yml --branch main", [
        {"status": "completed", "conclusion": conclusion, "databaseId": RUN, "headSha": SHA, "attempt": attempt,
         "displayTitle": "feat(system): one update for hz, pinned plugins and the desktop app (#803)"}])
    gh.on("run list -R " + CORE + " -L 60", [])
    gh.on(f"api repos/{CORE}/actions/runs/{RUN}", {"id": RUN, "status": "completed", "conclusion": conclusion,
                                                   "head_sha": SHA, "run_attempt": attempt})
    gh.on(f"api repos/{CORE}/actions/runs/{RUN}/attempts/1/jobs?per_page=100", jobs(1))
    gh.on(f"api repos/{CORE}/actions/runs/{RUN}/attempts/2/jobs?per_page=100",
          jobs(2, paths="success", ok="success", lane="success"))


def dry(rt):  # noqa: F811
    out = invoke(rt, ["--format", "json", "queue", "main-rerun", "--dry-run"])
    return out, area.QueueMain.model_validate(json.loads(out.stdout)) if out.stdout.strip() else None


def test_missing_plan_without_a_repair_stays_unknown(rt, gh):  # noqa: F811
    script(gh)
    out = invoke(rt, ["queue", "main-rerun", "--dry-run"])
    assert out.exit_code == 1 and "UNKNOWN failing job(s) Changed paths" in out.stderr


def test_a_written_repair_of_this_run_is_never_rerun_and_names_the_fix_forward(rt, gh, tmp_path):  # noqa: F811
    # bead 4yen: Core's focused jobs refuse run attempt 2 (CandidateError), so the repair ends in a fresh attempt-1 run
    script(gh)
    receipt(tmp_path)
    for argv in (["queue", "main-rerun", "--dry-run"], ["queue", "main-rerun"]):
        out = invoke(rt, argv)
        assert out.exit_code == 1 and "Focused tests (python) would run again on attempt 2" in out.stderr, out
        assert "fresh attempt-1 push run" in out.stderr and "sm queue add --fix-forward" in out.stderr
    assert not gh.writes() and not any(c[:3] == ["gh", "run", "rerun"] for c in gh.calls)


def test_a_known_failure_in_a_focused_job_is_never_rerun(rt, gh):  # noqa: F811
    script(gh)
    gh.on(f"api repos/{CORE}/actions/runs/{RUN}/attempts/1/jobs?per_page=100", jobs(1, paths="success", lane="failure"))
    (rt.records / "research" / "known-failures.toml").write_text(
        '[[known]]\nmatch = "Focused tests (python)"\nbead = "abc"\nadded = 2026-11-13\nuntil = 2099-01-01\n')
    out = invoke(rt, ["queue", "main-rerun"])
    assert out.exit_code == 1 and "Focused tests (python) would run again" in out.stderr and "--fix-forward" in out.stderr, out
    assert not any(c[:3] == ["gh", "run", "rerun"] for c in gh.calls)


@pytest.mark.parametrize(("lane", "repaired", "refusing"), [
    ("success", False, []), ("skipped", False, []), ("failure", False, ["Focused tests (python)"]),
    ("cancelled", False, ["Focused tests (python)"]), ("skipped", True, ["Focused tests (python)"])])
def test_which_focused_jobs_a_rerun_would_start_again(rt, lane, repaired, refusing):  # noqa: F811
    with using(NativeRuntime(rt.providers, rt.settings, rt.project)):
        module = queue_area._queue()
        beads = {"Changed paths": module.REPAIRED} if repaired else {"Secret scan": "x"}
        assert module.rerun_refusing_jobs(jobs(1, lane=lane)["jobs"], beads) == refusing


@pytest.mark.parametrize("other", ["run", "jobs"])
def test_a_repair_of_another_run_or_other_jobs_does_not_count(rt, gh, tmp_path, other):  # noqa: F811
    script(gh)
    receipt(tmp_path, run=RUN + 1) if other == "run" else receipt(tmp_path, jobs_={r.JOB: PATHS})
    out = invoke(rt, ["queue", "main-rerun", "--dry-run"])
    assert out.exit_code == 1 and "UNKNOWN" in out.stderr


def test_green_focused_attempt_two_restores_merges_and_clears_that_pin(rt, tmp_path, monkeypatch, gh):  # noqa: F811
    with using(NativeRuntime(rt.providers, rt.settings, rt.project)):
        _restores(tmp_path, monkeypatch, gh)


def _restores(tmp_path, monkeypatch, gh):  # noqa: F811
    receipt(tmp_path)
    script(gh, attempt=2, conclusion="success")
    module = queue_area._queue()
    state = {"main_reruns": {str(RUN): {"run": RUN, "sha": SHA, "repair_receipt_sha256": r.receipt_for(tmp_path, RUN, SHA, {
        r.JOB: PATHS, r.AGGREGATE: OK})["receipt_sha256"]}},
             "main_failure": {"databaseId": RUN, "headSha": SHA, "status": "completed", "conclusion": "failure"}}
    saved = {}
    monkeypatch.setattr(module, "load", lambda: {**state, **saved})
    monkeypatch.setattr(module, "update", lambda **fields: saved.update(fields))
    monkeypatch.setattr(module, "log", lambda line: None)
    monkeypatch.setattr(module.hzlib, "note", lambda line: None)
    monkeypatch.setattr(module.hzlib, "ghj", lambda *args, default=None: _answer(gh, args, default))
    monkeypatch.setattr(module.hzlib, "resolve_main_runs", lambda runs, **kw: runs)
    found = module.rerun_restoration()
    assert found["repaired"] is True and found["beads"] == ["repaired-plan"] and found["jobs"] == ["Changed paths"]
    assert module.restored_by_rerun() is True
    assert saved["main_failure"]["fixed_by"] == [SHA]


def test_a_full_matrix_rule_still_holds_for_known_job_flakes(rt, tmp_path, monkeypatch, gh):  # noqa: F811
    with using(NativeRuntime(rt.providers, rt.settings, rt.project)):
        _full_rule(monkeypatch, gh)


def _full_rule(monkeypatch, gh):  # noqa: F811
    script(gh, attempt=2, conclusion="success")
    module = queue_area._queue()
    monkeypatch.setattr(module.hzlib, "ghj", lambda *args, default=None: _answer(gh, args, default))
    monkeypatch.setattr(module.hzlib, "resolve_main_runs", lambda runs, **kw: runs)
    monkeypatch.setattr(module, "load", lambda: {"main_reruns": {}})
    assert module.rerun_restoration() == {}
    assert not module.focused_pass(None) and not module.focused_pass(jobs(1)["jobs"])


def _answer(gh, args, default):  # noqa: F811
    result = gh.run(["gh", *args])
    return json.loads(result.out) if result.rc == 0 else default
