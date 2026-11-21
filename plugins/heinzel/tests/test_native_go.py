"""go, native. Parity fixture fixtures/go/go-apply-running.json: recorded 2026-11-12 22:33 with
`python plugins/heinzel/tests/record_go_fixtures.py go apply-running` (the old tool's collect(), decide() and render()
on the real state, while an apply ran on a predict/bound packet). No test here starts a process or reads the network."""

import json
import re
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.native import NativeRuntime, using
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, fake_providers, invoke
from steuermaschine_heinzel.native import go as go_mod
from steuermaschine_heinzel.native.go_lib import decide as rules
from steuermaschine_heinzel.native.go_lib import legacy, probes
from steuermaschine_heinzel.native.go_lib.words import Words, q
from steuermaschine_heinzel.plugin import plugin
from steuermaschine_heinzel.toolmap import ROWS

FIXTURES = Path(__file__).parent / "fixtures" / "go"
MAIN = "437bb247d164d5c5140e5acab1518ba6ea54bb3e"
HEAD = "e9c943810ea75cfad155a431a2f168b2a9559828"
NOW = 1_800_000_000
OLD = re.compile(r"(?<![\w./-])hz-[a-z]")  # an old tool name as a word (a state file name does not match)


class OldWords(Words):
    """The spelling of the old tool, for byte-for-byte parity of the decision."""

    def trap(self, key):
        text = super().trap(key)
        return (text.replace("`sm queue why`", "`hz-queue why`").replace("only sm mini verify proves", "only hz-verify-mini proves")
                .replace("`sm focus done`", "`hz-go focus done`").replace("sm ci sonar-close handles", "hz-sonar-close handles"))

    def drop(self, version, has_drop):
        return f"hz-release {version} " + ("--drop-candidate" if has_drop else "--pause")

    def ci_why(self, run):
        return f"hz-ci-cause {run}"

    def release_status(self):
        return "hz-release-status"

    def release_finishing(self, version):
        return f"hz-release finishing v{version}"

    def main_evidence(self):
        return "hz-main-evidence"

    def verify_mini(self, version):
        return f"hz-verify-mini --minimum {version}"

    def go(self):
        return "hz-go"

    def queue_why_pr(self, number):
        return f"hz-why {number}"

    def queue_tick(self):
        return "the hz-queue tick (launchd, every 5 min)"

    def sonar(self, number):
        return (f"curl -fsS 'https://sonarcloud.io/api/issues/search?componentKeys=helmut-hoffer-von-ankershoffen_heinzel&pullRequest={number}&resolved=false&ps=100'"
                " | jq -r '.issues[] | [.key, .type, .severity, .rule, (.component | sub(\"^[^:]*:\"; \"\")), (.line // \"-\"), .message] | @tsv'")

    def faillog(self, target):
        return f"hz-faillog {target}"

    def ship(self, number):
        return f"hz-ship {number}"

    def pr_open(self, worktree, bead, subject, body=None):
        return f"cd {q(worktree)} && hz-pr-open {q(bead)} {q(subject)}" + (f" --body-file {q(body)}" if body else "")

    def apply_head(self):
        return ["hz-focused-admit", "apply"]

    def prepare(self, packet, core_repo, review_root, out_file):
        return f"hz-focused-admit prepare {q(packet)} --core-repo {q(core_repo)} --review-root {q(review_root)} > {q(out_file)}"

    def preopen_help(self):
        return "hz-admit-wave preopen --help"

    def focus(self, verb, worktree=None, entry=None):
        return f"hz-go focus {verb}" + (f" {q(worktree)}" if worktree is not None else "")

    def rehearse(self, rest, typed=True):
        return f"hz-rehearse {rest}"

    def cut(self, version=""):
        return "hz-train tick --now"

    def alarm_ack(self, key):
        return f"hz-alarm ack {q(key)}"


def renamed(doc):
    """Adapt legacy fixtures to explicit inventory scope and current command names."""
    # Legacy fixtures model complete inventories; make that premise explicit.
    recent = doc["state"].get("runs")
    doc["state"]["active_runs"] = {"complete": isinstance(recent, list), "runs": [r for r in recent if r.get("status") in rules.ACTIVE] if isinstance(recent, list) else []}
    tools = doc["state"].get("tools")
    if isinstance(tools, dict) and "unknown" not in tools:
        doc["state"]["tools"] = {"rehearse": tools.get("hz-rehearse"), "drop_candidate": tools.get("drop_candidate")}
    return doc


def recorded(name):
    return renamed(json.loads((FIXTURES / name).read_text()))


def old_shape(decision):
    return {k: v for k, v in decision.items() if k != "also_items"}


# ------------------------------------------------------------------ parity with the old tool
def test_the_decision_on_the_recorded_real_state_equals_the_old_tools_byte_for_byte():
    doc = recorded("go-apply-running.json")
    mine = rules.decide(doc["state"], doc["now"], OldWords())
    assert old_shape(mine) == doc["decision"]
    assert "\n".join(rules.render(mine)) == doc["text"]


def test_all_62_decisions_of_the_old_tools_own_test_file_are_reproduced_byte_for_byte():
    # fixtures/go/go-matrix.json: recorded 2026-11-12 with `record_go_fixtures.py matrix OLD/tests/test_go.py`: every
    # decide() call of the old tool's 48 tests, with its state, time, decision and text.
    matrix = [renamed(doc) for doc in json.loads((FIXTURES / "go-matrix.json").read_text())]
    assert len(matrix) == 62 and len({doc["decision"]["kind"] for doc in matrix}) >= 20
    retired = lambda doc: (doc["state"].get("tools") or {}).get("rehearse") or (doc["state"].get("rehearse") or {}).get("last") or (doc["state"].get("rehearse") or {}).get("current")  # noqa: E731
    for n, doc in enumerate(matrix):
        if retired(doc):  # the old tool's rehearsal decisions: `sm release rehearse` is deleted
            continue
        mine = rules.decide(doc["state"], doc["now"], OldWords())
        if not doc["state"]["active_runs"]["complete"]:
            expected = "active runs: unknown/incomplete inventory; idle not established"
            assert expected in mine["facts"]
            mine["facts"].remove(expected)  # The intentional new unknown-inventory disclosure.
        assert old_shape(mine) == doc["decision"], (n, doc["decision"]["kind"])
        assert "\n".join(rules.render(mine)) == doc["text"], n


def test_no_decision_of_the_matrix_names_an_old_tool_in_sm_words():
    for n, doc in enumerate(renamed(d) for d in json.loads((FIXTURES / "go-matrix.json").read_text())):
        text = "\n".join(go_mod.report(doc["state"], doc["now"]).render())
        assert not OLD.search(text), (n, OLD.search(text), text)


def test_in_sm_words_no_line_names_an_old_tool_and_the_information_is_the_same():
    doc = recorded("go-apply-running.json")
    report = go_mod.report(doc["state"], doc["now"])
    text = "\n".join(report.render())
    assert not OLD.search(text), OLD.search(text)
    assert report.kind == doc["decision"]["kind"] == "apply-running" and report.next == ""
    assert report.waiting_on == doc["decision"]["waiting"].replace("hz-go", "sm go")
    assert report.facts == doc["decision"]["facts"] and report.aside == doc["decision"]["side"]
    assert [a.phase for a in report.also][1:] == ["backlog: PR #690 (fix/cgdn-prerelease-hold cb15803c6) every check green but BLOCKED -> sm queue why 690",
                                                 "backlog: PR #784 (feat/py-bundle-0912b 45654064e) red: Focused tests (python), CI OK, coverage-floor -> sm ci faillog 784"]


# ------------------------------------------------------------------ pure decisions
def run_row(run_id, branch="main", status="completed", conclusion="success", sha=MAIN, workflow="CI", event="push"):
    return {"databaseId": run_id, "headBranch": branch, "headSha": sha, "status": status, "conclusion": conclusion, "workflowName": workflow, "event": event}


def pr_row(number, branch="fix/x", head="a" * 40, merge="CLEAN", checks=(("CI OK", "COMPLETED", "SUCCESS"),), draft=False):
    return {"number": number, "title": "t", "headRefName": branch, "headRefOid": head, "isDraft": draft, "mergeStateStatus": merge,
            "statusCheckRollup": [{"name": n, "status": s, "conclusion": c} for n, s, c in checks]}


def packet(**over):
    base = {"dir": "/p", "packet": "/p/packet.json", "branch": "fix/e2e", "head": HEAD, "main": MAIN, "core_repo": "/wt", "worktree": "/wt",
            "review_root": "/root", "prepared": "/p/prepare.out", "prepared_sha256": "9a" * 32, "approved_sha256": None, "apply_refused": None,
            "nonconsumers": "/n/STUCK.json", "body": "/p/PR-BODY.md", "subject": "test(web): fix", "bead": None}
    return {**base, **over}


def state(**over):
    base = {"main": {"sha": MAIN}, "remote_heads": {"main": MAIN}, "runs": [run_row(1)], "prs": [], "releases": {"latest": "0.9.39"},
            "queue": {"queued": [], "freeze": False, "verdict": "nothing queued", "main_runs": [], "added": {}}, "train": {"trains": [], "tails": []},
            "release": {"active": None, "dropped": []}, "holds": {"files": [], "prs": {}}, "procs": [], "receipts": [], "packets": [],
            "slots": {}, "mini": {"cli": "0.9.39", "live": None, "live_ok": None}, "tools": {}, "since_tag": {"count": 0}}
    result = {**base, **over}
    recent = result["runs"]
    result.setdefault("active_runs", {"complete": isinstance(recent, list), "runs": [r for r in recent if r.get("status") in rules.ACTIVE] if isinstance(recent, list) else []})
    return result


def focus(**over):
    return {"worktree": "/wt", "for": "0.9.40", "note": "", "head": HEAD, "branch": "fix/e2e", "subject": "s", "dirty": False, "prs": [], **over}


def test_idle_cut_ready_and_the_review_wait():
    assert rules.decide(state(), NOW)["kind"] == "idle"
    cut = rules.decide(state(since_tag={"count": 3}), NOW)
    assert cut["kind"] == "cut-ready" and cut["next"] == "sm release cut 0.9.40" and "Consumer pin" in cut["trap"]
    assert rules.decide(state(since_tag={"count": 3}, tools={"rehearse": True}), NOW)["next"] == "sm release cut 0.9.40"
    review = rules.decide(state(packets=[packet()]), NOW)
    assert review["kind"] == "packet-prepared" and review["next"] is None and "9a" * 32 in review["waiting"] and review["then"].startswith("after the verdict")


def test_an_approved_packet_names_the_whole_apply_command_only_in_an_idle_window():
    approved = packet(approved_sha256="9a" * 32)
    idle = rules.decide(state(packets=[approved]), NOW)
    assert idle["kind"] == "packet-approved" and idle["next"].startswith("sm head apply /p/packet.json --core-repo /wt --review-root /root")
    assert idle["next"].endswith("--approved-sha256 " + "9a" * 32) and "--nonconsumers /n/STUCK.json" in idle["next"]
    busy = rules.decide(state(packets=[approved], runs=[run_row(1), run_row(7, branch="fix/y", status="in_progress", conclusion=None)]), NOW)
    assert busy["next"] is None and busy["waiting"].startswith("idle window: active runs 7 fix/y") and busy["then"].startswith("sm head apply")


def test_candidate_main_and_queue_phases():
    failed = rules.decide(state(prs=[pr_row(9, branch="release/v0.9.40")], runs=[run_row(1), run_row(5, branch="release/v0.9.40", conclusion="failure")],
                                tools={"drop_candidate": True}), NOW)
    assert failed["kind"] == "candidate-failed" and failed["next"] == "sm ci why 5" and failed["then"].endswith("sm release drop 0.9.40")
    red = rules.decide(state(runs=[run_row(2, conclusion="failure")]), NOW)
    assert red["kind"] == "main-red" and red["next"] == "sm ci main"
    queue = {"queued": ["12"], "freeze": False, "verdict": "", "main_runs": [], "added": {"12": NOW - 40 * 60}}
    stalled = rules.decide(state(prs=[pr_row(12)], queue=queue), NOW)
    assert stalled["kind"] == "pr-queued-stalled" and stalled["next"] == "sm queue why 12"
    fresh = rules.decide(state(prs=[pr_row(12)], queue={**queue, "added": {"12": NOW - 60}}), NOW)
    assert fresh["kind"] == "pr-queued" and fresh["waiting"] == "the queue tick (launchd, every 5 min) to merge #12"
    sonar = rules.decide(state(prs=[pr_row(13, head=HEAD, checks=(("SonarCloud Code Analysis", "COMPLETED", "FAILURE"),))], packets=[packet()]), NOW)
    assert "sm head sonar 13" in [sonar["next"], *sonar["also"]] or sonar["next"] == "sm head sonar 13"


def test_an_unknown_probe_is_named_and_the_decision_is_made_on_the_rest():
    d = rules.decide(state(prs=rules.unknown("gh exited 1: rate limit")), NOW)
    assert d["unknown"] == ["prs: gh exited 1: rate limit"] and d["kind"] == "idle"


# ------------------------------------------------------------------ the defect: 'no packet' while one exists or is in the making
def test_a_focus_head_without_packet_or_process_still_says_no_packet():
    d = rules.decide(state(focus=[focus()]), NOW)
    assert d["kind"] == "focus-local" and "no packet" in d["phase"]


def test_a_packet_of_the_predict_bind_route_carries_the_focus_head():
    bound = packet(dir="/r/topic/predict/bound-437bb247d164", packet="/r/topic/predict/bound-437bb247d164/packet.json", route="bound")
    d = rules.decide(state(focus=[focus()], packets=[bound]), NOW)
    assert d["kind"] == "packet-prepared" and "no packet" not in json.dumps(d)




def test_a_running_prepare_or_admission_for_the_focus_worktree_without_a_readable_packet_is_not_no_packet():
    for proc in ({"pid": 9, "etime": "00:40", "kind": "prepare", "packet": "m-1/packet.json", "core_repo": "/wt"},
                 {"pid": 9, "etime": "00:40", "kind": "admit", "worktree": "/wt", "packet": None}):
        d = rules.decide(state(focus=[focus()], procs=[proc]), NOW)
        assert d["kind"] == "prepare-running" and "no packet" not in d["phase"] and d["waiting"] == "pid 9 to finish; then run sm go again"


def test_process_lines_of_both_eras_are_recognised():
    old = probes.parse_process(1, "00:29", "/usr/bin/python3 /t/tools/hz-focused-admit apply /r/bound-1/packet.json --core-repo /c")
    assert old == {"pid": 1, "etime": "00:29", "kind": "apply", "packet": "/r/bound-1/packet.json"}
    new = probes.parse_process(2, "01:00", "/v/bin/python /v/bin/sm head prepare /r/m-1/packet.json --core-repo /wt --review-root /r --wait --out /r/m-1/prepare.out")
    assert new["kind"] == "prepare" and new["packet"] == "/r/m-1/packet.json" and new["core_repo"] == "/wt" and new["waits"]
    opts_first = probes.parse_process(3, "01:00", "python /v/bin/steuermaschine --format json head prepare --wait --out o.json m-1/packet.json")
    assert opts_first["packet"] == "m-1/packet.json"
    admit = probes.parse_process(4, "02:00", "/v/bin/python /v/bin/sm head admit /wt --approved abc")
    assert admit["kind"] == "admit" and admit["worktree"] == "/wt"
    assert probes.parse_process(5, "1", "vim notes.md") is None


def wave(directory: Path, **over):
    directory.mkdir(parents=True)
    data = {"branch": "fix/e2e", "head": HEAD, "main": MAIN, "core_repo": "/scratch", "worktree": "/wt", "review_root": str(directory.parent), "kind": "preopen", **over}
    (directory / "wave.json").write_text(json.dumps(data))
    return directory


def make_world(tmp_path, commands=None, **settings):
    p = fake_providers(commands=commands or FakeCommands())
    return probes.World(commands=p.commands, clock=p.clock, state_dir=tmp_path / "state", core="/core", repo="o/r", settings=settings)






# ------------------------------------------------------------------ adopted state files and the sweep
def test_legacy_readers_fill_defaults_and_fail_closed_on_holds(tmp_path):
    assert legacy.queue_state(tmp_path)["items"] == [] and legacy.alarm_state(tmp_path) == {} and legacy.focus_entries(tmp_path) == []
    assert not legacy.merge_frozen(tmp_path) and legacy.rehearsal_state(tmp_path) is None and legacy.release_receipt(tmp_path) is None
    (tmp_path / "release-holds").write_text("not a directory")
    assert legacy.merge_frozen(tmp_path)  # a malformed hold blocks like an owned one
    (tmp_path / "queue.json").write_text(json.dumps({"items": [{"key": "7", "status": "queued", "added": 5}], "blocked": "merge freeze is on"}))
    assert legacy.queue_state(tmp_path)["blocked"] == "merge freeze is on"


def test_sentences_from_state_files_are_renamed_to_sm_commands_and_file_names_stay():
    text = "STALL #801 BLOCKED; hz-queue why | CANDIDATE run 5: 2 failed; hz-faillog 5 | hz-rehearse status --json | see ~/.cache/hz-queue.json | hz-alarm ack k"
    assert legacy.sweep(text, ROWS) == ("STALL #801 BLOCKED; sm queue why | CANDIDATE run 5: 2 failed; sm ci faillog 5 | the rehearse step status --json "
                                        "| see ~/.cache/hz-queue.json | sm alarm ack k")


def test_queue_verdict_and_waits_from_what_the_last_tick_recorded():
    items = [{"key": "790", "status": "queued", "waiting_since": NOW - 277 * 60, "wait_reason": "BLOCKED with green checks"}]
    waits = probes.queue_waits(items, NOW)
    assert waits == ["item 790: waiting 277 min: BLOCKED with green checks"]
    data = {"items": items, "blocked": ""}
    assert probes.queue_verdict(data, False, waits) == "no queue-level block recorded, but 790: waiting 277 min: BLOCKED with green checks"
    assert probes.queue_verdict(data, True, waits) == "merge freeze holds all core PRs"
    assert probes.queue_verdict({"items": [], "blocked": ""}, False, []) == "nothing queued"
    assert probes.queue_verdict({"items": items, "blocked": "main's latest run is not green"}, False, []) == "last tick: main's latest run is not green"


# ------------------------------------------------------------------ the command, through the whole tree
def scripted(tmp_path) -> FakeCommands:
    c = FakeCommands()
    c.on(["git", "-C", "/core", "ls-remote", "origin"], Result(0, f"{MAIN}\trefs/heads/main\n"))
    c.on(["git", "-C", "/core", "ls-remote", "--heads"], Result(0, f"{MAIN}\trefs/heads/main\n"))
    c.on(["git", "-C", "/core", "rev-list"], Result(0, "3\n"))
    c.on(["gh", "run", "list"], Result(0, json.dumps([run_row(1)])))
    c.on(["gh", "pr", "list"], Result(0, "[]"))
    c.on(["gh", "release", "list"], Result(0, json.dumps([{"tagName": "v0.9.39", "isLatest": True, "publishedAt": "x"}])))
    c.on(["gh", "api"], Result(0, json.dumps({"variables": [{"name": "FOCUSED_CI_PLANS", "value": "x" * 4915}]})))
    c.on(["ps"], Result(0, "  1 01:00 /sbin/launchd\n"))
    c.on(["hz", "--version"], Result(0, "hz 0.9.39\n"))
    return c


def runtime_for(tmp_path, commands):
    state_dir = tmp_path / "state"
    (state_dir / "focused-admission-receipts").mkdir(parents=True)
    (state_dir / "alarm.json").write_text(json.dumps({"stall:801": {"cls": "ALERT", "text": "STALL #801 BLOCKED; hz-queue why", "first": 5, "acked": False}}))
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {"legacy_state_dir": str(state_dir), "core_repo_dir": "/core", "go_packet_globs": [str(tmp_path / "no/*.json")]}}, Path("."))
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, settings, _providers=fake_providers(commands=commands))


def test_go_in_text_through_the_command_tree(tmp_path):
    commands = scripted(tmp_path)
    out = invoke(runtime_for(tmp_path, commands), ["go"])
    assert out.exit_code == 0, out
    lines = out.stdout.splitlines()
    assert lines[0] == "ALERT    STALL #801 BLOCKED; sm queue why  (silence: sm alarm ack stall:801)"
    assert lines[1] == f"PHASE    main {MAIN[:8]} green, 3 commits since v0.9.39, no candidate" and lines[2] == "NEXT     sm release cut 0.9.40"
    assert "         focused slots: PLANS 9%" in lines and lines[-1].endswith("s, read-only)") and not OLD.search(out.stdout)
    assert not [argv for argv in commands.calls if argv[0] not in ("git", "gh", "ps", "hz")]


def test_go_in_json_is_one_document_of_the_model(tmp_path):
    out = invoke(runtime_for(tmp_path, scripted(tmp_path)), ["--format", "json", "go"])
    assert out.exit_code == 0, out
    report = go_mod.Go.model_validate_json(out.stdout)
    assert report.kind == "cut-ready" and report.read_only and report.alarms[0].key == "stall:801" and report.unknown == []
    assert report.probes["queue"]["verdict"] == "nothing queued" and report.probes["main"] == {"sha": MAIN}


def test_a_failing_probe_degrades_to_unknown_and_go_still_answers(tmp_path):
    commands = scripted(tmp_path)
    commands.on(["gh", "pr", "list"], Result(1, "", "gh: HTTP 502"))
    rt = runtime_for(tmp_path, commands)
    with using(NativeRuntime(rt.providers, rt.settings, None)):
        report = go_mod.go(all_=False)
    assert any(u.startswith("prs: gh: HTTP 502") for u in report.unknown) and report.phase


# ------------------------------------------------------------------ focus and acknowledgements come from the core commands
def test_the_focus_list_of_sm_focus_replaces_the_old_focus_file_once_it_exists(tmp_path):
    w = make_world(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "go-focus.json").write_text(json.dumps({"entries": [{"worktree": "/old", "for": "0.9.40", "note": "n"}]}))
    assert probes.declared_focus(w) == [{"worktree": "/old", "for": "0.9.40", "note": "n", "source": "old-file"}]
    w.focus_list = {"/new": {"for": "0.9.41", "note": ""}, "1.4.0": {"for": ""}}
    assert probes.declared_focus(w) == [{"worktree": "/new", "for": "0.9.41", "note": "", "source": "focus-list"}]
    w.focus_list = {}
    assert probes.declared_focus(w) == []


def test_a_merged_focus_head_names_the_command_that_takes_it_off():
    merged = focus(prs=[{"state": "MERGED", "headRefOid": HEAD}])
    assert "fix/e2e e9c943810 is merged: sm focus done /wt" in rules.decide(state(focus=[{**merged, "source": "focus-list"}]), NOW)["side"][0]
    assert rules.decide(state(focus=[{**merged, "source": "old-file"}]), NOW)["side"][0].endswith("is merged: sm focus add /wt --for 0.9.40 && sm focus done /wt")


def test_an_alert_acknowledged_with_sm_alarm_ack_no_longer_leads_go():
    alarms = {"stall:801": {"cls": "ALERT", "text": "STALL #801", "first": 5, "acked": False}}
    assert go_mod.report(state(alarms=alarms), NOW).alarms[0].key == "stall:801"
    assert go_mod.report(state(alarms=alarms), NOW, acks={"stall:801": NOW}).alarms == []


def test_go_items_for_the_core_frame_carry_phase_next_and_the_rest(tmp_path):
    rt = runtime_for(tmp_path, scripted(tmp_path))
    items = go_mod.go_items(rt.providers, rt.settings)
    assert items[0].rank == 10 and items[0].phase.endswith("green, 3 commits since v0.9.39, no candidate") and items[0].next == "sm release cut 0.9.40"


# ------------------------------------------------------------------ the rehearsal verdict: the store document first, the old file only without one
REHEARSAL = ("heinzel.rehearse.state", "rehearsal")  # where the native rehearsal writes (rehearse_lib/rehearsal.py KIND, KEY)
RED = {"main": MAIN, "verdict": "red", "failures": ["End-to-end web (ubuntu-latest): failure https://github.com/o/r/actions/runs/71"]}
GREEN = {"main": MAIN, "verdict": "green", "failures": []}


def decided(rt):
    with using(NativeRuntime(rt.providers, rt.settings, None)):
        return go_mod.go(all_=False, live=False)










def test_global_idle_uses_complete_active_inventory_not_recent_history():
    recent = [run_row(i) for i in range(60)]
    old = run_row(900, branch="old/backlog", status="queued")
    snapshot = state(runs=recent, active_runs={"complete": True, "runs": [old]})
    decision = rules.decide(snapshot, NOW)
    assert any("900" in fact for fact in decision["facts"])
    assert not any("idle window open" in fact for fact in decision["facts"])


@pytest.mark.parametrize("inventory", [None, {"unknown": "page failure"}, {"complete": False, "reason": "truncated", "runs": []}])
def test_unknown_or_truncated_active_inventory_never_claims_idle(inventory):
    decision = rules.decide(state(active_runs=inventory), NOW)
    assert "active runs: unknown/incomplete inventory; idle not established" in decision["facts"]
    assert not any("idle window open" in fact for fact in decision["facts"])


def test_active_probe_preserves_failure_and_truncation(tmp_path, monkeypatch):
    from steuermaschine.github.models import RunList

    for result in (RunList(state="unknown: page failed"), RunList(truncated=True), RunList()):
        monkeypatch.setattr(probes.GitHub, "active", lambda self, limit, result=result: result)
        actual = probes.probe_active_runs(make_world(tmp_path))
        assert actual["complete"] is (result.state == "ok" and not result.truncated)


@pytest.mark.parametrize("failure", [True, False])
def test_active_probe_actual_paginated_provider_refuses_incomplete(tmp_path, failure):
    from steuermaschine.github.testing import FakeGh

    fake = FakeGh().on("actions/runs", {"workflow_runs": []})
    if failure:
        fake.on("status=queued", 403)
    else:
        rows = [{"id": i, "status": "queued"} for i in range(100)]
        fake.on("status=queued", {"total_count": 350, "workflow_runs": rows})
    inventory = probes.probe_active_runs(make_world(tmp_path, commands=fake))
    assert not inventory["complete"]
    assert inventory["reason"].startswith("unknown:") if failure else inventory["reason"] == "truncated"
    decision = rules.decide(state(active_runs=inventory), NOW)
    assert not any("idle window open" in fact for fact in decision["facts"])


