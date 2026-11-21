"""`sm beads sweep`. The assertions of the former sweep tests moved here; bd, gh, git and the product CLI are the
fake process runner's, the live verification is injected."""

import json
import sys
import types

import pytest
from coord_kit import app_runtime, home, native_runtime, ok  # noqa: F401  (home: autouse sandbox)

from steuermaschine.native import using
from steuermaschine.testing import FakeCommands, invoke
from steuermaschine_heinzel.native.sweep_lib import evidence as hs

PRS = [
    {"number": 10, "title": "fix weather credit pane", "state": "MERGED", "headRefName": "x", "mergeCommit": {"oid": "abc"}, "body": ""},
    {"number": 11, "title": "feat(ui): unrelated", "state": "OPEN", "headRefName": "ixpg-thing", "mergeCommit": None, "body": ""},
]
BEAD = {"id": "heinzel-management-qqq1", "title": "Remove weather credit pane", "notes": "PR #10 merged", "priority": 0, "updated_at": "2026-11-01T00:00:00Z",
        "labels": ["due-20261108-1930"]}


def old(sha, cache):
    return "v0.9.30"


def test_match_and_hint():
    rows = hs.build([BEAD], PRS, "0.9.32", release_of=old)
    assert rows[0]["hint"] == "A?" and rows[0]["due"].startswith("due-")
    assert hs.build([BEAD], PRS, "0.9.29", release_of=old)[0]["hint"] == "B?"


def test_id_match_open_pr():
    b = {"id": "heinzel-management-ixpg", "title": "zzz", "priority": 2, "updated_at": "2026-11-08T00:00:00Z"}
    assert hs.match_prs(b, PRS) == {11: "id"}
    assert hs.build([b], PRS, "0.9.32", release_of=old)[0]["hint"] == "C?"
    assert hs.build([{**b, "id": "heinzel-management-none", "updated_at": "never"}], PRS, None, release_of=old)[0]["hint"] == "D?"


def test_propose_close_only_class_a():
    cmds = hs.propose_close(hs.build([BEAD], PRS, "0.9.32", release_of=lambda sha, c: "v0.9.40"), "0.9.32")
    assert cmds == []
    rows = hs.build([BEAD], PRS, "0.9.32", release_of=old)
    assert hs.propose_close(rows) == []
    assert hs.propose_close(rows, "0.9.32")[0].startswith("bd close heinzel-management-qqq1")


def test_stale_filter_and_summary():
    assert hs.build([BEAD], PRS, "0.9.32", stale_hours=100000, release_of=old) == []
    line, rel = hs.summary(hs.build([BEAD], PRS, "0.9.32", release_of=old), "0.9.32")
    assert rel == 1 and line.startswith("beads: 1 open (1 P0)")


def test_malformed_or_keyword_release_cannot_support_proposal():
    row = {"hint": "A?", "id": "fixture", "prs": [
        {"pr": 1, "state": "MERGED", "why": "noted", "release": "v0.9.30-rc1"},
        {"pr": 2, "state": "MERGED", "why": "kw", "release": "v0.9.30"},
        {"pr": 3, "state": "MERGED", "why": "id", "release": "v0.9.40"},
    ]}
    assert hs.propose_close([row], "0.9.34") == []


def world(prs=PRS, tags=None, version="hz 0.9.34\n", beads=None):
    c = FakeCommands()
    c.on(["hz", "--version"], ok(version))
    c.on(["bd", "list"], ok([BEAD] if beads is None else beads))
    c.on(["gh", "pr", "list"], ok(prs))
    c.on(["git"], lambda argv: ok((tags or {}).get(argv[5], "nightly\nv0.9.30\nv0.9.31\n")))
    return c


@pytest.mark.parametrize("verdict", [None, "0.9.33", "0.9.340", "0.9.34-rc1", ""])
def test_cli_inventory_never_substitutes_for_live_verdict(tmp_path, verdict):
    seen = []

    def verify(cli):
        seen.append(cli)
        return verdict if verdict == cli else None

    with using(native_runtime(tmp_path, world())):
        got = hs.sweep(None, True, propose=True, verify=verify)
    assert got.exit_code == 1 and not got.proposals and "no closure proposals" in got.render()[0] and "bd close" not in "\n".join(got.render())
    assert seen == ["0.9.34"]


def test_the_live_verification_must_say_live_for_exactly_the_inventory_version(monkeypatch):
    from steuermaschine_heinzel.native import go

    monkeypatch.setattr(go, "world", lambda: object())
    for live, version, want in ((True, "0.9.34", "0.9.34"), (True, "0.9.33", None), (False, "0.9.34", None), ("yes", "0.9.34", None)):
        fake = types.ModuleType("steuermaschine_heinzel.native.mini")
        fake.verify = lambda w, live=live, version=version: types.SimpleNamespace(live=live, version=version)
        monkeypatch.setitem(sys.modules, "steuermaschine_heinzel.native.mini", fake)
        monkeypatch.setattr("steuermaschine_heinzel.native.mini", fake, raising=False)
        assert hs.verified_live_version("0.9.34") == want
    fake.verify = lambda w: 1 / 0
    assert hs.verified_live_version("0.9.34") is None  # a failing verification is unverified
    assert hs.verified_live_version("0.9.34-rc1") is None and hs.verified_live_version(None) is None


def test_verified_proposal_selects_released_evidence_within_exact_live_version(tmp_path):
    later = {**PRS[0], "number": 9, "mergeCommit": {"oid": "future"}, "body": "qqq1"}
    c = world(prs=[later, *PRS], tags={"future": "v0.9.40\n"})
    with using(native_runtime(tmp_path, c)):
        got = hs.sweep(None, True, propose=True, verify=lambda cli: cli)
    output = "\n".join(got.render())
    assert got.exit_code == 0 and got.verified_version == "0.9.34"
    assert "PR #10 in v0.9.30" in output and "PR #9" not in output
    assert "LIVE-OK 0.9.34" in output and "hz-" not in output
    assert "per-bead behaviour verification REQUIRED" in output


@pytest.mark.parametrize("extra", [[], ["--summary"], ["--summary", "--propose-close"]])
def test_inventory_and_summary_do_not_probe_live(tmp_path, monkeypatch, extra):
    def forbidden(*args, **kwargs):
        pytest.fail("Inventory or summary must not invoke live verification")

    monkeypatch.setattr(hs, "verified_live_version", forbidden)
    done = invoke(app_runtime(tmp_path, world()), ["work", "sweep", "--all", *extra])
    assert done.exit_code == 0 and "bd close" not in done.stdout
    assert ("beads:" if "--summary" in extra else "not live verification") in done.stdout


def test_the_command_in_text_and_json_and_its_refusals(tmp_path):
    c = world()
    rt = app_runtime(tmp_path, c)
    done = invoke(rt, ["work", "sweep", "--p0"])
    lines = done.stdout.splitlines()
    assert lines[0] == "CLI inventory 0.9.34 (not live verification); 1 beads" and lines[2].startswith("| bead | P | days |")
    assert lines[4].startswith("| heinzel-management-qqq1 | 0 | ") and "#10 merged/noted in v0.9.30" in lines[4]
    assert ["bd", "list", "--status", "open,in_progress,blocked", "--json", "--limit", "0", "--priority", "0"] in c.calls
    doc = hs.Sweep.model_validate(json.loads(invoke(rt, ["--format", "json", "work", "sweep", "--all", "--prs", "10", "--stale-hours", "1"]).stdout))
    assert doc.rows[0].evidence[0].release == "v0.9.30" and doc.mode == "table"
    assert len([call for call in c.calls if call[:3] == ["gh", "pr", "list"]]) == 1  # the list is kept for ten minutes
    assert invoke(rt, ["--format", "json", "work", "sweep", "--all", "--prs", "999"]).stdout.count('"rows": []') == 1
    assert invoke(rt, ["work", "sweep"]).exit_code == 2 and invoke(rt, ["work", "sweep", "--all", "--prs", "x"]).exit_code == 2
    done = invoke(rt, ["work", "sweep", "--all", "--propose-close"])  # no live verification on this branch: never a proposal
    assert done.exit_code == 1 and "bd close" not in done.stdout


def test_unreadable_tracker_forge_and_cli_give_an_empty_table(tmp_path):
    c = world(version="unknown")
    c.on(["bd", "list"], ok("not json"))
    c.on(["gh", "pr", "list"], ok("not json"))
    with using(native_runtime(tmp_path, c)):
        got = hs.sweep(1, False)
        assert got.rows == [] and got.inventory_version is None
        assert hs.World(native_runtime(tmp_path, c)).release_of(None, {}) is None
    c.on(["bd", "list"], ok({"issues": []}))
    with using(native_runtime(tmp_path, c)):
        assert hs.sweep(None, True).rows == []
    with using(native_runtime(tmp_path, world(beads=[BEAD]))):
        assert hs.sweep(None, True, propose=True, verify=lambda cli: cli).proposals[0].startswith("bd close")
        assert hs.sweep(None, True, propose=True, verify=lambda cli: cli).model_copy(update={"proposals": []}).render() == ["no class A candidates"]
