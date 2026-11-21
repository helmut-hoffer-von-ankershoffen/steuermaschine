"""go probes on a fake World: git and gh reads, the process list, packets on disk, focus, and the collect deadline."""

import json
import os
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel.native.go_lib import probes


class Clock:
    def now(self):
        return 1_000_000.0


class Cmds:
    def __init__(self, table=None):
        self.table, self.calls = table or {}, []

    def run(self, argv, cwd=None, timeout=None):
        self.calls.append(argv)
        for key, res in self.table.items():
            if key in " ".join(argv):
                return res(argv) if callable(res) else res
        return SimpleNamespace(rc=0, out="", err="")


def res(out="", rc=0, err=""):
    return SimpleNamespace(rc=rc, out=out, err=err)


class W(probes.World):
    """World whose gh reads come from a table instead of the Gh client."""

    gh_table: dict = {}

    def gh_json(self, args, timeout=12):
        for key, value in self.gh_table.items():
            if key in " ".join(args):
                if isinstance(value, Exception):
                    raise value
                return value
        return None


def world(tmp_path, table=None, gh=None, **kw):
    w = W(commands=Cmds(table), clock=Clock(), state_dir=tmp_path / "state", core=str(tmp_path / "core"), repo="o/r", **kw)
    w.gh_table = gh or {}
    return w


def test_sh_errors(tmp_path):
    w = world(tmp_path, {"git": res("", 1, "fatal: nope\nsecond"), "slow": res("", 124), "bare": res("tail", 2)})
    with pytest.raises(RuntimeError, match=r"git exited 1: second"):
        w.sh(["/usr/bin/git", "x"])
    with pytest.raises(RuntimeError, match="slow timed out after 5s"):
        w.sh(["slow"], timeout=5)
    with pytest.raises(RuntimeError, match="bare exited 2: tail"):
        w.sh(["bare"])


def test_processes_read_once(tmp_path):
    w = world(tmp_path, {"ps": res("  12 01:00 sm go --view tick\nbad line\n  x 1 y\n 13 5 cmd with args\n")})
    assert w.processes() == [(12, "01:00", "sm go --view tick"), (13, "5", "cmd with args")]
    w.commands.table.clear()
    assert len(w.processes()) == 2


def test_gh_property_and_gh_json(tmp_path):
    class Base(probes.World):
        pass
    w = Base(commands=Cmds(), clock=Clock(), state_dir=tmp_path, core="c", repo="o/r")
    assert w.gh is not None
    assert w.now() == 1_000_000.0
    w.__class__ = Base
    fake = SimpleNamespace(try_json=lambda args, timeout, retry: (None, "boom"))
    Base.gh = property(lambda self: fake)
    with pytest.raises(RuntimeError, match="gh: boom"):
        w.gh_json(["x"])
    Base.gh = property(lambda self: SimpleNamespace(try_json=lambda args, timeout, retry: ({"a": 1}, None)))
    assert w.gh_json(["x"]) == {"a": 1}
    del Base.gh


def test_setting():
    w = SimpleNamespace(settings={"a": "", "b": 3})
    assert probes.setting(w, "a", 9) == 9 and probes.setting(w, "b", 9) == 3 and probes.setting(w, "c", 9) == 9
    assert probes.setting(SimpleNamespace(settings=None), "a", 1) == 1


def test_git_probes(tmp_path):
    sha = "a" * 40
    w = world(tmp_path, {"ls-remote origin refs": res(f"{sha}\trefs/heads/main\n"),
                         "ls-remote --heads": res(f"{sha}\trefs/heads/main\n{'b' * 40}\trefs/heads/fix/x\nnoise\n"),
                         "rev-list": res("7\n")})
    assert probes.probe_main(w) == {"sha": sha}
    assert probes.probe_remote_heads(w) == {"main": sha, "fix/x": "b" * 40}
    assert probes.probe_since_tag(w, sha, "1.2.3") == {"count": 7}
    with pytest.raises(RuntimeError, match="needs main"):
        probes.probe_since_tag(w, "", "1")
    w2 = world(tmp_path, {"ls-remote": res("junk\n")})
    with pytest.raises(RuntimeError, match="no main"):
        probes.probe_main(w2)
    assert probes.probe_main.__name__


def test_gh_probes(tmp_path):
    rows = [{"tagName": "v1.0.0", "isLatest": False, "publishedAt": "t0"}, {"tagName": "v0.9.0", "isLatest": True, "publishedAt": "t1"}]
    w = world(tmp_path, gh={"release list": rows, "run list": [1], "pr list": [2],
                            "variables": {"variables": [{"name": "FOCUSED_CI_PLANS", "value": "abc"}, {"name": "OTHER", "value": "x"}]}})
    assert probes.probe_releases(w) == {"latest": "0.9.0", "published": "t1"}
    w.gh_table["release list"] = [rows[0]]
    assert probes.probe_releases(w)["latest"] == "1.0.0"
    w.gh_table["release list"] = []
    assert probes.probe_releases(w) == {"latest": "", "published": ""}
    assert probes.probe_runs(w) == [1] and probes.probe_prs(w) == [2]
    assert probes.probe_slots(w) == {"FOCUSED_CI_PLANS": 3}


def test_queue_waits_and_verdict():
    now = 100_000.0
    items = [{"key": "1", "status": "merged"}, {"key": "2", "status": "queued", "waiting_since": now - 600, "wait_reason": "why"},
             {"key": "3", "status": "queued", "waiting_since": now - 60},
             {"key": "4", "status": "queued", "ready_since": now - 3600}, {"key": "5", "status": "queued", "ready_since": now - 60}]
    waits = probes.queue_waits(items, now)
    assert waits[0] == "item 2: waiting 10 min: why" and "reason not recorded" in waits[1]
    assert waits[2].startswith("item 4: ready and unmerged for 60 min") and len(waits) == 3
    q = {"items": [{"key": "9", "status": "queued"}]}
    assert probes.queue_verdict({"items": []}, False, []) == "nothing queued"
    assert "freeze holds" in probes.queue_verdict(q, True, [])
    assert "lifted" in probes.queue_verdict(q | {"blocked": "merge freeze is on"}, False, [])
    assert probes.queue_verdict(q | {"blocked": "ci red"}, False, []) == "last tick: ci red"
    assert "but 2: waiting" in probes.queue_verdict(q, False, ["item 2: waiting"])
    assert "next tick should merge" in probes.queue_verdict(q, False, [])


def test_main_runs(tmp_path):
    class Base(probes.World):
        pass
    w = Base(commands=Cmds(), clock=Clock(), state_dir=tmp_path, core="c", repo="o/r")
    Base.gh = property(lambda self: SimpleNamespace(try_json=lambda *a, **k: ([{"databaseId": 1}], None)))
    assert probes.main_runs(w) == [{"databaseId": 1}]
    Base.gh = property(lambda self: SimpleNamespace(try_json=lambda *a, **k: (None, "x")))
    assert probes.main_runs(w) == []
    del Base.gh


def test_probe_queue_train_release_holds(tmp_path, monkeypatch):
    w = world(tmp_path)
    L = probes.legacy
    monkeypatch.setattr(L, "queue_state", lambda d: {"items": [{"key": 7, "status": "queued", "added": "t", "waiting_since": 1.0}], "blocked": None})
    monkeypatch.setattr(L, "merge_frozen", lambda d: False)
    monkeypatch.setattr(probes, "main_runs", lambda w: [{"databaseId": 3, "headSha": "s", "status": "completed", "conclusion": None}])
    q = probes.probe_queue(w)
    assert q["queued"] == ["7"] and q["added"] == {"7": "t"} and q["main_runs"][0]["conclusion"] == ""
    monkeypatch.setattr(L, "active_trains", lambda d: [{"version": "1.0.0", "phase": "cut", "reason": "r"}, {"version": "1.0.1", "phase": "x"}])
    monkeypatch.setattr(L, "release_tails", lambda d: [("0.9.1", {"finished": True, "accepted": True}),
                                                       ("0.9.2", {"finished": True, "absorbed_by": "0.9.3", "acceptance_run": 5}),
                                                       ("0.9.3", {"acceptance_requested": True}), ("0.9.4", {})])
    w.commands.table["ps"] = res("5 1 python x-train run 1.0.0\n")
    t = probes.probe_train(w)
    assert "running" in t["trains"][0] and "idle" in t["trains"][1] and ": r" in t["trains"][0]
    assert len(t["tails"]) == 2 and t["tails"][0].startswith("0.9.3 tail: acceptance dispatched")
    w.commands.table["ps"] = res("", 1)
    assert probes.probe_train(w)["trains"]
    monkeypatch.setattr(L, "accepted_versions", lambda d: ["1.0.0", "1.2.0"])
    monkeypatch.setattr(L, "release_receipt", lambda d: {"v": 1})
    monkeypatch.setattr(L, "dropped_candidates", lambda d: ["x"])
    assert probes.probe_release(w) == {"active": {"v": 1}, "latest_accepted": "1.2.0", "dropped": ["x"]}
    monkeypatch.setattr(L, "accepted_versions", lambda d: [])
    assert probes.probe_release(w)["latest_accepted"] is None
    monkeypatch.setattr(L, "hold_files", lambda d: ["f"])
    monkeypatch.setattr(L, "pr_actions", lambda p: {"a": {"hold": True, "action": "wait"}, "b": {"hold": False}, "c": 5})
    assert probes.probe_holds(w) == {"files": ["f"], "prs": {"a": "wait"}}

    def bad(p):
        raise ValueError("bad toml")
    monkeypatch.setattr(L, "pr_actions", bad)
    w.settings = {"go_pr_actions_file": "~/x.toml"}
    assert "unreadable: bad toml" in probes.probe_holds(w)["files"][-1]
    monkeypatch.setattr(L, "alarm_state", lambda d: {"alarms": 1})
    monkeypatch.setattr(L, "admission_receipt_heads", lambda d: ["r"])
    assert probes.probe_alarms(w) == {"alarms": 1} and probes.probe_receipts(w) == ["r"]
    assert probes.probe_tools(w) == {"rehearse": False, "drop_candidate": True}




def test_probe_mini(tmp_path):
    w = world(tmp_path, {"--version": res("hz 1.2.3\n")}, settings={"mini_cli": "myhz"})
    assert probes.probe_mini(w)["cli"] == "1.2.3"
    w.commands.table["--version"] = res("nothing\n")
    assert probes.probe_mini(w)["cli"] is None


def test_packet_argument_and_option_value():
    assert probes.packet_argument(["--out", "x", "-v", "/pkt"]) == "/pkt"
    assert probes.packet_argument(["--out", "x"]) is None
    assert probes.option_value(["--core-repo", "a"], "--core-repo") == "a"
    assert probes.option_value(["--core-repo=b"], "--core-repo") == "b"
    assert probes.option_value(["--core-repo"], "--core-repo") is None


def test_parse_process():
    old = probes.parse_process(1, "1:00", "py /x/hz-focused-admit prepare /p/wave")
    assert old["kind"] == "prepare" and old["packet"] == "/p/wave"
    row = probes.parse_process(2, "1:00", "sm head prepare /tmp/p --core-repo ~/core --wait")
    assert row["kind"] == "prepare" and row["waits"] and row["core_repo"].endswith("/core")
    row = probes.parse_process(2, "1:00", "sm head prepare /tmp/p")
    assert row["core_repo"] is None
    row = probes.parse_process(3, "1:00", "sm --format json head admit ~/wt")
    assert row["kind"] == "admit" and row["packet"] is None and "~" not in row["worktree"]
    assert probes.parse_process(3, "1", "sm head admit --wait") is None
    assert probes.parse_process(4, "1", "/x/hz-release 0.9.1")["kind"] == "release"
    assert probes.parse_process(5, "1", "vim") is None


def test_probe_procs(tmp_path):
    w = world(tmp_path, {"ps": res(f"{os.getpid()} 1 sm head admit /a\n7 1 sm go --view tick\n8 1 sm head admit /b\n9 1 vim\n")})
    rows = probes.probe_procs(w)
    assert [r["pid"] for r in rows] == [8]


def write_packet(root, *, name="m-1", prepared=None, review=None, err=None, args=None, extra=None):
    d = root / "admit-x" / "y" / name
    d.mkdir(parents=True)
    (d / "wave.json").write_text(json.dumps({"branch": "fix/x", "head": "h" * 40, "main": "m" * 40, "worktree": str(root)}))
    if prepared is not None:
        (d / "prepare.out").write_text(prepared if isinstance(prepared, str) else json.dumps(prepared))
    if review is not None:
        (d / "APPLY-REVIEW.md").write_text(review)
    if err is not None:
        (d / "apply.err").write_text(err)
    if args is not None:
        (d / "prepare.args").write_text(args if isinstance(args, str) else json.dumps(args))
    for fname, text in (extra or {}).items():
        (d / fname).write_text(text)
    return d


def test_read_packet_full(tmp_path):
    receipts = [{"id": 1}]
    d = write_packet(tmp_path, prepared={"pr": 5, "protected_producer": True, "resulting_main": 1,
                                         "pr_update": {"old": "c" * 40}, "nonconsumers": receipts},
                     review="x\nVERDICT: APPROVE sha256=" + "a" * 64 + "\n",
                     err='not json\n{"event": "apply.outcome", "operation": "apply", "result": "refused", "reason": "why"}\n[1]\n',
                     args={"argv": ["--core-repo", "core", "--nonconsumers", "nc.json"], "cwd": str(tmp_path)},
                     extra={"PR-BODY.md": "body"})
    (d / "nc.json").write_text("{}")
    (d.parent / "other.json").write_text(json.dumps({"receipts": receipts}))
    w = world(tmp_path, {"git": res("subject line\n")})
    p = probes.read_packet(w, d / "wave.json")
    assert p["pr"] == 5 and p["protected_producer"] and p["resulting_main_only"] and p["expected_remote_head"] == "c" * 40
    assert p["approved_sha256"] == "a" * 64 and p["apply_refused"] == "why" and p["subject"] == "subject line"
    assert p["nonconsumers"].endswith("other.json") and p["body"].endswith("PR-BODY.md")
    assert p["core_repo"] == str(tmp_path / "core")
    # success outcome clears the refusal; git failure leaves subject
    (d / "apply.err").write_text('{"event": "apply.outcome", "operation": "apply", "result": "success"}\n')
    w2 = world(tmp_path, {"git": res("", 1, "x")})
    p = probes.read_packet(w2, d / "wave.json")
    assert p["apply_refused"] is None and p["subject"] is None


def test_prepare_arguments_variants(tmp_path):
    d = write_packet(tmp_path, prepared={"members": []}, args="{bad")
    out = probes.prepare_arguments(d, {})
    assert any("unreadable" in u for u in out["unknown_args"]) and any("batch transaction" in u for u in out["unknown_args"])
    d = write_packet(tmp_path, name="m-2", prepared={"pr": "x", "pr_update": {"old": "nope"}, "nonconsumers": [{"id": 9}]})
    out = probes.prepare_arguments(d, {})
    assert out["pr"] is None and any("pr_update" in u for u in out["unknown_args"]) and any("--nonconsumers" in u for u in out["unknown_args"])
    d = write_packet(tmp_path, name="m-3", prepared={"nonconsumers": []})
    assert "nonconsumers" not in probes.prepare_arguments(d, {})
    d = write_packet(tmp_path, name="m-4", prepared="not json", extra={
        "notes.md": "RECEIPTS=$HOME_X/n.json\nsm head prepare --nonconsumers $RECEIPTS\nand --nonconsumers /abs/z.json",
        "run.sh": "--nonconsumers ~/q.json"})
    (d.parent / "prepare-loop.sh").write_text("--nonconsumers ${V}/l.json")
    out = probes.prepare_arguments(d, {})
    assert out["nonconsumers"].endswith("n.json") or out["nonconsumers"].endswith("l.json") or out["nonconsumers"].endswith("z.json") or out["nonconsumers"]
    assert probes._loop_scripts(d, str(tmp_path)) == [d.parent / "prepare-loop.sh"]
    big = tmp_path / "big.json"
    big.write_text("x")
    assert probes._receipts(big) is None
    assert probes._receipts(tmp_path / "missing.json") is None
    big.write_text('{"receipts": [1]}')
    assert probes._receipts(big) == [1]
    big.write_text("[1]")
    assert probes._receipts(big) is None




def test_focus(tmp_path, monkeypatch):
    sha = "d" * 40
    w = world(tmp_path, {"rev-parse HEAD": res(sha + "\n"), "abbrev-ref": res("feat/x\n"), "log": res("subj\n"), "status": res(" M f\n")},
              gh={"pr list": [{"number": 1}]})
    w.focus_list = {"/wt/a": {"for": "bead", "note": "n"}, "nonpath": {}, "~/b": "str"}
    rows = probes.probe_focus(w)
    assert len(rows) == 1 and rows[0]["dirty"] and rows[0]["prs"] == [{"number": 1}] and rows[0]["branch"] == "feat/x"
    w.gh_table["pr list"] = RuntimeError("gh down")
    assert probes.probe_focus(w)[0]["prs"] is None
    w.commands.table["rev-parse HEAD"] = res("zzz\n")
    row = probes.probe_focus(w)[0]
    assert row["head"] is None and "no commit" in row["error"]
    w.focus_list = None
    monkeypatch.setattr(probes.legacy, "focus_entries", lambda d: [{"worktree": "/old"}])
    assert probes.declared_focus(w)[0]["source"] == "old-file"


def test_collect_and_deadline(tmp_path, monkeypatch):
    names = ["probe_main", "probe_remote_heads", "probe_runs", "probe_prs", "probe_releases", "probe_queue", "probe_train",
             "probe_release", "probe_holds", "probe_receipts", "probe_slots", "probe_tools", "probe_alarms", "probe_rehearse", "probe_focus"]
    for name in names:
        monkeypatch.setattr(probes, name, lambda w, n=name: {"name": n})
    monkeypatch.setattr(probes, "probe_main", lambda w: {"sha": "s"})
    monkeypatch.setattr(probes, "probe_releases", lambda w: {"latest": "1.0.0"})
    monkeypatch.setattr(probes, "probe_procs", lambda w: [{"kind": "prepare", "packet": "/p/x"}])
    monkeypatch.setattr(probes, "probe_mini", lambda w, live=False: {"live": live})
    monkeypatch.setattr(probes, "probe_runs", lambda w: (_ for _ in ()).throw(RuntimeError("gh down")))
    monkeypatch.setattr(probes, "probe_prs", lambda w: 1 / 0)
    monkeypatch.setattr(probes, "probe_packets", lambda w, extra=None: [extra])
    monkeypatch.setattr(probes, "probe_since_tag", lambda w, sha, latest: {"count": 3})
    state = probes.collect(world(tmp_path), live=True, budget=5)
    assert state["main"] == {"sha": "s"} and state["mini"] == {"live": True}
    assert "gh down" in str(state["runs"]) and "ZeroDivisionError" in str(state["prs"])
    assert state["packets"] == [["/p/x"]] and state["since_tag"] == {"count": 3}
    monkeypatch.setattr(probes, "probe_packets", lambda w, extra=None: 1 / 0)
    monkeypatch.setattr(probes, "probe_since_tag", lambda w, sha, latest: 1 / 0)
    state = probes.collect(world(tmp_path))
    assert "ZeroDivisionError" in str(state["packets"]) and "unknown" in state["since_tag"]
    assert probes._Deadline(0).left() == 0.1


def test_git_reads_need_core_repo_dir_and_never_run_in_the_current_directory(tmp_path):
    w = world(tmp_path)
    w.core = ""  # core_repo_dir is not set
    with pytest.raises(RuntimeError, match="core_repo_dir is not set"):
        probes.probe_main(w)
    assert not [c for c in getattr(w.commands, "calls", []) if c and c[0] == "git"]
