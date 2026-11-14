"""queue_lib.main_checkpoint: events, config file, GitHub evidence readers, transports, establish and permits, on fakes."""

import json
import subprocess
import zipfile
from types import SimpleNamespace

import policy_kit as kit
import pytest

from steuermaschine_heinzel.native.queue_lib import hzlib
from steuermaschine_heinzel.native.queue_lib import main_checkpoint as mc
from steuermaschine_heinzel.native.queue_lib import release_candidate as rc

HEAD = "a" * 40
BASE = "b" * 40
TREE = "c" * 40
SRC = kit.manifest()["sources"]


def test_require_and_canonical():
    mc.require(True, "x")
    with pytest.raises(rc.CandidateError, match="why"):
        mc.require(False, "why")
    assert mc.canonical({"b": 1, "a": [1]}) == b'{"a":[1],"b":1}'
    with pytest.raises(ValueError):
        mc.canonical(float("nan"))


def test_checkpoint_event_prints_and_survives_a_closed_stderr(monkeypatch, capsys):
    mc.checkpoint_event({"a": 1})
    assert json.loads(capsys.readouterr().err) == {"a": 1}

    def broken(*a, **kw):
        raise OSError("closed")

    monkeypatch.setattr("builtins.print", broken)
    mc.checkpoint_event({"a": 1})


def test_timed_phase_reports_on_success_and_on_refusal():
    seen = []
    with mc.timed_phase("p", segment=2, report=seen.append):
        pass
    with pytest.raises(RuntimeError), mc.timed_phase("q", report=seen.append):
        raise RuntimeError("x")
    assert seen[0]["phase"] == "p" and seen[0]["segment"] == 2 and seen[1]["phase"] == "q" and "segment" not in seen[1]
    samples = []
    assert mc.timed_reader(lambda a, b: a + b, (1, 2), 3, samples) == 3 and samples[0]["segment"] == 3


def config(**more):
    return {"schema": 1, "pr": 5, "run": 9, "core_repo": "/core", "sources": dict(SRC), **more}


def test_read_config_accepts_only_the_reviewed_shape(tmp_path):
    state = tmp_path / "queue.json"
    assert mc.config_path(state) == tmp_path / "queue.checkpoint.json"
    assert mc.read_config(state) is None
    path = mc.config_path(state)
    path.write_text(json.dumps(config()))
    assert mc.read_config(state) == config()
    for bad in (config(schema=2), config(pr=0), config(run="9"), config(core_repo="relative"), config(sources={"x": "y"}), config(sources=[1]), {"schema": 1}, [1], config(pr=True)):
        path.write_text(json.dumps(bad))
        with pytest.raises(rc.CandidateError, match="differs from independently reviewed"):
            mc.read_config(state)
    path.write_text('{"schema": 1, "schema": 1}')
    with pytest.raises(ValueError):
        mc.read_config(state)
    path.write_text(" " * (mc.MAX_CONFIG + 1))
    with pytest.raises(rc.CandidateError, match="unsafe"):
        mc.read_config(state)
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere")
    (tmp_path / "elsewhere").write_text(json.dumps(config()))
    with pytest.raises(rc.CandidateError, match="unsafe"):
        mc.read_config(state)


def test_get_requires_evidence(monkeypatch):
    monkeypatch.setattr(hzlib, "ghj", lambda *a, default=None: {"ok": a[1]})
    assert mc.get("x") == {"ok": f"repos/{hzlib.CORE}/x"}
    monkeypatch.setattr(hzlib, "ghj", lambda *a, default=None: None)
    with pytest.raises(rc.CandidateError, match="unavailable"):
        mc.get("x")


def test_download_bounds_output(monkeypatch):
    def run(payload, rc_code=0):
        def fake(argv, stdout, **kw):
            stdout.write(payload)
            return SimpleNamespace(returncode=rc_code)
        return fake

    monkeypatch.setattr(mc.edge, "run", run(b"zipdata"))
    assert mc.download("a", 100) == b"zipdata"
    monkeypatch.setattr(mc.edge, "run", run(b"x" * 200))
    with pytest.raises(rc.CandidateError, match="oversized"):
        mc.download("a", 100)
    monkeypatch.setattr(mc.edge, "run", run(b"", 1))
    with pytest.raises(rc.CandidateError, match="unavailable"):
        mc.download("a", 100)


def push_run(**more):
    return {"head_sha": HEAD, "head_branch": "main", "event": "push", "status": "completed", "conclusion": "success", "run_attempt": 1, "id": 77, **more}


def test_main_run():
    api = lambda path: {"workflow_runs": [push_run()]}  # noqa: E731
    assert mc.main_run(api, HEAD) == 77
    for rows in ([], [push_run(), push_run()]):
        with pytest.raises(rc.CandidateError, match="missing or ambiguous"):
            mc.main_run(lambda path, r=rows: {"workflow_runs": r}, HEAD)
    for more in ({"head_sha": BASE}, {"head_branch": "x"}, {"event": "schedule"}, {"status": "queued"}, {"conclusion": "failure"}, {"run_attempt": 2}, {"id": 0}, {"id": True}):
        with pytest.raises(rc.CandidateError, match="not successful original"):
            mc.main_run(lambda path, m=more: {"workflow_runs": [push_run(**m)]}, HEAD)
    with pytest.raises(rc.CandidateError, match="missing or ambiguous"):
        mc.main_run(lambda path: {}, HEAD)


def ci_row(identifier, event="push", created="2026-11-13T10:00:00+00:00", status="completed", conclusion="success", **more):
    return {"id": identifier, "head_sha": HEAD, "head_branch": "main", "path": rc.WORKFLOW, "repository": {"full_name": rc.REPOSITORY}, "event": event,
            "workflow_id": 3, "run_attempt": 1, "status": status, "conclusion": conclusion, "created_at": created, "head_repository": {"full_name": rc.REPOSITORY}, **more}


def inventory(rows, total=None):
    return lambda path: {"workflow_runs": rows, "total_count": len(rows) if total is None else total}


def test_actual_main_ci_accepts_the_canonical_push_and_skips_pull_request_rows():
    rows = [ci_row(1, "pull_request", head_repository=None), ci_row(2, created="2026-11-13T10:00:00+00:00"), ci_row(3, "schedule", created="2026-11-13T09:00:00+00:00", status="queued", conclusion=None)]  # noqa: E501
    runs = mc.actual_main_ci(inventory(rows), HEAD, 2)
    assert set(runs) == {2, 3}


@pytest.mark.parametrize("rows,total,push,message", [
    ("notdict", None, 2, "invalid actual-main CI response"),
    ([], None, 2, "incomplete"),
    ([ci_row(2)], 2, 2, "incomplete"),
    (["x"], None, 2, "invalid actual-main CI run"),
    ([ci_row(2), ci_row(2)], None, 2, "mismatched"),
    ([ci_row(2, head_sha=BASE)], None, 2, "mismatched"),
    ([ci_row(2, path="other")], None, 2, "mismatched"),
    ([ci_row(2, repository=None)], None, 2, "mismatched"),
    ([ci_row(2, event="release")], None, 2, "invalid actual-main CI run metadata"),
    ([ci_row(2, head_repository={"full_name": "x/y"})], None, 2, "invalid actual-main CI run metadata"),
    ([ci_row(2, workflow_id=0)], None, 2, "invalid actual-main CI run metadata"),
    ([ci_row(2, status="weird")], None, 2, "invalid actual-main CI run metadata"),
    ([ci_row(2, status="queued", conclusion="success")], None, 2, "invalid actual-main CI run metadata"),
    ([ci_row(2, created=5)], None, 2, "invalid actual-main CI run metadata"),
    ([ci_row(2, created="2026-11-13T10:00:00")], None, 2, "no timezone"),
    ([ci_row(2)], None, 9, "canonical push absent"),
    ([ci_row(2, conclusion="failure")], None, 2, "disagrees"),
    ([ci_row(2), ci_row(3, "schedule", workflow_id=4)], None, 2, "disagrees"),
    ([ci_row(2), ci_row(3, "schedule", created="2026-11-13T11:00:00+00:00", status="in_progress", conclusion=None)], None, 2, "at or after the canonical push is in_progress"),
    ([ci_row(2), ci_row(3, "schedule", created="2026-11-13T11:00:00+00:00", conclusion="failure")], None, 2, "is failure"),
])
def test_actual_main_ci_refusals(rows, total, push, message):
    api = (lambda path: rows) if rows == "notdict" else inventory(rows, total)
    with pytest.raises(rc.CandidateError, match=message):
        mc.actual_main_ci(api, HEAD, push)


def test_actual_main_ci_allows_a_later_green_run():
    rows = [ci_row(2), ci_row(3, "workflow_dispatch", created="2026-11-13T11:00:00+00:00")]
    assert set(mc.actual_main_ci(inventory(rows), HEAD, 2)) == {2, 3}


def test_selected_main():
    commit = {"sha": HEAD, "commit": {"tree": {"sha": TREE}}}
    plan = {"base": BASE}
    assert mc.selected_main(lambda p: commit, {f"push:{HEAD}": plan}, HEAD) == (plan, TREE)
    assert mc.selected_main(lambda p: commit, {f"push-tree:{TREE}": plan}, HEAD)[0] == plan
    with pytest.raises(rc.CandidateError, match="identity unavailable"):
        mc.selected_main(lambda p: {"sha": BASE}, {}, HEAD)
    for registry in ({}, {f"push:{HEAD}": "x"}, {f"push:{HEAD}": {"base": "zz"}}, {f"push:{HEAD}": {"base": HEAD}}):
        with pytest.raises(rc.CandidateError, match="missing exact resulting-main manifest"):
            mc.selected_main(lambda p: commit, registry, HEAD)


def test_isolated_reader_and_segment(monkeypatch):
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout=b'{"a": 1}', stderr=b"")

    monkeypatch.setattr(mc.edge, "run", run)
    assert mc.isolated_reader({"x": 1}) == {"a": 1} and seen[0][1]["input"] == b'{"x":1}' and "-I" in seen[0][0]
    assert mc.isolated_segment("/core", "r", "h", "b", 7, {}) == {"a": 1}
    assert json.loads(seen[1][1]["input"])["run"] == 7
    monkeypatch.setattr(mc.edge, "run", lambda argv, **kw: SimpleNamespace(returncode=1, stdout=b"", stderr=b"x" * 3000 + b"bad\xff"))
    with pytest.raises(rc.CandidateError, match=r"protected focused reader refused: .*bad"):
        mc.isolated_reader({})
    monkeypatch.setattr(mc.edge, "run", lambda argv, **kw: SimpleNamespace(returncode=0, stdout=b"x" * (5 * 1024 * 1024 + 1), stderr=b""))
    with pytest.raises(rc.CandidateError):
        mc.isolated_reader({})
    monkeypatch.setattr(mc.edge, "run", lambda argv, **kw: SimpleNamespace(returncode=0, stdout=b'{"a": 1, "a": 2}', stderr=b""))
    with pytest.raises(ValueError):
        mc.isolated_reader({})


def variables(rows, total=None):
    return {"total_count": len(rows) if total is None else total, "variables": rows}


def test_transports(monkeypatch):
    rows = [{"name": "FOCUSED_CI_PLANS", "value": "legacy"}, {"name": "FOCUSED_CI_PLANS_V2_1", "value": "s1"}, {"name": "UNRELATED", "value": "x"}]
    monkeypatch.setattr(mc, "get", lambda path: variables(rows))
    assert mc.transports() == {"FOCUSED_CI_PLANS": "legacy", "FOCUSED_CI_PLANS_V2_0": "", "FOCUSED_CI_PLANS_V2_1": "s1", "FOCUSED_CI_PLANS_V2_2": "", "FOCUSED_CI_PLANS_V2_3": ""}
    monkeypatch.setattr(mc, "get", lambda path: variables(rows, total=9))
    with pytest.raises(rc.CandidateError, match="incomplete focused variable inventory"):
        mc.transports()
    monkeypatch.setattr(mc, "get", lambda path: {"variables": rows})
    with pytest.raises(rc.CandidateError, match="incomplete"):
        mc.transports()
    monkeypatch.setattr(mc, "get", lambda path: variables([*rows, rows[0]]))
    with pytest.raises(rc.CandidateError, match="duplicate"):
        mc.transports()
    monkeypatch.setattr(mc, "get", lambda path: variables([{"name": "FOCUSED_CI_PLANS_V2_0", "value": "x" * 40001}]))
    with pytest.raises(rc.CandidateError, match="exceeds transport bound"):
        mc.transports()
    monkeypatch.setattr(mc, "get", lambda path: variables([{"name": "FOCUSED_CI_PLANS", "value": 5}]))
    with pytest.raises(rc.CandidateError, match="exceeds transport bound"):
        mc.transports()


def test_verify_configured_checks_the_registry_again_at_the_tip(monkeypatch):
    snapshot = {"FOCUSED_CI_PLANS": "legacy", **{f"FOCUSED_CI_PLANS_V2_{i}": f"s{i}" for i in range(4)}}
    state = {"snapshot": snapshot}
    monkeypatch.setattr(mc, "transports", lambda: dict(state["snapshot"]))
    monkeypatch.setattr(mc, "get", lambda path: {"object": {"sha": "r" * 40}})
    requests = []
    monkeypatch.setattr(mc, "isolated_reader", lambda request: requests.append(request) or {"reg": 1})
    captured = {}

    def progress(config, pin, registry, api, read, verify_segment, *, check_registry):
        captured.update(registry=registry, check=check_registry, verify=verify_segment)
        return {"head": "h"}

    monkeypatch.setattr(mc, "verify_progress", progress)
    assert mc.verify_configured(config(), {"headSha": "h"}) == {"head": "h"}
    assert requests[0]["slots"] == ["s0", "s1", "s2", "s3"] and requests[0]["legacy"] == "legacy" and captured["registry"] == {"reg": 1}
    captured["check"]("r" * 40)
    with pytest.raises(rc.CandidateError, match="changed during verification"):
        captured["check"]("x" * 40)
    state["snapshot"] = {**snapshot, "FOCUSED_CI_PLANS": "changed"}
    with pytest.raises(rc.CandidateError, match="changed during verification"):
        captured["check"]("r" * 40)


def proof():
    return {"head": "h", "anchor": {"head": "a"}, "segments": [1, 2]}


def test_establish_writes_only_when_applied(tmp_path, monkeypatch):
    monkeypatch.setattr(hzlib, "merge_frozen", lambda legacy=None: False)
    monkeypatch.setattr(mc, "verify_configured", lambda config, pin: proof())
    monkeypatch.setattr(mc, "release_pins", lambda number: dict(SRC))
    state = tmp_path / "state" / "queue.json"
    assert mc.establish(state, {}, 5, 9, tmp_path) == proof() and not mc.config_path(state).exists()
    assert mc.establish(state, {}, 5, 9, tmp_path, apply=True) == proof()
    written = json.loads(mc.config_path(state).read_text())
    assert written["pr"] == 5 and written["core_repo"] == str(tmp_path.resolve()) and written["sources"] == SRC
    assert [p.name for p in state.parent.iterdir()] == ["queue.checkpoint.json"]
    monkeypatch.setattr(hzlib, "merge_frozen", lambda legacy=None: True)
    with pytest.raises(rc.CandidateError, match=r"freeze or release hold is set|is present"):
        mc.establish(state, {}, 5, 9, tmp_path)
    freezes = iter([False, True])
    monkeypatch.setattr(hzlib, "merge_frozen", lambda legacy=None: next(freezes))
    with pytest.raises(rc.CandidateError, match="appeared"):
        mc.establish(state, {}, 5, 9, tmp_path)


def test_establish_refuses_symlinked_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(hzlib, "merge_frozen", lambda legacy=None: False)
    monkeypatch.setattr(mc, "release_pins", lambda number: dict(SRC))
    monkeypatch.setattr(mc, "verify_configured", lambda config, pin: proof())
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(rc.CandidateError, match="unsafe checkpoint configuration path"):
        mc.establish(tmp_path / "link" / "queue.json", {}, 5, 9, tmp_path, apply=True)
    target = mc.config_path(tmp_path / "queue.json")
    target.symlink_to(real)
    with pytest.raises(rc.CandidateError, match="unsafe checkpoint configuration path"):
        mc.establish(tmp_path / "queue.json", {}, 5, 9, tmp_path, apply=True)


def test_permits_paths(tmp_path, monkeypatch, capsys):
    state = tmp_path / "queue.json"
    assert mc.permits(state, {}) is False and capsys.readouterr().err == ""
    mc.config_path(state).write_text(json.dumps(config()))
    monkeypatch.setattr(hzlib, "merge_frozen", lambda legacy=None: False)
    monkeypatch.setattr(mc, "verify_configured", lambda cfg, pin: proof())
    assert mc.permits(state, {}) is True
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert events[0]["event"] == "queue.checkpoint.start" and events[-1] == {"event": "queue.checkpoint.outcome", "outcome": "ok", "head": "h", "anchor": "a", "segments": 2}
    # frozen before, frozen during
    monkeypatch.setattr(hzlib, "merge_frozen", lambda legacy=None: True)
    assert mc.permits(state, {}) is False and "freeze" in capsys.readouterr().err
    freezes = iter([False, True])
    monkeypatch.setattr(hzlib, "merge_frozen", lambda legacy=None: next(freezes))
    assert mc.permits(state, {}) is False and "appeared during proof" in capsys.readouterr().err
    monkeypatch.setattr(hzlib, "merge_frozen", lambda legacy=None: False)
    for error in (KeyError("k"), subprocess.TimeoutExpired("x", 1), zipfile.BadZipFile("z"), OSError("o"), rc.CandidateError("c")):
        monkeypatch.setattr(mc, "verify_configured", lambda cfg, pin, e=error: (_ for _ in ()).throw(e))
        assert mc.permits(state, {}) is False and "refused" in capsys.readouterr().err
    mc.config_path(state).write_text("{bad")
    assert mc.permits(state, {}) is False
    mc.config_path(state).unlink()
    mc.config_path(state).symlink_to(tmp_path / "missing")
    assert mc.permits(state, {}) is False and "refused" in capsys.readouterr().err


def test_release_pins_are_the_policy_sources_at_the_release_head(monkeypatch):
    answers = {"pulls/5": {"head": {"sha": HEAD}}, **kit.contents_routes(HEAD, kit.files("new"))}
    monkeypatch.setattr(mc, "get", lambda path: answers[path])
    assert mc.release_pins(5) == kit.manifest("new")["sources"]
    answers["pulls/5"] = {"head": {}}
    with pytest.raises(rc.CandidateError, match="release head missing"):
        mc.release_pins(5)
