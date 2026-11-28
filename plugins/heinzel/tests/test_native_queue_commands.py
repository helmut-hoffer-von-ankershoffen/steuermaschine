"""`sm queue list | why | add | drop` and `sm daemon queue` through the command tree, on a copy of the real state file
and a scripted `gh`. No network, no real `gh`, `bd` or state.

fixtures/queue/state-2026-11-12.json: the last 25 entries and every other key of ~/.cache/hz-queue.json as the old
loop wrote it, copied 2026-11-12 22:41 CET with `python3 -c 'json.load(...)'` (read only)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native import queue as area
from steuermaschine_heinzel.plugin import plugin

REAL = Path(__file__).parent / "fixtures" / "queue" / "state-2026-11-12.json"
CORE = "helmut-hoffer-von-ankershoffen/heinzel"
HEAD = "e9c943810ea75cfad155a431a2f168b2a9559828"
MAIN = "1" * 40
DIFF = "diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n+++ b/src/x.py\n@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n"
WEAKER = "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -10,1 +10,2 @@\n   steps:\n+    continue-on-error: true\n"
GREEN = [{"name": n, "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-11-12T20:00:00Z"} for n in ("CI OK", "merge-gate", "coverage-floor")]
PR = {"state": "OPEN", "isDraft": False, "headRefOid": HEAD, "headRefName": "fix/x", "mergeStateStatus": "CLEAN", "statusCheckRollup": GREEN, "title": "fix: x", "body": "one fix", "author": {"login": "helmut-hoffer-von-ankershoffen"}}
RULES = [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": c} for c in ("CI OK", "merge-gate", "coverage-floor")]}}]
MAIN_RUNS = [{"status": "completed", "conclusion": "success", "databaseId": 500, "headSha": MAIN, "attempt": 1, "displayTitle": "feat: x (#700)", "createdAt": "2026-11-12T19:00:00Z"}]


@pytest.fixture(autouse=True)
def nothing_real(tmp_path, monkeypatch):
    """A throwaway HOME (holds, lock, log, notes, alarms), no owner push, and no process outside the scripted runner."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".cache").mkdir()
    monkeypatch.setenv("HZ_ALARM_NOTIFY", "off")
    monkeypatch.delenv("HZ_QUEUE_STATE", raising=False)
    monkeypatch.delenv("HZ_MAIN_BATCH_PUBLISH", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: pytest.fail(f"a real process was started: {argv}"))
    yield
    area.reset()


@pytest.fixture
def gh():
    return FakeGh().on("pr diff 801", DIFF).on("pr view 801", PR).on("rules/branches/main", RULES).on("run list", MAIN_RUNS)


@pytest.fixture
def rt(tmp_path, gh):
    state = tmp_path / ".cache" / "queue.json"
    shutil.copy(REAL, state)
    records = tmp_path / "records"
    (records / "research").mkdir(parents=True)
    (records / "AGENTS.md").write_text("## 9. Log\n")
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {"queue_state_file": str(state), "queue_records_dir": str(records)}}, Path("."))
    runtime = Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=True), stack, settings,
                      _providers=fake_providers(commands=gh))
    runtime.state_file, runtime.notes = state, records / "AGENTS.md"
    return runtime


def items(rt):
    return json.loads(rt.state_file.read_text())["items"]


def queue_one(rt, **fields):
    data = json.loads(rt.state_file.read_text())
    row = {**data["items"][-1], "key": "801", "pr": "801", "status": "queued", "detail": "", "noted": "", "reviewed_head": "", "waiting_since": None,
           "ready_since": None, "wait_reason": "", "merge_sha": "", "guarded_head": HEAD, **fields}
    data["items"].append(row)
    data["last_batch"] = 0
    rt.state_file.write_text(json.dumps(data))


def test_every_file_the_queue_reads_or_writes_is_inside_the_sandbox(rt, tmp_path):
    """No default path may apply in a test: state, lock, log, notes, alarm state, holds and the flakes cache."""
    from steuermaschine.native import NativeRuntime, using
    from steuermaschine_heinzel.native.queue_lib import edge, hzlib, stall

    with using(NativeRuntime(rt.providers, rt.settings, rt.project)):
        module = area._queue()
    paths = [module.state_path(), module.lock_path(), statedir.root() / "queue.log", edge.notes_file(), edge.records_dir(), stall.state_path(),
             hzlib.release_holds(), statedir.root() / "merge-freeze", statedir.root() / "note.lock", module.KNOWN_FILE, module.FIRST]
    assert all(tmp_path in path.parents or statedir.root() in path.parents for path in paths), paths
    assert module.state_path() == rt.state_file and edge.notes_file() == rt.notes
    assert statedir.root() in hzlib.load("hz-flakes").CACHE.parents  # the queue reads a run's failing tests through a fresh copy


# ----------------------------------------------------------------------------- list, why
def test_list_reads_the_real_state_file_and_prints_what_the_old_listing_printed(rt):
    out = invoke(rt, ["queue", "list"])
    assert out.exit_code == 0, out
    lines = out.stdout.splitlines()
    data = json.loads(REAL.read_text())
    assert all(line.startswith(f"{i['key']:<12} {i['status']:<11}") for line, i in zip(lines, data["items"], strict=False))
    assert any(line.startswith("last batch ") for line in lines)
    assert "hz-" not in out.stdout


def test_a_corrupt_state_file_refuses_every_command_with_exit_1_and_is_never_an_empty_queue(rt):
    rt.state_file.write_text('{"items": [')
    for argv in (["queue", "list"], ["queue", "why"], ["queue", "drop", "796"], ["queue", "add", "801"]):
        out = invoke(rt, argv)
        assert out.exit_code == 1 and "unreadable" in out.stderr, (argv, out)
    assert rt.state_file.read_text() == '{"items": ['


def test_list_json_is_one_document_of_the_model_with_typed_entries_and_red_main(rt):
    out = invoke(rt, ["--format", "json", "queue", "list", "--active"])
    doc = area.QueueList.model_validate(json.loads(out.stdout))
    data = json.loads(REAL.read_text())
    assert doc.hidden == sum(i["status"] in ("merged", "needs work") for i in data["items"])
    assert all(e.status not in ("merged", "needs work") for e in doc.entries)
    assert doc.red_main.restored_run == data["main_restored"]["run"] and doc.red_main.pinned_run is None


def test_list_shows_a_pinned_red_main_and_its_batch_as_typed_fields(rt):
    data = json.loads(rt.state_file.read_text())
    data.update(main_failure={"databaseId": 900, "headSha": "a" * 40, "fixed_by": ["bbbbbbbb"]}, red_batch={"prs": ["790", "791"], "shas": [], "at": 1})
    rt.state_file.write_text(json.dumps(data))
    doc = area.QueueList.model_validate(json.loads(invoke(rt, ["--format", "json", "queue", "list"]).stdout))
    assert (doc.red_main.pinned_run, doc.red_main.pinned_head, doc.red_main.fixed_by, doc.red_main.red_batch) == (900, "a" * 40, ["bbbbbbbb"], ["790", "791"])
    assert any(line.startswith("main failure pinned: run 900") for line in doc.lines)


def test_why_names_the_verdict_and_reads_main_through_the_process_runner(rt, gh):
    out = invoke(rt, ["--format", "json", "queue", "why"])
    doc = area.QueueWhy.model_validate(json.loads(out.stdout))
    assert out.exit_code == 0 and doc.verdict == "nothing queued" and doc.merge_freeze is False
    assert gh.calls and all(c[0] == "gh" for c in gh.calls) and gh.calls[0][:3] == ["gh", "run", "list"] and not gh.writes()
    text = invoke(rt, ["queue", "why"]).stdout
    assert text.splitlines()[0] == "queued: none" and text.splitlines()[-1] == "verdict: nothing queued"


def test_why_reports_a_stall_with_green_checks_as_a_typed_field(rt):
    queue_one(rt, key="9001", pr="9001", waiting_since=1, wait_reason="BLOCKED with green checks: required contexts attached to another suite")
    doc = area.QueueWhy.model_validate(json.loads(invoke(rt, ["--format", "json", "queue", "why"]).stdout))
    assert doc.queued == ["9001"] and doc.stalled_with_green_checks == ["9001"]
    assert any("9001" in line and "BLOCKED with green checks" in line for line in doc.stalls)


def test_why_names_the_holder_of_a_merge_freeze(rt, tmp_path):
    (statedir.root() / "merge-freeze").write_text("incident")
    doc = area.QueueWhy.model_validate(json.loads(invoke(rt, ["--format", "json", "queue", "why"]).stdout))
    assert doc.merge_freeze is True and doc.freeze_holder


# ----------------------------------------------------------------------------- add
def test_add_queues_a_pr_whose_diff_passes_the_guard(rt, gh):
    out = invoke(rt, ["queue", "add", "801"])
    assert (out.exit_code, out.stdout) == (0, "queued 801\n"), out
    row = items(rt)[-1]
    assert (row["key"], row["repo"], row["status"], row["reviewed_head"], row["fixes_main"], row["after"]) == ("801", CORE, "queued", "", False, [])
    assert row["guarded_head"] == HEAD
    view = f"pr view 801 -R {CORE} --json headRefOid"
    assert gh.lines() == [view, f"pr diff 801 -R {CORE}", view, f"pr view 801 -R {CORE} --json title,body"] and not gh.writes()
    assert rt.notes.read_text() == "## 9. Log\n"


def test_add_refuses_when_the_head_moves_while_the_guard_reads_the_diff(rt, gh):
    """Review F2: the guard's verdict is bound to one head; a push during the guard read binds nothing."""
    gh.on("--json headRefOid", {"headRefOid": HEAD}, {"headRefOid": "f" * 40})
    out = invoke(rt, ["queue", "add", "801"])
    assert out.exit_code == 1 and "moved while the diff guard read it" in out.stderr, out
    assert "801" not in [i["key"] for i in items(rt) if i["status"] == "queued"]


def test_add_json_is_one_document_with_the_queued_keys(rt):
    out = invoke(rt, ["--format", "json", "queue", "add", "--after", "796", "--bead", "abc", "--fixes-main", "801"])
    doc = area.QueueChanged.model_validate(json.loads(out.stdout))
    assert (doc.action, doc.queued, doc.lines, doc.exit) == ("add", ["801"], ["queued 801 after 796"], 0)
    row = items(rt)[-1]
    assert (row["after"], row["bead"], row["fixes_main"]) == (["796"], "abc", True)


def test_add_refuses_a_diff_that_weakens_a_check_and_queues_it_with_a_review_reason_and_a_note(rt, gh):
    gh.on("pr diff 801", WEAKER)
    before = rt.state_file.read_text()
    refused = invoke(rt, ["queue", "add", "801"])
    assert refused.exit_code == 1 and refused.stdout.strip() == "" and "FLAG" in refused.stderr and "refused 801" in refused.stderr
    assert rt.state_file.read_text() == before
    reviewed = invoke(rt, ["queue", "add", "--reviewed", "review: research/review-pr801.md", "acme", "801"])
    assert reviewed.exit_code == 0 and reviewed.stdout == "queued acme #801\n" and "FLAG" in reviewed.stderr
    note = rt.notes.read_text().splitlines()[-1]
    assert note.endswith("queued past the diff guard (coe), reviewed: review: research/review-pr801.md") and note.startswith("- ")


def test_add_pins_the_reviewed_head_and_refuses_when_the_pr_moved(rt, gh):
    pinned = invoke(rt, ["queue", "add", "--reviewed", "r", "--reviewed-head", HEAD, "801"])
    assert pinned.exit_code == 0 and items(rt)[-1]["reviewed_head"] == HEAD
    assert f"#801 pinned to {HEAD}; independent review: r" in rt.notes.read_text()
    moved = invoke(rt, ["queue", "add", "--reviewed", "r", "--reviewed-head", "f" * 40, "801"])
    assert moved.exit_code == 1 and "current reviewed head differs from --reviewed-head" in moved.stderr


@pytest.mark.parametrize(("argv", "message"), [
    (["--reviewed", "r", "--reviewed-head", "", "801"], "--reviewed-head requires one normal PR"),
    (["--reviewed-head", HEAD, "801"], "--reviewed-head requires one normal PR"),
    (["--diagnostics-main", "", "801"], "--diagnostics-main needs a purpose"),
    (["--safety-fix-main", "", "801"], "--safety-fix-main needs a purpose"),
    (["--mitigation-main", "", "--mitigation-evidence", "e", "801"], "--mitigation-evidence requires --mitigation-main"),
    (["--reviewed-progress-main", "", "801"], "--reviewed-progress-main needs a purpose"),
    (["--mitigation-evidence", "", "801"], "--mitigation-evidence requires --mitigation-main"),
    (["--mitigation-main", "m", "801"], "--mitigation-main requires --mitigation-evidence"),
    (["--diagnostic-head", HEAD, "801"], "--diagnostic-head requires --diagnostics-main"),
    (["--safety-head", HEAD, "801"], "--safety-head requires --safety-fix-main"),
    (["--mitigation-head", HEAD, "801"], "--mitigation-head requires --mitigation-main"),
    (["--progress-head", HEAD, "801"], "--progress-head requires --reviewed-progress-main"),
    (["--reviewed", "r", "--diagnostics-main", "d", "--diagnostic-head", "", "801"], "--diagnostics-main requires one core PR"),
    (["--for", "web=abc", "801"], "--for and --dry-run belong to --fix-forward"),
])
def test_add_refuses_what_the_old_parser_refused_also_for_an_option_given_empty(rt, argv, message):
    """Review finding B2: "given but empty" is not "absent". A caller whose SHA lookup failed must be refused."""
    before = rt.state_file.read_text()
    out = invoke(rt, ["queue", "add", *argv])
    assert out.exit_code == 1 and message in out.stderr and out.stdout.strip() == "", out
    assert rt.state_file.read_text() == before


def test_add_refuses_two_kinds_as_a_usage_error(rt):
    out = invoke(rt, ["queue", "add", "--fixes-main", "--fix-forward", "801"])
    assert out.exit_code == 2 and items(rt)[-1]["key"] != "801"


@pytest.mark.parametrize("argv", [["queue", "add", "x"], ["queue", "drop", "x"], ["queue", "add", "801", "nope 3"],
                                  ["queue", "add", "--after", "x", "801"]])
def test_a_word_that_names_no_pr_is_a_usage_error_with_a_clear_message(rt, argv):
    before = rt.state_file.read_text()
    out = invoke(rt, argv)
    assert out.exit_code == 2 and "not a PR" in out.stderr and "give a PR number" in out.stderr and out.stdout.strip() == "", out
    assert "Traceback" not in out.stderr + out.stdout and rt.state_file.read_text() == before
    out = invoke(rt, ["--format", "json", *argv])
    assert out.exit_code == 2 and "not a PR" in out.stdout + out.stderr


# ----------------------------------------------------------------------------- drop
def test_drop_removes_only_the_named_entry_keeps_every_other_key_and_says_what_it_did(rt):
    before = json.loads(rt.state_file.read_text())
    victim = before["items"][3]["key"]
    out = invoke(rt, ["queue", "drop", *victim.split(), "99999"])
    after = json.loads(rt.state_file.read_text())
    assert out.exit_code == 0 and out.stdout.splitlines() == [f"dropped {victim}", "not in the queue: 99999"], out
    assert after["items"] == [i for i in before["items"] if i["key"] != victim]
    assert {k: v for k, v in after.items() if k != "items"} == {k: v for k, v in before.items() if k != "items"}


def test_two_named_state_files_are_refused_and_without_the_setting_the_environment_names_the_file(rt, tmp_path, monkeypatch):
    """Review finding S3: a session that isolates itself with HZ_QUEUE_STATE must never write another file."""
    other = tmp_path / "other.json"
    shutil.copy(REAL, other)
    monkeypatch.setenv("HZ_QUEUE_STATE", str(other))
    before = rt.state_file.read_text()
    out = invoke(rt, ["queue", "drop", "796"])
    assert out.exit_code == 2 and "two state files" in out.stderr and rt.state_file.read_text() == before and other.read_text() == REAL.read_text()
    rt.settings.of("heinzel")["queue_state_file"] = None
    assert invoke(rt, ["queue", "drop", "796"]).exit_code == 0
    assert "796" not in [i["key"] for i in json.loads(other.read_text())["items"]] and rt.state_file.read_text() == before


# ----------------------------------------------------------------------------- the loop's tick
ARM = f"pr merge 801 -R {CORE} --squash --auto --match-head-commit {HEAD}"


def test_tick_arms_auto_merge_on_a_queued_pr_at_the_head_the_guard_read(rt, gh):
    """Owner 2026-11-22: GitHub merges on green; the queue only arms auto-merge, pinned to the guarded head."""
    queue_one(rt)
    gh.on("pr list", []).on("pr merge 801", "")
    doc = area.QueueTick.model_validate(json.loads(invoke(rt, ["--format", "json", "daemon", "queue"]).stdout))
    assert doc.ran and doc.queued == ["801"]
    assert [line for line in gh.lines() if line.startswith("pr merge")] == [ARM]
    assert not [line for line in gh.lines() if "update-branch" in line]  # the serial update loop is retired
    assert items(rt)[-1]["detail"] == "auto-merge armed"


def test_tick_records_a_merge_github_made_and_stamps_its_bead(rt, gh):
    queue_one(rt, bead="abc")
    gh.on("pr list", []).on("pr view 801", {**PR, "state": "MERGED"}).on("--json mergeCommit", {"mergeCommit": {"oid": MAIN}})
    gh.on("show heinzel-management-abc", {"labels": []}).on("update heinzel-management-abc", "").on("label add heinzel-management-abc", "")
    assert invoke(rt, ["daemon", "queue"]).exit_code == 0
    row = items(rt)[-1]
    assert (row["status"], row["detail"], row["merge_sha"]) == ("merged", HEAD[:8], MAIN[:8]) and not gh.writes()
    assert any(line.endswith("sm queue: #801 merged") for line in rt.notes.read_text().splitlines())


def test_tick_never_arms_a_head_the_guard_did_not_read(rt, gh):
    """Review F2: a push after queueing must not merge with a diff the guard never read."""
    queue_one(rt, guarded_head="f" * 40)
    gh.on("pr list", []).on("pr merge 801", "")
    assert invoke(rt, ["daemon", "queue"]).exit_code == 0
    assert not [line for line in gh.lines() if line.startswith("pr merge")]
    row = items(rt)[-1]
    assert row["status"] == "needs work" and row["detail"].startswith("head moved from ffffffff after the diff guard read it"), row


def test_a_moved_head_of_an_armed_pr_switches_auto_merge_off(rt, gh):
    queue_one(rt, guarded_head="f" * 40)
    gh.on("pr list", []).on("pr view 801", {**PR, "autoMergeRequest": {"mergeMethod": "SQUASH"}}).on("pr merge 801", "")
    assert invoke(rt, ["daemon", "queue"]).exit_code == 0
    assert [line for line in gh.lines() if line.startswith("pr merge")] == [f"pr merge 801 -R {CORE} --disable-auto"]


@pytest.mark.parametrize("freeze", ["hold", "release-pr", "unreadable"])
def test_a_freeze_keeps_auto_merge_off_and_it_is_armed_once_the_freeze_lifts(rt, gh, freeze):
    """The release freeze (8i4oi): no auto-merge while a release hold, an open release/* PR, or unreadable evidence."""
    queue_one(rt)
    hold = statedir.root() / "merge-freeze"
    if freeze == "hold":
        hold.touch()
        gh.on("pr list", [])
    elif freeze == "release-pr":
        gh.on("pr list", [{"number": 900, "headRefName": "release/0.9.56"}], [])
    gh.on("pr view 801", {**PR, "autoMergeRequest": {"mergeMethod": "SQUASH"}}, PR).on("pr merge 801", "")
    assert invoke(rt, ["daemon", "queue"]).exit_code == 0
    assert [line for line in gh.lines() if line.startswith("pr merge")] == [f"pr merge 801 -R {CORE} --disable-auto"]
    assert items(rt)[-1]["detail"] == "merge freeze: auto-merge off until it lifts"
    hold.unlink(missing_ok=True)
    gh.on("pr list", [])
    gh.calls.clear()
    area.reset()
    assert invoke(rt, ["daemon", "queue"]).exit_code == 0
    assert [line for line in gh.lines() if line.startswith("pr merge")] == [ARM] and items(rt)[-1]["detail"] == "auto-merge armed"


def test_an_incident_freeze_also_holds_queued_plugins(rt, gh):
    queue_one(rt, key="plugins #801", repo="helmut-hoffer-von-ankershoffen/heinzel-plugins")
    (statedir.root() / "merge-freeze").touch()
    gh.on("pr view 801", PR).on("pr merge 801", "")
    assert invoke(rt, ["daemon", "queue"]).exit_code == 0
    assert not any(line.startswith("pr merge") for line in gh.lines())
    assert items(rt)[-1]["detail"] == "merge freeze: auto-merge off until it lifts"


def test_a_pr_of_an_author_not_on_auto_queue_authors_is_never_armed(rt, gh):
    """Review of sm #70: a hand-queued PR of anyone else is not armed (and an armed one is switched off)."""
    queue_one(rt)
    gh.on("pr list", []).on("pr view 801", {**PR, "author": {"login": "someone"}, "autoMergeRequest": {"mergeMethod": "SQUASH"}}).on("pr merge 801", "")
    assert invoke(rt, ["daemon", "queue"]).exit_code == 0
    assert [line for line in gh.lines() if line.startswith("pr merge")] == [f"pr merge 801 -R {CORE} --disable-auto"]
    row = items(rt)[-1]
    assert row["status"] == "needs work" and "author someone is not on auto_queue_authors" in row["detail"], row


def test_the_loop_wakes_when_a_freeze_starts_and_disarms_at_once(rt, gh):
    """8i4oi: a freeze that starts during the interval wakes the loop; it does not wait for the next 300 s tick."""
    queue_one(rt)
    gh.on("pr list", []).on("pr view 801", PR, {**PR, "autoMergeRequest": {"mergeMethod": "SQUASH"}}).on("pr merge 801", "")
    assert invoke(rt, ["daemon", "queue"]).exit_code == 0
    assert json.loads(invoke(rt, ["--format", "json", "daemon", "queue"]).stdout) == {}  # not due, nothing changed
    (statedir.root() / "merge-freeze").touch()
    gh.calls.clear()
    woke = area.QueueTick.model_validate(json.loads(invoke(rt, ["--format", "json", "daemon", "queue"]).stdout))
    assert woke.ran and woke.wake == "merge freeze started"
    assert [line for line in gh.lines() if line.startswith("pr merge")] == [f"pr merge 801 -R {CORE} --disable-auto"]


def test_a_freeze_that_starts_while_the_tick_arms_is_undone_at_once(rt, gh):
    """The interleaving: the tick reads "not frozen", the cut takes the freeze and disarms everything, then the tick
    arms. The tick reads the freeze again after the arm and switches auto-merge off."""
    queue_one(rt)
    gh.on("pr list", [], [{"number": 900, "headRefName": "release/0.9.56"}]).on("pr merge 801", "")
    assert invoke(rt, ["daemon", "queue"]).exit_code == 0
    assert [line for line in gh.lines() if line.startswith("pr merge")] == [ARM, f"pr merge 801 -R {CORE} --disable-auto"]
    assert items(rt)[-1]["detail"] == "merge freeze: auto-merge off until it lifts"


def test_a_refused_arm_is_kept_queued_with_gh_s_reason(rt, gh):
    from steuermaschine.protocols import Result

    queue_one(rt)
    gh.on("pr list", []).on("pr merge 801", Result(1, "", "GraphQL: Pull request is in clean status"))
    assert invoke(rt, ["daemon", "queue"]).exit_code == 0
    row = items(rt)[-1]
    assert row["status"] == "queued" and row["detail"].startswith("auto-merge not armed"), row


def test_tick_refuses_a_row_without_a_guarded_head(rt, gh):
    queue_one(rt, guarded_head="")
    gh.on("pr merge 801", "")
    assert invoke(rt, ["daemon", "queue"]).exit_code == 0
    assert not [line for line in gh.lines() if line.startswith("pr merge")]
    row = items(rt)[-1]
    assert row["status"] == "needs work" and "no guarded head" in row["detail"], row


def test_tick_wakes_when_the_queue_file_changed_and_ticks_again_when_the_interval_passed(rt, gh):
    assert json.loads(invoke(rt, ["--format", "json", "daemon", "queue"]).stdout)["ran"] is True
    assert json.loads(invoke(rt, ["--format", "json", "daemon", "queue"]).stdout) == {}
    assert invoke(rt, ["queue", "add", "801"]).exit_code == 0
    assert json.loads(invoke(rt, ["--format", "json", "daemon", "queue", "--interval", "0"]).stdout)["wake"] == "queue changed"
    again = json.loads(invoke(rt, ["--format", "json", "daemon", "queue"]).stdout)  # the interval of the last tick has passed
    assert again["ran"] is True and again["wake"] == ""


def test_a_second_loop_waits_for_the_lock_holder_and_a_negative_interval_is_a_usage_error(rt, tmp_path):
    assert invoke(rt, ["daemon", "queue", "--interval", "-1"]).exit_code == 2
    import fcntl

    with (statedir.root() / "queue.lock").open("a+") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        out = invoke(rt, ["daemon", "queue"])
        assert out.exit_code == 75 and "another queue loop holds" in out.stderr


# ----------------------------------------------------------------------------- main proof in the tick (Core ADR 0149)
PROOF_PLAN = 'WHOLE_SUITES = {"web": VITEST_ALL}\nMAIN_PROOF_CONTEXT = "hz/main-proof"\n'


def proof_ready(sha=MAIN, run=21, state="ready", reason="its push run is green with CI OK: the judge can run"):
    from steuermaschine_heinzel.native import merge_ledger

    return merge_ledger.MainProof(state=state, sha=sha, run=run, reason=reason)


def detached(gh) -> list[list[str]]:
    return [c for c in gh.calls if c[:2] == ["sh", "-c"]]


def tier_main(rt, gh, monkeypatch, push_run: dict) -> None:
    """The merge tier at main with the main proof, and main's push run as GitHub shows it: read by the REAL command."""
    import base64

    from steuermaschine_heinzel.native import merge_tier
    from steuermaschine_heinzel.native.queue_lib import hzlib

    monkeypatch.setattr(hzlib, "merge_tier", lambda: True)
    for table in (rt.settings.of("heinzel"), rt.settings):  # the merged view too: core_repo_dir has no default
        table.update(repo=CORE, core_repo_dir=str(rt.state_file.parent / "core"))
    gh.on(f"contents/{merge_tier.PLAN_PATH}", {"encoding": "base64", "content": base64.b64encode(PROOF_PLAN.encode()).decode()})
    gh.on("git/ref/heads/main", {"object": {"sha": MAIN}}).on(f"commits/{MAIN}/statuses", [])
    gh.on(f"head_sha={MAIN}", {"workflow_runs": [{"id": 21, "head_sha": MAIN, **push_run}]})
    gh.on("actions/runs/21/jobs", {"jobs": [{"name": "CI OK", "conclusion": "success"}]})
    gh.on("nohup", "").on("cat-file -e", "")


def test_a_pending_main_run_does_not_block_the_tick_and_records_nothing(rt, gh, monkeypatch):
    """The tick asks the real `queue main-proof --check` once: a few reads, no judge, no clone, nothing recorded."""
    tier_main(rt, gh, monkeypatch, {"status": "in_progress"})
    tick = area.QueueTick.model_validate(json.loads(invoke(rt, ["--format", "json", "daemon", "queue"]).stdout))
    assert tick.ran and any(f"main proof {MAIN[:9]}: waiting: its push run has not finished" in line for line in tick.lines), tick.lines
    assert detached(gh) == [] and "main_proof" not in json.loads(rt.state_file.read_text())
    assert not [c for c in gh.calls if c[0] in ("uv", "git") and "clone" in c] and not gh.writes()
    asked = [line for line in gh.lines() if f"head_sha={MAIN}" in line]
    assert len(asked) == 1  # once per tick; the next tick asks again
    area.reset()
    invoke(rt, ["daemon", "queue"])
    assert len([line for line in gh.lines() if f"head_sha={MAIN}" in line]) == 2 and detached(gh) == []


def test_a_green_main_run_starts_the_judge_detached_once_per_commit(rt, gh, monkeypatch):
    tier_main(rt, gh, monkeypatch, {"status": "completed", "conclusion": "success"})
    first = area.QueueTick.model_validate(json.loads(invoke(rt, ["--format", "json", "daemon", "queue"]).stdout))
    assert any(f"main proof {MAIN[:9]}: judge started detached (run 21; log main-proof.log)" in line for line in first.lines), first.lines
    (started,) = detached(gh)
    assert started[3] == str(rt.state_file.with_name("main-proof.log")) and "os.setsid()" in started[6]  # its own session
    assert started[-4:] == ["queue", "main-proof", "--sha", MAIN]
    record = json.loads(rt.state_file.read_text())["main_proof"]
    assert (record["sha"], record["run"]) == (MAIN, 21)
    assert not [c for c in gh.calls if c[0] == "uv"]  # the judge never runs inside the tick
    area.reset()
    again = area.QueueTick.model_validate(json.loads(invoke(rt, ["--format", "json", "daemon", "queue"]).stdout))
    assert len(detached(gh)) == 1 and any("attempted already (run 21); not repeated" in line for line in again.lines), again.lines
    assert json.loads(rt.state_file.read_text())["main_proof"] == record  # the tick's own save keeps the record


def test_a_main_commit_the_core_clone_lacks_waits_and_spends_no_attempt(rt, gh, monkeypatch):
    from steuermaschine.protocols import Result

    tier_main(rt, gh, monkeypatch, {"status": "completed", "conclusion": "success"})
    gh.on("cat-file -e", Result(1, "", ""))  # the clone was not fetched yet
    tick = area.QueueTick.model_validate(json.loads(invoke(rt, ["--format", "json", "daemon", "queue"]).stdout))
    assert any("waiting: main's commit is not in the Core clone" in line for line in tick.lines), tick.lines
    assert detached(gh) == [] and "main_proof" not in json.loads(rt.state_file.read_text())
    area.reset()
    gh.on("cat-file -e", "")  # fetched: the next tick starts the judge
    invoke(rt, ["daemon", "queue"])
    assert len(detached(gh)) == 1 and json.loads(rt.state_file.read_text())["main_proof"]["sha"] == MAIN


def test_the_next_green_main_commit_gets_its_own_attempt(rt, gh, monkeypatch):
    from steuermaschine_heinzel.native.queue_lib import hzlib

    monkeypatch.setattr(hzlib, "merge_tier", lambda: True)
    asked = []
    monkeypatch.setattr(area, "call", lambda command, **kw: asked.append((command.path, kw)) or proof_ready())
    gh.on("nohup", "")
    invoke(rt, ["daemon", "queue"])
    assert asked == [("queue main-proof", {"check": True})] and len(detached(gh)) == 1
    area.reset()
    monkeypatch.setattr(area, "call", lambda command, **kw: proof_ready(sha="c" * 40, run=22))
    invoke(rt, ["daemon", "queue"])
    assert len(detached(gh)) == 2 and json.loads(rt.state_file.read_text())["main_proof"]["sha"] == "c" * 40


def test_the_tick_checks_main_proof_only_with_the_merge_tier(rt, gh, monkeypatch):
    from steuermaschine_heinzel.native.queue_lib import hzlib

    asked = []
    monkeypatch.setattr(area, "call", lambda command, **kw: asked.append(command.path) or proof_ready())
    monkeypatch.setattr(hzlib, "merge_tier", lambda: False)
    old = area.QueueTick.model_validate(json.loads(invoke(rt, ["--format", "json", "daemon", "queue"]).stdout))
    assert old.ran and asked == [] and not any("main proof" in line for line in old.lines)  # an older Core: nothing changes
    assert detached(gh) == [] and "main_proof" not in json.loads(rt.state_file.read_text())


def test_a_refused_or_failing_check_is_a_log_line(rt, gh, monkeypatch):
    from steuermaschine.engine import Refuse
    from steuermaschine.protocols import Result
    from steuermaschine_heinzel.native.queue_lib import hzlib

    monkeypatch.setattr(hzlib, "merge_tier", lambda: True)

    def refused(command, **kw):
        raise Refuse("cannot read main: down", "sm queue main-proof")

    monkeypatch.setattr(area, "call", refused)
    done = invoke(rt, ["--format", "json", "daemon", "queue"])
    assert done.exit_code == 0 and any("main proof: cannot read main" in line for line in json.loads(done.stdout)["lines"])
    area.reset()
    monkeypatch.setattr(area, "call", lambda command, **kw: 1 / 0)
    done = invoke(rt, ["--format", "json", "daemon", "queue"])
    assert done.exit_code == 0 and any("main proof failed: ZeroDivisionError" in line for line in json.loads(done.stdout)["lines"])
    area.reset()
    monkeypatch.setattr(area, "call", lambda command, **kw: proof_ready())
    gh.on("nohup", Result(1, "", "no shell"))
    done = invoke(rt, ["--format", "json", "daemon", "queue"])
    assert any("could not start the judge (exit 1); a later tick tries again" in line for line in json.loads(done.stdout)["lines"])
    assert "main_proof" not in json.loads(rt.state_file.read_text())  # nothing ran: no attempt is spent


