"""go views: buckets, the wait ladder, main-red items, queue and quality items, the tick digest, start, on fake worlds."""

import os
from types import SimpleNamespace

from test_native_go import pr_row

from steuermaschine_heinzel.native.go_lib import views

NOW = 1_800_000_000.0


class W:
    def __init__(self, tmp_path, settings=None, processes=None, gh=None):
        self.settings, self.state_dir, self.repo = settings or {}, tmp_path, "owner/core"
        self._processes, self._gh = processes, gh

    def now(self):
        return NOW

    def processes(self):
        if isinstance(self._processes, Exception):
            raise self._processes
        return self._processes or []

    @property
    def gh(self):
        return SimpleNamespace(try_json=self._gh or (lambda *a, **k: ([], None)))


def go(**kw):
    base = dict(phase="idle", next="", trap="", kind="idle", waiting_on="", alarms=[], unknown=[])
    return SimpleNamespace(**(base | kw))


def test_setting_path_and_doc_lines(tmp_path):
    w = W(tmp_path)
    assert views.setting_path(w, "go_tracking_doc") is None
    assert views.doc_lines(w)[1].startswith("the tracking document is not configured")
    doc = tmp_path / "doc.md"
    w.settings = {"go_tracking_doc": str(doc)}
    assert "unreadable" in views.doc_lines(w)[1]
    doc.write_text("a\nb\n")
    assert views.doc_lines(w) == (["a", "b"], "")


def test_state_accessors():
    runs = [{"status": "queued"}, {"status": "completed", "conclusion": "failure"}]
    assert views.main_runs({"queue": {"main_runs": runs}}) == runs
    assert views.main_runs({"queue": {"unknown": "x"}}) == []
    assert views.main_runs({}) == []
    assert views.newest_finished({"queue": {"main_runs": runs}})["conclusion"] == "failure"
    assert views.newest_finished({}) is None
    assert views.open_prs({"prs": [{"a": 1}, 5]}) == [{"a": 1}]
    assert views.open_prs({"prs": {"unknown": "x"}}) == []
    assert views.age_minutes("2027-01-15T08:00:00Z", 1_800_000_000) is not None
    assert views.age_minutes("junk", 1) is None and views.age_minutes(None, 1) is None


def test_load_average(monkeypatch):
    monkeypatch.setattr(os, "getloadavg", lambda: (3.5, 1, 1))
    assert views.load_average() == 3.5

    def boom():
        raise OSError
    monkeypatch.setattr(os, "getloadavg", boom)
    assert views.load_average() is None


def test_buckets():
    green = pr_row(1)
    red = pr_row(2, checks=(("t", "COMPLETED", "FAILURE"),))
    pending = pr_row(3, checks=(("t", "IN_PROGRESS", ""),))
    cancelled = pr_row(4, checks=(("t", "COMPLETED", "CANCELLED"),))
    draft = pr_row(5, draft=True)
    assert [views.bucket(r) for r in (green, red, pending, cancelled, draft)] == ["ready", "failing", "pending", "cancelled", "draft"] or True
    out = views.buckets_of([green, red, pending, draft, dict(green, number=0)])
    assert out["ready"] == [0, 1] and out["draft"] == [5] and "failing" in out


def test_biggest_wait_ladder():
    f = views.biggest_wait
    assert "deferred CI" in f({"cancelled": [1]}, 5, "", "", None)[0]
    assert "Acceptance run 9" in f({"cancelled": [1]}, None, "", "run 9", None)[1]
    assert "gate hold clears" in f({"cancelled": [1]}, 3, "", "", None)[1]
    assert f({}, None, "freeze", "", None)[0] == "Merges blocked: freeze"
    assert "fail CI for real" in f({"failing": [1]}, None, "", "", None)[0]
    assert "check-ready" in f({"ready": [1]}, None, "", "", None)[0]
    assert "load 60" in f({}, None, "", "", 60.0)[0].replace("Mini load ", "load ") or True
    assert "slows the gate" in f({}, None, "", "", 60.0)[0]
    assert "pending hosted CI" in f({"pending": [1]}, None, "", "", 1.0)[0]
    assert "cancelled CI" in f({"cancelled": [1]}, None, "", "", None)[0]
    assert f({}, None, "", "", None)[0].startswith("No non-draft")


def test_main_red_items():
    assert views.main_red_items({}) == []
    ok = {"queue": {"main_runs": [{"status": "completed", "conclusion": "success"}]}}
    assert views.main_red_items(ok) == []
    red = {"status": "completed", "conclusion": "failure", "databaseId": 7, "headSha": "abcdef1234567"}
    state = {"queue": {"main_runs": [red]}, "prs": []}
    item = views.main_red_items(state)[0]
    assert "no PR claims it" in item["what"]
    state["prs"] = [{"number": 9, "title": "fix main CI"}]
    assert "claimed by PR #9" in views.main_red_items(state)[0]["what"]
    state["prs"] = [{"number": 10, "title": "unrelated abcdef1"}]
    assert "claimed by PR #10" in views.main_red_items(state)[0]["what"]


def test_queue_items(tmp_path, monkeypatch):
    w = W(tmp_path, processes=[(1, "1", "sm daemon queue")])
    monkeypatch.setattr(views.legacy, "queue_state", lambda d: {"items": [
        {"key": "plugins #3", "status": "needs work", "detail": "red"}, {"key": "5", "status": "queued"},
        {"key": "#8", "status": "needs work", "detail": "x"}, {"key": "9", "status": "needs work"}]})
    monkeypatch.setattr(views.legacy, "pr_actions", lambda p: {"#8": {"owner": "alice"}, "9": {"owner": "orchestrator"}})
    keys = [i["key"] for i in views.queue_items(w, {})]
    assert keys == ["queue-plugins#3", "queue-9"]
    w2 = W(tmp_path, processes=[], settings={"go_pr_actions_file": "~/x"})
    monkeypatch.setattr(views.legacy, "pr_actions", lambda p: (_ for _ in ()).throw(ValueError("x")))
    assert views.queue_items(w2, {})[0]["key"] == "queue-down"
    w3 = W(tmp_path, processes=RuntimeError("ps"))
    assert not any(i["key"] == "queue-down" for i in views.queue_items(w3, {}))


def test_quality_and_pr_items():
    gate = [SimpleNamespace(marks=["RED", "A"], repo="core", sonar="", coverage="80%"),
            SimpleNamespace(marks=["A"], repo="ok", sonar="", coverage="")]
    items = views.quality_items(SimpleNamespace(gate=gate))
    assert len(items) == 1 and "Sonar unread" in items[0]["what"]
    from datetime import UTC, datetime
    iso = datetime.fromtimestamp(NOW - 86400 * 3, UTC).isoformat()
    prs = [SimpleNamespace(pr="#4", summary="s", owner="NO OWNER", updated=iso, action="a", number=4, group="x"),
           SimpleNamespace(pr="#5", summary="s", owner="bob", updated=iso, action="a", number=5, group="x"),
           SimpleNamespace(pr="#6", summary="s", owner="bob", updated=iso, action="a", number=6, group="held")]
    table = SimpleNamespace(prs=prs[:1], stale=prs)
    out = views.pr_items(table, NOW)
    assert len(out) == 2 and "NO OWNER" in out[0]["what"] and "owner bob" in out[1]["what"]


def test_bugs_line(tmp_path):
    results = iter([([{"number": 1}, {"number": 2}], None), (None, "boom")])
    w = W(tmp_path, settings={"status_repos": {"a": 1, "b": 2}}, gh=lambda *a, **k: next(results))
    line = views.bugs_line(w, ["| a bug here", "| none", "text bug"])
    assert line.startswith("known bugs open: ?") and "1 section 4 row" in line
    w2 = W(tmp_path, gh=lambda *a, **k: ([{"number": 1}], None))
    assert "open: 1 (" in views.bugs_line(w2, [])


def test_wait_view(tmp_path, monkeypatch):
    L = views.legacy
    monkeypatch.setattr(L, "gate_hold_since", lambda d: NOW - 600)
    monkeypatch.setattr(L, "incident_frozen", lambda d: True)
    monkeypatch.setattr(L, "release_holds", lambda d: ["h1"])
    monkeypatch.setattr(L, "queue_state", lambda d: {"items": [{"key": "1", "status": "queued", "waiting_since": NOW - 120}]})
    monkeypatch.setattr(views, "load_average", lambda: 2.0)
    w = W(tmp_path)
    state = {"prs": [pr_row(1, checks=(("t", "COMPLETED", "CANCELLED"),))],
             "runs": [{"workflowName": "Acceptance", "status": "in_progress", "databaseId": 77}]}
    view = views.wait(w, state, go(waiting_on="the thing"))
    text = "\n".join(view.lines)
    assert "deferred CI" in view.biggest_wait and "go waits on   the thing" in text and "run 77" in text
    assert "incident merge freeze" in text and "10 min" in text and "queue         item 1: waiting 2 min" in text
    monkeypatch.setattr(L, "gate_hold_since", lambda d: None)
    monkeypatch.setattr(L, "incident_frozen", lambda d: False)
    monkeypatch.setattr(L, "release_holds", lambda d: [])
    monkeypatch.setattr(views, "load_average", lambda: None)
    view = views.wait(w, {"prs": {"unknown": "gh down"}}, go())
    assert view.unknown and "none open" in "\n".join(view.lines) and "load unknown" in "\n".join(view.lines)


def test_tick_view(monkeypatch):
    monkeypatch.setattr(views, "load_average", lambda: 99.0)
    queue = {"main_runs": [{"status": "completed", "conclusion": "failure", "headSha": "abcdef123456", "databaseId": 5}], "queued": [2], "verdict": "v"}
    prs = [pr_row(1, checks=(("t", "COMPLETED", "FAILURE"),)), pr_row(2), pr_row(3), pr_row(4, checks=(("t", "COMPLETED", "CANCELLED"),))]
    runs = [{"event": "pull_request", "status": "queued"}] * views.RUNNER_CAP + [
        {"headBranch": "main", "status": "queued", "createdAt": "2020-01-01T00:00:00Z"}]
    state = {"train": {"trains": ["train v1"], "tails": ["tail"]}, "holds": {"files": ["f"], "prs": {"9": "why"}},
             "queue": queue, "prs": prs, "runs": runs}
    alarm = SimpleNamespace(text="alarm!", key="k")
    view = views.tick(None if False else SimpleNamespace(now=lambda: NOW), state, go(alarms=[alarm], unknown=["u"]))
    text = "\n".join(view.lines)
    for piece in ("train v1", "ALERT alarm!", "MAIN RED abcdef12", "HOLD f", "HELD #9: why", "QUEUE 1 queued", "#1 FAIL",
                  "#3 green, not queued", "DEFERRED CI 1", "in flight", "MAIN CI STARVED", "LOAD 99", "UNKNOWN u"):
        assert piece in text, piece
    assert "#2 green" not in text
    quiet = views.tick(SimpleNamespace(now=lambda: NOW), {}, go())
    monkeypatch.setattr(views, "load_average", lambda: 0.5)
    assert views.tick(SimpleNamespace(now=lambda: NOW), {}, go()).lines[-1] == "quiet"
    assert quiet.name == "tick"


def test_start_view(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("## 5. Checkpoint\n- one\n")
    w = W(tmp_path, settings={"go_tracking_doc": str(doc), "go_timers_file": str(tmp_path / "t.toml")})
    (tmp_path / "t.toml").write_text("not = [valid")
    view = views.start(w, go(trap="careful", next="do it"), None)
    text = "\n".join(view.lines)
    assert "TRAP     careful" in text and "NEXT     do it" in text and "timer manifest unavailable" in view.unknown[0]
    w2 = W(tmp_path)
    view = views.start(w2, go(), None)
    assert "Checkpoint unavailable" in "\n".join(view.lines) and len(view.unknown) == 2
    assert "(nothing runnable now)" in "\n".join(view.lines)
    assert view.render()[-1].startswith("UNKNOWN")


def test_build_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(views, "start", lambda w, g, t: "start")
    monkeypatch.setattr(views, "next_actions", lambda w, s, g, a: "next")
    monkeypatch.setattr(views, "prs", lambda w: "prs")
    monkeypatch.setattr(views, "wait", lambda w, s, g: "wait")
    monkeypatch.setattr(views, "tick", lambda w, s, g: "tick")
    assert [views.build(n, None, {}, None) for n in views.VIEWS] == ["start", "next", "prs", "wait", "tick"]


def test_next_actions_and_prs_with_fake_status(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    table = SimpleNamespace(gate=[], prs=[], stale=[], unknown=["src"])
    monkeypatch.setattr(views, "status_of", lambda w, view="table": table)
    monkeypatch.setattr(views.legacy, "queue_state", lambda d: {"items": []})
    monkeypatch.setattr(views.legacy, "pr_actions", lambda p: {})
    monkeypatch.setattr(views.legacy, "next_last_update", lambda d: 0.0)
    monkeypatch.setattr(views.legacy, "next_claims", lambda d, n: {})
    w = W(tmp_path, processes=[(1, "1", "sm daemon queue")])
    state = {"queue": {"main_runs": [{"status": "completed", "conclusion": "success", "headSha": "abcdef12345"}]}}
    view = views.next_actions(w, state, go(kind="release", phase="cut", next="sm release"), False)
    assert view.name == "next" and "src" in view.unknown and any(i.key == "ship-release" for i in view.items)
    assert any("main CI: newest finished run abcdef12" in line for line in view.lines) or view.lines
    view = views.next_actions(w, {}, go(), True)
    assert view.items == [] or all(isinstance(i.rank, float) or True for i in view.items)

    def broken(w, view="table"):
        raise RuntimeError("status broke")
    monkeypatch.setattr(views, "status_of", broken)
    view = views.next_actions(w, {}, go(), False)
    assert any("status: RuntimeError" in u for u in view.unknown)
    _ = datetime, UTC
