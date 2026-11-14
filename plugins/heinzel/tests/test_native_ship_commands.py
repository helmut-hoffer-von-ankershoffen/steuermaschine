"""`sm head push | guard | ship | pr` through the command tree, on a scripted runner
(`gh`, `git`, `bd` and the run lock all answer from one table). No network, no real process, every path in a sandbox."""

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.engine import Refuse
from steuermaschine.github.testing import FakeGh
from steuermaschine.native import NativeRuntime, using
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native import preflight as preflight_area
from steuermaschine_heinzel.native import queue as queue_area
from steuermaschine_heinzel.native import ship as area
from steuermaschine_heinzel.native.ship_lib import deps, push_gate
from steuermaschine_heinzel.plugin import plugin

REAL = Path(__file__).parent / "fixtures" / "queue" / "state-2026-11-12.json"
CORE = "helmut-hoffer-von-ankershoffen/heinzel"
HEAD = "e9c943810ea75cfad155a431a2f168b2a9559828"
DIFF = "diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n+++ b/src/x.py\n@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n"
WEAKER = "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -10,1 +10,2 @@\n   steps:\n+    continue-on-error: true\n"
GREEN = [{"name": n, "status": "COMPLETED", "conclusion": "SUCCESS"} for n in ("CI OK", "merge-gate", "coverage-floor")]


def long_ago() -> str:
    """Forty minutes before now, as the code host writes a time."""
    return (datetime.now(timezone.utc) - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def nothing_real(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".cache").mkdir()
    monkeypatch.setenv("HZ_ALARM_NOTIFY", "off")
    for name in ("HZ_QUEUE_STATE", "HZ_PUSH_GATE_MIN", "HZ_PR_OPEN_REPO", "DRAFT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: pytest.fail(f"a real process was started: {argv}"))
    yield
    queue_area.reset()


@pytest.fixture
def gh():
    fake = FakeGh().on("pr diff 801", DIFF).on("pr view 801", {"title": "fix: x", "body": "one fix", "headRefOid": HEAD, "headRefName": "fix/x"})
    return fake.on("run list", [])


@pytest.fixture
def rt(tmp_path, gh):
    state = tmp_path / ".cache" / "queue.json"
    shutil.copy(REAL, state)
    records = tmp_path / "records"
    (records / "research").mkdir(parents=True)
    (records / "tools").mkdir()
    (records / "AGENTS.md").write_text("## 9. Log\n")
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {"queue_state_file": str(state), "queue_records_dir": str(records)}}, Path("."))
    runtime = Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=True), stack, settings,
                      _providers=fake_providers(commands=gh))
    runtime.state_file, runtime.records = state, records
    return runtime


def keys(rt):
    return [i["key"] for i in json.loads(rt.state_file.read_text())["items"] if i["status"] == "queued"]


def doc(out):
    return area.HeadResult.model_validate(json.loads(out.stdout))


def starved(gh):
    """Main CI has had a run queued for 40 minutes and no job of main is running."""
    gh.on("run list -R " + CORE + " --status queued --branch main", [{"createdAt": long_ago()}])
    gh.on("--branch main --event push", [{"databaseId": 9, "status": "queued"}])
    gh.on("actions/runs/9/jobs", "0\n")
    gh.on("pr list", [])


# ----------------------------------------------------------------------------- push
def test_push_gate_says_ok_when_main_ci_is_not_starved_and_reads_only(rt, gh):
    out = invoke(rt, ["head", "push", "801", "--gate-only"])
    assert out.exit_code == 0 and out.stdout.startswith("OK: main CI not starved"), out
    assert not gh.writes() and all(c[0] == "gh" for c in gh.calls)


def test_push_gate_says_wait_with_exit_75_when_main_ci_is_starved_and_names_sm(rt, gh):
    starved(gh)
    out = invoke(rt, ["--format", "json", "head", "push", "801", "--gate-only"])
    result = doc(out)
    assert out.exit_code == 75 and result.verdict == "wait" and result.lines[0].startswith("WAIT: main CI has been queued 4")
    assert "`sm go`" in result.lines[0] and "`sm head push 801`" in result.lines[0] and "hz-" not in out.stdout


def test_push_gate_holds_for_an_active_release_candidate_and_a_minimum_waives_it(rt, gh):
    gh.on("--status queued --limit 50", [{"databaseId": 77, "headBranch": "release/v0.9.40", "status": "queued", "createdAt": long_ago()}])
    gh.on("--status in_progress --limit 50", [])
    held = invoke(rt, ["head", "push", "801", "--gate-only"])
    assert held.exit_code == 75 and "release candidate CI run 77" in held.stdout
    waived = invoke(rt, ["head", "push", "801", "--min", "15"])
    assert waived.exit_code == 0 and "waiver" in waived.stdout


def test_push_gate_waits_during_a_merge_freeze_except_for_fix_prs_and_the_waiver(rt, gh, tmp_path):
    assert invoke(rt, ["head", "push", "801", "--gate-only"]).exit_code == 0
    (statedir.root() / "merge-freeze").touch()  # the incident freeze; a release's owned hold below
    held = invoke(rt, ["head", "push", "801", "--gate-only"])
    assert held.exit_code == 75 and "a merge freeze is on" in held.stdout and "PR #801 is not a fix PR" in held.stdout
    (statedir.root() / "merge-freeze").unlink()
    (statedir.root() / "release-holds").mkdir()
    (statedir.root() / "release-holds" / "v0.9.41").write_text("owned\n")
    assert invoke(rt, ["head", "push", "801", "--gate-only"]).exit_code == 75
    assert invoke(rt, ["head", "push", "801", "--min", "15"]).exit_code == 0  # the coordinator's waiver
    (rt.records / "research" / "known-failures.toml").write_text('note = "fix PR #801 for the red lint job"\n')
    assert invoke(rt, ["head", "push", "801", "--gate-only"]).exit_code == 0  # a fix PR goes on
    with using(NativeRuntime(rt.providers, rt.settings, rt.project)):
        assert push_gate.check("release/v0.9.41")[0] == push_gate.OK
        gh.on("--head fix/y --state open --json number -q", "801\n").on("--head feat/z --state open --json number -q", "")
        assert push_gate.is_fix("fix/y") and not push_gate.is_fix("feat/z")
        assert push_gate.check("feat/z")[0] == push_gate.WAIT


def test_printed_lines_are_passed_on_line_by_line():
    assert area._translate("retry: sm head pr\nnext") == ["retry: sm head pr", "next"]


def test_wait_reason_is_empty_for_ok_and_the_wait_line_otherwise(rt, gh):
    with using(NativeRuntime(rt.providers, rt.settings, rt.project)):
        assert push_gate.wait_reason("801") == ""
        starved(gh)
        assert push_gate.wait_reason("801").startswith("WAIT: main CI has been queued")
        assert push_gate.check("fix/x")[0] == push_gate.WAIT  # a branch without a PR is never on the keep list
    down = FakeGh().on("run list", Result(1, "", "gh: Bad Gateway (HTTP 502)"))
    with using(NativeRuntime(fake_providers(commands=down), rt.settings, rt.project)):
        assert push_gate.check("801") == (0, ["OK (unchecked): gh unavailable (CalledProcessError); push"])  # fails open
        assert push_gate.wait_reason("801") == ""


def test_push_runs_the_preflight_areas_ci_meta_first_without_quoting_a_text(rt, gh, monkeypatch):
    seen = []

    def meta_of(runtime, worktree, ref="", pr=None, *, quote=True):
        seen.append((pr, ref, quote))
        return SimpleNamespace(problems=["commit 1 subject (1 = newest): not conventional -> reword"], commits=1)

    monkeypatch.setattr(preflight_area, "meta_of", meta_of)
    bad = invoke(rt, ["head", "push", "801"])
    assert bad.exit_code == 1 and seen == [(801, "", False)] and "commit 1 subject" in bad.stdout and "fix the CI meta problems above BEFORE pushing" in bad.stderr
    monkeypatch.setattr(deps, "ci_meta", lambda worktree, pr=None, ref="": [])
    assert invoke(rt, ["head", "push", "801"]).exit_code == 0
    starved(gh)
    wait = invoke(rt, ["head", "push", "801"])
    assert wait.exit_code == 75 and "DO NOT PUSH #801 now" in wait.stderr and "retry: sm head push 801" in wait.stderr


def test_push_of_a_branch_checks_that_branch_not_the_worktrees_head(rt, gh, monkeypatch):
    seen = []
    monkeypatch.setattr(deps, "ci_meta", lambda worktree, pr=None, ref="": seen.append((pr, ref)) or [])
    gh.on("pr list", [])
    invoke(rt, ["head", "push", "fix/other"])
    invoke(rt, ["head", "push", "801"])
    assert seen == [("fix/other", "fix/other"), ("801", "")]


TERM = "zorblax"  # an invented confidential term: no output, error or log line may ever carry it


def term_checker(argv):
    """The head's confidentiality checker, scripted: a finding (line and term number only) for each line of the
    checked file that carries TERM."""
    text = Path(argv[argv.index("--commit-msg") + 1]).read_text()
    found = [f"text:{n}: confidential term #1" for n, line in enumerate(text.splitlines(), 1) if TERM in line.lower()]
    return Result(1, "", "\n".join(found) + "\n") if found else Result(0, "", "")


def leaked(tmp_path, *outputs, skip=()) -> list[str]:
    """Every output and every file under the sandbox (state, records, logs) that carries TERM."""
    hits = [f"output {i}" for i, text in enumerate(outputs) if TERM in (text or "").lower()]
    for path in tmp_path.rglob("*"):
        if path.is_file() and path not in skip and TERM in path.read_text(errors="replace").lower():
            hits.append(str(path))
    return hits


def test_push_names_where_a_term_is_and_never_prints_it(rt, gh, tmp_path, monkeypatch):
    """`sm head push 801` with a term in a commit subject, the PR title and the branch name: the lines name the
    positions (and the subject rule), never the text."""
    head = tmp_path / "wt"
    (head / "scripts").mkdir(parents=True)
    (head / "scripts" / "check_confidentiality.py").write_text("# stand-in: the runner is scripted\n")
    monkeypatch.chdir(head)
    gh.on("pr view 801 --repo", {"title": f"fix: {TERM} launch", "headRefName": f"feat/{TERM.upper()}", "baseRefName": "main",
                                 "commits": [{"messageHeadline": "fix: clean one"}, {"messageHeadline": f"Add {TERM} export"}]})
    gh.on("--commit-msg", term_checker)
    out = invoke(rt, ["head", "push", "801"])
    assert out.exit_code == 1, out
    assert not leaked(tmp_path, out.stdout, out.stderr), out
    for line in ("commit 2 subject (1 = newest) -> not conventional", "branch name -> lowercase", "the commit 2 subject (1 = newest) contains a confidential term",
                 "the PR title contains a confidential term", "the branch name contains a confidential term"):
        assert any(line.replace(" -> not conventional", ": not conventional") in got or line in got for got in out.stdout.splitlines()), (line, out.stdout)
    assert len([c for c in gh.calls if "--commit-msg" in c]) == 1 and not any(TERM in a.lower() for c in gh.calls for a in c)  # one call, on a file


# ----------------------------------------------------------------------------- guard, ship
def test_guard_passes_a_plain_diff_and_flags_a_weakened_check(rt, gh):
    assert invoke(rt, ["head", "guard", "801"]).stdout == "PASS 801\n"
    gh.on("pr diff 801", WEAKER)
    flagged = invoke(rt, ["--format", "json", "head", "guard", "801"])
    assert flagged.exit_code == 1 and doc(flagged).verdict == "flag" and doc(flagged).lines[0].startswith("FLAG 801: 1 change(s)")
    gh.on("pr diff 801", Result(1, "", "no access"))
    assert invoke(rt, ["head", "guard", "801"]).exit_code == 2


def test_ship_queues_a_pr_the_guard_passes_with_the_calls_of_the_old_script_in_order(rt, gh):
    out = invoke(rt, ["head", "ship", "801"])
    assert (out.exit_code, out.stdout) == (0, "queued 801\n"), out
    assert keys(rt) == ["801"]
    assert gh.lines() == [
        f"run list -R {CORE} --event pull_request --limit 60 --json status -q " + '[.[]|select(.status=="queued" or .status=="in_progress")]|length',
        f"pr diff 801 -R {CORE}",  # the guard
        f"pr view 801 -R {CORE} --json headRefOid",  # the queue binds its guard verdict to this head ...
        f"pr diff 801 -R {CORE}",  # the queue guards again when it adds
        f"pr view 801 -R {CORE} --json headRefOid",  # ... and refuses when it moved meanwhile
        f"pr view 801 -R {CORE} --json title,body",
    ]


def test_ship_answers_needs_review_for_a_flagged_diff_and_queues_it_with_a_written_reason(rt, gh):
    gh.on("pr diff 801", WEAKER)
    out = invoke(rt, ["--format", "json", "head", "ship", "801"])
    result = doc(out)
    assert out.exit_code == 2 and result.verdict == "needs_review" and result.lines[0] == "NEEDS_REVIEW 801" and len(result.lines) <= 7
    assert keys(rt) == []
    reviewed = invoke(rt, ["head", "ship", "801", "independent review: research/review-pr801.md"])
    assert reviewed.exit_code == 0 and reviewed.stdout == "queued 801\n" and keys(rt) == ["801"]
    assert "reviewed: independent review: research/review-pr801.md" in (rt.records / "AGENTS.md").read_text()
    again = invoke(rt, ["head", "ship", "801"])
    assert again.exit_code == 0 and again.stdout.startswith("CLEARED 801: guard already cleared")
    gh.on("pr view 801", {"title": "fix: x", "body": "one fix", "headRefOid": "f" * 40, "headRefName": "fix/x"})
    moved = invoke(rt, ["head", "ship", "801"])  # review F2: a cleared verdict holds for the cleared head only
    assert moved.exit_code == 2 and moved.stdout.startswith("NEEDS_REVIEW 801"), moved


def test_ship_pins_the_reviewed_head_and_passes_the_queues_refusal_on(rt, gh):
    pinned = invoke(rt, ["head", "ship", "801", "review r", "--reviewed-head", HEAD])
    row = json.loads(rt.state_file.read_text())["items"][-1]
    assert pinned.exit_code == 0 and row["reviewed_head"] == HEAD
    moved = invoke(rt, ["head", "ship", "801", "review r", "--reviewed-head", "f" * 40])
    assert moved.exit_code == 1 and "current reviewed head differs" in moved.stderr
    empty = invoke(rt, ["head", "ship", "801", "review r", "--reviewed-head", ""])
    assert empty.exit_code == 1 and "--reviewed-head requires one normal core PR" in empty.stderr


@pytest.mark.parametrize("argv", [["801", "--reviewed-head", HEAD], ["801", "", "--reviewed-head", HEAD], ["801", "--for", "web=abc"], ["801", "--dry-run"]])
def test_ship_usage_errors_are_exit_2_and_queue_nothing(rt, argv):
    out = invoke(rt, ["head", "ship", *argv])
    assert out.exit_code == 2 and "usage:" in out.stderr and keys(rt) == []


def test_ship_says_when_more_than_five_pr_ci_runs_are_in_flight(rt, gh):
    gh.on("--event pull_request", "7\n")
    out = invoke(rt, ["head", "ship", "801"])
    assert out.stdout.splitlines() == ["NOTE: 7 PR CI runs in flight > 5: queue is fine, but push nothing new until it drops", "queued 801"]


def test_ship_fix_forward_goes_to_the_queues_lane_with_its_options(rt, gh):
    out = invoke(rt, ["head", "ship", "801", "--fix-forward", "--for", "web-2=9kk", "--dry-run"])
    assert out.exit_code == 1 and keys(rt) == []  # the lane checked and refused (no red main in this script); nothing queued
    assert any("fix-forward" in line for line in (out.stdout + out.stderr).splitlines())


def test_ship_fix_forward_passes_reviewed_head_to_the_lane_which_refuses_it(rt, gh):
    """Blocker: --reviewed-head was dropped, so the lane pinned whatever the PR head is now. It reaches the queue, as
    the script's "$@" did, and the queue refuses it in the fix-forward lane: nothing is queued."""
    out = invoke(rt, ["head", "ship", "801", "--fix-forward", "--for", "web-2=9kk", "--reviewed", "r", "--reviewed-head", HEAD])
    assert out.exit_code == 1 and "--reviewed-head requires one normal core PR" in out.stderr and keys(rt) == [], out
    assert not any("pr merge" in line for line in gh.lines())


def test_ship_fix_forward_passes_a_positional_reason_to_the_lane_which_refuses_it(rt, gh):
    out = invoke(rt, ["head", "ship", "801", "independent review: r", "--fix-forward", "--for", "web-2=9kk"])
    assert out.exit_code == 1 and "--fix-forward takes exactly one core PR" in out.stderr and keys(rt) == [], out


# ----------------------------------------------------------------------------- pr
@pytest.fixture
def worktree(gh, tmp_path, monkeypatch):
    """A scripted worktree on branch fix/x at HEAD, clean, containing origin/main; the remote branch does not exist."""
    monkeypatch.setattr(deps, "commit_meta", lambda worktree, pr=None, ref="": ([], 1))
    monkeypatch.setattr(preflight_area, "texts_check", lambda rt, worktree, *, title, body="": [])
    gh.on("rev-parse --abbrev-ref HEAD", "fix/x\n").on("status --porcelain", "").on("fetch --quiet origin main", "")
    gh.on("merge-base --is-ancestor origin/main " + HEAD, "").on("ls-remote --heads origin refs/heads/fix/x", "").on("rev-parse HEAD", HEAD + "\n")
    gh.on("remote get-url origin", f"git@github.com:{CORE}.git\n").on("rev-list --count origin/main.." + HEAD, "1\n")
    gh.on("pr list -R " + CORE + " --head fix/x", [])
    body = tmp_path / "body.md"
    body.write_text("What changed for users.\n")
    return body


def test_pr_without_live_checks_everything_and_prints_the_plan_but_pushes_and_opens_nothing(rt, gh, worktree):
    out = invoke(rt, ["--format", "json", "head", "pr", "abc", "fix(web): keep the draft", "--body-file", str(worktree)])
    result = doc(out)
    assert out.exit_code == 0 and result.verdict == "dry_run", out
    assert any(f"gate run --max-wait 1800 -- git push origin {HEAD}:refs/heads/fix/x" in line for line in result.lines)
    assert any("gh pr create" in line and "--body-file" in line for line in result.lines)
    assert not gh.writes() and not any("git push" in " ".join(c) for c in gh.calls) and not any(c[:3] == ["gh", "pr", "merge"] for c in gh.calls)


OPENED = {"number": 801, "title": "fix(web): keep the draft", "body": "What changed for users.\n", "state": "OPEN", "headRefName": "fix/x", "headRefOid": HEAD,
          "baseRefName": "main", "isCrossRepository": False, "headRepositoryOwner": {"login": CORE.split("/")[0]}, "headRepository": {"name": CORE.split("/")[1]},
          "url": "https://example.invalid/pull/801"}


def live_pr(gh, adopted=None, **view):
    """The forge side of a live run: no PR before the push (or `adopted`, an open one), one after create; the read back is OPENED + `view`."""
    if adopted is None:
        gh.on("pr list -R " + CORE + " --head fix/x", [], [], [], [OPENED])  # push gate, plan, before create, after create
    else:
        gh.on("pr list -R " + CORE + " --head fix/x", [], [adopted])
    gh.on("gate run --max-wait", "pushed\n").on("pr create", "https://example.invalid/pull/801\n")
    gh.on("pr view 801 -R " + CORE + " --json number", {**OPENED, **view})


def test_pr_live_pushes_the_checked_sha_once_under_the_run_lock_opens_the_pr_and_ships(rt, gh, worktree):
    live_pr(gh)
    bodies = []
    gh.on("pr create", lambda argv: bodies.append((argv[argv.index("--body-file") + 1], Path(argv[argv.index("--body-file") + 1]).read_text())) or "https://example.invalid/pull/801\n")
    out = invoke(rt, ["head", "pr", "abc", "fix(web): keep the draft", "--body-file", str(worktree), "--live"])
    assert out.exit_code == 0 and out.stdout.splitlines()[-2:] == ["queued 801", "801"], out
    lines = [" ".join(c) for c in gh.calls]
    push = [line for line in lines if "git push" in line]
    assert len(push) == 1 and push[0].endswith(f"-m steuermaschine.app gate run --max-wait 1800 -- git push origin {HEAD}:refs/heads/fix/x")
    assert not any("--force" in c or "+" + HEAD in " ".join(c) for c in gh.calls)  # never forced
    assert not any("make" in c for c in gh.calls)  # the repository's own PR route is never run
    create = next(c for c in gh.calls if c[:3] == ["gh", "pr", "create"])
    assert create[create.index("--title") + 1] == "fix(web): keep the draft"
    assert bodies[0][0] != str(worktree) and bodies[0][1] == "What changed for users.\n"  # a private copy of the checked text
    assert not Path(bodies[0][0]).exists()  # and removed afterwards
    order = [next(i for i, line in enumerate(lines) if needle in line) for needle in ("git push", "pr create", "pr diff 801")]
    assert order == sorted(order)
    assert not any(c[:3] == ["gh", "pr", "merge"] or "--auto" in c for c in gh.calls)  # merging is the queue's job; auto-merge is never switched on
    assert keys(rt) == ["801"]


def test_pr_body_changed_after_the_check_is_not_what_gh_reads(rt, gh, worktree):
    """gh used to re-read the user's file at create time; the file now changes during the push, gh gets the checked text."""
    live_pr(gh)
    bodies = []
    gh.on("gate run --max-wait", lambda argv: (worktree.write_text(f"Prepared for {TERM}.\n"), "pushed\n")[1])
    gh.on("pr create", lambda argv: bodies.append(Path(argv[argv.index("--body-file") + 1]).read_text()) or "https://example.invalid/pull/801\n")
    out = invoke(rt, ["head", "pr", "abc", "fix(web): keep the draft", "--body-file", str(worktree), "--live"])
    assert out.exit_code == 0 and bodies == ["What changed for users.\n"], out


def test_pr_of_a_focus_head_or_a_release_branch_pushes_exclusively(rt, gh, worktree):
    live_pr(gh)
    out = invoke(rt, ["head", "pr", "abc", "fix(web): keep the draft", "--body-file", str(worktree), "--live"])
    push = [" ".join(c) for c in gh.calls if "git push" in " ".join(c)]
    assert out.exit_code == 0 and "--exclusive" not in push[0]  # an ordinary head queues as usual
    rt.providers.store.save("focus", "list", {str(Path(".").resolve()): {"for": "0.9.41"}})
    gh.calls.clear()
    live_pr(gh)
    out = invoke(rt, ["head", "pr", "abc", "fix(web): keep the draft", "--body-file", str(worktree), "--live"])
    push = [" ".join(c) for c in gh.calls if "git push" in " ".join(c)]
    assert out.exit_code == 0, out
    assert push[0].endswith(f"gate run --max-wait 1800 --priority 2 --exclusive -- git push origin {HEAD}:refs/heads/fix/x")
    assert "critical path: exclusive, on an idle machine" in out.stdout + out.stderr
    assert deps.gate(5, exclusive=True)[-5:] == ["5", "--priority", "2", "--exclusive", "--"]


def test_pr_refuses_when_head_moves_during_the_checks_and_pushes_nothing(rt, gh, worktree):
    live_pr(gh)
    gh.on("rev-parse HEAD", HEAD + "\n", "f" * 40 + "\n")
    out = invoke(rt, ["head", "pr", "abc", "fix: x", "--body-file", str(worktree), "--live"])
    assert out.exit_code == 1 and "moved while it was checked" in out.stderr and not published(gh), out


def test_pr_opens_no_pr_when_head_moved_during_the_wait_for_the_run_lock(rt, gh, worktree):
    live_pr(gh)
    gh.on("rev-parse HEAD", HEAD + "\n", HEAD + "\n", "f" * 40 + "\n")  # before the checks, after them, after the push
    out = invoke(rt, ["head", "pr", "abc", "fix: x", "--body-file", str(worktree), "--live"])
    assert out.exit_code == 1 and "HEAD moved while this command waited for the run lock" in out.stderr, out
    pushes = [c for c in gh.calls if "push" in c]
    assert len(pushes) == 1 and pushes[0][-1] == f"{HEAD}:refs/heads/fix/x"  # what went out is the checked commit
    assert not any(c[:3] == ["gh", "pr", "create"] for c in gh.calls) and keys(rt) == []


def test_pr_gives_up_with_75_when_the_run_lock_is_not_free_within_the_setting(rt, gh, worktree, monkeypatch):
    live_pr(gh)
    configure = deps.configure
    monkeypatch.setattr(deps, "configure", lambda: {**configure(), "head_pr_lock_max_wait_seconds": 5})
    gh.on("gate run --max-wait", Result(75, "", "sm gate: gave up after 5s (max wait 5s)\n"))
    out = invoke(rt, ["head", "pr", "abc", "fix: x", "--body-file", str(worktree), "--live"])
    assert out.exit_code == 75 and "not free within 5s" in out.stderr and "nothing was pushed" in out.stderr, out
    assert any(c[-6:-2] == ["--max-wait", "5", "--", "git"] or "--max-wait 5 --" in " ".join(c) for c in gh.calls)
    assert not any(c[:3] == ["gh", "pr", "create"] for c in gh.calls) and keys(rt) == []


@pytest.mark.parametrize(("view", "why"), [
    ({"isCrossRepository": True, "headRepositoryOwner": {"login": "someone-else"}}, "from another repository (a fork)"),
    ({"headRefOid": "f" * 40}, "not at the pushed head"),
    ({"baseRefName": "release"}, "not into main"),
    ({"headRefName": "fix/y"}, "not on the branch fix/x"),
])
def test_pr_ships_only_the_pr_of_this_repository_at_the_pushed_sha(rt, gh, worktree, view, why):
    adopted = {**OPENED, **view}
    live_pr(gh, adopted=adopted, **view)
    out = invoke(rt, ["head", "pr", "abc", "fix: x", "--body-file", str(worktree), "--live"])
    assert out.exit_code == 1 and why in out.stderr and "it is not shipped" in out.stderr, out
    assert keys(rt) == [] and not any("pr diff 801" in line for line in gh.lines())


def test_pr_refuses_a_worktree_whose_origin_is_another_repository(rt, gh, worktree):
    gh.on("remote get-url origin", "https://github.com/someone/plugins.git\n")
    out = invoke(rt, ["head", "pr", "abc", "fix: x", "--body-file", str(worktree), "--live"])
    assert out.exit_code == 2 and f"origin is not {CORE}" in out.stderr and not published(gh), out


def test_pr_refuses_when_the_head_has_commits_the_preflight_did_not_check(rt, gh, worktree):
    gh.on("rev-list --count origin/main.." + HEAD, "3\n")  # the preflight saw 1 (no local origin/main, say)
    out = invoke(rt, ["head", "pr", "abc", "fix: x", "--body-file", str(worktree), "--live"])
    assert out.exit_code == 1 and "not every commit was checked" in out.stderr and not published(gh), out


def test_pr_with_a_reason_pins_the_ship_step_to_the_pushed_sha(rt, gh, worktree, monkeypatch):
    live_pr(gh)
    seen = []
    monkeypatch.setattr(deps, "ship", lambda number, reason="", reviewed_head=None: seen.append((number, reason, reviewed_head)) or deps.Done(0, "queued 801\n"))
    out = invoke(rt, ["head", "pr", "abc", "fix(web): keep the draft", "--body-file", str(worktree), "--reason", "independent review r", "--live"])
    assert out.exit_code == 0 and seen == [("801", "independent review r", HEAD)], out


@pytest.mark.parametrize(("needle", "answer", "code", "message"), [
    ("rev-parse --abbrev-ref HEAD", "main\n", 1, "not a work branch"),
    ("status --porcelain", "?? scratch.txt\n", 1, "uncommitted changes"),
    ("merge-base --is-ancestor origin/main " + HEAD, Result(1, "", ""), 2, "behind origin/main"),
    ("ls-remote --heads origin refs/heads/fix/x", "a" * 40 + "\trefs/heads/fix/x\n", 2, "never rebases and never forces. Nothing was pushed. Either bring them in (git merge origin/fix/x"),
])
def test_pr_refuses_before_any_push(rt, gh, worktree, needle, answer, code, message):
    gh.on(needle, answer).on("merge-base --is-ancestor " + "a" * 40, Result(1, "", "")).on("fetch --quiet origin fix/x", "")
    out = invoke(rt, ["head", "pr", "abc", "fix: x", "--body-file", str(worktree), "--live"])
    assert out.exit_code == code and message in out.stderr, out
    assert not any("git push" in " ".join(c) for c in gh.calls) and not gh.writes()


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("problems", [["title: not a conventional commit subject -> use type(scope): text"], ["body: a confidential term (line 3) -> remove it"]])
def test_pr_publishes_nothing_when_the_title_or_the_body_must_not_be_published(rt, gh, worktree, monkeypatch, live, problems):
    """Publishing is irreversible: a violating title or body means zero writes, also in a dry run, and the output
    carries the check's own lines only."""
    seen = {}
    monkeypatch.setattr(preflight_area, "texts_check", lambda rt, worktree, *, title, body="": seen.update(title=title, body=body) or problems)
    out = invoke(rt, ["head", "pr", "abc", "Fixes stuff", "--body-file", str(worktree), *(["--live"] if live else [])])
    assert out.exit_code == 1 and problems[0] in out.stderr and "nothing was pushed or opened" in out.stderr
    assert seen == {"title": "Fixes stuff", "body": "What changed for users.\n"}
    assert not gh.writes() and not any("git push" in " ".join(c) or c[:3] == ["gh", "pr", "create"] for c in gh.calls)
    assert not any(c[:2] == ["gh", "pr"] and c[2] in ("create", "merge", "edit") for c in gh.calls)


REAL_TEXTS_CHECK = preflight_area.texts_check
REAL_COMMIT_META = deps.commit_meta


@pytest.fixture
def checked(tmp_path, worktree, monkeypatch):
    """A worktree that has the head's confidentiality checker; the preflight area's real `texts_check` runs, the
    checker itself answers from the scripted runner."""
    head = tmp_path / "wt"
    (head / "scripts").mkdir(parents=True)
    (head / "scripts" / "check_confidentiality.py").write_text("# stand-in: the runner is scripted\n")
    monkeypatch.setattr(preflight_area, "texts_check", REAL_TEXTS_CHECK)
    return head


def published(gh) -> bool:
    return bool(gh.writes()) or any("git push" in " ".join(c) or c[:3] == ["gh", "pr", "create"] or c[:3] == ["gh", "pr", "merge"] for c in gh.calls)


@pytest.mark.parametrize("live", [False, True])
def test_pr_with_a_bad_title_writes_nothing(rt, gh, worktree, checked, live):
    gh.on("--commit-msg", "", "")  # no term in the title nor in the body
    out = invoke(rt, ["head", "pr", "abc", "Fixes stuff", "--body-file", str(worktree), "--worktree", str(checked), *(["--live"] if live else [])])
    assert out.exit_code == 1 and "PR title: not conventional" in out.stderr and "nothing was pushed or opened" in out.stderr, out
    assert "Fixes stuff" not in out.stderr  # the check names the rule, not the text
    assert not published(gh) and not any("fetch" in c or "ls-remote" in c for c in gh.calls)


@pytest.mark.parametrize("live", [False, True])
def test_pr_with_a_confidential_term_in_the_body_writes_nothing_and_never_prints_the_term(rt, gh, worktree, checked, live):
    worktree.write_text("What changed for users.\nNothing else.\nPrepared for ACME-INTERNAL.\n")
    gh.on("--commit-msg", "", Result(1, "", "text:3: confidential term #2\n"))  # title clean, body: a term on line 3
    out = invoke(rt, ["head", "pr", "abc", "fix(web): keep the draft", "--body-file", str(worktree), "--worktree", str(checked), *(["--live"] if live else [])])
    assert out.exit_code == 1 and "PR body: contains a confidential term (line 3)" in out.stderr and "nothing was pushed or opened" in out.stderr, out
    assert "ACME" not in out.stdout + out.stderr
    assert len([c for c in gh.calls if "--commit-msg" in c]) == 2 and not published(gh)


def test_pr_publishes_nothing_when_the_check_itself_refuses(rt, gh, worktree, monkeypatch):
    """The check raises a refusal when it could not check (no checker, no terms): that is never read as "clean"."""
    def unchecked(rt, worktree, *, title, body=""):
        raise Refuse("title and body were not checked, nothing may be published: no terms are loaded", "sm self config")

    monkeypatch.setattr(preflight_area, "texts_check", unchecked)
    out = invoke(rt, ["head", "pr", "abc", "fix: x", "--body-file", str(worktree), "--live"])
    assert out.exit_code == 1 and "nothing may be published" in out.stderr and "nothing was pushed or opened" in out.stderr
    assert not gh.writes() and not any("git push" in " ".join(c) or c[:3] == ["gh", "pr", "create"] for c in gh.calls)


def test_pr_checks_the_default_body_too(rt, gh, worktree, monkeypatch):
    seen = []
    monkeypatch.setattr(preflight_area, "texts_check", lambda rt, worktree, *, title, body="": seen.append(body) or [])
    assert invoke(rt, ["head", "pr", "abc", "fix: x"]).exit_code == 0
    assert seen == ["Bead abc.\n"]


def test_pr_refuses_a_long_title_an_empty_body_ci_meta_problems_and_a_push_gate_wait(rt, gh, worktree, monkeypatch):
    long = invoke(rt, ["head", "pr", "abc", "x" * 73, "--live"])
    assert long.exit_code == 1 and "title is 73 chars" in long.stderr
    worktree.write_text("  \n")
    assert invoke(rt, ["head", "pr", "abc", "fix: x", "--body-file", str(worktree), "--live"]).exit_code == 2
    worktree.write_text("body\n")
    starved(gh)
    wait = invoke(rt, ["head", "pr", "abc", "fix: x", "--body-file", str(worktree), "--live"])
    assert wait.exit_code == 75 and "DO NOT PUSH now" in wait.stderr
    monkeypatch.setattr(deps, "commit_meta", lambda worktree, pr=None, ref="": (["commit 1 subject (1 = newest): not conventional -> reword"], 1))
    meta = invoke(rt, ["head", "pr", "abc", "fix: x", "--body-file", str(worktree), "--live"])
    assert meta.exit_code == 1 and "fix the CI meta problems BEFORE pushing" in meta.stderr and "nothing was pushed or opened" in meta.stderr
    assert not any("git push" in " ".join(c) for c in gh.calls)


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("field", ["title", "body", "branch", "subject", "adopted title", "adopted body"])
def test_pr_never_prints_a_term_from_any_field(rt, gh, tmp_path, worktree, checked, monkeypatch, field, live):
    """Blocker: a term in a commit subject or the branch name was quoted by the meta lines, the branch was printed
    before any check, an adopted PR's title was printed unchecked. The real checks run (the checker scripted); the
    term is put in one field at a time and must be in no output and no file of the sandbox; nothing is shipped."""
    if field in ("adopted title", "adopted body") and not live:
        pytest.skip("a PR is adopted only by a live run")
    monkeypatch.setattr(deps, "commit_meta", REAL_COMMIT_META)
    gh.on("--commit-msg", term_checker)
    branch = f"fix/{TERM}" if field == "branch" else "fix/x"
    gh.on("rev-parse --abbrev-ref HEAD", branch + "\n").on("merge-base origin/main", "b" * 40 + "\n")
    gh.on("log --format=%s", "fix(web): keep the draft\n" + (f"Add {TERM} export\n" if field == "subject" else ""))
    gh.on("rev-list --count origin/main.." + HEAD, "1\n")
    title = f"fix(web): keep the {TERM} draft" if field == "title" else "fix(web): keep the draft"
    worktree.write_text(f"What changed.\nPrepared for {TERM.upper()}.\n" if field == "body" else "What changed for users.\n")
    adopted = {**OPENED, "title": f"fix: ship {TERM}" if field == "adopted title" else OPENED["title"],
               "body": f"Made for {TERM}.\n" if field == "adopted body" else OPENED["body"]}
    live_pr(gh, adopted=adopted, **adopted)
    out = invoke(rt, ["head", "pr", "abc", title, "--body-file", str(worktree), "--worktree", str(checked), *(["--live"] if live else [])])
    assert out.exit_code == 1, out
    assert not leaked(tmp_path, out.stdout, out.stderr, skip=(worktree,)), out
    assert "confidential term" in out.stderr and keys(rt) == []
    if field.startswith("adopted"):
        assert "PR #801 must not be merged as it is; it is not shipped" in out.stderr and not any("pr diff 801" in line for line in gh.lines())
    else:
        assert "nothing was pushed or opened" in out.stderr and not published(gh)


def test_a_cleared_guard_counts_for_the_same_repository_and_the_exact_head_only():
    """Review F2: guard_cleared matched the PR number alone; another repository or a moved head is not cleared."""
    from steuermaschine_heinzel.native.queue_lib import known_failures

    row = {"pr": "801", "repo": CORE, "status": "queued", "reviewed_head": "", "guarded_head": HEAD}
    assert known_failures.guard_cleared("801", [row], CORE, HEAD)
    assert not known_failures.guard_cleared("801", [row], CORE, "f" * 40)
    assert not known_failures.guard_cleared("801", [row], CORE + "-plugins", HEAD)
    assert not known_failures.guard_cleared("801", [row], CORE, "")
    assert not known_failures.guard_cleared("801", [{**row, "status": "needs work"}], CORE, HEAD)
