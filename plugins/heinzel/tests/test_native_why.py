"""why PR, native. Fixtures in fixtures/why: recorded 2026-11-12 22:57 with the old one-PR tool for PR 797 and 690
(`pr-N-old.txt`), and for 797 the two GitHub answers it read (`gh pr view 797 --json state,mergeStateStatus,headRefOid,title`
and the check runs of its head, the three required names only). No test starts a process or reads the network."""

import json
from pathlib import Path

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, fake_providers, invoke
from steuermaschine_heinzel.native import why as why_mod
from steuermaschine_heinzel.native.go_lib import legacy
from steuermaschine_heinzel.native.why import CheckState, per_name, verdict
from steuermaschine_heinzel.plugin import plugin
from steuermaschine_heinzel.toolmap import ROWS

FIXTURES = Path(__file__).parent / "fixtures" / "why"
REPO = "helmut-hoffer-von-ankershoffen/heinzel"
MAIN_RUNS = [{"databaseId": 43307443399, "headSha": "f19a3782232cb24b682b3e0f5b094abb96da18fd", "attempt": 1, "status": "completed", "conclusion": "success"}]
REQUIRED = ("CI OK", "merge-gate", "coverage-floor")  # what main's ruleset required when the fixtures were recorded
RULES = json.dumps([{"type": "deletion"}, {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": n} for n in REQUIRED]}}])
GREEN = [CheckState(name=n, latest="SUCCESS") for n in ("CI OK", "merge-gate", "coverage-floor")]


def runtime_for(tmp_path, view: str, check_runs: str, queue: dict | None = None, members: bool = True, main_runs=MAIN_RUNS, rules: str = RULES):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if queue is not None:
        (state / "queue.json").write_text(json.dumps(queue))
    c = FakeCommands()
    c.on(["gh", "pr", "view"], Result(0, view))
    c.on(["gh", "api"], Result(0, check_runs))
    c.on(["gh", "api", f"repos/{REPO}/rules/branches/main"], Result(0, rules))
    c.on(["gh", "run", "list"], Result(0, json.dumps(main_runs)))
    table = {"legacy_state_dir": str(state), **({"why_combine_members_glob": str(tmp_path / "combine-*-members.txt")} if members else {})}
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": table}, Path("."))
    return c, Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, settings, _providers=fake_providers(commands=c))


def test_the_recorded_pr_is_explained_line_for_line_as_the_old_tool_did(tmp_path):
    commands, rt = runtime_for(tmp_path, (FIXTURES / "pr-797-view.json").read_text(), (FIXTURES / "pr-797-check-runs.jsonl").read_text(), queue={"items": []})
    out = invoke(rt, ["why", "797"])
    old = (FIXTURES / "pr-797-old.txt").read_text().splitlines()
    assert out.exit_code == 0 and out.stdout.splitlines() == [legacy.sweep(line, ROWS) for line in old], out
    assert out.stdout.splitlines()[-1] == "  WHY: not in the queue: sm queue add 797"
    assert {tuple(argv[:3]) for argv in commands.calls} == {("gh", "pr", "view"), ("gh", "api", f"repos/{REPO}/commits/5e409527037b01f4fd7ad525bbfc13a39f37b7ae/check-runs"),
                                                             ("gh", "api", f"repos/{REPO}/rules/branches/main"), ("gh", "run", "list")}


def test_a_pr_without_the_required_checks_names_them_like_the_old_tool(tmp_path):
    view = json.dumps({"state": "OPEN", "mergeStateStatus": "BLOCKED", "headRefOid": "cb15803" + "0" * 33, "title": "fix(release): hold stable releases as pre-releases until acceptance"})
    _, rt = runtime_for(tmp_path, view, "", queue={"items": []})
    assert invoke(rt, ["why", "690"]).stdout.splitlines() == (FIXTURES / "pr-690-old.txt").read_text().splitlines()


def test_json_is_one_document_and_roles_are_unknown_without_member_lists(tmp_path):
    _, rt = runtime_for(tmp_path, (FIXTURES / "pr-797-view.json").read_text(), (FIXTURES / "pr-797-check-runs.jsonl").read_text(), members=False)
    doc = why_mod.PullRequestWhy.model_validate_json(invoke(rt, ["--format", "json", "why", "797"]).stdout)
    assert doc.number == 797 and doc.merge_state == "CLEAN" and [c.latest for c in doc.checks] == ["SUCCESS"] * 3 and not doc.roles_known
    assert "  role: unknown (no combine member lists configured)" in doc.render()


def test_queue_entry_holds_and_role_are_read_from_the_recorded_state(tmp_path):
    queue = {"items": [{"key": "797", "status": "merged"}, {"key": "797", "status": "needs work", "detail": "CI OK failed"}], "blocked": "main's latest run is not green"}
    (tmp_path / "combine-web-members.txt").write_text("795 797 801\n")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "release-holds").mkdir()
    (tmp_path / "state" / "release-holds" / "v0.9.40").write_text("")
    _, rt = runtime_for(tmp_path, (FIXTURES / "pr-797-view.json").read_text(), (FIXTURES / "pr-797-check-runs.jsonl").read_text(), queue=queue)
    lines = invoke(rt, ["why", "797"]).stdout.splitlines()
    assert "  queue: needs work CI OK failed" in lines and "  holds: freeze=ON release-holds=['v0.9.40']" in lines
    assert "  last tick waiting: main's latest run is not green" in lines and "  role: combine-web" in lines
    assert lines[-1] == "  WHY: queue says needs work (CI OK failed): re-ship with sm queue add (stale after a push or reopen)"


def test_per_name_takes_the_latest_run_and_keeps_older_failures():
    runs = [{"name": "CI OK", "status": "completed", "conclusion": "failure", "completed_at": "2026-11-12T10:00:00Z"},
            {"name": "CI OK", "status": "completed", "conclusion": "success", "completed_at": "2026-11-12T11:00:00Z"},
            {"name": "merge-gate", "status": "in_progress", "conclusion": None, "completed_at": None}]
    assert per_name(runs, ["CI OK", "merge-gate", "coverage-floor"]) == [
        CheckState(name="CI OK", latest="SUCCESS", older_not_green=["FAILURE"]), CheckState(name="merge-gate", latest="IN_PROGRESS"),
        CheckState(name="coverage-floor", latest="MISSING")]


def test_the_required_checks_are_the_rulesets_and_a_check_it_does_not_require_is_not_shown(tmp_path):
    rules = json.dumps([{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "check"}, {"context": "Gate review"}]}}])
    runs = "\n".join(json.dumps(r) for r in [{"name": "check", "status": "completed", "conclusion": "success", "completed_at": "2026-11-22T08:00:00Z"},
                                               {"name": "coverage-floor", "status": "completed", "conclusion": "failure", "completed_at": "2026-11-22T08:00:00Z"}])
    _, rt = runtime_for(tmp_path, (FIXTURES / "pr-797-view.json").read_text(), runs, queue={"items": []}, rules=rules)
    doc = why_mod.PullRequestWhy.model_validate_json(invoke(rt, ["--format", "json", "why", "797"]).stdout)
    assert [(c.name, c.latest) for c in doc.checks] == [("check", "SUCCESS"), ("Gate review", "MISSING")]


def test_the_verdict_in_the_order_a_person_would_test_it():
    dup = [CheckState(name="CI OK", latest="SUCCESS", older_not_green=["FAILURE"]), *GREEN[1:]]
    queued = {"status": "queued"}
    assert verdict(7, "MERGED", "CLEAN", GREEN, None, False, [], True) == "nothing: PR is MERGED"
    assert verdict(7, "OPEN", "CLEAN", [CheckState(name="CI OK", latest="FAILURE"), *GREEN[1:]], queued, False, [], True) == "required check not green: CI OK"
    assert verdict(7, "OPEN", "BLOCKED", dup, queued, False, [], True).startswith("GitHub BLOCKED with all latest required checks green and an older failed duplicate on CI OK")
    assert verdict(7, "OPEN", "DIRTY", GREEN, queued, False, [], True) == "GitHub mergeStateStatus DIRTY"
    assert verdict(7, "OPEN", "CLEAN", GREEN, None, False, [], True) == "not in the queue: sm queue add 7"
    assert verdict(7, "OPEN", "CLEAN", GREEN, queued, True, [], True) == "merge hold: freeze file"
    assert verdict(7, "OPEN", "CLEAN", GREEN, queued, False, ["v0.9.40"], True) == "merge hold:  v0.9.40"
    assert verdict(7, "OPEN", "CLEAN", GREEN, queued, False, [], False) == "main's latest run is not green for the queue"
    assert verdict(7, "OPEN", "UNSTABLE", GREEN, queued, False, [], True) == "ready: waits for the next 5 minute batch"
