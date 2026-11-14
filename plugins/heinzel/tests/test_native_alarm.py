"""alarm, native. Parity fixture fixtures/alarm/rules.json: recorded 2026-11-12 with
`python plugins/heinzel/tests/record_alarm_fixtures.py OLD_TOOLS_DIRECTORY` (the old tool's pure rules on
alarm_cases.CASES). Old texts name old tools; the native ones name sm commands, so the old text is compared after the
tool-map sweep. No test starts a process or reads the network."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import alarm_cases  # noqa: E402

from steuermaschine.app import Runtime  # noqa: E402
from steuermaschine.config import Project  # noqa: E402
from steuermaschine.plugins import stack_of  # noqa: E402
from steuermaschine.protocols import Result  # noqa: E402
from steuermaschine.testing import FakeCommands, fake_providers, invoke  # noqa: E402
from steuermaschine_heinzel.native import alarm as alarm_mod  # noqa: E402
from steuermaschine_heinzel.native.alarm_lib import rules, watch  # noqa: E402
from steuermaschine_heinzel.native.go_lib import legacy  # noqa: E402
from steuermaschine_heinzel.plugin import plugin  # noqa: E402
from steuermaschine_heinzel.toolmap import ROWS  # noqa: E402

RECORDED = json.loads((Path(__file__).parent / "fixtures" / "alarm" / "rules.json").read_text())
NOW = alarm_cases.NOW


def in_sm_words(value):
    """An old result as the native rules spell it. The one place the sweep cannot know: the slots line of the hourly table."""
    if isinstance(value, dict):
        return {k: in_sm_words(v) for k, v in value.items()}
    if isinstance(value, list):
        return [in_sm_words(v) for v in value]
    if isinstance(value, str):
        return legacy.sweep(value, ROWS).replace("next admission; sm status", "next admission; sm status slots")
    return value


def test_every_rule_answers_as_the_old_tool_did():
    assert len(RECORDED) == len(alarm_cases.CASES) >= 35
    for row, (name, args, kwargs) in zip(RECORDED, alarm_cases.CASES, strict=True):
        mine = alarm_cases.call(rules, name, args, kwargs)
        assert mine == in_sm_words(row["result"]), (row["case"], name)
        assert "hz-" not in json.dumps(mine), (row["case"], name)


def test_the_recorded_cases_cover_every_alarm_class():
    texts = json.dumps(RECORDED)
    for word in ("STALL #801 BLOCKED with green checks for 277 min", "MAIN RED run 5", "CANDIDATE release/v0.9.40 CI run 5", "ADMISSION blocked by 2",
                 "ADMISSION idle", "REGISTRY focused slots", "paused for", "unfinished, receipt unchanged", "REHEARSAL RED", "REHEARSAL INCOMPLETE", "REHEARSAL of main"):
        assert word in texts, word


def test_order_and_exit_code():
    items = [{"cls": "INFO", "key": "a"}, {"cls": "WARN", "key": "b"}, {"cls": "ALERT", "key": "c", "acked": True}, {"cls": "ALERT", "key": "d"}]
    assert [i["key"] for i in rules.order(items)] == ["d", "b", "c", "a"] and rules.exit_code(items) == 3
    assert rules.exit_code([items[2], items[1]]) == 0


def test_merged_keeps_first_seen_and_ack_and_drops_what_only_the_watch_recorded_and_is_over():
    recorded = {"stall:801": {"cls": "ALERT", "text": "old", "first": 100.0, "acked": True, "sources": ["watch", "queue"]},
                "main-red:5": {"cls": "ALERT", "text": "MAIN RED run 5", "first": 50.0, "sources": ["watch"]},
                "candidate:0.9.40": {"cls": "WARN", "text": "web-2 is slow", "first": 70.0, "sources": ["manual"]}}
    found = [rules.alarm("ALERT", "stall:801", "STALL #801 now"), rules.alarm("WARN", "registry-slots", "REGISTRY low")]
    rows = {r["key"]: r for r in watch.merged(found, recorded, NOW)}
    assert set(rows) == {"stall:801", "registry-slots", "candidate:0.9.40"}
    assert rows["stall:801"]["first"] == 100.0 and rows["stall:801"]["acked"] and not rows["stall:801"]["new"] and rows["stall:801"]["text"] == "STALL #801 now"
    assert rows["registry-slots"]["new"] and rows["registry-slots"]["first"] == NOW


def world_for(tmp_path, commands):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {"legacy_state_dir": str(state), "core_repo_dir": "/core"}}, Path("."))
    providers = fake_providers(commands=commands)
    providers.clock.t = NOW
    return state, Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=True), stack, settings, _providers=providers)


def scripted(runs=(), main_runs=(), variables=()):
    c = FakeCommands()
    c.on(["gh", "run", "list", "-R", "helmut-hoffer-von-ankershoffen/heinzel", "-L", "100"], Result(0, json.dumps(list(runs))))
    c.on(["gh", "run", "list", "-R", "helmut-hoffer-von-ankershoffen/heinzel", "--workflow"], Result(0, json.dumps(list(main_runs))))
    c.on(["gh", "api", "repos/helmut-hoffer-von-ankershoffen/heinzel/actions/variables?per_page=100"], Result(0, json.dumps({"variables": list(variables)})))
    c.on(["gh", "api", "repos/helmut-hoffer-von-ankershoffen/heinzel/actions/runs/5/jobs?per_page=100"], Result(0, json.dumps({"jobs": alarm_cases.JOBS["5"]})))
    c.on(["ps"], Result(0, "  1 01:00 /sbin/launchd\n"))
    return c


def test_check_with_nothing_wrong_prints_ok_and_reads_only(tmp_path):
    commands = scripted()
    state, rt = world_for(tmp_path, commands)
    out = invoke(rt, ["alarm", "check"])
    assert out.exit_code == 0 and out.stdout.strip() == "OK", out
    assert {argv[0] for argv in commands.calls} <= {"gh", "ps"} and not [a for a in commands.calls if a[0] == "gh" and a[1] not in ("run", "api", "pr")]
    assert list(state.iterdir()) == []  # nothing was written


def test_check_names_a_red_main_and_low_slots_exits_3_and_lists_what_others_recorded(tmp_path):
    commands = scripted(main_runs=[{"databaseId": 5, "headSha": "f19a3782232cb24b", "conclusion": "failure"}],
                        variables=[{"name": "FOCUSED_CI_PLANS", "value": "x" * 48000}, *({"name": f"FOCUSED_CI_PLANS_V2_{i}", "value": "x" * 39900} for i in range(4))])
    state, rt = world_for(tmp_path, commands)
    (state / "alarm.json").write_text(json.dumps({"stall:801": {"cls": "ALERT", "text": "STALL #801 unmerged; hz-queue why", "first": NOW - 600, "acked": True, "sources": ["queue"]}}))
    out = invoke(rt, ["alarm", "check"])
    lines = out.stdout.splitlines()
    assert out.exit_code == 3, out
    assert lines[0] == "ALERT MAIN RED run 5 at f19a3782: web-2 (ubuntu-24.04); sm ci faillog 5 [new; key main-red:5]"
    assert lines[1].startswith("WARN REGISTRY focused slot over 85%: PLANS_V2_3 99%") and lines[1].endswith("[new; key registry-slot-fill]")
    assert lines[2].startswith("WARN REGISTRY focused slots have 1552 B free (< 4000)") and lines[2].endswith("sm status slots [new; key registry-slots]")
    assert lines[3].startswith("ACKED STALL #801 unmerged; sm queue why [since ") and lines[3].endswith("; key stall:801]")
    doc = alarm_mod.Alarms.model_validate_json(invoke(rt, ["--format", "json", "alarm", "check"]).stdout)
    assert doc.checked and [a.key for a in doc.alarms] == ["main-red:5", "registry-slot-fill", "registry-slots", "stall:801"] and doc.alarms[3].first_seen.startswith("20")


def test_a_blind_check_is_a_warning_never_silence(tmp_path):
    commands = scripted()
    commands.on(["gh", "run", "list", "-R", "helmut-hoffer-von-ankershoffen/heinzel", "-L", "100"], Result(1, "", "gh: HTTP 502"))
    _, rt = world_for(tmp_path, commands)
    out = invoke(rt, ["alarm", "check"])
    assert "WARN ALARM CHECK candidate unavailable: recent runs unreadable" in out.stdout and out.exit_code == 0


def test_list_prints_the_recorded_alarms_without_any_process(tmp_path):
    commands = FakeCommands()
    state, rt = world_for(tmp_path, commands)
    assert invoke(rt, ["alarm", "list"]).stdout.strip() == "OK"
    (state / "alarm.json").write_text(json.dumps({"rehearsal:da13623068": {"cls": "ALERT", "text": "REHEARSAL RED main da13623068; hz-rehearse status --json", "first": NOW - 60}}))
    out = invoke(rt, ["alarm", "list"])
    assert out.exit_code == 3 and "ALERT REHEARSAL RED main da13623068; sm release rehearse status --json [since " in out.stdout
    assert commands.calls == []
    assert not alarm_mod.Alarms.model_validate_json(invoke(rt, ["--format", "json", "alarm", "list"]).stdout).checked


def test_an_alarm_acknowledged_with_sm_alarm_ack_prints_as_acked_and_does_not_exit_3(tmp_path):
    commands = scripted(main_runs=[{"databaseId": 5, "headSha": "f19a3782232cb24b", "conclusion": "failure"}])
    state, rt = world_for(tmp_path, commands)
    assert invoke(rt, ["alarm", "check"]).exit_code == 3
    rt.providers.store.save("alarms", "acks", {"main-red:5": NOW})
    out = invoke(rt, ["alarm", "check"])
    assert out.exit_code == 0 and out.stdout.startswith("ACKED MAIN RED run 5"), out
    assert list(state.iterdir()) == []


def test_sm_alarm_ack_accepts_a_recorded_key_through_the_hook_and_the_old_file_is_not_touched(tmp_path):
    commands = scripted()
    state, rt = world_for(tmp_path, commands)
    text = json.dumps({"stall:801": {"cls": "ALERT", "text": "STALL #801 unmerged; hz-queue why", "first": NOW - 600, "sources": ["queue"]}})
    (state / "alarm.json").write_text(text)
    assert invoke(rt, ["alarm", "check"]).exit_code == 3
    acked = invoke(rt, ["alarm", "ack", "stall:801"])
    assert acked.exit_code == 0 and "acknowledged: stall:801" in acked.stdout, acked
    assert invoke(rt, ["alarm", "ack", "nope"]).exit_code == 2
    after = invoke(rt, ["alarm", "check"])
    assert after.exit_code == 0 and after.stdout.startswith("ACKED STALL #801 unmerged; sm queue why"), after
    assert (state / "alarm.json").read_text() == text


# ------------------------------------------------------------------ the rehearsal verdict: the store document first, the old file only without one
REHEARSAL = ("heinzel.rehearse.state", "rehearsal")  # where the native rehearsal writes (rehearse_lib/rehearsal.py KIND, KEY)
RED = {"main": "da1362306815dc5e", "verdict": "red", "suspects": ["fix: late import (#801)"],
       "failures": ["End-to-end web (ubuntu-latest): failure https://github.com/o/r/actions/runs/71", "Consumer (heinzel-acme)"]}
GREEN = {"main": "f19a3782232cb24b", "verdict": "green", "failures": []}


def test_a_red_rehearsal_in_the_store_alone_is_an_alert_that_names_the_failed_runs(tmp_path):
    state, rt = world_for(tmp_path, scripted())
    rt.providers.store.save(*REHEARSAL, {"last": RED, "last_green": "f19a3782232cb24b"})
    out = invoke(rt, ["alarm", "check"])
    assert out.exit_code == 3 and list(state.iterdir()) == [], out
    assert ("ALERT REHEARSAL RED main da13623068: End-to-end web (ubuntu-latest): failure https://github.com/o/r/actions/runs/71, Consumer (heinzel-acme)"
            " | merges since last green (1): fix: late import (#801); sm release rehearse status --json") in out.stdout


def test_an_incomplete_rehearsal_in_the_store_is_a_warn_and_a_preempted_one_is_not(tmp_path):
    _, rt = world_for(tmp_path, scripted())
    rt.providers.store.save(*REHEARSAL, {"last": {"main": "da1362306815dc5e", "verdict": "incomplete"}})
    out = invoke(rt, ["alarm", "check"])
    assert out.exit_code == 0 and "WARN REHEARSAL INCOMPLETE main da13623068: not every lane ran" in out.stdout
    rt.providers.store.save(*REHEARSAL, {"last": {"main": "da1362306815dc5e", "verdict": "incomplete", "preempted": True, "yielded": "yielded to PR 801"}})
    assert "WARN" not in invoke(rt, ["alarm", "check"]).stdout


def test_a_red_rehearsal_in_the_old_file_alone_is_still_an_alert(tmp_path):
    state, rt = world_for(tmp_path, scripted())
    (state / "rehearse.json").write_text(json.dumps({"last": RED}))
    out = invoke(rt, ["alarm", "check"])
    assert out.exit_code == 3 and "ALERT REHEARSAL RED main da13623068: End-to-end web (ubuntu-latest)" in out.stdout


def test_with_both_the_store_document_wins_over_the_old_file(tmp_path):
    state, rt = world_for(tmp_path, scripted())
    (state / "rehearse.json").write_text(json.dumps({"last": RED}))
    rt.providers.store.save(*REHEARSAL, {"last": GREEN, "last_green": GREEN["main"]})
    assert invoke(rt, ["alarm", "check"]).stdout.strip() == "OK"  # the old red verdict is history once the native rehearsal has written
    (state / "rehearse.json").write_text(json.dumps({"last": GREEN}))
    rt.providers.store.save(*REHEARSAL, {"last": RED})
    out = invoke(rt, ["alarm", "check"])
    assert out.exit_code == 3 and "ALERT REHEARSAL RED main da13623068" in out.stdout
    rt.providers.store.save(*REHEARSAL, {})  # written and empty is still written: the old file stays out
    (state / "rehearse.json").write_text(json.dumps({"last": RED}))
    assert invoke(rt, ["alarm", "check"]).stdout.strip() == "OK"


def test_with_neither_there_is_no_rehearsal_alarm_and_an_unreadable_store_document_is_a_blind_check(tmp_path):
    _, rt = world_for(tmp_path, scripted())
    assert invoke(rt, ["alarm", "check"]).stdout.strip() == "OK"
    rt.providers.store.save(*REHEARSAL, ["not", "an", "object"])
    out = invoke(rt, ["alarm", "check"])
    assert "WARN ALARM CHECK rehearsal unavailable: the rehearsal state in the store is not an object" in out.stdout
