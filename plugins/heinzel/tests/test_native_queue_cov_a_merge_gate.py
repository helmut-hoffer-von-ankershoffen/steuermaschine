"""queue_lib.merge_gate: the Sonar and coverage reads, the ruleset flip and harden, the ultra bypass helpers; all faked."""

import io
import json
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel.native.queue_lib import hzlib
from steuermaschine_heinzel.native.queue_lib import merge_gate as mg

OWNER = hzlib.OWNER


def test_http_json(monkeypatch):
    monkeypatch.setattr(mg.urllib.request, "urlopen", lambda url, timeout: io.StringIO('{"a": 1}'))
    assert mg.http_json("https://x") == {"a": 1}
    monkeypatch.setattr(mg.urllib.request, "urlopen", lambda url, timeout: io.StringIO("nope"))
    assert mg.http_json("https://x") is None

    def boom(url, timeout):
        raise OSError("down")

    monkeypatch.setattr(mg.urllib.request, "urlopen", boom)
    assert mg.http_json("https://x") is None


def test_sonar_state(monkeypatch):
    status = {"projectStatus": {"status": "OK"}}
    measures = {"component": {"measures": [{"metric": m, "value": "1.0"} for m in mg.RATINGS]}}
    answers = iter([status, measures])
    monkeypatch.setattr(mg, "http_json", lambda url: next(answers))
    assert mg.sonar_state("heinzel") == (True, "gate OK, reliability A security A maintainability A")
    bad = {"component": {"measures": [{"metric": "sqale_rating", "value": "3.0"}, {"metric": "security_rating"}]}}
    answers = iter([status, bad])
    ok, words = mg.sonar_state("heinzel")
    assert not ok and "maintainability C" in words and "security ?" in words and "reliability ?" in words
    answers = iter([{}, measures])
    assert mg.sonar_state("heinzel") == (False, "Sonar UNAVAILABLE")
    answers = iter([{"projectStatus": {"status": "ERROR"}}, {"component": {}}])
    assert mg.sonar_state("heinzel")[0] is False


def test_coverage_state(monkeypatch):
    monkeypatch.setattr(mg, "http_json", lambda url: {"message": "91%", "color": "green"})
    assert mg.coverage_state("heinzel") == (True, "coverage 91% green")
    monkeypatch.setattr(mg, "http_json", lambda url: {"message": "10%", "color": "red"})
    assert mg.coverage_state("heinzel")[0] is False
    for value in (None, {}, {"color": "red"}):
        monkeypatch.setattr(mg, "http_json", lambda url, v=value: v)
        assert mg.coverage_state("heinzel") == (False, "coverage UNKNOWN")


def rs(checks, enforcement="active", bypass=(), rid=7):
    return {"id": rid, "name": "main", "target": "branch", "enforcement": enforcement, "conditions": {}, "extra": 1,
            "rules": [{"type": "deletion"}, {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": c} for c in checks]}}],
            "bypass_actors": list(bypass)}


def test_ruleset_picks_the_active_one_with_required_checks(monkeypatch):
    sets = {1: {"enforcement": "disabled", "rules": []}, 2: {"enforcement": "active", "rules": [{"type": "deletion"}]}, 3: rs(["CI OK"], rid=3)}

    def ghj(*args, default=None):
        if args[1].endswith("/rulesets"):
            return [{"id": i} for i in sets]
        return sets[int(args[1].rsplit("/", 1)[1])]

    monkeypatch.setattr(hzlib, "ghj", ghj)
    assert mg.ruleset("heinzel")["id"] == 3
    sets.pop(3)
    assert mg.ruleset("heinzel") is None
    monkeypatch.setattr(hzlib, "ghj", lambda *a, default=None: None)
    assert mg.ruleset("heinzel") is None
    assert [c["context"] for c in mg.required_in(rs(["a", "b"]))] == ["a", "b"]


def test_write_ruleset_refuses_drops_and_reports_refusals(monkeypatch, capsys):
    data = rs(["CI OK", "merge-gate"])
    assert mg.write_ruleset("heinzel", data, {"CI OK", "gone"}) is False
    assert "refusing a write that would drop" in capsys.readouterr().err
    seen = []
    monkeypatch.setattr(mg.edge, "run", lambda argv, **kw: seen.append((argv, json.loads(kw["input"]))) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert mg.write_ruleset("heinzel", data, {"CI OK"}) is True
    argv, body = seen[0]
    assert argv[-3:] == ["/rulesets/7".join([f"repos/{OWNER}/heinzel", ""]), "--input", "-"] and "extra" not in body and body["name"] == "main"
    monkeypatch.setattr(mg.edge, "run", lambda argv, **kw: SimpleNamespace(returncode=1, stdout="o", stderr="e"))
    assert mg.write_ruleset("heinzel", data, set()) is False
    assert "GitHub refused the ruleset: o e" in capsys.readouterr().err


def test_workflow_on_main_and_reported_on_pull_request(monkeypatch):
    monkeypatch.setattr(hzlib, "gh", lambda *a: (0, ""))
    assert mg.workflow_on_main("heinzel") is True
    monkeypatch.setattr(hzlib, "gh", lambda *a: (1, ""))
    assert mg.workflow_on_main("heinzel") is False
    runs = {"c1": {"check_runs": [{"conclusion": "success", "app": {"id": 1}}]},
            "c2": {"check_runs": [{"conclusion": "failure", "app": {"id": mg.ACTIONS_APP}}]},
            "c3": {"check_runs": [{"conclusion": "success", "app": {"id": mg.ACTIONS_APP}}]}}

    def ghj(*args, default=None):
        if args[0] == "pr":
            return [{"headRefOid": k} for k in runs]
        return runs[args[1].split("/commits/")[1].split("/")[0]]

    monkeypatch.setattr(hzlib, "ghj", ghj)
    assert mg.reported_on_a_pull_request("heinzel", "merge-gate") is True
    runs.pop("c3")
    assert mg.reported_on_a_pull_request("heinzel", "merge-gate") is False
    monkeypatch.setattr(hzlib, "ghj", lambda *a, default=None: None)
    assert mg.reported_on_a_pull_request("heinzel", "merge-gate") is False


def world(monkeypatch, data, passes, reported=True, workflow=True, write=True):
    notes, writes = [], []
    monkeypatch.setattr(mg, "ruleset", lambda repo: data if repo == "heinzel" else None)
    monkeypatch.setattr(mg, "workflow_on_main", lambda repo: workflow)
    monkeypatch.setattr(mg, "passing", lambda repo: {"merge-gate": (passes, "m"), "coverage-floor": (False, "c")})
    monkeypatch.setattr(mg, "reported_on_a_pull_request", lambda repo, check: reported)
    monkeypatch.setattr(mg, "write_ruleset", lambda repo, d, before: writes.append(before) or write)
    monkeypatch.setattr(hzlib, "note", notes.append)
    return notes, writes


def test_flip_variants(monkeypatch):
    notes, writes = world(monkeypatch, rs(["CI OK"]), True)
    assert mg.flip() == ["heinzel: merge-gate is required from now"] and notes and writes == [{"CI OK"}]
    world(monkeypatch, rs(["CI OK"]), True)
    assert mg.flip(dry_run=True) == ["would flip heinzel: merge-gate is required from now"]
    for kwargs in ({"passes": False}, {"reported": False}, {"workflow": False}):
        notes, writes = world(monkeypatch, rs(["CI OK"]), **{"passes": True, **kwargs})
        assert mg.flip() == [] and not writes
    notes, writes = world(monkeypatch, rs(["merge-gate"]), True)
    assert mg.flip() == []
    notes, writes = world(monkeypatch, rs(["CI OK"]), True, write=False)
    assert mg.flip() == [] and not notes
    world(monkeypatch, None, True)
    assert mg.flip() == []


def test_harden(monkeypatch, capsys):
    notes, _writes = world(monkeypatch, rs(["CI OK"], bypass=[{"actor_id": 5}]), True)
    mg.harden()
    assert "heinzel: bypass list emptied" in capsys.readouterr().out and notes
    notes, _writes = world(monkeypatch, rs(["CI OK"], bypass=[{"actor_id": 5}]), True, write=False)
    mg.harden()
    assert "NOT changed" in capsys.readouterr().out and not notes
    world(monkeypatch, rs(["CI OK"]), True)
    mg.harden()
    assert capsys.readouterr().out == ""


def test_status_lines(monkeypatch):
    world(monkeypatch, rs(["merge-gate"], bypass=[{"actor_id": 5}]), False)
    lines = mg.status_lines()
    assert lines[0].startswith("heinzel: m; c; required: merge-gate; not yet: coverage-floor") and "NOT A, RED, BYPASS" in lines[0]
    assert "NO RULESET" in lines[1] and "required: neither" in lines[1]
    assert lines[-1].startswith("heinzel-management: private")
    world(monkeypatch, rs(["merge-gate", "coverage-floor"]), True)
    monkeypatch.setattr(mg, "passing", lambda repo: {"merge-gate": (True, "m"), "coverage-floor": (True, "c")})
    assert mg.status_lines()[0] == "heinzel: m; c; required: merge-gate, coverage-floor"


def test_passing_calls_both(monkeypatch):
    monkeypatch.setattr(mg, "sonar_state", lambda repo: (True, "s"))
    monkeypatch.setattr(mg, "coverage_state", lambda repo: (False, "c"))
    assert mg.passing("heinzel") == {"merge-gate": (True, "s"), "coverage-floor": (False, "c")}


def row(rid=1, **more):
    return {"ruleset_id": rid, "ruleset_source_type": "Repository", "ruleset_source": hzlib.CORE, **more}


def full(rid=1, **more):
    return {"id": rid, "target": "branch", "enforcement": "active", "rules": [{"type": "required_status_checks"}], "bypass_actors": [], **more}


def test_ultra_main_rulesets():
    store = {f"repos/{hzlib.CORE}/rules/branches/main": [row(1), row(1), row(2)], f"repos/{hzlib.CORE}/rulesets/1": full(1), f"repos/{hzlib.CORE}/rulesets/2": full(2)}
    api = lambda method, path, body=None: store.get(path)  # noqa: E731
    assert [r["id"] for r in mg.ultra_main_rulesets(api)] == [1, 2]
    main = f"repos/{hzlib.CORE}/rules/branches/main"
    for rows in (None, [], [1], [row(True)], [row(1, ruleset_source_type="Organization")], [row(1, ruleset_source="other/x")], [row(i) for i in range(1, 7)]):
        store[main] = rows
        for i in range(1, 7):
            store.setdefault(f"repos/{hzlib.CORE}/rulesets/{i}", full(i))
        with pytest.raises(ValueError):
            mg.ultra_main_rulesets(api)
    store[main] = [row(1)]
    for bad in (None, full(2), full(1, target="tag"), full(1, enforcement="evaluate"), full(1, rules="x"), full(1, bypass_actors=None)):
        store[f"repos/{hzlib.CORE}/rulesets/1"] = bad
        with pytest.raises(ValueError, match="unreadable"):
            mg.ultra_main_rulesets(api)
    store[f"repos/{hzlib.CORE}/rulesets/1"] = {**full(1), "rules": [{"type": "deletion"}]}
    with pytest.raises(ValueError, match="required checks"):
        mg.ultra_main_rulesets(api)


def test_ultra_policy():
    rule = {"name": "n", "target": "branch", "enforcement": "active", "rules": [], "bypass_actors": [1], "id": 3}
    assert mg.ultra_policy(rule) == {"name": "n", "target": "branch", "enforcement": "active", "rules": []}


class Api:
    def __init__(self, actors, after=None):
        self.actors, self.after, self.puts, self.gets = actors, after, [], 0

    def __call__(self, method, path, body=None):
        if method == "PUT":
            self.puts.append(body)
            return None
        self.gets += 1
        if self.gets > 1 and self.after is not None:
            return self.after
        return {"id": 4, "bypass_actors": self.actors}


def test_ultra_bypass_enable_and_disable():
    api = Api([{"actor_type": "Team", "actor_id": 9}], after={"id": 4, "bypass_actors": [{"actor_type": "Team", "actor_id": 9}, mg.ULTRA_ADMIN]})
    armed = []
    mg.ultra_bypass(4, True, api, before_write=lambda: armed.append(1))
    assert armed == [1] and api.puts == [{"bypass_actors": [{"actor_type": "Team", "actor_id": 9}, mg.ULTRA_ADMIN]}]
    api = Api([mg.ULTRA_ADMIN, {"x": 1}], after={"id": 4, "bypass_actors": [{"x": 1}]})
    mg.ultra_bypass(4, False, api)
    assert api.puts == [{"bypass_actors": [{"x": 1}]}]
    api = Api([{"x": 1}])
    mg.ultra_bypass(4, False, api)
    assert api.puts == []


def test_ultra_bypass_refusals():
    with pytest.raises(ValueError, match="cannot read"):
        mg.ultra_bypass(4, True, lambda *a: None)
    with pytest.raises(ValueError, match="cannot read"):
        mg.ultra_bypass(4, True, lambda *a: {"id": 9, "bypass_actors": []})
    with pytest.raises(ValueError, match="malformed"):
        mg.ultra_bypass(4, True, Api(["x"]))
    with pytest.raises(ValueError, match="already exists"):
        mg.ultra_bypass(4, True, Api([{"actor_type": "RepositoryRole", "actor_id": 5}]), before_write=lambda: None)
    with pytest.raises(ValueError, match="journal must be armed"):
        mg.ultra_bypass(4, True, Api([]))
    for after in (None, {"id": 4, "bypass_actors": []}, {"id": 4, "bypass_actors": [mg.ULTRA_ADMIN, mg.ULTRA_ADMIN]}):
        with pytest.raises(ValueError, match="readback"):
            mg.ultra_bypass(4, True, Api([], after=after or {"id": 4}), before_write=lambda: None)
    with pytest.raises(ValueError, match="readback"):
        mg.ultra_bypass(4, False, Api([mg.ULTRA_ADMIN], after={"id": 4, "bypass_actors": [mg.ULTRA_ADMIN]}))


def test_main_commands(monkeypatch, capsys):
    monkeypatch.setattr(mg, "status_lines", lambda: ["l1", "l2"])
    assert mg.main([]) == 0 and capsys.readouterr().out == "l1\nl2\n"
    monkeypatch.setattr(mg, "flip", lambda dry: [f"d={dry}"])
    assert mg.main(["flip", "--dry-run"]) == 0 and "d=True" in capsys.readouterr().out
    called = []
    monkeypatch.setattr(mg, "harden", lambda: called.append(1))
    assert mg.main(["harden"]) == 0 and called == [1]
    assert mg.main(["nonsense"]) == 2
