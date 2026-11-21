"""`sm status --prs`, `sm status --owner` and the review threads in `sm why`. Everything is the fake gh's: no process, no network."""

import json
from datetime import UTC, datetime

from test_native_status import runtime_for

from steuermaschine.github.testing import FakeGh
from steuermaschine.testing import invoke
from steuermaschine_heinzel.native.status_lib import owner, prtable
from steuermaschine_heinzel.native.why_lib import threads

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def node(number, ci="SUCCESS", sonar="SUCCESS", unresolved=0, resolved=0, draft=False, state="CLEAN", created="2026-11-21T09:00:00Z", author="helmut", extra=()):
    checks = [{"__typename": "CheckRun", "name": "CI OK", "status": "COMPLETED", "conclusion": ci},
              {"__typename": "CheckRun", "name": "review", "status": "COMPLETED", "conclusion": "FAILURE"},
              {"__typename": "CheckRun", "name": "Focused X", "status": "COMPLETED", "conclusion": "FAILURE"}, *extra]
    if sonar:
        checks.append({"__typename": "StatusContext", "context": "SonarCloud Code Analysis", "state": sonar})
    return {"number": number, "title": f"feat: thing {number}", "isDraft": draft, "mergeStateStatus": state, "createdAt": created, "author": {"login": author},
            "reviewThreads": {"nodes": [{"isResolved": False}] * unresolved + [{"isResolved": True}] * resolved},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": checks}}}}]}}


def listing(*nodes):
    return {"data": {"repository": {"pullRequests": {"nodes": list(nodes)}}}}


def test_the_row_reads_ci_sonar_threads_age_and_author():
    r = prtable.row("heinzel", node(7, unresolved=2, resolved=1), NOW)
    assert (r.ci, r.sonar, r.threads, r.age, r.author, r.state) == ("ok", "ok", 2, "3h 0m", "helmut", "CLEAN")
    assert prtable.row("x", node(8, ci="FAILURE", sonar="FAILURE"), NOW).ci == "FAIL 1"  # review and Focused never decide
    assert prtable.row("x", node(9, sonar=None), NOW).sonar == "-" and prtable.row("x", node(9, sonar="PENDING"), NOW).sonar == "running"
    assert prtable.row("x", node(10, draft=True), NOW).state == "DRAFT"
    running = {"__typename": "CheckRun", "name": "Lint", "status": "IN_PROGRESS", "conclusion": None}
    assert prtable.row("x", node(11, extra=[running]), NOW).ci == "running 1"


def test_status_prs_is_one_stateless_table_over_four_repositories(tmp_path):
    fake = FakeGh()
    fake.on("repo=heinzel", listing(node(1)))
    fake.on("repo=heinzel-plugins", listing(node(2, ci="FAILURE", unresolved=1)))
    fake.on("repo=steuermaschine", listing(node(35, author="bot")))
    fake.on("repo=heinzel-acme", 502)
    out = invoke(runtime_for(tmp_path, fake), ["status", "--prs"])
    lines = out.stdout.splitlines()
    assert out.exit_code == 0 and lines[0].split() == prtable.HEAD
    assert [line.split()[:2] for line in lines[1:4]] == [["heinzel", "#1"], ["heinzel-plugins", "#2"], ["steuermaschine", "#35"]]
    assert ("FAIL 1" in lines[2] and lines[3].split()[-4] == "bot") or "bot" in lines[3]
    assert any(line.startswith("UNKNOWN  could not list heinzel-acme") for line in lines)
    assert not fake.writes() and "T" not in "".join(line.split()[6] for line in lines[1:4])  # an age, never a stamp


def test_since_accepts_durations_and_stamps_with_a_zone_only():
    now = NOW.timestamp()
    assert owner.since_of("2h", now) == now - 7200 and owner.since_of("1d", now) == now - 86400
    assert owner.since_of("2026-11-21T10:00:00Z", now) == datetime(2026, 11, 21, 10, tzinfo=UTC).timestamp()
    for bad in ("yesterday", "2026-11-21T10:00:00"):
        try:
            owner.since_of(bad, now)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_decisions_file_lists_only_open_items(tmp_path):
    path = tmp_path / "decisions.md"
    path.write_text("# Decisions\n\n- [ ] Ship the widget?\n- [x] done one\n- plain bullet\n1. numbered\nprose line\n")
    assert owner.read_decisions(path) == (["Ship the widget?", "plain bullet", "numbered"], "decisions.md")
    assert owner.read_decisions(None)[0] == [] and "no decisions file" in owner.read_decisions(None)[1]
    assert "cannot read" in owner.read_decisions(tmp_path / "missing.md")[1]


def test_owner_update_renders_every_section_from_reads(tmp_path):
    (tmp_path / "decisions.md").write_text("- [ ] Pick the icon\n")
    fake = FakeGh()
    for repo in prtable.DEFAULT_REPOS:
        fake.on(f"repo={repo}", listing(node(1), node(2, ci="FAILURE", unresolved=1)) if repo == "heinzel" else listing())
    fake.on("release list -R", [{"tagName": "v0.9.53", "publishedAt": "2026-11-12T21:00:00Z"}, {"tagName": "v0.9.52", "publishedAt": "2026-11-10T11:00:00Z"}])
    fake.on("pr list -R", [{"mergedAt": "2026-11-12T08:00:00Z"}, {"mergedAt": "2026-11-11T12:00:00Z"}, {"mergedAt": "2026-11-11T13:00:00Z"}])
    fake.on("run list", [{"databaseId": 9, "headSha": "abcdef012345", "status": "completed", "conclusion": "success", "createdAt": "2026-11-12T21:30:00Z"}])
    (tmp_path / "AGENTS.md").write_text("# tracking\n")
    fake.on("list --status open", [{"id": "heinzel-management-a1"}] * 5).on("list --status in_progress", [{"id": "heinzel-management-b1"}] * 2)
    fake.on("list --status blocked", []).on("ready --json", [{"id": "heinzel-management-a1"}] * 3)
    rt = runtime_for(tmp_path, fake, status_decisions_file=str(tmp_path / "decisions.md"), coord_document=str(tmp_path / "AGENTS.md"))
    out = invoke(rt, ["status", "--owner", "--since", "2h"])
    text = out.stdout
    assert out.exit_code == 0, out
    head = text.split("Pull requests")[0]
    assert "Releases since then (4)" in head and "v0.9.53" in head and "v0.9.52" not in head  # one tag per repository; the old one is before the window
    assert "heinzel: 2 open, 1 ready, 1 red CI, 1 with unresolved threads, 0 draft" in text
    assert "Main CI: run 9 on abcdef01: success" in text and "Open owner decisions (1)" in text and "1. Pick the icon" in text
    assert "Merges: 4 today against 8 yesterday" in text and "Main CI: run 9 on abcdef01: success, started 25m ago" in text
    assert "Beads: open 5, in progress 2, blocked 0, ready 3" in text and "UNKNOWN" not in text
    assert "2026-11-21T" not in text  # no raw UTC stamp
    doc = json.loads(invoke(rt, ["--format", "json", "status", "--owner"]).stdout)
    assert doc["decisions"] == ["Pick the icon"] and doc["since"].endswith("+00:00")


def test_both_views_at_once_are_refused(tmp_path):
    out = invoke(runtime_for(tmp_path, FakeGh()), ["status", "--prs", "--owner"])
    assert out.exit_code == 2


def thread(login="github-advanced-security", resolved=False, outdated=False):
    return {"isResolved": resolved, "isOutdated": outdated, "path": "src/a.py", "line": 12,
            "comments": {"nodes": [{"url": "https://x/1", "body": "Incomplete URL substring sanitization\nmore", "author": {"login": login}}]}}


def test_threads_parse_names_codeql_and_skips_resolved():
    found = threads.parse({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [thread(), thread(resolved=True), thread("someone", outdated=True)]}}}}})
    assert [t.source for t in found] == ["CodeQL", "someone"]
    assert found[0].line_text() == "CodeQL src/a.py:12: Incomplete URL substring sanitization" and "(outdated)" in found[1].line_text()
    assert threads.parse(None) == [] and threads.parse({"data": {"repository": None}}) == []
    assert "2 unresolved review thread(s), not a CI problem" in threads.reason(found) and "do not rerun CI" in threads.reason(found)


# ----------------------------------------------------------------------------- sm why: threads as the block reason
def why_runtime(tmp_path, view_state, graphql):
    import test_native_why as base

    from steuermaschine.protocols import Result

    runs = "".join(json.dumps({"name": n, "status": "completed", "conclusion": "success", "completed_at": "2026-11-12T20:00:00Z"}) + "\n" for n in base.why_mod.REQUIRED)
    view = json.dumps({"state": "OPEN", "mergeStateStatus": view_state, "headRefOid": "a" * 40, "title": "fix: x"})
    commands, rt = base.runtime_for(tmp_path, view, runs, queue={"items": []})
    commands.on(["gh", "api", "graphql"], Result(0, json.dumps(graphql)))
    return commands, rt


def test_blocked_with_green_checks_names_the_unresolved_codeql_thread(tmp_path):
    answer = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [thread(), thread(resolved=True)]}}}}}
    commands, rt = why_runtime(tmp_path, "BLOCKED", answer)
    out = invoke(rt, ["why", "888"])
    assert out.exit_code == 0, out
    assert "  unresolved thread: CodeQL src/a.py:12: Incomplete URL substring sanitization" in out.stdout
    assert out.stdout.splitlines()[-1].startswith("  WHY: GitHub BLOCKED with all required checks green: 1 unresolved review thread(s), not a CI problem")
    assert [c for c in commands.calls if c[:3] == ["gh", "api", "graphql"]]


def test_threads_are_read_only_when_blocked_and_green(tmp_path):
    commands, rt = why_runtime(tmp_path, "CLEAN", {})
    out = invoke(rt, ["why", "888"])
    assert not [c for c in commands.calls if c[:3] == ["gh", "api", "graphql"]] and "unresolved thread" not in out.stdout


def test_blocked_without_threads_or_unreadable_threads_says_so(tmp_path, tmp_path_factory):
    _, rt = why_runtime(tmp_path, "BLOCKED", {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}})
    assert invoke(rt, ["why", "888"]).stdout.splitlines()[-1] == "  WHY: GitHub mergeStateStatus BLOCKED"
    commands, rt = why_runtime(tmp_path_factory.mktemp("b"), "BLOCKED", {})
    from steuermaschine.protocols import Result

    commands.on(["gh", "api", "graphql"], Result(1, "", "HTTP 502"))
    assert "review threads unread:" in invoke(rt, ["why", "888"]).stdout


# ----------------------------------------------------------------------------- sm work brief BEAD
def test_work_brief_is_generated_from_the_bead_and_creates_nothing(tmp_path):
    from coord_kit import app_runtime, ok

    from steuermaschine.testing import FakeCommands

    c = FakeCommands()
    bead = {"id": "heinzel-management-ab12", "title": "Bigger logo in the header!", "description": "Make the logo bigger. Done = a PR with a screenshot."}
    c.on(["bd", "show"], lambda argv: ok([bead]))
    c.on(["git", "-C"], lambda argv: ok("Owner Name\n" if argv[-1] == "user.name" else "owner@example.org\n"))
    rt = app_runtime(tmp_path, c, brief_code_dir=str(tmp_path / "Code"))
    out = invoke(rt, ["beads", "brief", "ab12"])
    text = out.stdout
    assert out.exit_code == 0, out
    assert text.splitlines()[0] == "Builder brief for bead heinzel-management-ab12: Bigger logo in the header!"
    assert "Make the logo bigger. Done = a PR with a screenshot." in text and "Identity: Owner Name <owner@example.org>" in text
    for rule in ("--no-verify", "force-push", "live Mini hz", "MacBook Air", "no git stash"):
        assert rule in text
    assert "worktree add" in text and "-b feat/bigger-logo-in-the-header origin/main" in text and "Report once, in at most 150 words" in text and "`sm check`" in text
    assert not [a for a in c.calls if a[:2] == ["git", "-C"] and ("worktree" in a or "fetch" in a)]  # nothing created
    assert invoke(rt, ["beads", "brief", "ab12", "--task", "x"]).exit_code == 2


def test_the_slug_is_a_safe_branch_name():
    from steuermaschine_heinzel.native.briefs_lib import workbrief

    assert workbrief.slug("Fix: A/B  test (again)!") == "fix-a-b-test-again" and workbrief.slug("???") == "work" and len(workbrief.slug("x" * 90)) == 40
