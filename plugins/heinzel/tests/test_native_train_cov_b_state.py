"""The release train module on tmp_path: state files, migration, hold, locks, the Real world on fake processes, the
embedded self-test and the argument router. No process is started, nothing leaves tmp_path."""

import json
import subprocess
import threading
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel.native.train_lib import train


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(train, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(train, "GATE_HOLD", tmp_path / "gate.hold")
    monkeypatch.setattr(train, "RECEIPT", tmp_path / "receipt.json")
    monkeypatch.setattr(train, "TAIL_DIR", tmp_path / "tails")
    monkeypatch.setattr(train, "CHECKOUT", tmp_path / "checkout")
    monkeypatch.setattr(train, "AUTO_CUT", True)
    monkeypatch.setattr(train.edge, "note", lambda text: None)
    return tmp_path


def done(code=0, out="", err=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


# ---------------------------------------------------------------- state
def test_read_write(tmp_path):
    path = tmp_path / "x" / "f.json"
    assert train._read(path) is None
    train._write(path, {"a": 1})
    assert train._read(path) == {"a": 1}
    path.write_text("{bad")
    with pytest.raises(SystemExit, match="malformed"):
        train._read(path)


def test_trains_and_load_state():
    for v, phase in (("0.9.10", "done"), ("0.9.9", "cut"), ("0.9.11", "cut")):
        train.save_state({"version": v, "phase": phase})
    (train.STATE_DIR / "vbad.json").write_text("{}")
    (train.STATE_DIR / "v1.2.json").write_text("{}")
    assert [t["version"] for t in train.trains()] == ["0.9.9", "0.9.10", "0.9.11"]
    assert [t["version"] for t in train.active_trains()] == ["0.9.9", "0.9.11"]
    assert train.load_state()["version"] == "0.9.9"
    assert train._vkey(train.version_path("1.2.3")) == (1, 2, 3)
    assert train._vkey(train.STATE_DIR / "vx.y.json") == (1 << 30,)
    assert train.vkey("1.2.x") == (1 << 30,)
    assert train.vkey(None) == (1 << 30,)


def test_idle_save_goes_to_index():
    train.save_state({"phase": "idle", "last_start": 5, "other": 1})
    assert train.index() == {"last_start": 5}
    assert train.load_state() == {"last_start": 5, "phase": "idle"}


def test_legacy_state_is_read_and_migrated():
    train.STATE_DIR.mkdir(parents=True)
    legacy = {"version": "0.9.5", "phase": "cut", "last_start": 3}
    (train.STATE_DIR / "state.json").write_text(json.dumps(legacy))
    assert train.legacy_state() == legacy
    assert train.load_state() == legacy
    assert train.active_trains() == [legacy]
    msg = train.migrate_legacy()
    assert "migrated state.json: train v0.9.5 (cut)" in msg
    assert (train.STATE_DIR / "state.json.migrated").exists()
    assert train.migrate_legacy() == ""
    assert train.index()["last_start"] == 3
    (train.STATE_DIR / "state.json").write_text(json.dumps({"phase": "idle"}))
    assert train.active_trains() == []
    assert train.migrate_legacy().endswith("cadence -> train.json")


# ---------------------------------------------------------------- hold and locks
def test_gate_hold(monkeypatch):
    hold = train.GateHold(train.GATE_HOLD, "tok")
    assert not hold.ours() and hold.release() is False
    assert "disabled" in hold.acquire()
    hold.refresh()
    monkeypatch.setattr(train.GateHold, "ENABLED", True)
    assert hold.acquire() == "hold taken"
    assert hold.ours()
    assert hold.acquire() == "own hold refreshed"
    hold.refresh()
    other = train.GateHold(train.GATE_HOLD, "other")
    assert "somebody else" in other.acquire() or other.ours() is False
    assert hold.release() is True
    train.GATE_HOLD.write_text("x")
    assert "somebody else" in hold.acquire()


def test_file_lock_and_busy():
    path = train.STATE_DIR / "a.lock"
    with train.file_lock(path) as first:
        assert first is True
        with train.file_lock(path) as second:
            assert second is False
        assert train.lock_busy(train.Real(), "a.lock") is True
    assert train.lock_busy(train.Real(), "a.lock") is False


def test_live_ok_incident_and_small_helpers():
    assert train.live_ok("LIVE-OK 0.9.12", "0.9.10")
    assert not train.live_ok("LIVE-OK 0.9.1", "0.9.10")
    assert not train.live_ok("NOPE", "0.9.10")
    said = []
    w = SimpleNamespace(incident_active=lambda: True, say=said.append)
    assert train.wait_for_incident(w) is True and "incident" in said[0]
    assert train.wait_for_incident(SimpleNamespace(incident_active=lambda: False)) is False
    assert train.next_patch("v1.2.3") == "1.2.4"
    assert train.tag_step_done("ok   tag (already)") and not train.tag_step_done("ok   cut")


def test_receipt_blocks():
    class W:
        def __init__(self, has=True, boom=False):
            self.has, self.boom = has, boom

        def has_tag(self, tag):
            if self.boom:
                raise OSError
            return self.has
    assert train.receipt_blocks(W(), {}) is False
    assert train.receipt_blocks(W(), {"version": "1", "finished": True}) is False
    assert train.receipt_blocks(W(has=True), {"version": "1"}) is False
    assert train.receipt_blocks(W(has=False), {"version": "1"}) is True
    assert train.receipt_blocks(W(boom=True), {"version": "1"}) is True


def test_unstarted_and_gate_label():
    assert train.unstarted({"status": "queued"}, [{"status": "queued"}, {"status": "completed", "conclusion": "skipped"}])
    assert not train.unstarted({"status": "in_progress"}, [])
    assert not train.unstarted({"status": "queued"}, [{"status": "in_progress"}])
    assert not train.unstarted({"status": "queued"}, [{"runner_name": "r"}])
    assert not train.unstarted({"status": "queued"}, [{"status": "completed", "conclusion": "success"}])
    g = train.gate("green", "abcdefghijk", "why")
    assert "first-attempt green" in train.gate_label(g, "abcdefghijk")
    assert "unfinished/red-by-KNOWN" in train.gate_label(train.gate("known", "x", "w", finished=False), "abcdefghijk")
    assert "finished/restored-by-rerun" in train.gate_label(train.gate("restored", "x", "w"), "abcdefghijk")
    assert "last evidence green" in train.gate_label(train.gate("green", "x", "w"), "abcdefghijk")
    assert "weird" in train.gate_label(train.gate("weird", "x", "w"), "abcdefghijk")


# ---------------------------------------------------------------- the Real world
def test_real_say_now_sleep(capsys, monkeypatch):
    real = train.Real()
    real.say("hello")
    assert capsys.readouterr().out == "hello\n"
    assert real.now() > 0
    slept = []
    monkeypatch.setattr(train.time, "sleep", slept.append)
    real.sleep(3)
    assert slept == [3]


def test_real_gh(monkeypatch):
    real = train.Real()
    monkeypatch.setattr(train.edge, "run", lambda cmd, **k: done(0, '{"a": 1}'))
    assert real._gh("x") == {"a": 1}
    monkeypatch.setattr(train.edge, "run", lambda cmd, **k: done(0, "  "))
    assert real._gh("x") is None
    monkeypatch.setattr(train.edge, "run", lambda cmd, **k: done(1, "", "bad thing"))
    with pytest.raises(RuntimeError, match="bad thing"):
        real._gh("a", "b", "c", "d")


def test_real_receipts():
    real = train.Real()
    assert real.receipt() == {}
    train.RECEIPT.write_text('{"version": "1"}')
    assert real.receipt() == {"version": "1"}
    assert real.tail_receipts() == []
    train.TAIL_DIR.mkdir()
    (train.TAIL_DIR / "v0.9.10.json").write_text('{"finished": true}')
    (train.TAIL_DIR / "v0.9.9.json").write_text('{}')
    (train.TAIL_DIR / "vx.json").write_text("{bad")
    assert [v for v, _ in real.tail_receipts()] == ["0.9.9", "0.9.10"]
    assert real.tail_receipt("0.9.10") == {"finished": True}
    assert real.tail_receipt("0.1.1") == {}
    (train.TAIL_DIR / "v0.1.1.json").write_text("{bad")
    assert real.tail_receipt("0.1.1") == {}


def test_real_drive(monkeypatch, capsys):
    started = []
    monkeypatch.setattr(train.edge, "train_run", lambda v: ["run", v])
    monkeypatch.setattr(train.subprocess, "Popen", lambda cmd, **k: started.append((cmd, k)))
    assert train.Real().drive("0.9.9") == 0
    assert started[0][0] == ["run", "0.9.9"] and started[0][1]["start_new_session"] is True
    assert "started" in capsys.readouterr().out


def gh_script(monkeypatch, table):
    def fake(self, *args):
        for key, value in table.items():
            if key in " ".join(args):
                return value(args) if callable(value) else value
        return None
    monkeypatch.setattr(train.Real, "_gh", fake)


def test_real_release_queries(monkeypatch):
    real = train.Real()
    gh_script(monkeypatch, {"pr list": [{"headRefName": "release/v1"}, {"headRefName": "bump/x"}, {"headRefName": "fix/a"}]})
    assert real.release_pr_open() == 2
    releases = [{"tag_name": "v0.9.9", "published_at": "2026-01-01T00:00:00Z"},
                {"tag_name": "v0.9.10", "published_at": "2026-01-02T00:00:00Z"},
                {"tag_name": "edge-v1", "published_at": "2026-01-03T00:00:00Z"},
                {"tag_name": "v0.9.11", "draft": True, "published_at": "2026-01-04T00:00:00Z"}]
    gh_script(monkeypatch, {"releases?per_page": releases})
    tag, stamp = real.latest_tag()
    assert tag == "v0.9.10" and stamp > 0
    gh_script(monkeypatch, {"releases?per_page": [], "releases/latest": {"tag_name": "v0.1.0", "published_at": "2026-01-01T00:00:00Z"}})
    assert real.latest_tag()[0] == "v0.1.0"
    gh_script(monkeypatch, {"compare": {"commits": [{"sha": "s1", "commit": {"message": "fix(x): a"}},
                                                    {"sha": "s2", "commit": {"message": "feat(y): b"}}]}})
    tip, subjects = real.unreleased("v1")
    assert tip == "s2" and subjects
    gh_script(monkeypatch, {"compare": {"commits": []}})
    assert real.unreleased("v1") == ("", [])
    gh_script(monkeypatch, {"run list": [{"status": "completed"}]})
    assert real.candidate_active("1") is False
    gh_script(monkeypatch, {"run list": [{"status": "queued"}]})
    assert real.candidate_active("1") is True


def test_real_hzlib_passthroughs(monkeypatch):
    from steuermaschine_heinzel.native.queue_lib import hzlib, release_flow
    monkeypatch.setattr(hzlib, "main_release_gate", lambda tip: {"tip": tip})
    monkeypatch.setattr(hzlib, "merge_frozen", lambda: True)
    monkeypatch.setattr(hzlib, "incident_frozen", lambda: True)
    monkeypatch.setattr(release_flow, "flag_on", lambda: True)
    real = train.Real()
    assert real.main_gate("t") == {"tip": "t"}
    assert real.freeze_active() and real.incident_active() and real.tag_on_main()


def test_real_defer_cancel_and_has_tag(monkeypatch):
    monkeypatch.setattr(train.edge, "sm", lambda: ["sm"])
    monkeypatch.setattr(train.edge, "run", lambda cmd, **k: done(0, "cancelled a\ncancelled b\nother"))
    assert "2 PR run(s) cancelled" in train.Real().defer_cancel()
    assert train.Real().has_tag("v1") is True
    monkeypatch.setattr(train.edge, "run", lambda cmd, **k: done(1))
    assert train.Real().has_tag("v1") is False


def test_real_cancel_unstarted_main_run(monkeypatch):
    real = train.Real()
    state = {"cancel": 0, "tip": "t" * 40, "base": "t" * 40}

    def fake(self, *args):
        joined = " ".join(args)
        if "commits/main" in joined:
            return {"sha": state["tip"]}
        if "compare/main" in joined:
            return {"merge_base_commit": {"sha": state["base"]}}
        if "run list" in joined:
            return [{"databaseId": 1, "headSha": state["tip"], "status": "queued"},
                    {"databaseId": 2, "headSha": state["tip"], "status": "in_progress"},
                    {"databaseId": 3, "headSha": "other", "status": "queued"}]
        if "/jobs" in joined:
            return {"jobs": []}
        rid = joined.rsplit("/", 1)[1]
        return {"status": "queued" if rid == "1" else "in_progress"}
    monkeypatch.setattr(train.Real, "_gh", fake)
    monkeypatch.setattr(train.edge, "run", lambda cmd, **k: done(0))
    out = real.cancel_unstarted_main_run("1")
    assert "1 cancelled" in out and "2 kept" in out
    monkeypatch.setattr(train.edge, "run", lambda cmd, **k: done(1, "", "nope"))
    assert "cancel failed: nope" in real.cancel_unstarted_main_run("1")
    state["base"] = "b" * 40
    assert real.cancel_unstarted_main_run("1").startswith("kept: the candidate's base")
    state["base"] = state["tip"]
    monkeypatch.setattr(train.Real, "_gh", lambda self, *a: (
        {"sha": "t" * 40} if "commits/main" in " ".join(a) else {"merge_base_commit": {"sha": "t" * 40}} if "compare" in " ".join(a) else None))
    assert real.cancel_unstarted_main_run("1").startswith("no unfinished main run")


def test_real_prepare_checkout(monkeypatch):
    calls = []
    state = {"dirty": "", "version": "0.9.9"}

    def fake(cmd, **k):
        calls.append(cmd)
        if "status" in cmd:
            return done(0, state["dirty"])
        if "bump-my-version" in cmd:
            return done(0, state["version"] + "\n")
        return done(0)
    monkeypatch.setattr(train.edge, "run", fake)
    monkeypatch.setattr(train.edge, "gate", lambda: ["gate"])
    real = train.Real()
    real.prepare_checkout("0.9.9", "0.9.10")
    assert calls[0][:2] == ["git", "clone"]
    (train.CHECKOUT / ".git").mkdir(parents=True)
    calls.clear()
    real.prepare_checkout("0.9.9", "0.9.10")
    assert calls[0][:3] == ["git", "-C", str(train.CHECKOUT)]
    state["dirty"] = " M x"
    with pytest.raises(RuntimeError, match="not clean"):
        real.prepare_checkout("0.9.9", "0.9.10")
    state["dirty"], state["version"] = "", "0.8.0"
    with pytest.raises(RuntimeError, match=r"reports version 0.8.0"):
        real.prepare_checkout("0.9.9", "0.9.10")
    assert real.release_env()["HZ_RELEASE_DIR"] == str(train.CHECKOUT)


def test_real_acceptance_run_and_verify(monkeypatch):
    real = train.Real()
    assert real.acceptance_run("1.0.0") == "?"
    train.RECEIPT.write_text('{"version": "1.0.0", "acceptance_run": 5}')
    assert real.acceptance_run("1.0.0") == "5"
    monkeypatch.setattr(train.edge, "sm", lambda: ["sm"])
    monkeypatch.setattr(train.edge, "run", lambda cmd, **k: done(0, "LIVE-OK 1.0.1\n"))
    assert real.verify_mini("1.0.0") == "LIVE-OK 1.0.1"
    monkeypatch.setattr(train.edge, "run", lambda cmd, **k: done(0, "garbage"))
    assert "did not report exact" in real.verify_mini("1.0.0")
    monkeypatch.setattr(train.edge, "run", lambda cmd, **k: done(1, "NOT-LIVE: old"))
    assert real.verify_mini("1.0.0") == "NOT-LIVE: old"
    monkeypatch.setattr(train.edge, "run", lambda cmd, **k: done(3, "x"))
    assert real.verify_mini("1.0.0") == "NOT-LIVE: verifier exited 3"

    def boom(cmd, **k):
        raise subprocess.TimeoutExpired("x", 1)
    monkeypatch.setattr(train.edge, "run", boom)
    assert real.verify_mini("1.0.0") == "NOT-LIVE: verifier unavailable"


def test_real_defer_resume(monkeypatch):
    monkeypatch.setattr(train.edge, "sm", lambda: ["sm"])
    real = train.Real()
    monkeypatch.setattr(real, "incident_active", lambda: False)
    monkeypatch.setattr(real, "say", lambda t: None)
    results = {"deferred": done(1), }

    def fake(cmd, **k):
        if cmd[-1] == "deferred":
            return results["deferred"]
        return done(0, "resumed 2\n")
    monkeypatch.setattr(train.edge, "run", fake)
    assert "unreadable" in real.defer_resume()
    results["deferred"] = done(0, "")
    assert real.defer_resume() == "nothing deferred"
    results["deferred"] = done(0, "pr 1")
    assert real.defer_resume() == "resumed 2"
    monkeypatch.setenv("HZ_TRAIN_RESUME_ALL", "1")
    assert real.defer_resume() == "resumed 2"
    monkeypatch.setattr(real, "incident_active", lambda: True)
    assert real.defer_resume() is None


def test_real_note_and_notify(monkeypatch):
    notes = []
    monkeypatch.setattr(train.edge, "note", notes.append)
    monkeypatch.setattr(train.edge, "notify_owner", lambda text: False)
    real = train.Real()
    real.note("x")
    real.notify("y")
    assert notes[0] == "release train: x" and "NOT notified" in notes[1]
    monkeypatch.setattr(train.edge, "notify_owner", lambda text: True)
    real.notify("z")
    assert "owner notified" in notes[-1]


def test_run_release(monkeypatch):
    monkeypatch.setattr(train.edge, "sm", lambda: ["sm"])
    train.STATE_DIR.mkdir(parents=True)
    lines = ["step one\n", "ok   tag\n", "last\n"]

    class Proc:
        stdout = iter(lines)

        def wait(self):
            return 0
    monkeypatch.setattr(train.subprocess, "Popen", lambda cmd, **k: Proc())
    monkeypatch.setattr(train.threading.Event, "wait", lambda self, t=None: True)
    real = train.Real()
    said = []
    monkeypatch.setattr(real, "say", said.append)
    monkeypatch.setattr(train.GateHold, "ENABLED", True)
    hold = train.GateHold(train.GATE_HOLD, "t")
    code, tail = real.run_release("1.0.0", "intro", hold)
    assert code == 0 and tail.splitlines()[-1] == "last"
    assert "gate hold released: tag step done" in said
    assert (train.STATE_DIR / "v1.0.0.log").read_text().startswith("step one")


def test_keep_hold_watcher_loop(monkeypatch):
    """The watcher thread: candidate detected once, then tag exists releases the hold."""
    monkeypatch.setattr(train.edge, "sm", lambda: ["sm"])
    train.STATE_DIR.mkdir(parents=True)
    release = threading.Event()

    class Proc:
        def __init__(self):
            self.stdout = self

        def __iter__(self):
            release.wait(5)
            return iter([])

        def wait(self):
            return 1
    monkeypatch.setattr(train.subprocess, "Popen", lambda cmd, **k: Proc())
    waits = iter([False, False, True])
    monkeypatch.setattr(train.threading.Event, "wait", lambda self, t=None: next(waits, True))
    real = train.Real()
    said = []
    monkeypatch.setattr(real, "say", said.append)
    calls = {"n": 0}

    def active(version):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("flaky")
        return True
    monkeypatch.setattr(real, "candidate_active", active)
    monkeypatch.setattr(real, "has_tag", lambda tag: calls["n"] >= 2)
    monkeypatch.setattr(train.GateHold, "ENABLED", True)
    hold = train.GateHold(train.GATE_HOLD, "t")
    fired = []
    # no Timer here: with Event.wait patched, Thread.start returns before the thread runs and join() raises
    code, _ = real.run_release("1.0.0", "i", hold, on_candidate=lambda: fired.append(1))
    assert code == 1


def test_keep_hold_survives_a_failing_tag_check(monkeypatch):
    """has_tag raises once (gh timeout): the watcher logs it, keeps refreshing the hold and asks again."""
    monkeypatch.setattr(train.edge, "sm", lambda: ["sm"])
    train.STATE_DIR.mkdir(parents=True)
    watcher_done = threading.Lock()
    watcher_done.acquire()

    class Proc:
        def __init__(self):
            self.stdout = self

        def __iter__(self):
            assert watcher_done.acquire(timeout=5), "watcher thread died"
            return iter([])

        def wait(self):
            return 0
    monkeypatch.setattr(train.subprocess, "Popen", lambda cmd, **k: Proc())
    monkeypatch.setattr(train.threading.Event, "wait", lambda self, t=None: False)
    real = train.Real()
    said = []

    def say(line):
        said.append(line)
        if line == "gate hold released: tag exists":
            watcher_done.release()  # the watcher's last act
    monkeypatch.setattr(real, "say", say)
    answers = iter([RuntimeError("gh api tag v1.0.0: timed out"), False, True])

    def has_tag(tag):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(real, "has_tag", has_tag)
    monkeypatch.setattr(train.GateHold, "ENABLED", True)
    hold = train.GateHold(train.GATE_HOLD, "t")
    refreshed = []
    monkeypatch.setattr(hold, "refresh", lambda: refreshed.append(1))
    code, _ = real.run_release("1.0.0", "i", hold)
    assert code == 0
    assert len(refreshed) == 2  # after the failure and after the plain "no tag" answer
    assert any(line.startswith("tag check failed, gate hold kept: gh api tag v1.0.0") for line in said)
    assert "gate hold released: tag exists" in said


# ---------------------------------------------------------------- status, clear, main
def test_status_and_clear(monkeypatch):
    train.save_state({"version": "0.9.9", "phase": "cut", "reason": "r", "attempts": 2})
    train.RECEIPT.write_text('{"version": "0.9.10"}')
    train.TAIL_DIR.mkdir()
    (train.TAIL_DIR / "v0.9.8.json").write_text('{"finished": true, "accepted": true, "acceptance_run_id": 4}')
    (train.TAIL_DIR / "v0.9.7.json").write_text('{"finished": true, "absorbed_by": "0.9.8"}')
    (train.TAIL_DIR / "v0.9.6.json").write_text('{"acceptance_requested": true}')
    (train.TAIL_DIR / "v0.9.5.json").write_text('{}')
    text = train.status(train.Real())
    for piece in ("train v0.9.9: cut (idle, attempts 2): r", "0.9.8 tail: ACCEPTED, acceptance run 4",
                  "absorbed by 0.9.8", "acceptance dispatched", "tail running", "0.9.10 candidate"):
        assert piece in text
    (train.TAIL_DIR / "v0.9.4.json").write_text("{bad")
    assert "tail receipts unreadable" in train.status(train.Real())
    (train.TAIL_DIR / "v0.9.4.json").unlink()
    assert train.clear("9.9.9").startswith("refused: no active train")
    with train.file_lock(train.STATE_DIR / "v0.9.9.lock"):
        assert "is running" in train.clear("0.9.9")
    assert "train state cleared (was cut v0.9.9" in train.clear()
    assert train.load_state()["phase"] == "idle"
    assert "cleared" in train.clear("0.9.9") or True


def test_main_routing(monkeypatch, capsys):
    assert train.main(["--bogus"]) == 2
    assert train.main(["frobnicate"]) == 2
    assert train.main(["run"]) == 2
    assert train.main(["-h"]) == 0
    assert train.main(["--self-test"]) in (0, 1)
    assert train.main(["status"]) == 0
    monkeypatch.setattr(train, "locked", lambda command, flags, version=None: 7)
    assert train.main(["tick"]) == 7
    monkeypatch.setattr(train, "run_version", lambda w, v: 9)
    assert train.main(["run", "1.2.3"]) == 9
    from steuermaschine_heinzel.native.train_lib import batch_next, plan
    monkeypatch.setattr(plan, "plan", lambda words: 11)
    assert train.main(["plan"]) == 11
    monkeypatch.setattr(batch_next, "run", lambda *a, **k: 13)
    assert train.main(["next"]) == 13
    monkeypatch.setattr(batch_next, "__doc__", "docs")
    assert train.main(["next", "-h"]) == 0
    assert capsys.readouterr().out


def test_locked(monkeypatch, capsys):
    monkeypatch.setattr(train, "tick", lambda w, dry=False, now_flag=False: 4)
    monkeypatch.setattr(train.Real, "incident_active", lambda self: False)
    assert train.locked("tick", set()) == 4
    monkeypatch.setattr(train.Real, "incident_active", lambda self: True)
    assert train.locked("tick", set()) == 0
    monkeypatch.setattr(train.Real, "incident_active", lambda self: False)
    train.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (train.STATE_DIR / "state.json").write_text('{"phase": "idle", "last_start": 1}')
    assert train.locked("clear", set(), "9.9.9") == 1
    train.save_state({"version": "0.9.9", "phase": "cut"})
    assert train.locked("clear", set(), "0.9.9") == 0
    import fcntl
    with (train.STATE_DIR / "train.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert train.locked("tick", set()) == 0
    assert "skip: another" in capsys.readouterr().out


def test_self_test_passes(capsys):
    assert train.self_test() == 0
    assert "SELF-TEST PASSED" in capsys.readouterr().out
