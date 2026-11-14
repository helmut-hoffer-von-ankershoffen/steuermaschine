"""`sm tick`, the deadline watch: the moved tests of the former watch module (and of the two functions it took from
the tool-environment guard's and the push gate's tests), then the command through the whole command tree, on fakes.

fixtures/tick/watch-ignore.toml: the noise rules as they were on 2026-11-12 (the settings lines of the management
checkout's research/watch-ignore.toml, comments removed).

Moved tests keep their assertions. Changed where an assertion named an old command: the cancellation INFO line says
`sm ci triage deadline`. `load_cfg` takes the rules file (it was a module default). In the detector test the
acknowledgement marker is written by the test: acknowledging is the core's `sm alarm ack tick:venv` now."""

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from steuermaschine_heinzel.native import tick
from steuermaschine_heinzel.native.tick_lib import legacy
from steuermaschine_heinzel.native.tick_lib import watch as w
from tick_support import REPO, build, contract_gaps

from steuermaschine.github.testing import FakeGh
from steuermaschine.native import NativeCommand
from steuermaschine.testing import invoke

RULES = Path(__file__).parent / "fixtures/tick/watch-ignore.toml"
T0 = 1_799_990_000.0  # file times of the command tests: before the fake clock's now (1_800_000_000), whatever today is
LIVE = "a1a821f217b36c21e"


@pytest.fixture
def home():
    return None


# ----------------------------------------------------------------------------- moved
def test_crash():
    fl = [("SFA-sos.json_1.diag", 9), ("a.ips", 5), ("b.ips", 20), ("c.crash", 30)]
    assert w.new_crashes(fl, 10) == ["b.ips", "c.crash"]
    assert w.new_crashes(fl, 99) == []


def test_filter():
    ids = "| x | 1 | a1a821f217b36c21e | b |"
    lines = ["NEEDS_REVIEW #5 foo [owner: ykib]", "FLAG #6 bar [owner: a1a821f217b36c21e]", "FAIL #7 x [owner: ykib]", "FLAG #8 q"]
    out = w.filter_lines(lines, ["research/review-5.md"], {8}, ids)
    assert len(out) == 2 and out[0].startswith("FLAG #6") and "UNREACHABLE" not in out[0]
    assert "UNREACHABLE" in out[1]
    assert not w.reviewed(15, ["research/review-5.md"], set())


def test_summarize():
    cfg = {"action_prefixes": ["NEEDS_REVIEW", "FLAG", "STALE", "READY", "OWNER_MSG"], "ignore_prefixes": ["MERGED-VIA", "DEFERRED"], "starved_min": 15,
           "overdue_beads": [], "owned_prs": [9]}
    L = ["MERGED-VIA #1: x", "DEFERRED #2: y", "STALE #9 owned", "STALE #3 old [owner: a1a821f217b36c21e]", "STALE #4 [owner: topic]  UNREACHABLE (x)",
         "READY #5 green", "main starved 20", "P0 NEW CRASH x", "OVERDUE beads 3: a b c"]
    out, info = w.summarize(L, cfg, 5)
    assert out == ["ACTION: STALE #3 old [owner: a1a821f217b36c21e]", "ACTION: READY #5 green", "ACTION: P0 NEW CRASH x"] and info == 1
    assert w.summarize(["MERGED-VIA #1"], cfg) == (["OK"], 0)
    assert any("starved" in x for x in w.summarize(L, dict(cfg, action_prefixes=["main"]), 20)[0])


def test_legacy_cancellation_hints_become_one_informational_line(home):
    cfg = w.load_cfg(RULES)
    lines = [f"RERUN #{number} (cancelled, not failed)" for number in (654, 657, 658, 595, 596, 654)]
    lines.append("RERUN command for all of them: hz-rerun-batch (--dry-run first)")
    out, info = w.summarize(lines, cfg)
    assert out == ["INFO: legacy cancellation hints for #595 #596 #654 #657 #658; "
                   "use sm ci triage deadline for exact-head status and cancellation evidence"]
    assert info == 0
    assert not any("ACTION:" in line or "RERUN" in line or "hz-rerun" in line for line in out)


def test_cancellation_summary_preserves_failure_starvation_and_crash_alerts(home):
    cfg = {"action_prefixes": ["FAIL", "main", "P0", "RERUN"], "ignore_prefixes": [],
           "starved_min": 15, "overdue_beads": [], "owned_prs": []}
    lines = ["FAIL #42 Unit tests failed", "main starved 20", "P0 NEW CRASH fictional.ips",
             "RERUN #654 (cancelled, not failed)"]
    out, info = w.summarize(lines, cfg, 20)
    assert out[:3] == ["ACTION: FAIL #42 Unit tests failed", "ACTION: main starved 20",
                       "ACTION: P0 NEW CRASH fictional.ips"]
    assert len(out) == 4 and out[-1].startswith("INFO: legacy cancellation hints for #654;")
    assert info == 0
    assert not any("starved" in line for line in w.summarize(lines, cfg, 5)[0])


def test_legacy_cancellation_command_alone_does_not_instruct_a_rerun(home):
    out, info = w.summarize(["RERUN command for all of them: hz-rerun-batch"], w.load_cfg(RULES))
    assert out == ["INFO: legacy cancellation hints; use sm ci triage deadline for exact-head status and cancellation evidence"]
    assert info == 0


def test_detector(tmp_path):
    hz_watch = w

    tool, ack = tmp_path / "heinzel", str(tmp_path / "ack")
    tool.mkdir()
    allow = str(tmp_path / "allow.log")
    now = time.time()
    os.utime(tool, (now, now))
    assert "WARN" in hz_watch.live_uv_warning(str(tool), ack, allow)
    Path(ack).write_text(str(int(now)))  # the marker the former acknowledgement wrote
    os.utime(ack, (now + 120, now + 120))
    assert hz_watch.live_uv_warning(str(tool), ack, allow) is None
    os.utime(tool, (now + 600, now + 600))
    assert "WARN" in hz_watch.live_uv_warning(str(tool), ack, allow)
    assert hz_watch.live_uv_warning(str(tmp_path / "none"), ack, allow) is None


def test_queue_age():
    NOW = datetime(2026, 11, 7, 18, 0, tzinfo=UTC)
    queue_age_min = w.queue_age_min
    assert queue_age_min([], NOW) is None
    assert queue_age_min(["2026-11-07T17:42:08Z", "2026-11-07T17:50:00Z"], NOW) == 17


# ----------------------------------------------------------------------------- new: the library's edges
def test_rules_and_holds_without_a_file_and_with_one(tmp_path):
    assert w.load_cfg(None) == w.load_cfg(str(tmp_path / "none.toml")) == {"action_prefixes": [], "ignore_prefixes": [], "starved_min": 15, "overdue_beads": [], "owned_prs": []}
    assert w.load_cfg(str(RULES))["action_prefixes"] == ["NEEDS_REVIEW", "FLAG", "STALE", "OWNER_MSG", "P0", "READY"]
    actions = tmp_path / "pr-actions.toml"
    actions.write_text('"545" = { action = "await", owner = "x" }\n"546" = { action = "parked", hold = true }\n"plugins #38" = { hold = true }\n')
    assert w.pr_actions_holds(str(actions)) == {546} and w.pr_actions_holds(None) == set() and w.pr_actions_holds(str(tmp_path / "none")) == set()


def test_the_acknowledgement_of_the_venv_also_comes_from_the_store_time(tmp_path):
    tool = tmp_path / "heinzel"
    tool.mkdir()
    changed = tool.stat().st_mtime
    assert "sm alarm ack tick:venv" in w.live_uv_warning(str(tool), str(tmp_path / "no-ack"), str(tmp_path / "no-log"))
    assert w.live_uv_warning(str(tool), str(tmp_path / "no-ack"), str(tmp_path / "no-log"), 60, changed) is None
    assert w.live_uv_warning(str(tool), str(tmp_path / "no-ack"), str(tmp_path / "no-log"), 60, changed - 61) is not None


def test_starved_age_reads_main_and_release_runs_only_and_degrades_to_none():
    class Clock:
        def now(self):
            return datetime(2026, 11, 7, 18, 0, tzinfo=UTC).timestamp()

        def sleep(self, _seconds):
            raise AssertionError("no sleep")

    gh = FakeGh().on("run list", [{"createdAt": "2026-11-07T17:42:08Z", "headBranch": "main"}, {"createdAt": "2026-11-07T17:50:00Z", "headBranch": "release/v1.2.3"},
                                  {"createdAt": "2026-11-07T10:00:00Z", "headBranch": "fix/x"}])
    assert w.starved_age(gh, Clock(), REPO) == 17
    assert gh.lines() == [f"run list -R {REPO} --status queued --limit 50 --json createdAt,headBranch"]
    assert w.starved_age(FakeGh().on("run list", []), Clock(), REPO) is None
    assert w.starved_age(FakeGh().on("run list", "not json"), Clock(), REPO) is None


def test_the_adopted_markers_are_read_and_never_written(tmp_path):
    assert legacy.crash_ack(tmp_path) == 0.0
    (tmp_path / "crash-ack.ts").write_text("1")
    os.utime(tmp_path / "crash-ack.ts", (500, 500))
    assert legacy.crash_ack(tmp_path) == 500
    assert legacy.venv_ack(tmp_path).name == "uv-ack.ts" and legacy.venv_allowed(tmp_path).name == "live-uv.log"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["crash-ack.ts"]


# ----------------------------------------------------------------------------- the command, through the tree
QUIET = "OK: no new crash report, tool environment unchanged (alarms and the ship path: sm go)"
NOT_READ = "NOT READ: anomaly digest: not read here (PR lines: sm go and sm ci triage prs; alarms: sm alarm check)"


def report(tmp_path, name, age_seconds=0.0):
    path = tmp_path / "diagnostics" / name
    path.write_text("x")
    at = T0 - age_seconds
    os.utime(path, (at, at))
    return path


def test_a_quiet_tick_in_text_and_json_reads_nothing_outside_and_writes_nothing(tmp_path, monkeypatch):
    rt = build(tmp_path, monkeypatch)
    done = invoke(rt, ["tick"])
    assert (done.exit_code, done.stdout.splitlines()) == (0, [QUIET, NOT_READ])
    doc = json.loads(invoke(rt, ["--format", "json", "tick"]).stdout)
    result = tick.Tick.model_validate(doc)
    assert (result.status, result.crashes, result.actions, result.venv_warning, result.acknowledged) == ("ok", [], [], "", [])
    assert result.not_read == [NOT_READ.removeprefix("NOT READ: ")]
    p = rt.providers
    assert p.commands.calls == [] and p.notifier.sent == [] and p.store.writes == 0  # no GitHub, no notification, no state


def test_a_new_crash_report_is_an_action_until_it_is_acknowledged(tmp_path, monkeypatch):
    rt = build(tmp_path, monkeypatch)
    report(tmp_path, "hz-2026-11-12-101500.ips")
    report(tmp_path, "python3.12-2026-11-12.crash")
    report(tmp_path, "SFA-ckks.json_2026-11-12.diag")  # other diagnostics never count
    done = invoke(rt, ["tick"])
    crash = "P0 NEW CRASH hz-2026-11-12-101500.ips, python3.12-2026-11-12.crash (investigate, then sm alarm ack tick:crash)"
    assert done.exit_code == 0 and done.stdout.splitlines() == [f"ACTION: {crash}", NOT_READ]
    result = tick.Tick.model_validate(json.loads(invoke(rt, ["--format", "json", "tick"]).stdout))
    assert result.status == "action" and result.crashes == ["hz-2026-11-12-101500.ips", "python3.12-2026-11-12.crash"] and result.actions == [f"ACTION: {crash}"]
    assert invoke(rt, ["tick", "--raw"]).stdout.splitlines() == [crash]
    assert rt.providers.store.writes == 0
    assert invoke(rt, ["tick", "--ack-crash"]).exit_code == 2  # CHANGED (review T1): the tick never acknowledges
    rt.providers.store.save("alarms", "acks", {"tick:crash": rt.providers.clock.now()})  # what `sm alarm ack tick:crash` writes
    acked = tick.Tick.model_validate(json.loads(invoke(rt, ["--format", "json", "tick"]).stdout))
    assert acked.acknowledged == [] and acked.crashes == [] and acked.status == "ok" and rt.providers.store.writes == 1
    assert invoke(rt, ["tick"]).stdout.splitlines() == [QUIET, NOT_READ]
    assert invoke(rt, ["tick", "--raw"]).stdout == "quiet\n"
    assert rt.providers.commands.calls == [] and rt.providers.notifier.sent == []


def test_an_acknowledgement_made_the_old_way_still_counts(tmp_path, monkeypatch):
    rt = build(tmp_path, monkeypatch)
    report(tmp_path, "old.ips", age_seconds=600)
    assert "P0 NEW CRASH old.ips" in invoke(rt, ["tick"]).stdout
    (tmp_path / "state" / "crash-ack.ts").write_text("x")  # written five minutes after the crash
    at = T0 - 300
    os.utime(tmp_path / "state" / "crash-ack.ts", (at, at))
    assert invoke(rt, ["tick"]).stdout.splitlines() == [QUIET, NOT_READ]
    report(tmp_path, "new.ips")
    assert "P0 NEW CRASH new.ips (" in invoke(rt, ["tick"]).stdout
    assert rt.providers.store.writes == 0 and (tmp_path / "state" / "crash-ack.ts").read_text() == "x"


def test_a_changed_tool_environment_warns_until_it_is_acknowledged(tmp_path, monkeypatch):
    rt = build(tmp_path, monkeypatch)
    (tmp_path / "venv").mkdir()
    os.utime(tmp_path / "venv", (T0, T0))
    done = invoke(rt, ["tick"])
    lines = done.stdout.splitlines()
    assert done.exit_code == 0 and lines[0].startswith("WARN: live heinzel tool venv changed ") and lines[0].endswith("then sm alarm ack tick:venv)") and lines[1] == NOT_READ
    result = tick.Tick.model_validate(json.loads(invoke(rt, ["--format", "json", "tick"]).stdout))
    assert result.status == "action" and result.venv_warning == lines[0] and result.actions == []
    assert invoke(rt, ["tick", "--ack-venv"]).exit_code == 2  # CHANGED (review T1): the tick never acknowledges
    rt.providers.store.save("alarms", "acks", {"tick:venv": rt.providers.clock.now()})  # what `sm alarm ack tick:venv` writes
    assert invoke(rt, ["tick"]).stdout.splitlines() == [QUIET, NOT_READ]


def test_a_record_of_the_former_ack_option_still_counts(tmp_path, monkeypatch):
    rt = build(tmp_path, monkeypatch)
    report(tmp_path, "old.ips", age_seconds=600)
    rt.providers.store.save("heinzel.tick.ack", "crash", {"at": T0 - 300})
    assert invoke(rt, ["tick"]).stdout.splitlines() == [QUIET, NOT_READ]


def test_without_the_path_settings_nothing_is_read_and_the_tick_says_so(tmp_path, monkeypatch):
    rt = build(tmp_path, monkeypatch, tick_diagnostics_dir=None, tick_tool_venv=None, tick_research_dir=None)
    rt.settings.pop("legacy_state_dir", None)
    lines = invoke(rt, ["tick"]).stdout.splitlines()
    assert lines[0] == QUIET and "NOT READ: crash reports: the setting tick_diagnostics_dir is not set" in lines
    assert "NOT READ: tool environment: the setting tick_tool_venv is not set" in lines and rt.providers.store.writes == 0


def test_a_change_the_environment_guard_allowed_is_not_a_warning(tmp_path, monkeypatch):
    rt = build(tmp_path, monkeypatch)
    (tmp_path / "venv").mkdir()
    os.utime(tmp_path / "venv", (T0, T0))
    allowed = datetime.fromtimestamp(T0 - 20).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")  # the guard's record, 20 s before the change landed
    rt.providers.store.save(tick.ALLOWED_VENV_CHANGES, f"{allowed}-123", {"at": allowed, "pid": 123, "args": ["tool", "upgrade", "heinzel"]})
    rt.providers.store.save(tick.ALLOWED_VENV_CHANGES, "unreadable", {"at": "yesterday"})
    assert invoke(rt, ["tick"]).stdout.splitlines() == [QUIET, NOT_READ]
    os.utime(tmp_path / "venv", (T0 + 3600, T0 + 3600))  # a later change nobody allowed
    assert invoke(rt, ["tick"]).stdout.startswith("WARN: live heinzel tool venv changed ")
    assert rt.providers.store.writes == 2  # the two records above; the tick wrote nothing


class Lines:
    def __init__(self, lines):
        self.lines = list(lines)

    def render(self):
        return list(self.lines)


def digest_command(monkeypatch, lines, writes=False):
    """A registered command named in DIGEST (the seam takes paths of read commands, never code)."""

    def digest_fake(limit: int = 5):
        assert limit == 5  # run with its defaults
        return Lines(lines)

    monkeypatch.setattr(tick, "DIGEST", ("digest fake",))
    monkeypatch.setattr(tick, "registered", lambda: [NativeCommand("digest fake", digest_fake, "help", writes=writes)])


def test_a_digest_source_that_writes_or_is_not_registered_is_refused_before_it_runs(tmp_path, monkeypatch):
    digest_command(monkeypatch, ["READY #12 green"], writes=True)
    rt = build(tmp_path, monkeypatch)
    refused = invoke(rt, ["tick"])
    assert refused.exit_code == 1 and "not a registered read command" in refused.stderr
    monkeypatch.setattr(tick, "DIGEST", ("no such command",))
    assert invoke(rt, ["tick"]).exit_code == 1


def test_the_real_registry_is_the_default_seam(tmp_path, monkeypatch):
    build(tmp_path, monkeypatch)
    assert any(c.path == "tick" and not c.writes for c in tick.registered())


def digest_setup(tmp_path, monkeypatch, lines, **more):
    research = tmp_path / "research"
    research.mkdir(exist_ok=True)
    (research / "watch-ignore.toml").write_text(RULES.read_text())
    (research / "agent-ids.md").write_text(f"| builder | 1 | {LIVE} | sonnet |\n")
    (research / "review-5-astra.md").write_text("reviewed\n")
    digest_command(monkeypatch, lines)
    rt = build(tmp_path, monkeypatch, **more)
    (tmp_path / "state" / "queue.json").write_text(json.dumps({"items": [{"key": "8", "status": "queued"}, {"key": "9", "status": "merged"}, {"key": "plugins #38", "status": "queued"}]}))
    return rt


def test_digest_lines_go_through_the_reviewed_filter_and_the_noise_rules(tmp_path, monkeypatch):
    lines = ["RULES: run sm first", "NEEDS_REVIEW #5 foo [owner: ykib]", f"FLAG #6 bar [owner: {LIVE}]", "FAIL #7 x [owner: ykib]", "FLAG #8 q", "FLAG #9 merged, so listed",
             "STALE #4 old [owner: topic]", "READY #12 green", "MERGED-VIA #1: x", "DEFERRED #2: y", "RERUN #654 (cancelled, not failed)", "OVERDUE beads 3: a b c"]
    rt = digest_setup(tmp_path, monkeypatch, lines)
    done = invoke(rt, ["tick"])
    assert done.exit_code == 0 and done.stdout.splitlines() == [
        f"ACTION: FLAG #6 bar [owner: {LIVE}]", "ACTION: FLAG #9 merged, so listed", "ACTION: READY #12 green",
        "INFO: legacy cancellation hints for #654; use sm ci triage deadline for exact-head status and cancellation evidence",
        "INFO: 1 stale/flagged PRs have no live agent (not actionable here; sm tick --raw)"]
    result = tick.Tick.model_validate(json.loads(invoke(rt, ["--format", "json", "tick"]).stdout))
    assert (result.status, len(result.actions), len(result.notes), result.unreachable, result.not_read) == ("action", 3, 2, 1, [])
    raw = invoke(rt, ["tick", "--raw"]).stdout.splitlines()
    assert raw[0] == f"FLAG #6 bar [owner: {LIVE}]" and "FAIL #7 x [owner: ykib]  UNREACHABLE (do not SendMessage; route via orchestrator)" in raw
    assert not any(line.startswith(("RULES:", "NEEDS_REVIEW #5", "FLAG #8")) for line in raw)  # reviewed (#5) and queued (#8) are not re-listed
    assert rt.providers.commands.calls == [] and rt.providers.store.writes == 0  # no starved line: GitHub is not asked


def test_a_quiet_digest_says_ok_and_a_held_pr_is_never_an_action(tmp_path, monkeypatch):
    actions = tmp_path / "pr-actions.toml"
    actions.write_text('"12" = { action = "parked", hold = true }\n')
    rt = digest_setup(tmp_path, monkeypatch, ["READY #12 green", "MERGED-VIA #1: x"])
    rt.settings["go_pr_actions_file"] = str(actions)
    assert invoke(rt, ["tick"]).stdout == "OK: no new crash report, tool environment unchanged, no watch action (alarms and the ship path: sm go)\n"


def test_a_starved_line_survives_only_while_a_main_or_release_run_waits_now(tmp_path, monkeypatch):
    def rules(research):
        (research / "watch-ignore.toml").write_text(RULES.read_text().replace('"READY"]', '"READY", "main"]'))

    queued_at = datetime.fromtimestamp(1_800_000_000 - 20 * 60, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh = FakeGh().on("run list", [{"createdAt": queued_at, "headBranch": "main"}])
    rt = digest_setup(tmp_path, monkeypatch, ["main starved 20 min: 3 runs queued"], commands=gh)
    rules(tmp_path / "research")
    assert invoke(rt, ["tick"]).stdout == "ACTION: main starved 20 min: 3 runs queued\n"
    assert gh.lines() == [f"run list -R {REPO} --status queued --limit 50 --json createdAt,headBranch"] and not gh.writes()
    gh = FakeGh().on("run list", [])
    rt = digest_setup(tmp_path, monkeypatch, ["main starved 20 min: 3 runs queued"], commands=gh)
    rules(tmp_path / "research")
    assert invoke(rt, ["tick"]).stdout.startswith("OK: ")


def test_the_command_is_read_only_and_its_help_meets_the_contract():
    command = tick.tick.__sm_native__
    assert command.writes is False and command.replaces == ("hz-tick",) and command.path == "tick"
    assert contract_gaps(tick.tick) == {}
    assert "hz-" not in command.help and "hz-" not in tick.DIGEST_MISSING
