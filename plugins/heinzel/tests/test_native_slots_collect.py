"""`sm head slots collect|apply` and the slot fill alarm, on a fake forge and a scripted `gh`. No network, no real process."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Run
from steuermaschine.testing import FakeForge, fake_providers, invoke
from steuermaschine_heinzel.native.alarm_lib import rules
from steuermaschine_heinzel.native.slots_lib import collect, legacy
from steuermaschine_heinzel.plugin import plugin

REPO = "owner/core"
H = {n: f"{n}" * 40 for n in "abcdef"}  # head shas a..f
T = {n: f"{n}" * 40 for n in "1234567"}  # tree shas; 7 is the tree of main in the default world
MAIN = "9" * 40
OLD = "2026-11-01T00:00:00Z"  # closed long before the fake clock


def main_head(tree):
    return {"sha": MAIN, "commit": {"tree": {"sha": T[tree]}}}


def pr_plan(head, tree):
    return {"event": "pull_request", "head": H[head], "base": "0" * 40, "tree": T[tree]}


def main_plan(tree):
    return {"event": "push", "ref": "refs/heads/main", "base": "0" * 40, "tree": T[tree]}


def registry():
    """Legacy slot: PR a (merged, tree 1) and its main plan; PR b (closed, tree 2); PR c (open, tree 3).
    Compressed slot 0: PR d (merged, tree 3: the same tree as the open PR c), PR e (merged, tree 4) and its main plan;
    a main plan of tree 5 without a pull request; PR f (no PR at that head, tree 6)."""
    return {
        collect.NAMES[0]: {f"pull_request:{H['a']}": pr_plan("a", "1"), f"push-tree:{T['1']}": main_plan("1"),
                           f"pull_request:{H['b']}": pr_plan("b", "2"), f"pull_request:{H['c']}": pr_plan("c", "3")},
        collect.NAMES[1]: {f"pull_request:{H['d']}": pr_plan("d", "3"), f"pull_request:{H['e']}": pr_plan("e", "4"),
                           f"push-tree:{T['4']}": main_plan("4"), f"push-tree:{T['5']}": main_plan("5"), f"pull_request:{H['f']}": pr_plan("f", "6")},
    }


PRS = [{"number": 1, "state": "MERGED", "headRefOid": H["a"]}, {"number": 2, "state": "CLOSED", "headRefOid": H["b"], "closedAt": OLD},
       {"number": 3, "state": "OPEN", "headRefOid": H["c"]}, {"number": 4, "state": "MERGED", "headRefOid": H["d"]},
       {"number": 5, "state": "MERGED", "headRefOid": H["e"]}]


@pytest.fixture
def world(tmp_path):
    gh = FakeGh().on("pr list", PRS).on("release list", [{"tagName": "v0.9.41"}]).on("commits/main", main_head("7"))
    forge = FakeForge()
    for name, entries in registry().items():
        forge.variables[name] = collect.encode(name, entries)
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {"repo": REPO}}, Path("."))
    rt = Runtime(Project(name="heinzel", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=True), stack, settings,
                 _providers=fake_providers(commands=gh, forge=forge))
    return rt, forge, gh


def collected(rt, *extra):
    out = invoke(rt, ["--format", "json", "head", "slots", "collect", *extra])
    assert out.exit_code == 0, out
    return json.loads(out.stdout)


def test_collect_lists_merged_and_closed_entries_whose_tree_nothing_wants(world):
    rt, forge, gh = world
    doc = collected(rt)
    going = sorted(e["key"] for e in doc["entries"])
    # a and its main plan, b, e and its main plan go. d stays through tree 3 (open PR c), tree 5 and f stay (nothing to say), c is open.
    assert going == sorted([f"pull_request:{H['a']}", f"push-tree:{T['1']}", f"pull_request:{H['b']}", f"pull_request:{H['e']}", f"push-tree:{T['4']}"])
    assert (doc["collect_count"], doc["keep_count"]) == (5, 4) and doc["digest"] == collect.digest(doc["transaction"])
    assert doc["transaction"]["remove"].keys() == {collect.NAMES[0], collect.NAMES[1]}
    assert all(f["after_bytes"] is None or f["after_bytes"] < f["bytes"] for f in doc["fills"])
    assert forge.calls == [] and not gh.writes()  # a dry run: no variable written


def github_fallback(rt, forge, gh):
    """Route the real GitHub provider to scripted gh responses and mutable fixture values."""
    variables = dict(forge.variables)
    for name in collect.NAMES:
        gh.on(f"repos/{REPO}/actions/variables/{name}",
              lambda _cmd, name=name: {"value": variables[name]} if name in variables else 404)

    def write(cmd):
        assert cmd[1:3] == ["variable", "set"] and cmd[4:7] == ["-R", REPO, "--body"]
        variables[cmd[3]] = cmd[7]
        return ""

    gh.on(f"repos/{REPO}/actions/runs?", {"total_count": 0, "workflow_runs": []})
    gh.on("variable set", write)
    rt.providers.forge = None
    return variables


def test_collect_without_forge_reads_github_and_apply_requires_opt_in(world, monkeypatch):
    rt, forge, gh = world
    monkeypatch.delenv("SM_GITHUB__VARIABLE_WRITES", raising=False)
    expected = collected(rt, "--all")
    values = github_fallback(rt, forge, gh)
    assert collected(rt, "--all") == expected
    denied = invoke(rt, ["head", "slots", "apply", "--approved", expected["digest"]])
    assert denied.exit_code == 1 and "variable writes are off" in denied.stderr + denied.stdout
    assert values == forge.variables and not gh.writes()

    monkeypatch.setenv("SM_GITHUB__VARIABLE_WRITES", "true")
    # A separate default GitHub setting must never redirect this reviewed project registry.
    monkeypatch.setenv("SM_GITHUB__REPO", "other/wrong")
    denied = invoke(rt, ["head", "slots", "apply", "--approved", "0" * 64])
    assert denied.exit_code == 1 and not gh.writes()
    out = invoke(rt, ["--format", "json", "head", "slots", "apply", "--approved", expected["digest"]])
    assert out.exit_code == 0, out
    assert values == (forge.variables | collect.new_values(forge.variables, expected["transaction"]))
    assert len(gh.writes()) == 2 and forge.calls == []
    receipt = rt.providers.store.get_receipt("heinzel.slots.collect", expected["digest"][:16])
    assert receipt["transaction"] == expected["transaction"] and receipt["partial"] is False


@pytest.mark.parametrize("unavailable", [False, True])
def test_configured_slot_writer_refuses_when_idle_actions_cannot_be_established(world, monkeypatch, unavailable):
    rt, forge, gh = world
    digest = collected(rt)["digest"]
    values = github_fallback(rt, forge, gh)
    monkeypatch.setenv("SM_GITHUB__VARIABLE_WRITES", "true")
    gh.on(f"repos/{REPO}/actions/runs?status=queued", 403 if unavailable else {"total_count": 1, "workflow_runs": [{"id": 19, "status": "queued"}]})
    out = invoke(rt, ["head", "slots", "apply", "--approved", digest])
    assert out.exit_code == (1 if unavailable else 75), out
    assert values == forge.variables and not gh.writes()


def test_configured_slot_writer_records_unconfirmed_write(world, monkeypatch):
    rt, forge, gh = world
    digest = collected(rt)["digest"]
    values = github_fallback(rt, forge, gh)
    monkeypatch.setenv("SM_GITHUB__VARIABLE_WRITES", "true")
    gh.on(f"variable set {collect.NAMES[1]}", 403)
    out = invoke(rt, ["head", "slots", "apply", "--approved", digest])
    assert out.exit_code == 1 and "did not confirm" in out.stderr + out.stdout
    assert values[collect.NAMES[0]] != forge.variables[collect.NAMES[0]]
    assert values[collect.NAMES[1]] == forge.variables[collect.NAMES[1]]
    receipt = rt.providers.store.get_receipt("heinzel.slots.collect", digest[:16])
    assert receipt["partial"] is True and receipt["written"] == [collect.NAMES[0]]


def test_collect_without_forge_refuses_unreadable_registry(world):
    rt, _, gh = world
    rt.providers.forge = None
    gh.on("actions/variables/", 403)
    out = invoke(rt, ["head", "slots", "collect"])
    assert out.exit_code == 1 and "cannot collect" in out.stderr + out.stdout
    assert not gh.writes()


def test_collect_all_says_why_each_entry_stays(world):
    rt, _, _ = world
    rows = {e["key"]: e for e in collected(rt, "--all")["entries"]}
    assert rows[f"pull_request:{H['c']}"]["why"] == "its pull request is open"
    assert "still wanted" in rows[f"pull_request:{H['d']}"]["why"]
    assert rows[f"pull_request:{H['f']}"]["state"] == "unmatched" and rows[f"push-tree:{T['5']}"]["action"] == "keep"
    assert len(rows) == 9


def test_a_release_reservation_protects_its_tree_until_the_version_is_published(world):
    rt, _, _ = world
    receipts = Path(rt.project.home) / "focused-admission-receipts"
    receipts.mkdir(parents=True)
    (receipts / "head-x.json").write_text(json.dumps({"release_main": {"version": "0.9.42", "tree": T["4"]}}))
    (receipts / "bad.json").write_text("{")
    protected = collected(rt)
    assert protected["reservations"] == 1 and f"pull_request:{H['e']}" not in [e["key"] for e in protected["entries"]]
    (receipts / "head-x.json").write_text(json.dumps({"release_main": {"version": "0.9.41", "tree": T["4"]}}))
    assert f"pull_request:{H['e']}" in [e["key"] for e in collected(rt)["entries"]]


def test_text_output_and_nothing_to_collect(world):
    rt, forge, _ = world
    text = invoke(rt, ["head", "slots", "collect"])
    assert text.exit_code == 0 and "5 entries to collect, 4 stay" in text.stdout and "sm head slots apply --approved" in text.stdout
    for name in list(forge.variables):
        forge.variables[name] = collect.encode(name, {k: v for k, v in collect.decode(name, forge.variables[name]).items() if "c" * 40 in k})
    quiet = invoke(rt, ["head", "slots", "collect"])
    assert "nothing to collect." in quiet.stdout


def test_collect_refuses_on_an_undecodable_slot_and_with_no_registry_and_without_a_repo(world, tmp_path):
    rt, forge, _ = world
    forge.variables[collect.NAMES[2]] = '{"codec":"gzip+base64","bytes":3,"data":"!!"}'
    out = invoke(rt, ["head", "slots", "collect"])
    assert out.exit_code == 1 and "cannot collect" in out.stderr + out.stdout
    forge.variables.clear()
    assert "no focused registry variable" in (lambda o: o.stderr + o.stdout)(invoke(rt, ["head", "slots", "collect"]))
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {"repo": ""}}, Path("."))
    bare = Runtime(Project(name="heinzel", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=True), stack, settings,
                   _providers=fake_providers(commands=FakeGh(), forge=FakeForge()))
    assert invoke(bare, ["head", "slots", "collect"]).exit_code == 2


def test_apply_needs_the_exact_approved_digest_and_writes_only_the_changed_slots(world):
    rt, forge, _ = world
    doc = collected(rt)
    wrong = invoke(rt, ["head", "slots", "apply", "--approved", "0" * 64])
    assert wrong.exit_code == 1 and doc["digest"] in wrong.stderr + wrong.stdout and forge.calls == []
    out = invoke(rt, ["--format", "json", "head", "slots", "apply", "--approved", doc["digest"]])
    assert out.exit_code == 0, out
    assert sorted(c[1] for c in forge.calls if c[0] == "set_variable") == sorted(collect.NAMES[:2])
    left = {n: collect.decode(n, forge.variables[n]) for n in collect.NAMES[:2]}
    assert sorted(left[collect.NAMES[0]]) == [f"pull_request:{H['c']}"]
    assert sorted(left[collect.NAMES[1]]) == sorted([f"pull_request:{H['d']}", f"push-tree:{T['5']}", f"pull_request:{H['f']}"])
    assert json.loads(out.stdout)["removed"] == 5
    assert rt.providers.store.get_receipt("heinzel.slots.collect", doc["digest"][:16])["transaction"] == doc["transaction"]
    again = invoke(rt, ["head", "slots", "apply", "--approved", doc["digest"]])
    assert again.exit_code == 1  # the digest of what reads now differs (or there is nothing left): never a second write


def test_apply_waits_while_an_actions_run_is_active_and_refuses_a_registry_that_moves(world, monkeypatch):
    rt, forge, _ = world
    digest = collected(rt)["digest"]
    forge.runs.append(Run("9", "in_progress", "fix/x", "pull_request", "ci"))
    waiting = invoke(rt, ["head", "slots", "apply", "--approved", digest])
    assert waiting.exit_code == 75 and forge.calls == []
    forge.runs.clear()
    real = forge.get_variable
    state = {"reads": 0}

    def moving(name):
        state["reads"] += 1
        return real(name) if state["reads"] <= len(collect.NAMES) else (real(name) or "") + " "  # a writer got in after the read

    monkeypatch.setattr(forge, "get_variable", moving)
    moved = invoke(rt, ["head", "slots", "apply", "--approved", digest])
    assert moved.exit_code == 1 and "changed since the read" in moved.stderr + moved.stdout and forge.calls == []


def test_apply_stops_when_the_readback_differs(world, monkeypatch):
    rt, forge, _ = world
    digest = collected(rt)["digest"]
    real = forge.set_variable
    monkeypatch.setattr(forge, "set_variable", lambda name, value: real(name, value + " " if name == collect.NAMES[1] else value))  # the last write does not take as sent
    out = invoke(rt, ["head", "slots", "apply", "--approved", digest])
    assert out.exit_code == 1 and "readback differs" in out.stderr + out.stdout


def test_collect_is_read_only_apply_is_a_reviewed_writer(world):
    from steuermaschine_heinzel.native import commands

    by_path = {c.path: c for c in commands()}
    assert by_path["head slots collect"].writes is False and by_path["head slots apply"].writes is True


# ---- the library
def test_encode_decode_roundtrip_is_deterministic_and_checks_the_envelope():
    entries = {"pull_request:" + "a" * 40: {"tree": "t"}}
    for name in collect.NAMES:
        assert collect.decode(name, collect.encode(name, entries)) == entries and collect.encode(name, entries) == collect.encode(name, entries)
    assert collect.decode(collect.NAMES[1], collect.encode(collect.NAMES[1], {})) == {} and collect.encode(collect.NAMES[0], {}) == "{}" and collect.decode(collect.NAMES[1], "") == {}
    good = json.loads(collect.encode(collect.NAMES[1], entries))
    for bad in ('[]', json.dumps({**good, "bytes": good["bytes"] + 1}), json.dumps({**good, "codec": "x"}), json.dumps({**good, "data": "%%"}), "not json"):
        with pytest.raises(collect.Unreadable):
            collect.decode(collect.NAMES[1], bad)
    with pytest.raises(collect.Unreadable):
        collect.decode(collect.NAMES[0], "[1]")


def test_new_values_refuses_a_key_that_is_gone_and_other_kinds_stay():
    values = {collect.NAMES[0]: collect.encode(collect.NAMES[0], {"x": {}})}
    with pytest.raises(collect.Unreadable):
        collect.new_values(values, {"remove": {collect.NAMES[0]: ["pull_request:gone"]}})
    rows = collect.classify({collect.NAMES[0]: {"weird": {}}}, {}, set())
    assert rows[0]["kind"] == "other" and rows[0]["action"] == "keep"
    rows = collect.classify({collect.NAMES[0]: {f"pull_request:{H['a']}": {"event": "pull_request"}}}, {H["a"]: [(1, "merged", None)]}, set())
    assert rows[0]["action"] == "keep" and rows[0]["why"] == "its plan names no tree"


def test_reservation_reader_skips_what_it_cannot_read(tmp_path):
    assert legacy.reservations(tmp_path / "missing") == []
    (tmp_path / "a.json").write_text(json.dumps({"release_main": {"tree": "t1", "version": "v1.0.0"}}))
    (tmp_path / "b.json").write_text(json.dumps({"other": 1}))
    (tmp_path / "c.json").write_text("x" * (legacy.MAX_RECEIPT_BYTES + 1))
    assert legacy.reservations(tmp_path) == [("1.0.0", "t1")]


# ---- the alarm
def test_a_slot_over_85_percent_raises_a_warn_alarm_naming_the_command():
    sizes = {"FOCUSED_CI_PLANS": 47000, "FOCUSED_CI_PLANS_V2_0": 35000, "FOCUSED_CI_PLANS_V2_1": 33000, "FOCUSED_CI_PLANS_V2_2": 1, "FOCUSED_CI_PLANS_V2_3": 0}
    alarm = rules.slot_fill_alarm(sizes)
    assert alarm and alarm["cls"] == "WARN" and alarm["key"] == "registry-slot-fill"
    assert "PLANS 95%" in alarm["text"] and "PLANS_V2_0 87%" in alarm["text"] and "V2_1" not in alarm["text"] and "sm head slots collect" in alarm["text"]
    assert rules.slot_fill_alarm({"FOCUSED_CI_PLANS": 40000}) is None and rules.slot_fill_alarm(None) is None
    assert collect.over_threshold(sizes)[0] == ("FOCUSED_CI_PLANS", 95) and len(collect.over_threshold(sizes)) == 2


# ---- review round: closed PRs, main's tree, the shared writer lock, never an empty value
def test_a_pull_request_closed_within_the_grace_period_keeps_its_entry_and_an_old_closed_one_goes(world):
    rt, _, gh = world
    day_ago = datetime.fromtimestamp(rt.providers.clock.now() - 86400, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh.on("pr list", [dict(p, closedAt=day_ago) if p["number"] == 2 else p for p in PRS])
    rows = {e["key"]: e for e in collected(rt, "--all")["entries"]}
    assert rows[f"pull_request:{H['b']}"]["action"] == "keep" and rows[f"pull_request:{H['b']}"]["state"] == "recent"
    assert "close and reopen" in rows[f"pull_request:{H['b']}"]["why"]
    gh.on("pr list", [dict(p, closedAt="not a time") if p["number"] == 2 else p for p in PRS])  # unreadable time: stays
    assert {e["key"]: e for e in collected(rt, "--all")["entries"]}[f"pull_request:{H['b']}"]["action"] == "keep"
    gh.on("pr list", PRS)
    assert {e["key"]: e for e in collected(rt, "--all")["entries"]}[f"pull_request:{H['b']}"]["action"] == "collect"


def test_the_plan_of_mains_current_tree_is_never_collected_and_the_digest_binds_main(world):
    rt, forge, gh = world
    gh.on("commits/main", main_head("4"))  # main is at the merge of PR e
    doc = collected(rt, "--all")
    rows = {e["key"]: e for e in doc["entries"]}
    for key in (f"pull_request:{H['e']}", f"push-tree:{T['4']}"):
        assert rows[key]["action"] == "keep" and rows[key]["why"] == "its tree is the tree of main's head"
    assert doc["transaction"]["main"] == {"sha": MAIN, "tree": T["4"]}
    other = collected(world[0])  # same read; the default world differs only by main's tree
    gh.on("commits/main", main_head("7"))
    assert collected(rt)["digest"] != other["digest"] and forge.calls == []


def test_apply_refuses_when_main_moved_between_the_read_and_the_write(world):
    rt, forge, gh = world
    digest = collected(rt)["digest"]
    gh.on("commits/main", main_head("7"), main_head("4"))  # the apply reads it twice: at the start, and before the first write
    out = invoke(rt, ["head", "slots", "apply", "--approved", digest])
    assert out.exit_code == 1 and "main moved" in out.stderr + out.stdout and forge.calls == []


def test_apply_takes_the_lock_head_apply_takes_and_refuses_while_it_is_held(world, monkeypatch):
    from types import SimpleNamespace

    from steuermaschine_heinzel import state as statedir
    from steuermaschine_heinzel.native.admission_lib import focused_admit

    rt, forge, _ = world
    digest = collected(rt)["digest"]
    path = statedir.path("focused-pr-registry.lock")  # the path `head apply` hands to the same function
    ticks = iter(range(0, 10_000, 10))
    with focused_admit._lock(path):
        monkeypatch.setattr(focused_admit, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda seconds: None))
        out = invoke(rt, ["head", "slots", "apply", "--approved", digest])
    assert out.exit_code == 1 and "writer lock is busy" in out.stderr + out.stdout and forge.calls == []


def test_a_writer_that_appends_after_the_read_is_never_overwritten(world, monkeypatch):
    rt, forge, _ = world
    digest = collected(rt)["digest"]
    real = forge.get_variable
    reads = {"n": 0}
    late = f"pull_request:{H['f']}x"

    def racing(name):
        reads["n"] += 1
        if reads["n"] == len(collect.NAMES) + 1:  # the first read before the first write: head apply got in between
            entries = collect.decode(collect.NAMES[0], forge.variables[collect.NAMES[0]])
            forge.variables[collect.NAMES[0]] = collect.encode(collect.NAMES[0], {**entries, late: {"tree": T["7"]}})
        return real(name)

    monkeypatch.setattr(forge, "get_variable", racing)
    out = invoke(rt, ["head", "slots", "apply", "--approved", digest])
    assert out.exit_code == 1 and "registry changed since the read" in out.stderr + out.stdout
    assert late in collect.decode(collect.NAMES[0], forge.variables[collect.NAMES[0]]) and forge.calls == []


def test_a_writer_that_finished_before_the_apply_keeps_its_entry(world):
    from steuermaschine_heinzel import state as statedir
    from steuermaschine_heinzel.native.slots_lib import registry_lock

    rt, forge, _ = world
    first = collected(rt)
    with registry_lock.writer_lock(statedir.path("focused-pr-registry.lock")):  # a `head apply` appends its plan, under the shared lock
        entries = collect.decode(collect.NAMES[0], forge.variables[collect.NAMES[0]])
        forge.variables[collect.NAMES[0]] = collect.encode(collect.NAMES[0], {**entries, f"pull_request:{H['f']}y": {"tree": T["7"]}})
    stale = invoke(rt, ["head", "slots", "apply", "--approved", first["digest"]])
    assert stale.exit_code == 1 and forge.calls == []  # the approved digest no longer matches what reads now
    out = invoke(rt, ["head", "slots", "apply", "--approved", collected(rt)["digest"]])
    assert out.exit_code == 0 and f"pull_request:{H['f']}y" in collect.decode(collect.NAMES[0], forge.variables[collect.NAMES[0]])


def test_a_slot_that_loses_every_entry_is_written_as_an_empty_registry_not_an_empty_value(world):
    rt, forge, _ = world
    forge.variables[collect.NAMES[1]] = collect.encode(collect.NAMES[1], {f"pull_request:{H['e']}": pr_plan("e", "4"), f"push-tree:{T['4']}": main_plan("4")})
    out = invoke(rt, ["--format", "json", "head", "slots", "apply", "--approved", collected(rt)["digest"]])
    assert out.exit_code == 0, out
    assert forge.variables[collect.NAMES[1]] != "" and collect.decode(collect.NAMES[1], forge.variables[collect.NAMES[1]]) == {}


def test_a_partial_write_records_a_receipt_of_what_was_written(world, monkeypatch):
    rt, forge, _ = world
    digest = collected(rt)["digest"]
    real = forge.get_variable
    state = {"writes": 0}

    def get(name):
        if forge.calls and not state["writes"]:
            state["writes"] = 1
            forge.variables[collect.NAMES[3]] = collect.encode(collect.NAMES[3], {"pull_request:" + "7" * 40: {}})  # someone writes another slot meanwhile
        return real(name)

    monkeypatch.setattr(forge, "get_variable", get)
    out = invoke(rt, ["head", "slots", "apply", "--approved", digest])
    assert out.exit_code == 1 and "Written before this" in out.stderr + out.stdout and "receipt heinzel.slots.collect/" in out.stderr + out.stdout
    receipt = rt.providers.store.get_receipt("heinzel.slots.collect", digest[:16])
    assert receipt["partial"] is True and receipt["written"] == [collect.NAMES[0]]
