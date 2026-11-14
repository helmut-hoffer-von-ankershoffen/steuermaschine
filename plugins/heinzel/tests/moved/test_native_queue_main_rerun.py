"""hz-queue main-rerun and restored-by-rerun (policy 2026-11-07 (iii)+(ii)): merges only, never releases, KNOWN only."""

import json

import pytest
from steuermaschine_heinzel import state as statedir

SHA = "c68db2fc73be77813333be47781d1fb6712e2b75"
RID = 42390245496
MAC = "Test (macos-latest, Python 3.15, unit)"
MATRIX = (["CI OK"] + [f"Test (ubuntu-24.04, Python {v})" for v in ("3.12", "3.13", "3.14", "3.15")]
          + [f"Test (macos-latest, Python {v}, {lvl})" for v in ("3.12", "3.13", "3.14", "3.15") for lvl in ("unit", "integration")])
KNOWN = f'[[known]]\nmatch = "{MAC}"\nbead = "5ox"\nadded = 2026-11-07\nuntil = 2099-01-01\n'


def jobs(failed=(), status="completed"):
    names = MATRIX + [n for n in failed if n not in MATRIX]
    rows = [{"id": 1000 + i, "name": n, "status": status, "conclusion": "failure" if n in failed or (failed and n == "CI OK") else "success"}
            for i, n in enumerate(names)]
    return {"total_count": len(rows), "jobs": rows}


class GH:
    """Answers the few gh reads the tools make; `calls` records every `gh` write."""

    def __init__(self, attempt=1, conclusion="failure", status="completed", first=(MAC,), second=(), releases=(), newer=(), log=""):
        self.log = log  # the text every job log read returns (bead 4mgv: KNOWN by failing test id)
        self.newer = newer  # statuses of newer main runs in front of RID (bead qnd)
        self.attempt, self.conclusion, self.status = attempt, conclusion, status
        self.first, self.second, self.releases, self.calls = first, second, releases, []

    def ghj(self, *args, default=None):
        joined = " ".join(args)
        if args[:2] == ("run", "list") and "--workflow" in args:
            ahead = [{"databaseId": 900 + i, "headSha": f"{i:040d}", "status": st, "conclusion": None, "attempt": 1, "displayTitle": "CI"}
                     for i, st in enumerate(self.newer)]
            return ahead + [{"databaseId": RID, "headSha": SHA, "status": self.status, "conclusion": self.conclusion if self.status == "completed" else None,
                     "attempt": self.attempt, "displayTitle": "CI " + SHA}]
        if args[:2] == ("run", "list"):
            return [{"headBranch": b, "status": "in_progress", "databaseId": 1} for b in self.releases]
        if joined.endswith(f"actions/runs/{RID}"):
            return {"id": RID, "run_attempt": self.attempt, "status": self.status, "conclusion": self.conclusion, "head_sha": SHA}
        if f"runs/{RID}/attempts/1/jobs" in joined:
            return jobs(self.first)
        if f"runs/{RID}/attempts/2/jobs" in joined:
            return jobs(self.second)
        return default

    def gh(self, *args):
        if args[:2] == ("api", "--allow-escape-sequences") and args[2].endswith("/logs"):
            return 0, self.log  # a read, not a write
        self.calls.append(args)
        return 0, ""


@pytest.fixture
def q(tool, home, monkeypatch, tmp_path):
    q = tool("hz-queue")
    hzlib = q.hzlib

    known = tmp_path / "known.toml"
    known.write_text(KNOWN)
    monkeypatch.setenv("HZ_KNOWN_FAILURES", str(known))
    monkeypatch.setenv("HZ_QUEUE_STATE", str(statedir.root() / "queue.json"))
    q.fake, q.notes, q.known = GH(), [], known
    monkeypatch.setattr(hzlib, "ghj", lambda *a, default=None: q.fake.ghj(*a, default=default))
    monkeypatch.setattr(hzlib, "gh", lambda *a: q.fake.gh(*a))
    monkeypatch.setattr(hzlib, "note", q.notes.append)
    monkeypatch.setattr(hzlib, "merge_frozen", lambda *a: False)
    return q


def reruns(q):
    return [c for c in q.fake.calls if c[:2] == ("run", "rerun")]


def test_known_single_job_failure_is_rerun_once_and_noted(q, capsys):
    assert q.main(["main-rerun"]) == 0
    assert reruns(q) == [("run", "rerun", str(RID), "--failed", "-R", q.hzlib.CORE)]
    out = capsys.readouterr().out
    assert f"reran the failed jobs of run {RID} at c68db2fc: {MAC}=KNOWN(5ox)" in out
    assert any("main-rerun" in n and "KNOWN(5ox)" in n and "releases still need" in n for n in q.notes)
    assert str(RID) in q.load()["main_reruns"]
    assert q.main(["main-rerun"]) == 1  # never more than once per run, even before GitHub shows attempt 2
    assert len(reruns(q)) == 1
    assert "already rerun" in capsys.readouterr().err


def test_dry_run_starts_nothing(q, capsys):
    assert q.main(["main-rerun", "--dry-run"]) == 0
    assert "would rerun the failed jobs of run" in capsys.readouterr().out
    assert not reruns(q) and not q.notes and not q.load()["main_reruns"]


@pytest.mark.parametrize(("fake", "why"), [
    (GH(first=(MAC, "End-to-end web-1 (macos-latest)")), "UNKNOWN failing job(s) End-to-end web-1 (macos-latest)"),
    (GH(first=("Lint",)), "UNKNOWN failing job(s) Lint"),
    (GH(first=()), "no failing job besides CI OK"),
    (GH(attempt=2), "is attempt 2: already rerun"),
    (GH(releases=("release/v0.7.30",)), "release guard: release/* run active"),
])
def test_refusals_rerun_nothing(q, capsys, fake, why):
    q.fake = fake
    assert q.main(["main-rerun"]) == 1
    assert why in capsys.readouterr().err
    assert not reruns(q) and not q.notes


@pytest.mark.parametrize(("fake", "why"), [
    (GH(conclusion="success"), "is success"),
    (GH(status="in_progress"), "is in_progress"),
    (GH(conclusion="cancelled"), "is cancelled"),
])
def test_nothing_to_rerun(q, capsys, fake, why):
    q.fake = fake
    assert q.main(["main-rerun"]) == 0
    assert why in capsys.readouterr().out and not reruns(q)


def test_older_completed_known_red_run_is_rerun_while_newer_runs_are_queued(q):
    q.fake = GH(newer=("queued", "in_progress"))
    assert q.main(["main-rerun"]) == 0
    assert reruns(q) and str(RID) in " ".join(reruns(q)[0])


def test_older_run_is_not_rerun_twice_nor_when_unknown_nor_when_green(q, capsys):
    q.fake = GH(newer=("queued",))
    q.main(["main-rerun"])
    q.main(["main-rerun"])
    assert len(reruns(q)) == 1 and "already rerun" in capsys.readouterr().err
    q.fake = GH(newer=("queued",), first=("End-to-end cli (macos-latest)",))
    assert q.main(["main-rerun"]) == 1 and not reruns(q)
    q.fake = GH(newer=("queued",), conclusion="success")
    assert q.main(["main-rerun"]) == 0 and not reruns(q)


def test_only_unfinished_runs_means_nothing_to_rerun(q, capsys):
    q.fake = GH(newer=("queued",), status="in_progress")
    assert q.main(["main-rerun"]) == 0 and not reruns(q) and "is queued" in capsys.readouterr().out


def test_release_guard_still_blocks_with_newer_runs(q):
    q.fake = GH(newer=("queued",), releases=("release/v0.9.29",))
    assert q.main(["main-rerun"]) == 1 and not reruns(q)


def test_freeze_blocks_the_rerun(q, monkeypatch, capsys):
    monkeypatch.setattr(q.hzlib, "merge_frozen", lambda *a: True)
    assert q.main(["main-rerun"]) == 1
    assert "release guard: merge freeze is on" in capsys.readouterr().err and not reruns(q)


def test_overdue_entry_is_not_known(q, capsys):
    q.known.write_text(KNOWN.replace("until = 2099-01-01", "until = 2026-11-01"))
    assert q.main(["main-rerun"]) == 1
    assert f"UNKNOWN failing job(s) {MAC}" in capsys.readouterr().err and not reruns(q)


def test_undated_entry_is_not_known(q, capsys):
    q.known.write_text(f'[[known]]\nmatch = "{MAC}"\nbead = "5ox"\n')
    assert q.main(["main-rerun"]) == 1 and not reruns(q)


def test_failed_gh_rerun_is_forgotten_so_the_next_tick_retries(q, monkeypatch):
    monkeypatch.setattr(q.hzlib, "gh", lambda *a: (1, ""))
    assert q.main(["main-rerun"]) == 1
    assert not q.load()["main_reruns"] and not q.notes


def test_tick_runs_main_rerun_and_logs_a_skip_once(q):
    q.fake = GH(first=("Lint",))
    q._main_rerun(auto=True)
    q._main_rerun(auto=True)
    log = (statedir.root() / "queue.log").read_text()
    assert log.count("main-rerun: refused: run") == 1 and not reruns(q)
    q.fake = GH()
    q._main_rerun(auto=True)
    assert len(reruns(q)) == 1


# --- restored-by-rerun ----------------------------------------------------------------------------------------------
def restored(q, **kw):
    q.fake = GH(attempt=2, conclusion="success", **kw)
    return q.rerun_restoration()


def test_attempt_two_full_green_after_known_failure_restores_merges(q):
    found = restored(q)
    assert found == {"run": RID, "sha": SHA, "jobs": [MAC], "beads": ["5ox"]}
    q.update(main_failure={"databaseId": 1, "headSha": "e" * 40, "status": "completed", "conclusion": "failure", "attempt": 1})
    assert q.restored_by_rerun() and q.restored_by_rerun()
    state = q.load()["main_restored"]
    assert state["label"] == "restored-by-rerun(KNOWN: 5ox)" and state["run"] == RID
    assert len([n for n in q.notes if "restored-by-rerun(KNOWN: 5ox)" in n]) == 1  # audited once per run
    assert q.load()["main_failure"]["databaseId"] == 1  # the pin stays


def test_gate_lets_merges_through_only_when_restored(q, monkeypatch):
    item = [{"repo": q.hzlib.CORE, "key": "610"}]
    data = {"last_batch": 0}
    monkeypatch.setattr(q, "main_state", lambda: "red")
    monkeypatch.setattr(q, "later_main_passes", lambda: False)
    q.fake = GH(attempt=2, conclusion="success")
    assert q.gate(item, 10_000, data) == ""
    q.fake = GH(attempt=2, conclusion="success", first=(MAC, "Lint"))
    assert q.gate(item, 10_000, data) == "main's latest run is not green"


@pytest.mark.parametrize("kw", [
    {"first": (MAC, "Lint")},          # an UNKNOWN attempt-1 failure keeps the old rule
    {"first": ()},                     # nothing named failed
    {"second": (MAC,)},                # attempt 2 not fully green
])
def test_no_restoration_without_known_failures_and_full_green(q, kw):
    assert restored(q, **kw) == {}


def test_no_restoration_for_first_attempt_or_third_attempt(q):
    q.fake = GH(attempt=1, conclusion="success")
    assert q.rerun_restoration() == {}
    q.fake = GH(attempt=3, conclusion="success")
    assert q.rerun_restoration() == {}


def test_no_restoration_once_the_entry_is_overdue(q):
    q.known.write_text(KNOWN.replace("until = 2099-01-01", "until = 2026-11-01"))
    assert restored(q) == {}


def test_release_never_counts_a_rerun_green(tool, monkeypatch):
    rel = tool("hz-release")
    hzlib = rel.hzlib
    monkeypatch.setattr(hzlib, "gh", lambda *a: (0, SHA))
    monkeypatch.setattr(hzlib, "resolve_main_runs", lambda rows: rows)
    monkeypatch.setattr(rel, "runs", lambda *a, **kw: [{"databaseId": RID, "headSha": SHA, "status": "completed", "conclusion": "success", "attempt": 2}])
    with pytest.raises(rel.Stop, match="green only on rerun attempt 2.*first-attempt full green"):
        rel.main_tip_green()
    monkeypatch.setattr(rel, "runs", lambda *a, **kw: [{"databaseId": RID, "headSha": SHA, "status": "completed", "conclusion": "success", "attempt": 1}])
    assert rel.main_tip_green() is True


def test_help_names_main_rerun(q, capsys):
    with pytest.raises(SystemExit):
        q.main(["--help"])
    out = capsys.readouterr().out
    assert "main-rerun [--dry-run]" in out and "MERGES ONLY" in out


def test_state_round_trips(q):
    q.update(main_reruns={"1": {"run": 1}})
    assert json.loads(q.state_path().read_text())["main_reruns"] == {"1": {"run": 1}}


FACE = "tests/e2e/notifier_face_test.py::test_bundled_face_renders_in_an_owned_private_directory_without_posting"
MACOS_SPECIFIC = "macOS-specific tests"
STAMP = "2026-11-08T01:02:03.4567890Z "


def with_test_entry(q):
    q.known.write_text(KNOWN + f'\n[[known]]\nmatch = "notifier_face_test.py::test_bundled_face_renders"\nbead = "4mgv"\nadded = 2026-11-08\nuntil = 2099-01-01\n')


def test_a_job_whose_only_failing_test_is_known_is_rerun(q, capsys):
    with_test_entry(q)
    q.fake = GH(first=(MACOS_SPECIFIC,), log=f"{STAMP}FAILED {FACE} - subprocess.TimeoutExpired: swiftc\n{STAMP}1 failed, 63 passed\n")
    assert q.main(["main-rerun"]) == 0
    assert f"{MACOS_SPECIFIC}=KNOWN(4mgv)" in capsys.readouterr().out
    assert len(reruns(q)) == 1


@pytest.mark.parametrize("log", [
    f"{STAMP}FAILED {FACE} - TimeoutExpired\n{STAMP}FAILED tests/e2e/other_test.py::test_new - AssertionError\n",
    f"{STAMP}error: the notifier did not build\n",  # failed without naming a test
    "",  # unreadable or empty log
])
def test_a_job_with_any_unknown_or_unnamed_failure_stays_unknown(q, capsys, log):
    with_test_entry(q)
    q.fake = GH(first=(MACOS_SPECIFIC,), log=log)
    assert q.main(["main-rerun"]) == 1
    assert f"UNKNOWN failing job(s) {MACOS_SPECIFIC}" in capsys.readouterr().err
    assert not reruns(q)


@pytest.mark.parametrize(("events", "blocked"), [
    ([], True),
    ([{"action": "pause-request"}], True),
    ([{"action": "pause-request"}, {"action": "pause"}], False),
    ([{"action": "pause"}, {"action": "resume"}], True),
])
def test_only_an_unfinished_unpaused_release_guards_the_rerun(q, home, events, blocked):
    (home / ".cache").mkdir(exist_ok=True)
    (statedir.root() / "release.json").write_text(json.dumps({"version": "0.9.30", "freeze_token": "t", "hold_events": events}))
    reason = q.release_active()
    assert (reason == "sm release resume 0.9.30 unfinished") is blocked


SHARDED = (["CI OK"] + [f"Test (ubuntu-24.04, Python {v}, {lvl})" for v in ("3.12", "3.13", "3.14", "3.15") for lvl in ("unit", "integration")]
           + [f"Test (macos-latest, Python {v}, {lvl})" for v in ("3.12", "3.13", "3.14", "3.15") for lvl in ("unit", "integration")])


@pytest.mark.parametrize(("names", "complete"), [
    (MATRIX, True),
    (SHARDED, True),  # core #592 split each Linux Python into a unit and an integration cell
    ([n for n in SHARDED if n != "Test (ubuntu-24.04, Python 3.13, integration)"], False),
    ([n for n in MATRIX if n != "Test (ubuntu-24.04, Python 3.15)"], False),
])
def test_full_pass_accepts_either_complete_linux_layout(q, names, complete):
    rows = [{"name": n, "status": "completed", "conclusion": "success"} for n in names]
    assert q.full_pass(rows) is complete



INFRA_JOB = "End-to-end web-1 (ubuntu-24.04)"
INFRA_STEP = "Install browser runtimes and host dependencies"


@pytest.fixture
def infra(q, monkeypatch, tmp_path):
    """A reviewed setup incident cannot classify any other run or failure."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    q.fake = GH(first=(INFRA_JOB,))
    first = jobs(q.fake.first)
    for job in first["jobs"]:
        job.update(run_id=RID, run_attempt=1, head_sha=SHA)
        if job["conclusion"] == "failure":
            step_name = INFRA_STEP if job["name"] == INFRA_JOB else "Fail if any required job failed"
            job["steps"] = [{"number": 6, "name": step_name, "status": "completed", "conclusion": "failure"}]

    receipt = tmp_path / "known-main-infra.toml"
    receipt.write_text(f'''
run = {RID}
sha = "{SHA}"
attempt = 1
total_jobs = {first["total_count"]}
cause = "pinned-tool-http-504"
bead = "p7tv"
review = "independent failed-job log review"
reviewed_at = {(now - timedelta(hours=1)).isoformat()}
until = {(now + timedelta(hours=1)).isoformat()}
''' + "".join(f'''
[[failures]]
id = {j["id"]}
name = "{j["name"]}"
step_number = 6
step_name = "{j["steps"][0]["name"]}"
''' for j in first["jobs"] if j["conclusion"] == "failure"))
    monkeypatch.setattr(q, "KNOWN_MAIN_INFRA", receipt)
    monkeypatch.setattr(q, "attempt_jobs", lambda rid, attempt: first["jobs"] if attempt == 1 else jobs(q.fake.second)["jobs"])
    return q, receipt, first["jobs"]


def test_scoped_incident_authorizes_existing_rerun_and_merge_restoration(infra):
    q, receipt, _ = infra
    assert q.main(["main-rerun", "--dry-run"]) == 0
    assert not reruns(q)
    assert q.main(["main-rerun"]) == 0
    assert len(reruns(q)) == 1
    assert q.load()["main_reruns"][str(RID)]["infra_receipt_sha256"]
    found = restored(q)
    assert found == {"run": RID, "sha": SHA, "jobs": [INFRA_JOB], "beads": ["p7tv"]}
    assert restored(q, second=(INFRA_JOB,)) == {}
    receipt.write_text(receipt.read_text().replace("independent failed-job log review", "different receipt"))
    assert restored(q) == {}


def test_scoped_incident_does_not_recognize_a_rerun_started_outside_the_queue(infra):
    q, _, _ = infra
    assert restored(q) == {}


@pytest.mark.parametrize("change", [
    "run", "head", "attempt", "job", "step", "expired", "future", "no review",
    "duration", "extra failure", "cancelled job", "duplicate job", "test failure",
])
def test_scoped_incident_refuses_changed_or_unreviewed_evidence(infra, change):
    q, receipt, first = infra
    row = next(j for j in first if j["name"] == INFRA_JOB)
    content = receipt.read_text()
    if change == "run":
        content = content.replace(f"run = {RID}", f"run = {RID + 1}")
    elif change == "head":
        content = content.replace(SHA, "f" * 40)
    elif change == "attempt":
        row["run_attempt"] = 2
    elif change == "job":
        row["id"] += 800
    elif change == "step":
        row["steps"][0]["number"] = 7
    elif change == "expired":
        content = content.replace(next(x for x in content.splitlines() if x.startswith("until")), "until = 2000-01-01T00:00:00+00:00")
    elif change == "future":
        content = content.replace(next(x for x in content.splitlines() if x.startswith("reviewed_at")), "reviewed_at = 2099-01-01T00:00:00+00:00")
    elif change == "no review":
        content = content.replace('review = "independent failed-job log review"', 'review = " "')
    elif change == "duration":
        content = content.replace(next(x for x in content.splitlines() if x.startswith("until")), "until = 2099-01-01T00:00:00+00:00")
    elif change == "extra failure":
        extra = next(j for j in first if j["conclusion"] == "success")
        extra.update(conclusion="failure", steps=row["steps"])
    elif change == "cancelled job":
        row["conclusion"] = "cancelled"
    elif change == "duplicate job":
        next(j for j in first if j["conclusion"] == "success")["id"] = row["id"]
    elif change == "test failure":
        # Even a matching receipt cannot enroll a step that runs the product tests.
        row["steps"][0]["name"] = "Run make test-e2e"
        content = content.replace(INFRA_STEP, "Run make test-e2e")
    receipt.write_text(content)
    assert q.main(["main-rerun", "--dry-run"]) == 1
    assert not reruns(q)


@pytest.mark.parametrize("guard", ["freeze", "release"])
def test_scoped_incident_still_obeys_queue_holds(infra, monkeypatch, guard):
    q, _, _ = infra
    if guard == "freeze":
        monkeypatch.setattr(q.hzlib, "merge_frozen", lambda *a: True)
    else:
        q.fake.releases = ("release/v0.9.35",)
    assert q.main(["main-rerun", "--dry-run"]) == 1
    assert not reruns(q)


def add_overlapping_known_job(q):
    q.known.write_text(q.known.read_text() + f'''
[[known]]
match = "{INFRA_JOB}"
bead = "cmr"
until = 2099-01-01
''')


@pytest.mark.parametrize("change", ["missing", "expired", "changed"])
def test_recorded_incident_never_downgrades_to_generic_known(infra, change):
    q, receipt, _ = infra
    assert q.main(["main-rerun"]) == 0
    add_overlapping_known_job(q)
    assert restored(q)["beads"] == ["p7tv"]
    if change == "missing":
        receipt.unlink()
    elif change == "expired":
        text = receipt.read_text()
        end = next(line for line in text.splitlines() if line.startswith("until = "))
        receipt.write_text(text.replace(end, "until = 2000-01-01T00:00:00+00:00"))
    else:
        receipt.write_text(receipt.read_text() + "\n# edited since dispatch\n")
    assert restored(q) == {}


def test_manual_and_automatic_rerun_share_queue_lock(infra, capsys):
    q, _, _ = infra
    with q.acquire_lock(block=False):
        # This is the runner's lock, held before tick. A manual invocation may only dry-run.
        assert q.main(["main-rerun", "--dry-run"]) == 0
        assert q.main(["main-rerun"]) == 1
        assert "queue lock unavailable" in capsys.readouterr().err
        assert not reruns(q)
        assert q._main_rerun(auto=True) == 0
    assert q.main(["main-rerun"]) == 1
    assert "already rerun" in capsys.readouterr().err
    assert len(reruns(q)) == 1


def test_manual_plan_and_dispatch_run_under_lock(infra, monkeypatch):
    q, _, _ = infra
    original = q.main_rerun_plan

    def plan():
        assert q.acquire_lock(block=False) is None
        return original()

    def dispatch(*args):
        assert q.acquire_lock(block=False) is None
        return q.fake.gh(*args)

    monkeypatch.setattr(q, "main_rerun_plan", plan)
    monkeypatch.setattr(q.hzlib, "gh", dispatch)
    assert q.main(["main-rerun"]) == 0
    assert len(reruns(q)) == 1


def test_receipt_is_revalidated_after_release_guard(infra, monkeypatch, capsys):
    q, receipt, _ = infra

    def release_guard():
        assert q.acquire_lock(block=False) is None
        receipt.write_text(receipt.read_text() + "\n# replaced while checking release state\n")
        return ""

    monkeypatch.setattr(q, "release_active", release_guard)
    assert q.main(["main-rerun"]) == 1
    assert "receipt changed or expired" in capsys.readouterr().err
    assert not reruns(q) and not q.load()["main_reruns"]


def test_freeze_is_revalidated_after_release_guard(infra, monkeypatch, capsys):
    q, _, _ = infra

    def release_guard():
        monkeypatch.setattr(q.hzlib, "merge_frozen", lambda: True)
        return ""

    monkeypatch.setattr(q, "release_active", release_guard)
    assert q.main(["main-rerun"]) == 1
    assert "release guard: merge freeze is on" in capsys.readouterr().err
    assert not reruns(q)


def test_scoped_request_with_uncertain_result_cannot_retry_or_restore(infra, monkeypatch):
    q, _, _ = infra
    calls = []

    def dispatch(*args):
        calls.append(args)
        return 1, "response lost"

    monkeypatch.setattr(q.hzlib, "gh", dispatch)
    assert q.main(["main-rerun"]) == 1
    assert q.main(["main-rerun"]) == 1
    assert len(calls) == 1
    add_overlapping_known_job(q)
    assert restored(q) == {}


def test_runner_does_not_tick_without_the_queue_lock(q, monkeypatch, capsys):
    monkeypatch.setattr(q, "acquire_lock", lambda **_: None)
    monkeypatch.setattr(q, "tick", lambda: pytest.fail("unlocked queue tick"))
    assert q.main(["run", "--once"]) == 1
    assert "cannot acquire queue lock" in capsys.readouterr().err
    assert not reruns(q)
