"""The verbs that absorb old commands: `sm why SUBJECT` picks the read by its subject, `sm ship PR` and `sm ship` for a
worktree of another repository, `sm review sonar [PR]`, `sm alarm list [--check]`. The reads themselves are the old
commands' functions; here they are replaced, so only the routing is tested."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from coord_kit import app_runtime

from steuermaschine.engine import Refuse
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, invoke
from steuermaschine_heinzel.native import alarm, ci_ops, ci_read, ship, ship_driver, why_subject
from steuermaschine_heinzel.native.alarm import AlarmRow, Alarms
from steuermaschine_heinzel.native.ci_read import CiWhy
from steuermaschine_heinzel.native.queue import QueueWhy
from steuermaschine_heinzel.native.release_read import ReleaseStatus
from steuermaschine_heinzel.native.ship import HeadResult
from steuermaschine_heinzel.native.ship_lib import prepare
from steuermaschine_heinzel.native.tasks_lib.beads import TaskShown
from steuermaschine_heinzel.native.why import CheckState, PullRequestWhy


def pr(number=7, red=()):
    return PullRequestWhy(number=number, state="OPEN", checks=[CheckState(name=n, latest="FAILURE" if n in red else "SUCCESS") for n in ("CI OK", "merge-gate")],
                          why="x")


@pytest.fixture
def reads(monkeypatch):
    calls = []
    monkeypatch.setattr(why_subject, "world", lambda: SimpleNamespace(repo="o/r", gh=SimpleNamespace(try_json=lambda argv, timeout=30: (calls.append(argv) or [{"number": 9}], ""))))
    monkeypatch.setattr(why_subject, "explain", lambda w, n: pr(n, red={7: ("CI OK", "merge-gate"), 8: ()}.get(n, ())))
    monkeypatch.setattr(ci_read, "explain_ci", lambda w, n: CiWhy(target=n, kind="run" if n > 99999 else "pr", cause=f"cause {n}"))
    monkeypatch.setattr(why_subject.rejudge, "sonar_head", lambda n: (_ for _ in ()).throw(RuntimeError("sonar down")))
    monkeypatch.setattr(why_subject.queue, "queue_why", lambda: QueueWhy(queued=[], merge_freeze=False, lines=["queue: empty"]))
    monkeypatch.setattr(why_subject.release_read, "release_status", lambda line=False: ReleaseStatus(active=True, version="0.9.55", lines=["release 0.9.55: tag"]))
    return calls


def why(tmp_path, *args):
    return invoke(app_runtime(tmp_path), ["why", *args])


def test_a_pr_with_red_checks_adds_the_ci_cause_and_sonar(tmp_path, reads):
    out = why(tmp_path, "7").stdout
    assert "CI\n  CAUSE    cause 7" in out and "SONAR    unknown: RuntimeError: sonar down" in out
    green = why(tmp_path, "8").stdout
    assert "CAUSE" not in green and "SONAR" not in green


def test_a_run_a_version_a_branch_and_nothing_named(tmp_path, reads):
    assert why(tmp_path, "44934734258").stdout.startswith("CAUSE    cause 44934734258")
    assert why(tmp_path, "v0.9.55").stdout == "release 0.9.55: tag\n"
    assert why(tmp_path, "0.9.54").stdout.startswith("v0.9.54 is not in flight; the release in flight is v0.9.55:")
    assert why(tmp_path, "fix/topic").stdout.startswith("PR 9 ") and reads[-1][:6] == ["pr", "list", "-R", "o/r", "--head", "fix/topic"]
    assert why(tmp_path).stdout == "QUEUE\n  queue: empty\nRELEASE\n  release 0.9.55: tag\n"


def test_nothing_named_keeps_what_could_be_read(tmp_path, reads, monkeypatch):
    monkeypatch.setattr(why_subject.queue, "queue_why", lambda: (_ for _ in ()).throw(Refuse("no queue state")))
    out = why(tmp_path).stdout
    assert "UNKNOWN  queue: no queue state" in out and "release 0.9.55: tag" in out


def test_a_bead_or_a_branch_without_a_slash(tmp_path, reads, monkeypatch):
    monkeypatch.setattr(why_subject.beads.Tracker, "show", lambda self, bead: TaskShown(bead=bead, text="the bead"))
    assert why(tmp_path, "lb7p").exit_code == 0
    monkeypatch.setattr(why_subject.beads.Tracker, "show", lambda self, bead: (_ for _ in ()).throw(RuntimeError("no bead")))
    assert why(tmp_path, "topic").stdout.startswith("PR 9 ")
    monkeypatch.setattr(why_subject, "world", lambda: SimpleNamespace(repo="o/r", gh=SimpleNamespace(try_json=lambda argv, timeout=30: ([], ""))))
    gone = why(tmp_path, "nothing")
    assert gone.exit_code == 2 and "no PR number, version, run id, branch or bead" in gone.stderr
    assert why(tmp_path, "fix/none").exit_code == 1


def test_why_main_reads_mains_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(why_subject.ci_evidence, "ci_main", lambda: TaskShown(bead="main", text="main green"))
    assert why(tmp_path, "main").exit_code == 0


def test_ship_pr_needs_live_and_queues_through_the_old_ship(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ship, "head_ship", lambda **kw: seen.append(kw) or HeadResult(action="ship", target=kw["pr"], verdict="queued", lines=["queued 801"]))
    rt = app_runtime(tmp_path)
    dry = invoke(rt, ["ship", "801"])
    assert dry.exit_code == 2 and "sm ship 801 --live" in dry.stderr and not seen
    done = invoke(rt, ["--format", "json", "ship", "801", "--live", "--set", "reviewed=review: FILE"])
    doc = json.loads(done.stdout)
    assert done.exit_code == 0 and doc["pr"] == 801 and doc["status"] == "queued" and seen[0]["reason"] == "review: FILE"


def test_ship_of_another_repository_opens_its_pr_through_the_old_route(tmp_path, monkeypatch):
    c = FakeCommands()
    c.on(["git", "-C", "/wt", "remote", "get-url", "origin"], Result(0, "git@github.com:helmut-hoffer-von-ankershoffen/heinzel-plugins.git\n"))
    c.on(["git", "-C", "/wt", "log", "--no-merges", f"--format=%s{prepare.FIELD}%b{prepare.RECORD}", "origin/main..HEAD"],
         Result(0, f"fix(audio): louder{prepare.FIELD}{prepare.RECORD}"))  # the commits of the branch: the title is derived from them
    seen = []
    monkeypatch.setattr(ship, "head_pr", lambda **kw: seen.append(kw) or HeadResult(action="pr", target="x", verdict="queued", lines=["opened PR #38"]))
    out = invoke(app_runtime(tmp_path, c), ["--format", "json", "ship", "/wt", "--live", "--set", "bead=abc"])
    doc = json.loads(out.stdout)
    assert doc["pr"] == 38 and doc["phase"] == "helmut-hoffer-von-ankershoffen/heinzel-plugins: queued" and seen[0]["title"] == "fix(audio): louder" and seen[0]["bead"] == "abc"
    assert seen[0]["live"] is True


def test_ship_of_another_repository_needs_a_title(tmp_path, monkeypatch):
    c = FakeCommands()
    c.on(["git", "-C", "/wt", "remote", "get-url", "origin"], Result(0, "https://github.com/helmut-hoffer-von-ankershoffen/heinzel-plugins\n"))
    c.on(["git", "-C", "/wt", "log", "-1", "--format=%s"], Result(1, ""))
    out = invoke(app_runtime(tmp_path, c), ["ship", "/wt"])
    assert out.exit_code == 2 and "no title" in out.stderr
    assert ship_driver.other_repo(SimpleNamespace(providers=SimpleNamespace(commands=c)), "/elsewhere") is None


def test_review_sonar_is_the_old_closure_for_one_pr(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ci_ops, "ci_sonar_close", lambda **kw: seen.append(kw) or ci_ops.SonarClose(live=False, project="p", pull_request=kw["pull_request"], open_before=0))
    out = invoke(app_runtime(tmp_path), ["review", "sonar", "485", "--issue", "A,B", "--issue", "C"])
    assert out.exit_code == 0 and seen == [{"config": None, "apply": False, "pull_request": 485, "issue": ["A", "B", "C"]}]


def test_alarm_list_adds_the_asks_and_with_check_the_deadlines(tmp_path, monkeypatch):
    monkeypatch.setattr(alarm, "alarm_check", lambda: Alarms(checked=True, alarms=[AlarmRow(level="WARN", key="k", text="slow")]))
    monkeypatch.setattr(alarm, "owner_asks", lambda: ["message 1"])
    monkeypatch.setattr(alarm, "deadlines", lambda: ["ACTION: crash"])
    out = invoke(app_runtime(tmp_path), ["alarm", "list", "--check"]).stdout
    assert out == "WARN slow [since ?; key k]\nASKS\n  message 1\nDEADLINES\n  ACTION: crash\n"


def test_the_asks_and_the_deadlines_degrade(monkeypatch):
    from steuermaschine_heinzel.native import tick
    from steuermaschine_heinzel.native.asks_lib import asks

    monkeypatch.setattr(asks, "audit", lambda *a: (_ for _ in ()).throw(FileNotFoundError("none")))
    assert alarm.owner_asks() == []
    monkeypatch.setattr(asks, "audit", lambda *a: (_ for _ in ()).throw(ValueError("broken")))
    assert alarm.owner_asks() == ["unknown: ValueError: broken"]
    monkeypatch.setattr(asks, "audit", lambda *a: SimpleNamespace(messages=[1], open=[], render=lambda: ["one", ""]))
    assert alarm.owner_asks() == ["one"]
    from steuermaschine.native import NativeCommand, NativeRuntime, using

    quiet = NativeCommand("tick", lambda raw=False: SimpleNamespace(render=lambda: ["OK quiet", "ACTION: x"]), "")
    with using(NativeRuntime(None, {}, None, commands=lambda: [quiet])):
        assert alarm.deadlines() == ["ACTION: x"]
    broken = NativeCommand("tick", lambda raw=False: (_ for _ in ()).throw(OSError("gone")), "")
    with using(NativeRuntime(None, {}, None, commands=lambda: [broken])):
        assert alarm.deadlines() == ["unknown: OSError: gone"]
    assert tick.tick


# ----------------------------------------------------------------------------- sm release VERSION
def test_release_refuses_a_typo_and_a_version_that_is_neither_current_nor_next(tmp_path, monkeypatch):
    from steuermaschine_heinzel.native import release_drive, release_read

    ran = []
    monkeypatch.setattr(release_drive, "release_resume", lambda **kw: ran.append(kw) or SimpleNamespace(render=lambda: ["ran"], exit_code=0, model_dump=lambda **k: {}))
    monkeypatch.setattr(release_read, "release_status", lambda line=False: ReleaseStatus(active=False))
    c = FakeCommands()
    c.on(["gh", "release", "list"], Result(0, json.dumps([{"tagName": "v0.9.53"}, {"tagName": "v0.9.52"}, {"tagName": "nightly"}])))
    rt = app_runtime(tmp_path, c)
    typo = invoke(rt, ["release", "0.9.5x"])
    assert typo.exit_code == 2 and "'0.9.5x' is no version" in typo.stderr and not ran
    far = invoke(rt, ["release", "0.9.80"])
    assert far.exit_code == 2 and "0.9.53, 0.9.54, 0.10.0" in far.stderr and "sm release 0.9.54" in far.stderr and not ran
    assert release_drive.allowed("", "") == [] and release_drive.released([]) == ""


def test_release_runs_the_driver_for_the_next_version_and_patch(tmp_path, monkeypatch):
    from steuermaschine_heinzel.native import release_drive, release_read

    monkeypatch.setattr(release_read, "release_status", lambda line=False: (_ for _ in ()).throw(Refuse("unreadable")))
    for version in ("0.9.54", "patch"):
        release_drive.guard(version, [{"tagName": "v0.9.53"}], "")
    release_drive.guard("0.9.60", [{"tagName": "v0.9.53"}], "0.9.60")  # the release in flight
    with pytest.raises(release_drive.Wait):
        release_drive.guard("0.9.54", None, "")
    seen = []
    monkeypatch.setattr(release_drive, "release_resume", lambda **kw: seen.append(kw) or "ran")
    monkeypatch.setattr(release_drive, "world", lambda: SimpleNamespace(repo="o/r", gh_json=lambda argv: [{"tagName": "v0.9.53"}]))
    assert release_drive.release_drive(version="0.9.54", absorb=False) == "ran" and release_drive.release_drive(version="9.9.9", absorb=True) == "ran"
    monkeypatch.setattr(release_drive, "world", lambda: SimpleNamespace(repo="o/r", gh_json=lambda argv: (_ for _ in ()).throw(RuntimeError("down"))))
    with pytest.raises(release_drive.Wait):
        release_drive.release_drive(version="0.9.54", absorb=False)


# ----------------------------------------------------------------------------- the working files in the journals
def test_mirror_puts_every_piece_into_its_subjects_journal_and_the_view_gives_it_back(tmp_path):
    from steuermaschine.subjects import Journals
    from steuermaschine_heinzel.native import journal_view

    files = tmp_path / "state"
    (files / "release").mkdir(parents=True)
    (files / "train").mkdir()
    (files / "queue.json").write_text(json.dumps({"items": [{"key": "801", "status": "queued"}, {"no": "key"}], "blocked": "main red"}))
    (files / "release" / "v0.9.53.json").write_text(json.dumps({"finished": True, "accepted": True}))
    (files / "train" / "v0.9.54.json").write_text(json.dumps({"version": "0.9.54", "phase": "cut"}))
    (files / "release.json").write_text(json.dumps({"version": "0.9.54", "step": "tag"}))
    (files / "alarm.json").write_text(json.dumps({"stall:801": {"cls": "ALERT", "text": "stall"}}))
    j = Journals(tmp_path / "journals")
    changed = journal_view.mirror(j, files, 1.0)
    assert "pr/801 queue" in changed and "version/0.9.54 train" in changed and journal_view.mirror(j, files, 2.0) == []
    view, old = journal_view.View(j), journal_view.Files(files)
    for name in ("queue_state", "merge_frozen", "hold_files", "active_trains", "release_tails", "release_receipt", "accepted_versions", "dropped_candidates", "alarm_state"):
        got, want = getattr(view, name)(), getattr(old, name)()
        assert (got == want) if name != "queue_state" else (got["items"] == [i for i in want["items"] if "key" in i] and got["blocked"] == "main red"), name
    assert view.release_holds() == []
    (files / "queue.json").write_text(json.dumps({"items": []}))
    journal_view.mirror(j, files, 3.0)
    assert view.queue_state()["items"] == [] and journal_view.mirror(None, files, 4.0) == []
    empty = journal_view.View(Journals(tmp_path / "none"))
    assert empty.queue_state()["readable"] is False and empty.release_receipt() is None and empty.alarm_state() == {}
    (files / "alarm.json").write_text("{")  # unreadable: the journal keeps the last good one
    journal_view.mirror(j, files, 5.0)
    assert view.alarm_state() == {"stall:801": {"cls": "ALERT", "text": "stall"}}


def test_mirrored_commands_never_fail_on_the_mirror(monkeypatch, capsys):
    from steuermaschine_heinzel.native import journal_view

    monkeypatch.setattr(journal_view, "mirror_now", lambda: (_ for _ in ()).throw(OSError("disk")))
    assert journal_view.mirrored(lambda x: x + 1)(1) == 2
    assert "subject journals were not updated (OSError: disk)" in capsys.readouterr().err


def test_go_reads_the_queue_from_the_journals(tmp_path):
    from steuermaschine.subjects import Journals
    from steuermaschine_heinzel.native import journal_view
    from steuermaschine_heinzel.native.go_lib import probes

    j = Journals(tmp_path / "journals")
    j.append("pr", "801", "queue", {"key": "801", "status": "queued", "added": 5}, 1.0)
    j.append("branch", "main", "queue", {"keys": ["801"], "blocked": ""}, 1.0)
    w = SimpleNamespace(journals=j, state_dir=tmp_path / "nothing", now=lambda: 10.0, settings={}, gh_json=lambda *a, **k: [], repo="o/r")
    assert isinstance(journal_view.source(w), journal_view.View)
    assert probes.probe_alarms(w) == {} and probes.probe_receipts(w) == [] and probes.probe_holds(w)["files"] == []
    assert probes.probe_release(w) == {"active": None, "latest_accepted": None, "dropped": []}


def test_the_plugin_brings_its_state_into_the_journals_at_migrate_state(tmp_path):
    from steuermaschine.subjects import JournalStore
    from steuermaschine_heinzel.plugin import plugin

    store = JournalStore(tmp_path)
    (tmp_path / "dash").mkdir()
    (tmp_path / "dash" / "queue.json").write_text(json.dumps({"items": [{"key": "9", "status": "queued"}]}))
    lines = plugin.sm_journal_migrate(SimpleNamespace(store=store, clock=SimpleNamespace(now=lambda: 1.0)), {"legacy_state_dir": str(tmp_path / "dash")})
    assert "pr/9 queue" in lines


def test_note_goes_to_the_log_of_the_day_not_the_tracking_document(tmp_path):
    doc = tmp_path / "AGENTS.md"
    doc.write_text("# Doc\n")
    rt = app_runtime(tmp_path, coord_document=str(doc), coord_log_dir=str(tmp_path / "log"))
    out = invoke(rt, ["--format", "json", "note", "0.9.54 dropped"])
    path = Path(json.loads(out.stdout)["document"])
    assert out.exit_code == 0 and path.parent == tmp_path / "log" and "0.9.54 dropped" in path.read_text() and doc.read_text() == "# Doc\n"


def test_the_driver_writes_its_own_gap_note_for_exit_1_and_70_and_runs_the_ios_stage_after_accepted(tmp_path, monkeypatch):
    from steuermaschine_heinzel.native import release_drive, release_read
    from steuermaschine_heinzel.native.release import ReleaseRun

    notes, stages = [], []
    monkeypatch.setattr(release_read, "release_status", lambda line=False: ReleaseStatus(active=False))
    monkeypatch.setattr(release_drive.journal, "note", lambda text: notes.append(text))
    monkeypatch.setattr(release_drive, "world", lambda: SimpleNamespace(repo="o/r", gh_json=lambda argv: [{"tagName": "v0.9.53"}]))
    monkeypatch.setattr(release_drive.release_ios, "after_accepted", lambda w, v: stages.append(v) or ["IOS-OK"])
    monkeypatch.setattr(release_drive, "release_resume", lambda **kw: ReleaseRun(action="resume", version="0.9.54", exit=1, stopped="compose failed"))
    release_drive.release_drive(version="0.9.54", absorb=False)
    assert notes == ["sm gap: `sm release 0.9.54` exited 1: compose failed"] and not stages
    monkeypatch.setattr(release_drive, "release_resume", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        release_drive.release_drive(version="0.9.54", absorb=False)
    assert notes[-1] == "sm gap: `sm release 0.9.54` exited 70: RuntimeError: boom"
    monkeypatch.setattr(release_drive, "release_resume", lambda **kw: ReleaseRun(action="resume", version="0.9.54", exit=75, stopped="WAITING"))
    release_drive.release_drive(version="0.9.54", absorb=False)
    assert len(notes) == 2  # exit 75 is not a gap
    ok = ReleaseRun(action="resume", version="0.9.54", exit=0, finished=True, accepted=True)
    monkeypatch.setattr(release_drive, "release_resume", lambda **kw: ok)
    assert release_drive.release_drive(version="0.9.54", absorb=False).ios == ["IOS-OK"] and stages == ["0.9.54"]
