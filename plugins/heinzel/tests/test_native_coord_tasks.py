"""`sm beads tasks ...`, `sm beads refill` and `sm doc checkpoint`. The refill helpers' assertions are the former
tests'; the tracker (bd) and GitHub are the fake process runner's: no bead is created, changed or closed."""

import json
import time
from datetime import UTC, datetime

from coord_kit import NOW, app_runtime, ok

from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, invoke
from steuermaschine_heinzel.native import docs, refill
from steuermaschine_heinzel.native.refill_lib import slots as r
from steuermaschine_heinzel.native.tasks_lib import beads

TABLE = "### Now\n\n| Item | Status | Next step | Where |\n|---|---|---|---|\n\n## 5. State\n"


def world(rows=None):
    c = FakeCommands()
    c.on(["bd"], ok("updated heinzel-management-abc\n"))
    c.on(["bd", "create"], ok("warning: slow\nheinzel-management-xy9\n"))
    c.on(["bd", "close"], ok("closed heinzel-management-abc\n"))
    c.on(["bd", "show"], lambda argv: ok([{"id": argv[2]}]) if "--json" in argv else ok("heinzel-management-abc\n" + "x" * 3000))  # --json: bd knows the id exactly
    c.on(["bd", "list"], ok(rows if rows is not None else []))
    c.on(["bd", "ready"], ok(rows if rows is not None else []))
    return c


def bd(c):
    return [call for call in c.calls if call[0] == "bd"]


def test_add_creates_the_bead_with_labels_and_prints_the_short_id(tmp_path):
    c = world()
    done = invoke(app_runtime(tmp_path, c), ["work", "add", "Logo cut off", "-p", "1", "-a", "logo", "-l", "ui,web", "--due", "10:30", "-d", "owner words", "--type", "bug"])
    today = time.strftime("%Y%m%d", time.localtime(NOW))
    assert done.stdout == "xy9\n"
    assert bd(c) == [["bd", "create", "--title", "Logo cut off", "-p", "1", "-t", "bug", "--silent", "-a", "logo", "-l", f"ui,web,due-{today}-1030", "-d", "owner words"]]
    assert invoke(app_runtime(tmp_path, c), ["work", "add", " "]).exit_code == 2


def test_add_with_ask_writes_the_ask_row_too_and_says_so_when_the_table_is_missing(tmp_path):
    (tmp_path / "AGENTS.md").write_text(TABLE)
    c = world()
    doc = beads.TaskDone.model_validate(json.loads(invoke(app_runtime(tmp_path, c), ["--format", "json", "work", "add", "Make the logo bigger", "--ask"]).stdout))
    assert doc.bead == "xy9" and doc.ask_row.startswith("| Make the logo bigger (") and doc.ask_row in (tmp_path / "AGENTS.md").read_text()
    assert bd(c)[0][-2:] == ["-l", "owner-ask"]
    (tmp_path / "AGENTS.md").write_text("no table\n")
    done = invoke(app_runtime(tmp_path, world()), ["work", "add", "x", "--ask"])
    assert done.exit_code == 1 and "the bead exists, but the tracking document has no asks table" in done.stderr


def test_note_pr_claim_due_set_and_done_call_the_tracker_with_the_time_and_the_full_id(tmp_path):
    stamp = time.strftime("%H:%M", time.localtime(NOW))
    rows = [{"id": "heinzel-management-abc", "labels": ["due-20261112-0900", "ui"]}]
    c = world(rows)
    rt = app_runtime(tmp_path, c)
    assert invoke(rt, ["work", "note", "abc", "root cause found"]).stdout == "updated heinzel-management-abc\n"
    invoke(rt, ["beads", "tasks", "pr", "heinzel-management-abc", "796", "merged"])
    invoke(rt, ["beads", "tasks", "pr", "abc", "797"])
    invoke(rt, ["beads", "tasks", "claim", "abc", "builder-1", "--due", "2026-11-13 09:00"])
    assert invoke(rt, ["beads", "tasks", "due-set", "abc", "11:45"]).stdout == "ok\n"
    assert invoke(rt, ["work", "done", "abc", "live in 0.9.40"]).stdout == "closed heinzel-management-abc\n"
    calls = bd(c)
    today = time.strftime("%Y%m%d", time.localtime(NOW))
    assert ["bd", "update", "heinzel-management-abc", "--append-notes", f"{stamp} root cause found"] in calls
    assert ["bd", "update", "heinzel-management-abc", "--append-notes", f"{stamp} PR #796 merged"] in calls and calls[2][-1] == f"{stamp} PR #797 open"
    assert ["bd", "update", "heinzel-management-abc", "--status", "in_progress", "--assignee", "builder-1"] in calls
    assert ["bd", "label", "remove", "heinzel-management-abc", "due-20261112-0900"] in calls and ["bd", "label", "add", "heinzel-management-abc", "due-20261113-0900"] in calls
    assert ["bd", "label", "add", "heinzel-management-abc", f"due-{today}-1145"] in calls
    assert calls[-2:] == [["bd", "show", "heinzel-management-abc", "--json"], ["bd", "close", "heinzel-management-abc", "--force", "--reason", "live in 0.9.40"]]
    assert invoke(rt, ["work", "done", "abc", " "]).exit_code == 2


def test_done_refuses_a_partial_id_that_the_tracker_resolves_to_another_bead_and_closes_nothing(tmp_path):
    for answer in ([{"id": "heinzel-management-abcd"}], {"id": "heinzel-management-abcd"}, [], [{"id": "heinzel-management-abc"}, {"id": "heinzel-management-abcd"}]):
        c = world()
        c.on(["bd", "show"], ok(answer))
        done = invoke(app_runtime(tmp_path, c), ["work", "done", "abc", "verified live"])
        assert done.exit_code == 2 and "is not the exact id of one bead" in done.stderr and "close" not in [call[1] for call in bd(c)], answer
    c = world()
    c.on(["bd", "show"], ok("not json"))
    done = invoke(app_runtime(tmp_path, c), ["work", "done", "abc", "verified live"])
    assert done.exit_code == 1 and "is not JSON" in done.stderr and "close" not in [call[1] for call in bd(c)]
    c = world()
    c.on(["bd", "show"], ok({"id": "heinzel-management-abc"}))  # one object instead of a list: the same bead
    assert invoke(app_runtime(tmp_path, c), ["work", "done", "heinzel-management-abc", "verified live"]).exit_code == 0
    assert bd(c)[-1][:3] == ["bd", "close", "heinzel-management-abc"]


FAIL = Result(1, "", 'Error: no issue found matching "nope"\n')


def failing_at(prefix):
    """The world with `bd PREFIX...` failing (exit 1, bd's message on stderr)."""
    c = world([{"id": "heinzel-management-abc", "labels": ["due-20261112-0900"]}])
    c.on(prefix, FAIL)
    return c


def verb(args):
    """The visible verb where there is one (`sm work add|note|done|show`); claim, pr and due-set have none yet."""
    return ["work", "list" if args[0] == "ls" else args[0], *args[1:]] if args[0] in ("add", "note", "done", "show", "ls") else ["beads", "tasks", *args]


def test_every_write_refuses_with_the_trackers_message_when_bd_fails_and_writes_nothing_after_it(tmp_path):
    (tmp_path / "AGENTS.md").write_text(TABLE)
    cases = [
        (["bd", "create"], ["add", "New thing", "--ask"]),
        (["bd", "create"], ["add", "New thing", "--due", "10:00"]),
        (["bd", "update"], ["note", "abc", "x"]),
        (["bd", "update"], ["pr", "abc", "796", "merged"]),
        (["bd", "update"], ["claim", "abc", "builder", "--due", "10:00"]),
        (["bd", "list"], ["claim", "abc", "builder", "--due", "10:00"]),
        (["bd", "label", "remove"], ["claim", "abc", "builder", "--due", "10:00"]),
        (["bd", "label", "add"], ["claim", "abc", "builder", "--due", "10:00"]),
        (["bd", "list"], ["due-set", "abc", "10:00"]),
        (["bd", "label", "remove"], ["due-set", "abc", "10:00"]),
        (["bd", "label", "add"], ["due-set", "abc", "10:00"]),
        (["bd", "show"], ["done", "abc", "verified live"]),
        (["bd", "close"], ["done", "abc", "verified live"]),
    ]
    for prefix, args in cases:
        c = failing_at(prefix)
        done = invoke(app_runtime(tmp_path, c), verb(args))
        assert done.exit_code == 1 and done.stdout == "", (prefix, args)
        assert 'Error: no issue found matching "nope"' in done.stderr and "exit 1" in done.stderr, (prefix, args)
        failed = next(i for i, call in enumerate(c.calls) if call[:len(prefix)] == prefix)
        assert failed == len(c.calls) - 1, (prefix, args, c.calls)  # nothing ran after the failing call
    assert (tmp_path / "AGENTS.md").read_text() == TABLE  # no ask row for a bead that was not created
    c = world()
    c.on(["bd", "list"], ok("not json"))
    done = invoke(app_runtime(tmp_path, c), ["beads", "tasks", "due-set", "abc", "10:00"])
    assert done.exit_code == 1 and "not JSON" in done.stderr and not [call for call in c.calls if call[1] == "label"]
    c = world()
    c.on(["bd", "show"], FAIL)
    assert invoke(app_runtime(tmp_path, c), ["work", "show", "abc"]).exit_code == 1


def test_add_refuses_when_bd_prints_no_bead_id_and_writes_no_ask_row(tmp_path):
    (tmp_path / "AGENTS.md").write_text(TABLE)
    for printed in ('Error: no issue found matching "nope"\n', "", "heinzel-management-\n", "heinzel-management-AB\n", "other-prefix-xy9\n", "heinzel-management-xy9 extra\n"):
        c = world()
        c.on(["bd", "create"], ok(printed))
        done = invoke(app_runtime(tmp_path, c), ["work", "add", "New thing", "--ask"])
        assert done.exit_code == 1 and "printed no bead id" in done.stderr and done.stdout == "", printed
    assert (tmp_path / "AGENTS.md").read_text() == TABLE
    c = world()
    c.on(["bd", "create"], ok("heinzel-management-uz3c.3\n"))
    assert invoke(app_runtime(tmp_path, c), ["work", "add", "--", "-starts with a dash"]).stdout == "uz3c.3\n"
    assert c.calls[-1][:4] == ["bd", "create", "--title", "-starts with a dash"]


def test_ids_that_are_not_of_the_projects_form_are_refused_before_bd_runs(tmp_path):
    for bad in ("", "ab", "heinzel-management-", "ABC", "abc def", "abc;rm", "heinzel-management-abc.x", "a" * 13):
        for args in (["note", bad, "x"], ["claim", bad, "a"], ["due-set", bad, "10:00"], ["pr", bad, "1"], ["done", bad, "evidence"], ["show", bad]):
            c = world()
            done = invoke(app_runtime(tmp_path, c), verb(args))
            assert done.exit_code == 2 and "not a bead id" in done.stderr and not c.calls, (bad, args)
    c = world()
    assert invoke(app_runtime(tmp_path, c), ["work", "note", "heinzel-management-uz3c.3", "x"]).exit_code == 0
    assert c.calls[-1][2] == "heinzel-management-uz3c.3"


def test_due_ls_and_show_only_read(tmp_path):
    soon = time.strftime("due-%Y%m%d-%H%M", time.localtime(NOW + 1800))
    past = time.strftime("due-%Y%m%d-%H%M", time.localtime(NOW - 1800))
    rows = [{"id": "heinzel-management-aaa", "priority": 1, "assignee": "builder-with-a-long-name", "labels": [soon], "title": "soon"},
            {"id": "heinzel-management-bbb", "priority": 0, "labels": [past], "title": "late"},
            {"id": "heinzel-management-ccc", "labels": ["due-20991231-2359", "due-bad-label-xxxxx"], "title": "far"}, {"id": "heinzel-management-ddd", "title": "none"}]
    c = world(rows)
    rt = app_runtime(tmp_path, c)
    due = invoke(rt, ["beads", "tasks", "due"]).stdout.splitlines()
    assert [line.split()[0] for line in due] == ["OVERDUE", "due"] and "bbb" in due[0] and "aaa" in due[1] and "builder-with-a-l " in due[1]
    listed = beads.TaskList.model_validate(json.loads(invoke(rt, ["--format", "json", "work", "list", "all"]).stdout))
    assert [row.bead for row in listed.rows] == ["aaa", "bbb", "ccc", "ddd"] and listed.rows[3].due == ""
    assert invoke(rt, ["work", "list", "ready"]).stdout.splitlines()[3].startswith("ddd   P? -")
    assert ["bd", "ready", "--json"] in c.calls and ["bd", "list", "--json", "--limit", "0"] in c.calls and ["bd", "list", "--status", "in_progress", "--json", "--limit", "0"] in c.calls
    shown = invoke(rt, ["work", "show", "abc"]).stdout
    assert shown.startswith("heinzel-management-abc\n") and len(shown) <= 1801
    assert not [call for call in bd(c) if call[1] in ("update", "close", "create", "label")]
    c.on(["bd", "list"], ok("not json"))
    assert invoke(rt, ["work", "list"]).stdout == ""  # an empty listing prints nothing (core: no empty line)
    assert beads.dlabel(None, NOW) is None and beads.dlabel("2026-11-13 09:00", NOW) == "due-20261113-0900"


# ----------------------------------------------------------------------------- refill (assertions moved)
NOW_UTC = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def b(i, t, labels=(), p=2, u="2026-11-08T11:50:00Z"):
    return {"id": "heinzel-management-" + i, "title": t, "labels": list(labels), "priority": p, "updated_at": u}


def test_models():
    assert r.suggest_model(b("a", "Update safety: auto-update hold")) == "Opus"
    assert r.suggest_model(b("a", "Fix docs typo")) == "Haiku"
    assert r.suggest_model(b("a", "Audio popup chips")) == "Sonnet"
    assert r.suggest_model(b("a", "Onboarding design")) == "Fable"
    assert r.suggest_model(b("a", "x", ["flaky"])) == "Opus"


def test_slots_and_load():
    assert r.free_slots([b("a", "x")] * 5, 10) == 7
    assert r.free_slots([b("a", "x")] * 5, 26) == 0
    assert r.free_slots([b("a", "x")] * 15, 1) == 0
    assert r.refill_line(0, 9) == "" and r.refill_line(3, 9) == "REFILL: 3 free slots, 9 ready"


def test_start_lines_skip_busy_and_limit():
    ready = [b("a", "one", p=2), b("b", "two", p=0), b("c", "three")]
    out = r.start_lines(ready, [b("c", "three")], 2)
    assert [line.split()[1] for line in out] == ["b", "a"]


def test_dead_owner_uses_pr_push_and_agent_ids():
    ids = r.agent_ids("| Bead | PR | Agent | Role |\n|---|---|---|---|\n| x28, hvr | #1 | a1a821f217b36c21e | Opus |")
    old = b("x28", "stale", u="2026-11-08T08:00:00Z")
    assert "resume a1a821f217b36c21e" in r.dead_owners([old], {}, ids, NOW_UTC)[0]
    assert r.dead_owners([old], {"x28": "2026-11-08T11:30:00Z"}, ids, NOW_UTC) == []
    assert r.dead_owners([b("y", "fresh")], {}, ids, NOW_UTC) == []
    assert r.bead_of({"headRefName": "fix/x28-details", "title": "t"}) == "x28" and r.bead_of({"title": "bead hvr: fix"}) == "hvr" and r.bead_of({}) is None


def test_refill_command_counts_slots_names_dead_owners_and_start_lines(tmp_path):
    fresh = datetime.fromtimestamp(NOW - 600, UTC).isoformat().replace("+00:00", "Z")
    stale = datetime.fromtimestamp(NOW - 4 * 3600, UTC).isoformat().replace("+00:00", "Z")
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "agent-ids.md").write_text("| Bead | PR | Agent | Model |\n|---|---|---|---|\n| x28 | #1 | a1a821f217b36c21e | Opus |\n")
    c = FakeCommands()
    c.on(["bd", "ready"], ok("note: 2 ready\n" + json.dumps([b("new", "Fix docs typo", p=1), b("x28", "busy")])))
    c.on(["bd", "list"], ok([b("x28", "stale work", u=stale), b("lv1", "moving", u=stale)]))
    c.on(["gh", "pr", "list"], ok([{"headRefName": "fix/lv1-thing", "title": "t", "updatedAt": fresh}, {"headRefName": "other", "title": "no bead", "updatedAt": fresh}]))
    rt = app_runtime(tmp_path, c, refill_test_load=3.0)
    out = invoke(rt, ["work", "refill"]).stdout.splitlines()
    assert out[0] == "2 in progress, 10 free slots, 2 ready, load 3"
    assert out[1].startswith("DEAD x28 idle 240 min -> resume a1a821f217b36c21e or restart: stale work") and out[2] == "START new P1 Haiku: Fix docs typo" and len(out) == 3
    doc = refill.Refill.model_validate(json.loads(invoke(app_runtime(tmp_path, c, refill_test_load=30.0), ["--format", "json", "work", "refill"]).stdout))
    assert doc.free_slots == 0 and doc.start == []
    c.on(["bd", "ready"], ok("[not json"))
    c.on(["gh", "pr", "list"], ok("x"))
    (tmp_path / "research" / "agent-ids.md").unlink()
    out = invoke(rt, ["work", "refill"]).stdout.splitlines()
    assert out[0].startswith("2 in progress, 10 free slots, 0 ready") and "resume no agent id" in out[1] and len(out) == 3  # both owners look dead without PR evidence


# ----------------------------------------------------------------------------- checkpoint
DOC = "# T\n\n## 5. State, checkpoints\n\n### Checkpoint 2026-11-12 22:36 (docs)\nline a\nline b\n\n### Checkpoint 2026-11-12 22:10\nolder\n\n## 6. Work instructions\n### not a checkpoint\n"


def test_checkpoint_is_the_first_block_of_the_state_section(tmp_path):
    (tmp_path / "AGENTS.md").write_text(DOC)
    out = invoke(app_runtime(tmp_path), ["doc", "checkpoint"]).stdout.splitlines()
    assert out == ["Latest checkpoint (historical; reconcile with `sm go` before acting):", "Checkpoint 2026-11-12 22:36 (docs)", "line a", "line b"]
    doc = docs.Checkpoint.model_validate(json.loads(invoke(app_runtime(tmp_path, doc_checkpoint_lines=2), ["--format", "json", "doc", "checkpoint"]).stdout))
    assert doc.found and doc.lines == ["Checkpoint 2026-11-12 22:36 (docs)", "line a"]
    other = invoke(app_runtime(tmp_path, doc_checkpoint_section="## 6.", doc_checkpoint_end="## 7."), ["doc", "checkpoint"]).stdout
    assert "not a checkpoint" in other


def test_checkpoint_says_so_when_there_is_none_or_the_document_is_unreadable(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# T\n\n## 5. State\nnothing dated\n\n## 6. Work\n")
    assert invoke(app_runtime(tmp_path), ["doc", "checkpoint"]).stdout.startswith("No dated checkpoint found.")
    (tmp_path / "AGENTS.md").unlink()
    done = invoke(app_runtime(tmp_path), ["doc", "checkpoint"])
    assert done.exit_code == 0 and done.stdout.startswith("Checkpoint unavailable")
