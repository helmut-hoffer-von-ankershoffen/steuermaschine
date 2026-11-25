"""go --view start|next|prs|wait|tick, native. Scripted GitHub and git answers (the shapes of test_native_go), adopted
state files and a tracking document in tmp_path. The prs view is compared with `sm status` on the recorded status
fixture. No test here starts a process, reads the network or writes outside tmp_path."""

import json
import time
from datetime import datetime
from pathlib import Path

import pytest
from test_native_go import MAIN, NOW, pr_row, run_row
from test_native_status import Web, recorded, strip
from test_native_status import runtime_for as status_runtime

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, fake_providers, invoke
from steuermaschine_heinzel.native import go as go_mod
from steuermaschine_heinzel.native.go_lib import legacy, probes, views
from steuermaschine_heinzel.native.go_lib import views_moved as moved
from steuermaschine_heinzel.native.queue_lib import auto_arm, hzlib
from steuermaschine_heinzel.plugin import plugin

EMPTY_PRS = {"data": {"repository": {"pullRequests": {"nodes": []}}}}


@pytest.fixture(autouse=True)
def _calm_machine(monkeypatch):
    """The load of the machine running the tests must not decide a view."""
    monkeypatch.setattr(views, "load_average", lambda: 1.0)


def stamp(minutes_ago: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(NOW - minutes_ago * 60))


def scripted(prs=(), runs=None, main_runs=None) -> FakeCommands:
    c = FakeCommands()
    c.on(["git", "-C", "/core", "ls-remote", "origin"], Result(0, f"{MAIN}\trefs/heads/main\n"))
    c.on(["git", "-C", "/core", "ls-remote", "--heads"], Result(0, f"{MAIN}\trefs/heads/main\n"))
    c.on(["git", "-C", "/core", "rev-list"], Result(0, "3\n"))
    c.on(["gh", "run", "list"], Result(0, json.dumps(runs if runs is not None else [run_row(1)])))
    c.on(["gh", "run", "list", "-R", "o/core", "--workflow"], Result(0, json.dumps(main_runs if main_runs is not None else [run_row(1)])))
    c.on(["gh", "pr", "list"], Result(0, json.dumps(list(prs))))
    c.on(["gh", "release", "list"], Result(0, json.dumps([{"tagName": "v0.9.39", "isLatest": True, "publishedAt": "x"}])))
    c.on(["gh", "api"], Result(0, json.dumps({"variables": []})))
    c.on(["gh", "api", "graphql"], Result(0, json.dumps(EMPTY_PRS)))
    c.on(["gh", "issue", "list"], Result(0, json.dumps([{"number": 1}, {"number": 2}])))
    c.on(["ps"], Result(0, "  1 01:00 /sbin/launchd\n  7 10:00 /v/bin/python /v/bin/sm daemon queue --loop 300\n"))
    c.on(["hz", "--version"], Result(0, "hz 0.9.39\n"))
    return c


def runtime_for(tmp_path, commands, **settings):
    state_dir = tmp_path / "state"
    (state_dir / "focused-admission-receipts").mkdir(parents=True, exist_ok=True)
    table = {"legacy_state_dir": str(state_dir), "core_repo_dir": "/core", "go_packet_globs": [str(tmp_path / "no/*.json")], "repo": "o/core",
             "status_repos": {"core": ""}, "status_gate_repos": [], **settings}
    stack = stack_of(plugin)
    validated, _ = stack.validate({"heinzel": table}, Path("."))
    providers = fake_providers(commands=commands, extra={"http_fetch": Web({})})
    providers.clock.t = NOW
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, validated, _providers=providers)


def view(tmp_path, commands, name, *extra, fmt="text", **settings):
    args = (["--format", "json"] if fmt == "json" else []) + ["go", "--view", name, *extra]
    out = invoke(runtime_for(tmp_path, commands, **settings), args)
    assert out.exit_code == 0, out
    return out


def no_writes(commands):
    gh = [argv for argv in commands.calls if argv[0] == "gh"]
    assert not [a for a in gh if a[1] in ("pr", "run") and a[2] not in ("list", "view")] and not [a for a in gh if "-X" in a or "--method" in a]
    assert {argv[0] for argv in commands.calls} <= {"git", "gh", "ps", "hz"}


# ------------------------------------------------------------------ start
def test_start_prints_the_route_the_latest_checkpoint_and_the_timers(tmp_path):
    doc = tmp_path / "AGENTS.md"
    doc.write_text("# x\n## 5. Checkpoints\n### 2026-11-13 10:00\n- merged 801\n- next: cut 0.9.40\n### 2026-11-12\nold\n## 6. Rest\n")
    timers = tmp_path / "timers.toml"
    timers.write_text('[tick]\nevery_minutes = 30\ncodex_id = "auto-1"\n\n[queue]\nevery_minutes = 5\n')
    automation = tmp_path / "codex" / "automations" / "auto-1"
    automation.mkdir(parents=True)
    (automation / "automation.toml").write_text('id = "auto-1"\nkind = "heartbeat"\nstatus = "ACTIVE"\ntarget_thread_id = "T1"\nrrule = "FREQ=MINUTELY;INTERVAL=30"\n')
    commands = scripted()
    out = view(tmp_path, commands, "start", "--thread", "T1", go_tracking_doc=str(doc), go_timers_file=str(timers), go_codex_home=str(tmp_path / "codex"))
    lines = out.stdout.splitlines()
    assert lines[0].startswith("PHASE    main ") and lines[1].startswith("NEXT     sm ")
    assert f"Tracking document: {doc}" in lines and "2026-11-13 10:00" in lines and "- next: cut 0.9.40" in lines and "old" not in lines
    assert "auto-1 (30 min) | ACTIVE | T1 (this chat) | cadence matches" in lines and "queue (5 min) | launchd | inspect separately" in lines
    assert "hz-" not in out.stdout
    no_writes(commands)


def test_start_in_json_says_what_is_not_configured(tmp_path):
    out = view(tmp_path, scripted(), "start", fmt="json")
    report = go_mod.Go.model_validate_json(out.stdout)
    assert report.view is not None and report.view.name == "start" and report.phase
    assert report.view.unknown == ["the tracking document is not configured (setting go_tracking_doc)",
                                   "the timer manifest is not configured (setting go_timers_file)"]


def test_timer_rows_without_a_codex_home_and_with_odd_records(tmp_path):
    manifest = {"a": {"every_minutes": 60, "codex_id": "x"}, "b": {"every_minutes": 60, "codex_id": "y"}, "c": {"every_minutes": 60, "codex_id": "z"}}
    assert moved.timer_rows(manifest, None, None)[0] == "x (60 min) | no local record (setting go_codex_home is not set)"
    (tmp_path / "automations" / "y").mkdir(parents=True)
    (tmp_path / "automations" / "y" / "automation.toml").write_text("not toml [")
    (tmp_path / "automations" / "z").mkdir(parents=True)
    (tmp_path / "automations" / "z" / "automation.toml").write_text('id = "other"\n')
    assert moved.timer_rows(manifest, tmp_path, "T") == ["x (60 min) | no local record", "y (60 min) | unreadable local record", "z (60 min) | unexpected local record"]
    assert moved.latest_checkpoint("no sections") == "No dated checkpoint found. Reconcile live state."


# ------------------------------------------------------------------ next
def test_next_ranks_main_red_queue_asks_and_paused_work_and_writes_no_claim(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "queue.json").write_text(json.dumps({"items": [{"key": "812", "repo": "o/core", "pr": 812, "status": "needs work", "detail": "web-2 red"}]}))
    (state / "next-claims.json").write_text(json.dumps({"812": {"owner": "builder-a", "until": NOW + 600, "at": NOW}}))
    (state / "next.json").write_text(json.dumps({"last_update": NOW}))
    doc = tmp_path / "AGENTS.md"
    doc.write_text("\n".join([f"| make the logo bigger | ask {stamp(40)} |", f"| fix flaky ci test | ask {stamp(20)} | #801 |",
                              "| release notes page | paused, waiting for a slot |", "| one known bug in the web chat |"]))
    red = run_row(9, conclusion="failure", sha="f" * 40)
    commands = scripted(main_runs=[red], prs=[pr_row(820, branch="fix/other")])
    commands.on(["gh", "pr", "view", "812"], Result(0, json.dumps({"state": "OPEN"})))  # fresh PR state: a queue row of an open PR stays
    out = view(tmp_path, commands, "next", go_tracking_doc=str(doc))
    lines = out.stdout.splitlines()
    assert lines[0] == "main CI: newest finished run ffffffff failure; full-candidate evidence: unknown"
    assert lines[2] == "known bugs open: 2 (issues labelled bug), 1 section 4 row(s) naming a bug"
    assert lines[3] == "1. [main-red-ffffffff] main RED at ffffffff, run 9 (failure); no PR claims it; sm ci main shows the evidence"
    assert lines[4].startswith("   do: sm beads brief fix-main-ffffffff fix/main-ffffffff --task 'Make main green again.")
    assert any("ask, 40 min, no PR: make the logo bigger" in line for line in lines)
    assert any("paused or waiting for a slot: release notes page" in line for line in lines)
    assert any(line.startswith("1. [queue-812] queue: 812 needs work, no owner: web-2 red") for line in lines) is False  # rank 3, after the asks
    assert any(line.endswith("[queue-812] queue: 812 needs work, no owner: web-2 red") for line in lines)
    assert "hz-" not in out.stdout and json.loads((state / "next-claims.json").read_text())["812"]["owner"] == "builder-a"
    no_writes(commands)
    doc_json = go_mod.Go.model_validate_json(view(tmp_path, scripted(main_runs=[red]), "next", fmt="json", go_tracking_doc=str(doc)).stdout)
    assert doc_json.view.items[0].key == "main-red-ffffffff" and doc_json.view.items[0].priority == 2


def test_next_shows_a_claimed_item_as_watching_and_says_nothing_to_do(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "next-claims.json").write_text(json.dumps({"main-red-ffffffff": {"owner": "fixer", "until": NOW + 600, "at": NOW}, "old": {"owner": "x", "until": NOW - 1}}))
    (state / "next.json").write_text(json.dumps({"last_update": NOW}))
    red = run_row(9, conclusion="failure", sha="f" * 40)
    out = view(tmp_path, scripted(main_runs=[red]), "next")
    lines = out.stdout.splitlines()
    assert lines[1].endswith(": 0 to do, 1 watching") and "1. [main-red-ffffffff]" not in out.stdout
    assert any(line.startswith("watching [main-red-ffffffff] fixer until ") for line in lines)
    claimed = view(tmp_path, scripted(main_runs=[red], prs=[{**pr_row(5, branch="fix/main-red"), "title": "fix main red"}]), "next").stdout
    assert "main red at ffffffff, run 9; claimed by PR #5" in claimed
    assert legacy.next_claims(state, NOW) == {"main-red-ffffffff": {"owner": "fixer", "until": NOW + 600, "at": NOW}}


def test_next_items_from_the_status_table_and_the_queue_loop(tmp_path):
    commands = scripted()
    commands.on(["ps"], Result(0, "  1 01:00 /sbin/launchd\n"))
    out = view(tmp_path, commands, "next")
    assert "[queue-down] the merge queue loop is not running" in out.stdout
    assert "UNKNOWN  the tracking document is not configured (setting go_tracking_doc)" in out.stdout


def test_hourly_update_and_paused_log_lines():
    berlin_noon = datetime(2026, 11, 13, 12, 0).timestamp()
    assert moved.update_due(berlin_noon, 0) and not moved.update_due(berlin_noon, berlin_noon - 60)
    line = f"- {time.strftime('%m-%d %H:%M', time.localtime(NOW - 600))} push failed under load, paused"
    found = moved.src_paused([line, "- 99-99 99:99 paused nonsense"], NOW)
    assert len(found) == 1 and found[0]["rank"] == 1 and found[0]["do"].endswith("; or log why not")
    assert moved.classify("the merge queue is slow") == 3 and moved.classify("something else") == 2.5


# ------------------------------------------------------------------ prs
def test_prs_is_the_pull_request_table_of_status(tmp_path):
    status_text = invoke(status_runtime(tmp_path, recorded()), ["status"]).stdout.splitlines()
    table = status_text[status_text.index("", 2) + 1:]  # after the merge gate block
    fake = recorded()
    out = invoke(status_runtime(tmp_path, fake, Web()), ["go", "--view", "prs"])
    assert out.exit_code == 0, out
    lines = [line for line in out.stdout.splitlines() if not line.startswith("UNKNOWN")]
    assert lines[:-1] == strip("\n".join(table)) and lines[-1].endswith("s, read-only)")
    doc = go_mod.Go.model_validate_json(invoke(status_runtime(tmp_path, recorded(), Web()), ["--format", "json", "go", "--view", "prs"]).stdout)
    assert doc.view.prs and {p.group for p in doc.view.prs} <= {"ready", "needs work", "held", "draft"}
    assert not fake.writes()


# ------------------------------------------------------------------ wait
def test_wait_names_the_biggest_wait_and_the_buckets(tmp_path):
    prs = [pr_row(1, checks=(("web-2", "COMPLETED", "FAILURE"),)), pr_row(2), pr_row(3, checks=(("web-1", "IN_PROGRESS", ""),)),
           pr_row(4, draft=True), pr_row(5, checks=(("web-1", "COMPLETED", "CANCELLED"),))]
    commands = scripted(prs=prs)
    out = view(tmp_path, commands, "wait")
    lines = out.stdout.splitlines()
    assert lines[0] == "BIGGEST WAIT  1 PRs fail CI for real"
    assert lines[1] == "UNBLOCKS      triage the first failing job per PR: sm ci triage prs, then sm ci faillog PR"
    assert "PRs           cancelled 1: #5; draft 1: #4; failing 1: #1; pending 1: #3; ready 1: #2" in lines
    doc = go_mod.Go.model_validate_json(view(tmp_path, scripted(prs=prs), "wait", fmt="json").stdout)
    assert doc.view.buckets == {"cancelled": [5], "draft": [4], "failing": [1], "pending": [3], "ready": [2]}
    no_writes(commands)


def test_wait_ladder():
    assert views.biggest_wait({"cancelled": [1]}, 5, "", "", 1.0)[1] == "wait until the local gate hold clears; the release resumes CI"
    assert views.biggest_wait({"cancelled": [1]}, None, "", "run 7", 1.0)[1].startswith("wait until Acceptance run 7 finishes")
    assert views.biggest_wait({}, None, "incident merge freeze", "", 1.0)[0] == "Merges blocked: incident merge freeze"
    assert views.biggest_wait({"ready": [2]}, None, "", "", 1.0)[0] == "1 PRs are check-ready; merge readiness unverified"
    assert views.biggest_wait({}, None, "", "", 60.0)[0] == "Mini load 60 slows the gate"
    assert views.biggest_wait({"pending": [3]}, None, "", "", None)[0] == "1 PRs pending hosted CI"
    assert views.biggest_wait({"cancelled": [1]}, None, "", "", None)[0] == "1 PRs with cancelled CI"
    assert views.biggest_wait({}, None, "", "", None)[0] == "No non-draft PRs in the check snapshot"


def test_wait_sees_holds_and_queue_waits(tmp_path):
    state = tmp_path / "state"
    (state / "release-holds").mkdir(parents=True)
    (state / "release-holds" / "v0.9.40").write_text("")
    (state / "queue.json").write_text(json.dumps({"items": [{"key": "790", "status": "queued", "waiting_since": NOW - 600, "wait_reason": "BEHIND"}]}))
    out = view(tmp_path, scripted(), "wait")
    assert out.stdout.splitlines()[0] == "BIGGEST WAIT  Merges blocked: unverified release hold state blocks every repository, release hold v0.9.40"
    assert "queue         item 790: waiting 10 min: BEHIND" in out.stdout


@pytest.mark.parametrize("scope", ["release", "uncertain", "incident"])
def test_wait_reports_the_same_hold_scope_as_the_merge_gate(tmp_path, monkeypatch, scope):
    state = tmp_path / "state"
    holds = state / "release-holds"
    holds.mkdir(parents=True)
    token, version = "a" * 32, "0.9.55"
    (state / "release.json").write_text(json.dumps({"version": version, "freeze_token": token}))
    if scope != "uncertain":
        (holds / token).write_text(version)
    if scope == "incident":
        (state / "merge-freeze").touch()
    monkeypatch.setattr(hzlib.statedir, "path", lambda name: state / name)
    assert auto_arm.freeze_without_candidate(f"{hzlib.OWNER}/steuermaschine")
    assert probes.FREEZE_STATUS[scope] in view(tmp_path, scripted(), "wait").stdout.splitlines()[0]


# ------------------------------------------------------------------ tick
def test_tick_lists_anomalies_and_never_cancels(tmp_path):
    prs = [pr_row(1, checks=(("web-2", "COMPLETED", "FAILURE"),)), pr_row(2), pr_row(5, checks=(("web-1", "COMPLETED", "CANCELLED"),))]
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 30 * 60))
    runs = [run_row(1), {**run_row(2, status="queued", conclusion=None), "createdAt": old}] + [run_row(10 + i, branch=f"b{i}", status="in_progress", conclusion=None, event="pull_request") for i in range(5)]
    red = run_row(9, conclusion="failure", sha="f" * 40)
    commands = scripted(prs=prs, runs=runs, main_runs=[red])
    out = view(tmp_path, commands, "tick")
    lines = out.stdout.splitlines()
    assert lines[0].startswith("RULES: run `sm` first")
    assert "MAIN RED ffffffff run 9 failure: sm ci main, then sm ci why 9" in lines
    assert "#1 FAIL web-2 (t): sm ci why 1" in lines and "DEFERRED CI 1: #5 (cancelled checks; sm ci triage prs)" in lines
    assert "#2 green, not queued (CLEAN): sm head merge-ready 2 or sm queue add 2" in lines
    assert "PR CI runs in flight 5/5: hold new pushes" in lines and any(line.startswith("MAIN CI STARVED: queued 30 min") for line in lines)
    no_writes(commands)
    doc = go_mod.Go.model_validate_json(view(tmp_path, scripted(), "tick", fmt="json").stdout)
    assert doc.view.name == "tick" and doc.view.lines[1:] == ["quiet"]


def test_an_unknown_view_is_refused(tmp_path):
    out = invoke(runtime_for(tmp_path, scripted()), ["go", "--view", "nope"])
    assert out.exit_code == 2 and "--view must be one of start, next, prs, wait, tick" in out.stdout + out.stderr
