import json
import subprocess
import sys

import pytest
from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native.queue_lib import core_policy

CORE = "helmut-hoffer-von-ankershoffen/heinzel"
PLUGINS = "helmut-hoffer-von-ankershoffen/heinzel-plugins"


@pytest.fixture
def lib(tool):
    sys.path.insert(0, str(tool.__globals__["TOOLS"]))
    from steuermaschine_heinzel.native.queue_lib import hzlib

    return hzlib


def check(name, conclusion="SUCCESS", status="COMPLETED", run=None):
    url = f"https://github.com/o/r/actions/runs/{run}/job/1" if run else ""
    return {"name": name, "status": status, "conclusion": conclusion, "detailsUrl": url}


class Fake:
    """gh stand-in: `views` maps (subcommand, number) to a list of replies (the last repeats); other calls are recorded."""

    def __init__(self, **replies):
        self.replies, self.calls = {k: list(v) for k, v in replies.items()}, []

    def __call__(self, *args):
        self.calls.append(args)
        key = " ".join(args[:2]) + (" " + args[2] if args[0] in ("pr", "run") and args[1] == "view" else "")
        for name, queue in self.replies.items():
            if key.replace(" ", "_") == name or key.startswith(name.replace("_", " ")):
                reply = queue.pop(0) if len(queue) > 1 else queue[0]
                return 0, json.dumps(reply) if not isinstance(reply, str) else reply
        return 0, "{}"


def patch(monkeypatch, module, lib, fake, **extra):
    monkeypatch.setattr(lib, "gh", fake)
    monkeypatch.setattr(lib, "sleep", lambda s: None)
    monkeypatch.setattr(lib, "ghj", lambda *a, default=None: json.loads(fake(*a)[1]) or default)
    for name, value in extra.items():
        monkeypatch.setattr(lib, name, value)


def test_watch_parses_runs_prs_and_urls(tool, lib):
    w = tool("hz-watch")
    assert w.parse(["41295168692"], lib.CORE) == ("run", lib.CORE, "41295168692")
    assert w.parse(["393"], lib.CORE) == ("pr", lib.CORE, "393")
    assert w.parse(["plugins", "38"], lib.CORE) == ("pr", lib.PLUGINS, "38")
    assert w.parse(["https://github.com/a/b/pull/7"], lib.CORE) == ("pr", "a/b", "7")
    assert w.parse(["https://github.com/a/b/actions/runs/99"], lib.CORE) == ("run", "a/b", "99")


def test_watch_prints_failing_tests_of_a_red_pr(tool, lib, monkeypatch, capsys):
    w = tool("hz-watch")
    rollup = [check("Lint"), check("E2E", "FAILURE", run=555), check("review", "FAILURE", run=556)]
    fake = Fake(pr_view=[{"state": "OPEN", "statusCheckRollup": rollup}])
    patch(monkeypatch, w, lib, fake, failing_tests=lambda repo, run: ["a_test.py::test_x"] if run == 555 else ["WRONG"])
    code, text = w.watch_pr(lib.CORE, "7", 1e18)
    assert code == 1
    assert text == "PR #7: red\n  failed: E2E\n  tests:\n    a_test.py::test_x"


def test_watch_waits_then_reports_green_and_ignores_review(tool, lib, monkeypatch):
    w = tool("hz-watch")
    pending = {"state": "OPEN", "statusCheckRollup": [check("CI OK", "", "IN_PROGRESS")]}
    done = {"state": "OPEN", "statusCheckRollup": [check("CI OK"), check("review", "FAILURE")]}
    patch(monkeypatch, w, lib, Fake(pr_view=[pending, done]))
    assert w.watch_pr(lib.CORE, "7", 1e18) == (0, "PR #7: green")


def test_watch_run_outcomes_and_timeout(tool, lib, monkeypatch):
    w = tool("hz-watch")
    patch(monkeypatch, w, lib, Fake(run_view=[{"status": "completed", "conclusion": "success", "jobs": []}]))
    assert w.watch_run(lib.CORE, "123", 1e18) == (0, "run 123: success")
    red = {"status": "completed", "conclusion": "failure", "jobs": [{"name": "Unit", "conclusion": "failure"}, {"name": "Lint", "conclusion": "success"}]}
    patch(monkeypatch, w, lib, Fake(run_view=[red]), failing_tests=lambda r, i: ["t::a"])
    assert w.watch_run(lib.CORE, "123", 1e18) == (1, "run 123: failure\n  failed: Unit\n  tests:\n    t::a")
    patch(monkeypatch, w, lib, Fake(run_view=[{"status": "in_progress"}]))
    assert w.watch_run(lib.CORE, "123", 0)[0] == 2


def test_keys(lib):
    assert lib.split_key(["plugins", "#38"]) == (lib.PLUGINS, "38", "plugins #38")
    assert lib.split_key("393") == (lib.CORE, "393", "393")
    with pytest.raises(ValueError):
        lib.split_key(["nope", "1"])


# the queue


GUARDED = "0" * 40


@pytest.fixture
def queue(tool, lib, home, monkeypatch):
    monkeypatch.setenv("HZ_QUEUE_STATE", str(statedir.root() / "queue.json"))
    q = tool("hz-queue")
    monkeypatch.setattr(q, "guard_findings", lambda repo, number: ([], "PASS"))
    read_head = q.pr_head

    def guarded_head(repo, number):  # review F2: every row binds the head the guard read; a test without a view reads GUARDED
        try:
            return read_head(repo, number) or GUARDED
        except Exception:  # noqa: BLE001 - a test whose fake answers other calls only
            return GUARDED

    monkeypatch.setattr(q, "pr_head", guarded_head)
    notes, merges = [], []
    monkeypatch.setattr(lib, "note", notes.append)
    monkeypatch.setattr(lib, "sleep", lambda s: None)
    q.notes, q.merges = notes, merges
    train = tool("hz-merge-train")

    def fake_train(repo, pr, ignore_freeze=False, queue_core=False, expected_head="", pre_merge_guard=None):
        assert queue_core == (repo == lib.CORE)
        if expected_head:
            item = next(i for i in q.load()["items"] if i["pr"] == pr)
            assert expected_head == (item["reviewed_head"] or item["guarded_head"])
        if pre_merge_guard is not None and not pre_merge_guard():
            return False, "main evidence changed"
        merges.append((repo, pr))
        return True, "abc12345"

    monkeypatch.setattr(train, "train", fake_train)
    monkeypatch.setattr(lib, "load", lambda name: train)
    return q


def add(q, *prs, after=(), fixes_main=False):
    class A:
        pass

    a = A()
    a.pr, a.after, a.fixes_main = q.pr_words(list(prs)), list(after), fixes_main
    q.add(a)


def view(rollup=None, status="CLEAN", state="OPEN"):
    return {"state": state, "isDraft": False, "mergeStateStatus": status, "statusCheckRollup": rollup if rollup is not None else [check("CI OK")]}


def setup(monkeypatch, lib, q, views, main_green=True):
    fake = Fake(**{"pr_view": views})
    patch(monkeypatch, q, lib, fake)
    monkeypatch.setattr(q, "main_state", lambda: "green" if main_green else "red")
    return fake


def test_a_green_pr_merges_once_and_notes_once(queue, lib, monkeypatch, capsys):
    add(queue, "393")
    setup(monkeypatch, lib, queue, [view()])
    queue.train_tick(now=10_000)
    assert queue.merges == [(lib.CORE, "393")]
    assert queue.notes == ["sm queue: #393 merged", "sm queue: batch: 393"]
    assert queue.load()["items"][0]["status"] == "merged"
    queue.train_tick(now=10_001)
    assert len(queue.merges) == 1


REVIEWED_HEAD = "1" * 40
REVIEWED_ADD = ["add", "--reviewed", "independent pin review", "--reviewed-head", REVIEWED_HEAD, "594"]


@pytest.mark.parametrize("observed_head", ["2" * 40, None])
def test_reviewed_normal_admission_refuses_unverified_head(queue, lib, monkeypatch, capsys, observed_head):
    setup(monkeypatch, lib, queue, [{"headRefOid": observed_head}])
    assert queue.main(REVIEWED_ADD) == 1
    assert "independent review required" in capsys.readouterr().err
    assert not queue.load()["items"]
    assert not queue.notes


@pytest.mark.parametrize("move", [False, True])
def test_reviewed_normal_admission_keeps_the_pin_through_the_queue(queue, lib, monkeypatch, move):
    reviewed = {**view(), "headRefOid": REVIEWED_HEAD}
    current = {**reviewed, "headRefOid": "2" * 40} if move else reviewed
    setup(monkeypatch, lib, queue, [reviewed, reviewed, current])  # moved: add reads the PR twice (title and body, then the head)
    assert queue.main(REVIEWED_ADD) == 0
    assert queue.load()["items"][0]["reviewed_head"] == REVIEWED_HEAD
    calls = []

    def merge(repo, number, **kwargs):
        assert callable(kwargs.pop("pre_merge_guard"))  # bead q9kh: every core merge carries the main plan check
        calls.append((repo, number, kwargs))
        return True, REVIEWED_HEAD[:8]

    monkeypatch.setattr(lib.load("hz-merge-train"), "train", merge)
    queue.train_tick(now=10_000)
    item = queue.load()["items"][0]
    if move:
        assert calls == []
        assert item["status"] == "needs work" and "independent review" in item["detail"]
    else:
        assert calls == [(lib.CORE, "594", {"queue_core": True, "expected_head": REVIEWED_HEAD})]
        assert item["status"] == "merged"


def test_reviewed_normal_pin_does_not_open_a_red_main_exception(queue, lib, monkeypatch):
    setup(monkeypatch, lib, queue, [{**view(), "headRefOid": REVIEWED_HEAD}], main_green=False)
    assert queue.main(REVIEWED_ADD) == 0
    queue.train_tick(now=10_000)
    assert not queue.merges
    assert queue.load()["items"][0]["status"] == "queued"


def test_one_batch_per_hour_and_after_waits_for_its_dependency(queue, lib, monkeypatch):
    add(queue, "393")
    add(queue, "plugins 38", after=["393"])
    setup(monkeypatch, lib, queue, [view()])
    queue.train_tick(now=10_000)
    assert queue.merges == [(lib.CORE, "393")]
    queue.train_tick(now=10_100)  # dependency met, but the hour is not over
    assert len(queue.merges) == 1
    queue.train_tick(now=10_000 + 3601)
    assert queue.merges[-1] == (lib.PLUGINS, "38")


def test_freeze_and_red_main_hold_the_batch(queue, lib, monkeypatch, home):
    add(queue, "393")
    setup(monkeypatch, lib, queue, [view()], main_green=False)
    queue.train_tick(now=10_000)
    assert not queue.merges and "not green" in queue.load()["blocked"]
    setup(monkeypatch, lib, queue, [view()], main_green=True)
    (statedir.root() / "merge-freeze").touch()
    queue.train_tick(now=10_000)
    assert not queue.merges
    (statedir.root() / "merge-freeze").unlink()
    queue.train_tick(now=10_000)
    assert queue.merges


def test_a_red_core_main_holds_only_core(queue, lib, monkeypatch):
    add(queue, "393", "plugins 38")
    setup(monkeypatch, lib, queue, [view()], main_green=False)
    queue.train_tick(now=10_000)
    assert queue.merges == [(lib.PLUGINS, "38")]


def test_the_fix_for_a_red_main_goes_alone_while_main_is_red(queue, lib, monkeypatch):
    add(queue, "393")
    add(queue, "394", fixes_main=True)
    setup(monkeypatch, lib, queue, [view(), view()], main_green=False)
    queue.train_tick(now=10_000)
    assert queue.merges == [(lib.CORE, "394")]
    items = {i["key"]: i["status"] for i in queue.load()["items"]}
    assert items == {"393": "queued", "394": "merged"}


def test_a_failure_is_needs_work_with_test_names_and_never_rerun(queue, lib, monkeypatch):
    add(queue, "393")
    fake = setup(monkeypatch, lib, queue, [view([check("Unit", "FAILURE", run=9)], status="BLOCKED")])
    monkeypatch.setattr(lib, "failing_tests", lambda repo, run: ["tests/a_test.py::test_real"])
    queue.train_tick(now=10_000)
    item = queue.load()["items"][0]
    assert item["status"] == "needs work" and "tests/a_test.py::test_real" in item["detail"]
    assert not [c for c in fake.calls if c[:2] == ("run", "rerun")]


def test_behind_core_is_not_updated_and_review_is_ignored(queue, lib, monkeypatch):
    add(queue, "393")
    fake = setup(monkeypatch, lib, queue, [view([check("CI OK"), check("review", "FAILURE")], status="BEHIND")])
    queue.train_tick(now=10_000)
    assert ("pr", "update-branch", "393", "-R", lib.CORE) not in fake.calls
    assert queue.merges == [(lib.CORE, "393")]


def test_behind_non_core_is_still_updated(queue, lib, monkeypatch):
    add(queue, "plugins", "38")
    fake = setup(monkeypatch, lib, queue, [view(status="BEHIND")])
    queue.train_tick(now=10_000)
    assert ("pr", "update-branch", "38", "-R", lib.PLUGINS) in fake.calls
    assert not queue.merges


def test_add_drop_and_list(queue, capsys):
    add(queue, "393", "plugins", "38")
    queue.listing()
    out = capsys.readouterr().out
    assert "393" in out and "plugins #38" in out


def test_site_only_pull_requests_count_toward_the_core_cap(queue, lib, monkeypatch):
    files = {"1": ["docs/guides/install.md"], "2": ["src/a.py"], "3": ["src/b.py"], "4": ["src/c.py"], "5": ["src/d.py"], "6": ["README.md"], "7": ["src/e.py"]}
    monkeypatch.setattr(lib, "ghj", lambda *a, default=None: {"files": [{"path": p} for p in files[a[2]]]})
    ready = [{"key": k, "pr": k, "repo": lib.CORE} for k in files]
    ready.append({"key": "plugins #38", "pr": "38", "repo": lib.PLUGINS})
    assert [i["key"] for i in queue.core_batch(ready)] == ["1", "2", "3", "4", "5", "plugins #38"]


def test_a_batch_of_five_core_prs_is_capped_and_noted_for_bisect(queue, lib, monkeypatch):
    add_all = [str(400 + i) for i in range(7)]
    add(queue, *add_all)
    setup(monkeypatch, lib, queue, [view()] * 7)
    monkeypatch.setattr(queue, "site_only", lambda item: False)
    queue.train_tick(now=10_000)
    assert [pr for _, pr in queue.merges] == add_all[:5]
    assert queue.load()["batch"]["prs"] == add_all[:5]
    assert queue.notes[-1] == "sm queue: batch: 400 401 402 403 404"


def test_batches_are_five_minutes_apart(queue, lib, monkeypatch):
    add(queue, "393", "394")
    setup(monkeypatch, lib, queue, [view()] * 2)
    monkeypatch.setattr(queue, "BATCH_SECONDS", 300)
    queue.train_tick(now=10_000)
    assert queue.BATCH_SECONDS == 300 and queue.MAX_CORE_BATCH == 5
    add(queue, "395")
    queue.train_tick(now=10_299)
    assert queue.merges == [(lib.CORE, "393"), (lib.CORE, "394")]
    queue.train_tick(now=10_301)
    assert queue.merges[-1] == (lib.CORE, "395")


def runs_for(monkeypatch, lib, runs, macos_only=False):
    monkeypatch.setattr(lib, "ghj", lambda *a, default=None: runs)
    monkeypatch.setattr(lib, "only_never_started_macos_jobs_failed", lambda repo, run: macos_only)


def run(id, status, conclusion, sha):
    return {"databaseId": id, "status": status, "conclusion": conclusion, "headSha": sha.ljust(40, "0"), "displayTitle": "Legacy full CI", "attempt": 1}


def test_an_in_progress_main_run_does_not_block_the_next_batch(queue, lib, monkeypatch):
    runs_for(monkeypatch, lib, [run(3, "in_progress", "", "ccc"), run(2, "completed", "success", "bbb")])
    assert queue.main_is_green()
    runs_for(monkeypatch, lib, [run(3, "in_progress", "", "ccc")])
    assert not queue.main_is_green()


def test_a_red_main_blocks_only_the_next_batch_and_records_it(queue, lib, monkeypatch):
    real_gate = queue.main_state
    add(queue, "393", "394")
    setup(monkeypatch, lib, queue, [view()] * 2)
    queue.train_tick(now=10_000)
    queue.save({**queue.load(), "batch": {**queue.load()["batch"], "shas": ["abc12345"]}})
    # main's one run for the batch finished red; a newer in-progress run is ignored
    add(queue, "395")
    fake = Fake(**{"pr_view": [view()]})
    patch(monkeypatch, queue, lib, fake)
    monkeypatch.setattr(queue, "main_state", real_gate)
    pr_view = lib.ghj
    runs_for(monkeypatch, lib, [run(5, "in_progress", "", "fff"), run(4, "completed", "failure", "abc12345ffff"), run(3, "completed", "success", "aaa")])
    runs = lib.ghj
    monkeypatch.setattr(lib, "ghj", lambda *a, default=None: runs(*a) if a[0] == "run" else pr_view(*a, default=default))
    monkeypatch.setattr(queue, "site_only", lambda item: False)
    queue.train_tick(now=10_400)
    assert [pr for _, pr in queue.merges] == ["393", "394"]
    state = queue.load()
    assert state["red_batch"]["prs"] == ["393", "394"] and "not green" in state["blocked"]


def test_a_red_batch_run_is_not_hidden_by_a_newer_green_run(queue, lib, monkeypatch):
    queue.save({**queue.load(), "batch": {"prs": ["1"], "shas": ["abc12345"]}})
    runs_for(monkeypatch, lib, [run(5, "completed", "success", "fff"), run(4, "completed", "failure", "abc12345ffff")], macos_only=True)
    assert queue.main_is_green()
    runs_for(monkeypatch, lib, [run(5, "completed", "success", "fff"), run(4, "completed", "failure", "abc12345ffff")])
    assert not queue.main_is_green()


def test_the_fix_still_goes_alone_when_main_is_completed_red(queue, lib, monkeypatch):
    add(queue, "393")
    add(queue, "394", fixes_main=True)
    setup(monkeypatch, lib, queue, [view(), view()], main_green=False)
    queue.train_tick(now=10_000)
    assert queue.merges == [(lib.CORE, "394")]


def test_list_prints_the_red_batch_with_a_bisect_hint(queue, capsys):
    queue.save({**queue.load(), "red_batch": {"prs": ["393", "385"]}})
    queue.listing()
    out = capsys.readouterr().out
    assert "RED after batch: 393 385" in out and "compare/" in out


def test_the_batch_records_the_commit_a_squash_merge_made_on_main(queue, lib, monkeypatch):
    patch(monkeypatch, queue, lib, Fake(pr_view=[{"mergeCommit": {"oid": "abcdef1234567890"}}]))
    assert queue.main_commit({"repo": lib.CORE, "pr": "393"}) == "abcdef12"
    patch(monkeypatch, queue, lib, Fake(pr_view=[{"state": "MERGED"}]))
    assert queue.main_commit({"repo": lib.CORE, "pr": "393"}) == ""


DIAGNOSTIC_HEAD = "a" * 40
MITIGATION_EVIDENCE = "controlled before/after setup measurements: 12.0s to 3.0s; original host trigger unproven"


def exception_options(kind):
    return {"diagnostic": ("--diagnostics-main", "--diagnostic-head"), "safety fix": ("--safety-fix-main", "--safety-head"), "mitigation": ("--mitigation-main", "--mitigation-head")}[kind]


def exception_evidence(kind):
    return ["--mitigation-evidence", MITIGATION_EVIDENCE] if kind == "mitigation" else []


def add_diagnostic(queue, lib, monkeypatch, pr="481", exception_kind="diagnostic"):
    option, head_option = exception_options(exception_kind)
    monkeypatch.setattr(lib, "ghj", lambda *a, default=None: {"headRefOid": DIAGNOSTIC_HEAD})
    purpose = {"safety fix": "repair a proven safety defect", "mitigation": "reduce measured setup overhead; original cause unresolved", "diagnostic": "measure the unexplained timeout"}[exception_kind]
    assert queue.main(["add", option, purpose, "--reviewed", "independent reviewer: NO weakening at this commit", head_option, DIAGNOSTIC_HEAD, *exception_evidence(exception_kind), pr]) == 0


@pytest.mark.parametrize("args", [
    ["--diagnostics-main", "metrics", "--diagnostic-head", DIAGNOSTIC_HEAD, "481"],
    ["--diagnostics-main", "metrics", "--reviewed", "NO weakening", "481"],
    ["--diagnostics-main", " ", "--reviewed", "NO weakening", "481"],
    ["--diagnostics-main", "metrics", "--reviewed", "NO weakening", "--diagnostic-head", DIAGNOSTIC_HEAD, "plugins 38"],
    ["--diagnostics-main", "metrics", "--reviewed", "NO weakening", "--diagnostic-head", DIAGNOSTIC_HEAD, "481", "482"],
    ["--diagnostic-head", DIAGNOSTIC_HEAD, "481"],
])
@pytest.mark.parametrize("exception_kind", ["diagnostic", "safety fix", "mitigation"])
def test_diagnostic_requires_explicit_review_purpose_head_and_one_core_pr(queue, args, exception_kind):
    option, head_option = exception_options(exception_kind)
    args = [a.replace("--diagnostics-main", option).replace("--diagnostic-head", head_option) for a in args]
    args = [*exception_evidence(exception_kind), *args]
    assert queue.main(["add", *args]) == 1
    assert not queue.load()["items"]


def test_diagnostic_and_fixes_main_are_mutually_exclusive(queue):
    with pytest.raises(SystemExit):
        queue.main(["add", "--diagnostics-main", "metrics", "--fixes-main", "481"])
    assert not queue.load()["items"]


@pytest.mark.parametrize("exception_kind", ["diagnostic", "safety fix", "mitigation"])
def test_diagnostic_refuses_a_different_or_unreadable_head(queue, lib, monkeypatch, exception_kind):
    option, head_option = exception_options(exception_kind)
    for actual in ({"headRefOid": "b" * 40}, {}):
        monkeypatch.setattr(lib, "ghj", lambda *a, default=None: actual)
        assert queue.main(["add", option, "metrics", "--reviewed", "NO weakening", head_option, DIAGNOSTIC_HEAD, *exception_evidence(exception_kind), "481"]) == 1
        assert not queue.load()["items"]


@pytest.mark.parametrize("exception_kind", ["diagnostic", "safety fix", "mitigation"])
def test_diagnostic_is_labeled_and_merges_alone_without_replacing_failed_batch(queue, lib, monkeypatch, capsys, exception_kind):
    real_main_state = queue.main_state
    add(queue, "482")
    add_diagnostic(queue, lib, monkeypatch, exception_kind=exception_kind)
    add_diagnostic(queue, lib, monkeypatch, "483", exception_kind=exception_kind)
    original = {"prs": ["480"], "shas": ["bad12345"], "at": 9000}
    queue.update(batch=original)
    queue.listing()
    assert f"{exception_kind} (not a main fix) at aaaaaaaa" in capsys.readouterr().out
    setup(monkeypatch, lib, queue, [{**view(), "headRefOid": DIAGNOSTIC_HEAD}], main_green=False)
    queue.train_tick(now=10_000)
    assert queue.merges == [(lib.CORE, "481")]
    assert queue.load()["batch"] == queue.load()["red_batch"] == original
    assert {i["key"]: i["status"] for i in queue.load()["items"]} == {"482": "queued", "481": "merged", "483": "queued"}
    assert any("not a main fix" in note for note in queue.notes)
    assert not any("batch: 481" in note for note in queue.notes)
    # The diagnostic's own green main run must not erase the previous failure.
    runs_for(monkeypatch, lib, [run(6, "completed", "success", "d1a6"), run(5, "completed", "failure", "bad12345ffff")])
    assert real_main_state() == "red"


@pytest.mark.parametrize("block", ["freeze", "release hold", "cadence", "dependency", "CI OK", "merge-gate", "coverage-floor", "SonarCloud Code Analysis", "CodeQL"])
@pytest.mark.parametrize("exception_kind", ["diagnostic", "safety fix", "mitigation"])
def test_diagnostic_retains_holds_dependencies_cadence_and_check_failures(queue, lib, monkeypatch, home, block, exception_kind):
    add_diagnostic(queue, lib, monkeypatch, exception_kind=exception_kind)
    info = {**view(), "headRefOid": DIAGNOSTIC_HEAD}
    setup(monkeypatch, lib, queue, [info], main_green=False)
    monkeypatch.setattr(lib, "advisory_checks", lambda repo: {"review"})
    if block == "freeze":
        (statedir.root() / "merge-freeze").touch()
    elif block == "release hold":
        monkeypatch.setattr(lib, "merge_frozen", lambda *a: True)
    elif block == "cadence":
        queue.update(last_batch=9900)
    elif block == "dependency":
        queue.update("481", after=["480"])
    else:
        info["statusCheckRollup"] = [check(block, "FAILURE")]
        monkeypatch.setattr(lib, "failing_tests", lambda *a: [])
    queue.train_tick(now=10_000)
    assert not queue.merges


@pytest.mark.parametrize("exception_kind", ["diagnostic", "safety fix", "mitigation"])
def test_diagnostic_changed_head_needs_new_review_and_never_reruns(queue, lib, monkeypatch, exception_kind):
    add_diagnostic(queue, lib, monkeypatch, exception_kind=exception_kind)
    fake = setup(monkeypatch, lib, queue, [{**view(), "headRefOid": "b" * 40}], main_green=False)
    queue.train_tick(now=10_000)
    assert not queue.merges
    assert queue.load()["items"][0]["status"] == "needs work"
    assert "independent review" in queue.load()["items"][0]["detail"]
    assert not any(c[:2] in (("run", "rerun"), ("pr", "update-branch")) for c in fake.calls)


@pytest.mark.parametrize("checks", [[], [check("CI OK", "", "IN_PROGRESS")], [check("merge-gate", "", "IN_PROGRESS")], [check("coverage-floor", "", "IN_PROGRESS")]])
@pytest.mark.parametrize("exception_kind", ["diagnostic", "safety fix", "mitigation"])
def test_diagnostic_does_not_skip_pending_or_empty_checks(queue, lib, monkeypatch, checks, exception_kind):
    add_diagnostic(queue, lib, monkeypatch, exception_kind=exception_kind)
    setup(monkeypatch, lib, queue, [{**view(checks), "headRefOid": DIAGNOSTIC_HEAD}], main_green=False)
    monkeypatch.setattr(lib, "advisory_checks", lambda repo: {"review"})
    queue.train_tick(now=10_000)
    assert not queue.merges and queue.load()["items"][0]["status"] == "queued"


@pytest.mark.parametrize("exception_kind", ["diagnostic", "safety fix", "mitigation"])
def test_actual_red_main_fix_takes_priority_over_diagnostics(queue, lib, monkeypatch, exception_kind):
    add_diagnostic(queue, lib, monkeypatch, exception_kind=exception_kind)
    add(queue, "484", fixes_main=True)
    setup(monkeypatch, lib, queue, [{**view(), "headRefOid": DIAGNOSTIC_HEAD}], main_green=False)
    queue.train_tick(now=10_000)
    assert queue.merges == [(lib.CORE, "484")]


UNKNOWN_MAIN_EVIDENCE = [
    None, [], {}, {"message": "API unavailable"}, "malformed JSON shape", [None],
    [{"status": "completed", "conclusion": "failure"}],
    [run(7, "queued", "", "abc")],
    [run(7, "in_progress", "", "abc")],
    [run(7, "completed", "", "abc")],
    [run(7, "completed", None, "abc")],
    [run(7, "completed", "new_unknown_conclusion", "abc")],
    [run(7, "completed", "cancelled", "abc")],
    [run(7, "completed", "skipped", "abc")],
    [run(7, "completed", "neutral", "abc")],
    [run(7, "completed", "action_required", "abc")],
    [{**run(7, "completed", "failure", "abc"), "headSha": "not-a-sha"}],
    [{**run(7, "completed", "failure", "abc"), "databaseId": None}],
    [{**run(7, "completed", "failure", "abc"), "databaseId": True}],
    [{**run(7, "completed", "failure", "abc"), "status": []}],
    [{**run(7, "completed", "failure", "abc"), "conclusion": {}}],
]


def real_main_evidence(monkeypatch, lib, evidence, merge_sha=""):
    """Stub only remote replies; exercise the real state classification, gate and tick."""
    monkeypatch.setattr(lib, "advisory_checks", lambda repo: {"review"})
    monkeypatch.setattr(lib, "only_never_started_macos_jobs_failed", lambda *a: False)
    monkeypatch.setattr(lib, "ghj", lambda *a, default=None: evidence if a[:2] == ("run", "list") else {**view(), "headRefOid": DIAGNOSTIC_HEAD, "mergeCommit": {"oid": merge_sha}})


@pytest.mark.parametrize("evidence", UNKNOWN_MAIN_EVIDENCE)
@pytest.mark.parametrize("prior_red", [False, True])
@pytest.mark.parametrize("exception_kind", ["diagnostic", "safety fix", "mitigation"])
def test_unknown_main_blocks_diagnostic_without_creating_or_clearing_red_attribution(queue, lib, monkeypatch, evidence, prior_red, exception_kind):
    add_diagnostic(queue, lib, monkeypatch, exception_kind=exception_kind)
    batch = {"prs": ["480"], "shas": ["abc"], "at": 9000}
    previous = batch if prior_red else {}
    queue.update(batch=batch, red_batch=previous)
    real_main_evidence(monkeypatch, lib, evidence)
    assert queue.main_state() == "unknown"
    queue.train_tick(now=10_000)
    assert not queue.merges
    state = queue.load()
    assert state["blocked"] == "main CI evidence is unknown"
    assert state["red_batch"] == previous and state["batch"] == batch
    assert state["last_batch"] == 0 and state["items"][0]["status"] == "queued"


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "startup_failure"])
@pytest.mark.parametrize("exception_kind", ["diagnostic", "safety fix", "mitigation"])
def test_proven_failed_main_allows_only_diagnostic_through_real_state_evaluation(queue, lib, monkeypatch, conclusion, exception_kind):
    add_diagnostic(queue, lib, monkeypatch, exception_kind=exception_kind)
    add(queue, "482")
    batch = {"prs": ["480"], "shas": ["abc"], "at": 9000}
    queue.update(batch=batch)
    real_main_evidence(monkeypatch, lib, [run(7, "completed", conclusion, "abc")])
    assert queue.main_state() == "red"
    queue.train_tick(now=10_000)
    assert queue.merges == [(lib.CORE, "481")]
    assert queue.load()["batch"] == queue.load()["red_batch"] == batch
    assert queue.load()["items"][1]["status"] == "queued"


def test_a_batch_run_cancelled_by_a_newer_push_is_superseded_by_the_newer_green_run(queue, lib, monkeypatch):
    """Concurrency cancel-in-progress: the batch's own run is cancelled once a newer push's run starts; a newer
    COMPLETED green run tested its commits, so the cancellation is no evidence either way."""
    queue.update(batch={"prs": ["480"], "shas": ["abc"], "at": 9000})
    real_main_evidence(monkeypatch, lib, [run(8, "completed", "success", "def"), run(7, "completed", "cancelled", "abc")])
    assert queue.main_state() == "green"


@pytest.mark.parametrize("newer", [run(8, "in_progress", "", "def"), run(8, "completed", "failure", "def"),
                                   run(8, "completed", "cancelled", "def")])
def test_a_cancelled_batch_run_stays_unknown_without_a_newer_completed_green_run(queue, lib, monkeypatch, newer):
    queue.update(batch={"prs": ["480"], "shas": ["abc"], "at": 9000})
    real_main_evidence(monkeypatch, lib, [newer, run(7, "completed", "cancelled", "abc")])
    assert queue.main_state() in {"unknown", "red"}
    assert queue.main_state() != "green"


def test_newer_pending_main_keeps_proven_failed_completed_run(queue, lib, monkeypatch):
    real_main_evidence(monkeypatch, lib, [run(8, "in_progress", "", "def"), run(7, "completed", "failure", "abc")])
    assert queue.main_state() == "red"


def test_unknown_core_main_does_not_hold_other_repos_or_attribute_red_batch(queue, lib, monkeypatch):
    add_diagnostic(queue, lib, monkeypatch)
    add(queue, "plugins 38")
    queue.update(batch={"prs": ["480"], "shas": ["abc"], "at": 9000})
    real_main_evidence(monkeypatch, lib, None)
    queue.train_tick(now=10_000)
    assert queue.merges == [(lib.PLUGINS, "38")]
    assert not queue.load()["red_batch"]


def pin_failed_main(queue, lib, monkeypatch):
    failed = run(5, "completed", "failure", "bad12345")
    batch = {"prs": ["480"], "shas": ["bad12345"], "at": 9000}
    queue.update(batch=batch, red_batch=batch)
    real_main_evidence(monkeypatch, lib, [failed])
    assert queue.main_state() == "red"
    assert queue.load()["main_failure"] == failed
    return failed, batch


def ten_new_green_runs():
    return [run(i, "completed", "success", f"{i:040x}") for i in range(110, 100, -1)]


@pytest.mark.parametrize("exception_kind", ["diagnostic", "safety fix", "mitigation"])
def test_diagnostic_does_not_let_failed_batch_age_out_of_ten_run_window(queue, lib, monkeypatch, exception_kind):
    add_diagnostic(queue, lib, monkeypatch, exception_kind=exception_kind)
    failed, batch = pin_failed_main(queue, lib, monkeypatch)
    queue.train_tick(now=10_000)
    assert queue.merges == [(lib.CORE, "481")]
    add(queue, "482")
    recent = ten_new_green_runs()
    assert len(recent) == 10 and all(r["databaseId"] != failed["databaseId"] for r in recent)
    real_main_evidence(monkeypatch, lib, recent)
    assert queue.main_state() == "red"
    queue.train_tick(now=10_400)
    assert queue.merges == [(lib.CORE, "481")]
    assert queue.load()["main_failure"] == failed
    assert queue.load()["batch"] == queue.load()["red_batch"] == batch


def test_successful_rerun_of_pinned_failed_commit_does_not_clear_it(queue, lib, monkeypatch):
    failed, _ = pin_failed_main(queue, lib, monkeypatch)
    real_main_evidence(monkeypatch, lib, [{**failed, "conclusion": "success"}])
    assert queue.main_state() == "red"
    assert queue.load()["main_failure"] == failed


@pytest.mark.parametrize("exception_kind", ["diagnostic", "safety fix", "mitigation"])
def test_unknown_history_keeps_pinned_failure_without_authorizing_diagnostics(queue, lib, monkeypatch, exception_kind):
    add_diagnostic(queue, lib, monkeypatch, exception_kind=exception_kind)
    failed, batch = pin_failed_main(queue, lib, monkeypatch)
    real_main_evidence(monkeypatch, lib, None)
    queue.train_tick(now=10_000)
    assert not queue.merges
    assert queue.load()["main_failure"] == failed and queue.load()["red_batch"] == batch
    assert queue.load()["blocked"] == "main CI evidence is unknown"


def test_non_core_only_batch_preserves_failed_main_pin_and_attribution(queue, lib, monkeypatch):
    failed, batch = pin_failed_main(queue, lib, monkeypatch)
    add(queue, "plugins 38")
    queue.train_tick(now=10_000)
    assert queue.merges == [(lib.PLUGINS, "38")]
    assert queue.load()["main_failure"] == failed and queue.load()["red_batch"] == batch


def test_only_merged_fix_and_its_successful_main_ci_can_supersede_failure(queue, lib, monkeypatch):
    failed, batch = pin_failed_main(queue, lib, monkeypatch)
    add(queue, "484", fixes_main=True)
    add(queue, "482")
    fix_sha = "f1a12345".ljust(40, "0")
    real_main_evidence(monkeypatch, lib, [failed], merge_sha=fix_sha)
    queue.train_tick(now=10_000)
    assert queue.merges == [(lib.CORE, "484")]
    assert queue.load()["main_failure"] == {**failed, "fixed_by": [fix_sha[:8]]}
    # Neither unrelated successful history nor a pending fix run proves the fix.
    real_main_evidence(monkeypatch, lib, [run(111, "in_progress", "", fix_sha), *ten_new_green_runs()[:9]])
    assert queue.main_state() == "red"
    assert queue.load()["red_batch"] == batch
    real_main_evidence(monkeypatch, lib, [run(111, "completed", "success", fix_sha), *ten_new_green_runs()[:9]])
    assert queue.main_state() == "green"
    assert not queue.load()["main_failure"]
    queue.train_tick(now=10_400)
    assert queue.merges == [(lib.CORE, "484"), (lib.CORE, "482")]
    assert not queue.load()["red_batch"]


def test_failed_fix_main_ci_replaces_pin_with_new_failure(queue, lib, monkeypatch):
    failed, _ = pin_failed_main(queue, lib, monkeypatch)
    add(queue, "484", fixes_main=True)
    fix_sha = "f1a12345".ljust(40, "0")
    real_main_evidence(monkeypatch, lib, [failed], merge_sha=fix_sha)
    queue.train_tick(now=10_000)
    fix_failure = run(111, "completed", "failure", fix_sha)
    real_main_evidence(monkeypatch, lib, [fix_failure, *ten_new_green_runs()[:9]])
    assert queue.main_state() == "red"
    assert queue.load()["main_failure"] == fix_failure
    real_main_evidence(monkeypatch, lib, ten_new_green_runs())
    assert queue.main_state() == "red"


def test_unsuccessful_fix_merge_does_not_mark_failure_as_superseded(queue, lib, monkeypatch):
    failed, _ = pin_failed_main(queue, lib, monkeypatch)
    add(queue, "484", fixes_main=True)
    merge = lib.load("hz-merge-train")
    monkeypatch.setattr(merge, "train", lambda *a, **kw: (False, "merge rejected"))
    queue.train_tick(now=10_000)
    assert queue.load()["main_failure"] == failed
    real_main_evidence(monkeypatch, lib, ten_new_green_runs())
    assert queue.main_state() == "red"


def test_fix_merge_without_known_squash_commit_cannot_supersede_pin(queue, lib, monkeypatch):
    failed, _ = pin_failed_main(queue, lib, monkeypatch)
    add(queue, "484", fixes_main=True)
    queue.train_tick(now=10_000)  # remote has no mergeCommit; the train's PR head is not proof of a main commit
    assert queue.merges == [(lib.CORE, "484")]
    assert queue.load()["main_failure"] == failed
    real_main_evidence(monkeypatch, lib, ten_new_green_runs())
    assert queue.main_state() == "red"


def test_new_failure_after_fix_stays_pinned_even_if_fix_itself_was_green(queue, lib, monkeypatch):
    failed, _ = pin_failed_main(queue, lib, monkeypatch)
    fix_sha = "f1a12345".ljust(40, "0")
    add(queue, "484", fixes_main=True)
    real_main_evidence(monkeypatch, lib, [failed], merge_sha=fix_sha)
    queue.train_tick(now=10_000)
    fresh_failure = run(113, "completed", "failure", "facade11")
    fixed = run(111, "completed", "success", fix_sha)
    real_main_evidence(monkeypatch, lib, [fresh_failure, fixed, *ten_new_green_runs()[:8]])
    assert queue.main_state() == "red"
    assert queue.load()["main_failure"] == fresh_failure
    real_main_evidence(monkeypatch, lib, [fixed, *ten_new_green_runs()[:9]])
    assert queue.main_state() == "red"
    assert queue.load()["main_failure"] == fresh_failure


def test_add_preserves_failure_pinned_during_remote_diff_read(queue, monkeypatch):
    failed = run(5, "completed", "failure", "bad12345")

    def guard(repo, pr):
        queue.update(main_failure=failed)
        return [], "PASS"

    monkeypatch.setattr(queue, "guard_findings", guard)
    add(queue, "482")
    assert queue.load()["main_failure"] == failed


def test_legacy_red_batch_without_pinned_run_fails_closed_when_failure_aged_out(queue, lib, monkeypatch):
    add(queue, "482")
    batch = {"prs": ["480"], "shas": ["bad12345"], "at": 9000}
    queue.update(batch=batch, red_batch=batch)
    real_main_evidence(monkeypatch, lib, ten_new_green_runs())
    assert queue.main_state() == "unknown"
    queue.train_tick(now=10_000)
    assert not queue.merges
    assert queue.load()["batch"] == queue.load()["red_batch"] == batch


@pytest.mark.parametrize("other", [["--fixes-main"], ["--diagnostics-main", "metrics"]])
def test_safety_fix_cannot_claim_another_exception(queue, other):
    with pytest.raises(SystemExit):
        queue.main(["add", "--safety-fix-main", "isolation defect", *other, "484"])
    assert not queue.load()["items"]


@pytest.mark.parametrize("head", ["short", "a" * 39, "a" * 41, "g" * 40])
def test_safety_fix_requires_full_reviewed_sha(queue, head):
    assert queue.main(["add", "--safety-fix-main", "isolation defect", "--safety-head", head, "--reviewed", "NO weakening", "484"]) == 1
    assert not queue.load()["items"]


def test_safety_fix_takes_priority_over_diagnostics_and_is_head_guarded(queue, lib, monkeypatch):
    add_diagnostic(queue, lib, monkeypatch)
    add_diagnostic(queue, lib, monkeypatch, "484", exception_kind="safety fix")
    seen = []
    merge = lib.load("hz-merge-train")

    def reject_changed_head(repo, pr, **kwargs):
        assert callable(kwargs.pop("pre_merge_guard"))  # bead q9kh: every core merge carries the main plan check
        seen.append((repo, pr, kwargs))
        return False, "head changed between assessment and merge"

    monkeypatch.setattr(merge, "train", reject_changed_head)
    setup(monkeypatch, lib, queue, [{**view(), "headRefOid": DIAGNOSTIC_HEAD}], main_green=False)
    queue.train_tick(now=10_000)
    assert seen == [(lib.CORE, "484", {"queue_core": True, "expected_head": DIAGNOSTIC_HEAD})]
    assert not queue.merges
    assert {i["key"]: i["status"] for i in queue.load()["items"]} == {"481": "queued", "484": "needs work"}


@pytest.mark.parametrize("exception_kind", ["safety fix", "mitigation"])
def test_green_safety_main_run_keeps_failure_pin_and_cannot_authorize_normal_merges(queue, lib, monkeypatch, exception_kind):
    add_diagnostic(queue, lib, monkeypatch, "484", exception_kind=exception_kind)
    failed, batch = pin_failed_main(queue, lib, monkeypatch)
    queue.train_tick(now=10_000)
    assert queue.merges == [(lib.CORE, "484")]
    assert queue.load()["main_failure"] == failed
    assert queue.load()["batch"] == queue.load()["red_batch"] == batch
    assert not queue.load()["main_failure"].get("fixed_by")
    add(queue, "482")
    real_main_evidence(monkeypatch, lib, [run(6, "completed", "success", "f1a12345"), failed])
    queue.train_tick(now=10_400)
    assert queue.merges == [(lib.CORE, "484")]
    assert queue.load()["main_failure"] == failed
    assert queue.load()["red_batch"] == batch


@pytest.mark.parametrize("evidence", [None, "", "   "])
def test_mitigation_requires_nonempty_measured_evidence(queue, evidence):
    args = ["add", "--mitigation-main", "reduce setup overhead", "--mitigation-head", DIAGNOSTIC_HEAD, "--reviewed", "NO weakening"]
    if evidence is not None:
        args += ["--mitigation-evidence", evidence]
    assert queue.main([*args, "486"]) == 1
    assert not queue.load()["items"]


@pytest.mark.parametrize("extra", [[], ["--diagnostics-main", "metrics", "--diagnostic-head", DIAGNOSTIC_HEAD], ["--safety-fix-main", "isolation", "--safety-head", DIAGNOSTIC_HEAD]])
def test_mitigation_evidence_requires_mitigation_purpose(queue, extra):
    assert queue.main(["add", *extra, "--mitigation-evidence", MITIGATION_EVIDENCE, "--reviewed", "NO weakening", "486"]) == 1
    assert not queue.load()["items"]


@pytest.mark.parametrize("head", ["short", "a" * 39, "a" * 41, "g" * 40])
def test_mitigation_requires_full_reviewed_sha(queue, head):
    assert queue.main(["add", "--mitigation-main", "setup overhead", "--mitigation-head", head, "--mitigation-evidence", MITIGATION_EVIDENCE, "--reviewed", "NO weakening", "486"]) == 1
    assert not queue.load()["items"]


@pytest.mark.parametrize("other", [["--fixes-main"], ["--diagnostics-main", "metrics"], ["--safety-fix-main", "isolation"]])
def test_mitigation_cannot_claim_a_fix_or_another_exception(queue, other):
    with pytest.raises(SystemExit):
        queue.main(["add", "--mitigation-main", "setup overhead", *other, "486"])
    assert not queue.load()["items"]


def test_mitigation_records_measurement_and_unresolved_cause_honestly(queue, lib, monkeypatch, capsys):
    add_diagnostic(queue, lib, monkeypatch, "486", exception_kind="mitigation")
    item = queue.load()["items"][0]
    assert not item["fixes_main"] and not item["diagnostics_main"] and not item["safety_fix_main"]
    assert item["mitigation_evidence"] == MITIGATION_EVIDENCE
    assert item["reviewed_head"] == DIAGNOSTIC_HEAD
    assert any(MITIGATION_EVIDENCE in n and "main failure remains unresolved" in n for n in queue.notes)
    queue.listing()
    out = capsys.readouterr().out
    assert "mitigation (not a main fix)" in out and "cause unresolved" in out and MITIGATION_EVIDENCE in out


def test_safety_then_mitigation_then_diagnostic_each_goes_alone(queue, lib, monkeypatch):
    add_diagnostic(queue, lib, monkeypatch, "481")
    add_diagnostic(queue, lib, monkeypatch, "486", exception_kind="mitigation")
    add_diagnostic(queue, lib, monkeypatch, "484", exception_kind="safety fix")
    setup(monkeypatch, lib, queue, [{**view(), "headRefOid": DIAGNOSTIC_HEAD}], main_green=False)
    queue.train_tick(now=10_000)
    assert queue.merges == [(lib.CORE, "484")]
    queue.train_tick(now=10_300)
    assert queue.merges == [(lib.CORE, "484"), (lib.CORE, "486")]
    queue.train_tick(now=10_600)
    assert queue.merges == [(lib.CORE, "484"), (lib.CORE, "486"), (lib.CORE, "481")]

@pytest.mark.parametrize('conclusion,attempt,relationship,expected', [
    ('success', 1, 'ahead', True), ('failure', 1, 'ahead', False),
    ('success', 2, 'ahead', False), ('success', 1, 'diverged', False),
    ('success', 1, 'identical', False), ('success', 1, None, False),
])
def test_later_main_first_attempt_descendant(tool, lib, monkeypatch, conclusion, attempt, relationship, expected):
    q = tool('hz-queue')
    old, new = 'a' * 40, 'b' * 40
    monkeypatch.setattr(q, 'load', lambda: {'main_failure': {'headSha': old}})
    def response(*args, **kwargs):
        if args[0] == 'run':
            return [{'status': 'completed', 'conclusion': conclusion, 'databaseId': 123, 'headSha': new, 'displayTitle': 'Legacy full CI', 'attempt': attempt}]
        if '/actions/runs/' in args[1] and '/jobs?' not in args[1]:
            return {'run_attempt': attempt, 'head_sha': new, 'conclusion': conclusion, 'status': 'completed'}
        if '/jobs?' in args[1]:
            return recovery_evidence()['jobs']
        return {'status': relationship}
    monkeypatch.setattr(lib, 'ghj', response)
    assert q.later_main_passes() is expected


@pytest.fixture(autouse=True)
def main_policy(monkeypatch):
    """Core's release policy at main: its Test cells are those recovery_evidence runs (read by full_pass)."""
    import policy_kit as kit

    cells = frozenset(job["name"] for job in recovery_evidence()["jobs"]["jobs"]) - {"CI OK"}
    monkeypatch.setattr(core_policy, "at_commit_api", lambda get, sha: kit.policy(commit=sha, test_jobs=cells))


def recovery_evidence():
    names = ["CI OK"] + [f"Test (ubuntu-24.04, Python {v})" for v in ("3.12", "3.13", "3.14", "3.15")]
    names += [f"Test (macos-latest, Python {v}, {level})" for v in ("3.12", "3.13", "3.14", "3.15") for level in ("unit", "integration")]
    old, new = "a" * 40, "b" * 40
    jobs = [{"name": n, "status": "completed", "conclusion": "success"} for n in names]
    return {
        "pin": {"status": "completed", "conclusion": "failure", "databaseId": 100, "headSha": old},
        "runs": [{"status": "completed", "conclusion": "success", "databaseId": 123, "headSha": new, "displayTitle": "Legacy full CI", "attempt": 1}],
        "run": {"status": "completed", "conclusion": "success", "head_sha": new, "run_attempt": 1},
        "jobs": {"total_count": len(jobs), "jobs": jobs},
        "compare": {"status": "ahead"},
    }


def install_recovery_evidence(q, lib, monkeypatch, evidence):
    monkeypatch.setattr(q, "load", lambda: {"main_failure": evidence["pin"]})
    def response(*args, **kwargs):
        if args[0] == "run":
            return evidence["runs"]
        if "/jobs" in args[1]:
            return evidence["jobs"]
        if "/actions/runs/" in args[1]:
            return evidence["run"]
        return evidence["compare"]
    monkeypatch.setattr(lib, "ghj", response)


@pytest.mark.parametrize("case", [
    "run_not_dict", "boolean_attempt", "float_attempt", "run_in_progress", "sha_mismatch",
    "jobs_not_dict", "jobs_not_list", "job_not_dict", "job_name_not_string", "boolean_count",
    "truncated_jobs", "hidden_failed_job", "missing_linux", "skipped_linux", "neutral_linux",
    "missing_macos_cell", "skipped_macos_cell", "missing_ci_ok", "compare_not_dict",
    "newest_completed_red", "newest_completed_cancelled", "malformed_run_list",
])
def test_recovery_rejects_incomplete_or_malformed_evidence(tool, lib, monkeypatch, case):
    q, e = tool("hz-queue"), recovery_evidence()
    if case == "run_not_dict": e["run"] = ["invalid"]
    elif case == "boolean_attempt": e["run"]["run_attempt"] = True
    elif case == "float_attempt": e["run"]["run_attempt"] = 1.0
    elif case == "run_in_progress": e["run"]["status"] = "in_progress"
    elif case == "sha_mismatch": e["run"]["head_sha"] = "c" * 40
    elif case == "jobs_not_dict": e["jobs"] = ["invalid"]
    elif case == "jobs_not_list": e["jobs"]["jobs"] = {"unexpected": "mapping"}
    elif case == "job_not_dict": e["jobs"]["jobs"][0] = "invalid"
    elif case == "job_name_not_string": e["jobs"]["jobs"][0]["name"] = None
    elif case == "boolean_count": e["jobs"]["total_count"] = True
    elif case == "truncated_jobs": e["jobs"]["total_count"] += 1
    elif case == "hidden_failed_job": e["jobs"]["jobs"][-1]["conclusion"] = "failure"
    elif case in ("missing_linux", "missing_macos_cell", "missing_ci_ok"):
        index = {"missing_linux": 1, "missing_macos_cell": -1, "missing_ci_ok": 0}[case]
        e["jobs"]["jobs"].pop(index)
        e["jobs"]["total_count"] -= 1
    elif case in ("skipped_linux", "neutral_linux", "skipped_macos_cell"):
        index = -1 if case == "skipped_macos_cell" else 1
        e["jobs"]["jobs"][index]["conclusion"] = "neutral" if case == "neutral_linux" else "skipped"
    elif case == "compare_not_dict": e["compare"] = ["ahead"]
    elif case.startswith("newest_completed_"):
        conclusion = "failure" if case.endswith("red") else "cancelled"
        e["runs"].insert(0, {"status": "completed", "conclusion": conclusion, "databaseId": 124, "headSha": "c" * 40})
    elif case == "malformed_run_list": e["runs"].append({"status": "completed"})
    install_recovery_evidence(q, lib, monkeypatch, e)
    assert q.later_main_passes() is False


def test_recovery_accepts_full_first_attempt_with_pending_newer_run(tool, lib, monkeypatch):
    q, e = tool("hz-queue"), recovery_evidence()
    e["runs"].insert(0, {"status": "in_progress", "conclusion": "", "databaseId": 124, "headSha": "c" * 40, "displayTitle": "Legacy full CI", "attempt": 1})
    install_recovery_evidence(q, lib, monkeypatch, e)
    assert q.later_main_passes() is True


@pytest.mark.parametrize("responses,first_failure,reason,restored", [
    (("full",), None, None, True),
    (("gh: Bad Gateway (HTTP 502)", "full"), "HTTP 502", None, True),
    (("gh: HTTP 503", "full"), "HTTP 503", None, True),
    (("timeout", "full"), "command timeout", None, True),
    (("transport: read tcp 1:2->3:4: read: connection reset by peer", "full"), "connection reset", None, True),
    (("transport: net/http: TLS handshake timeout", "full"), "TLS handshake timeout", None, True),
    (("transport: net/http: timeout awaiting response headers", "full"), "response headers timeout", None, True),
    (("gh: secret-marker (HTTP 504)", "timeout"), "HTTP 504", "command timeout", False),
    (("gh: secret-marker (HTTP 504)", "partial"), "HTTP 504", "incomplete admission jobs", False),
    (("gh: secret-marker (HTTP 403)",), None, "HTTP 403", False),
    (("gh: unauthorized (HTTP 401)",), None, "HTTP 401", False),
    (("gh: rate limited (HTTP 429)",), None, "HTTP 429", False),
    (("gh: unknown secret-marker",), None, "unclassified CLI error", False),
    (("gh: misleading (HTTP 504)\ngh: forbidden (HTTP 403)",), None, "unclassified CLI error", False),
    (("partial",), None, "incomplete admission jobs", False),
    (("malformed",), None, "malformed admission job", False),
    (("invalid_json",), None, "invalid JSON", False),
])
def test_recovery_admission_read_is_bounded_and_fail_closed(tool, lib, monkeypatch, capsys,
                                                            responses, first_failure, reason, restored):
    from steuermaschine_heinzel.native.queue_lib import main_batch

    q, e = tool("hz-queue"), recovery_evidence()
    run_id = 42504612404
    old = "c" * 40
    e["runs"].append({"status": "completed", "conclusion": "success", "databaseId": run_id,
                      "headSha": old, "displayTitle": main_batch.PREFIX + old, "attempt": 2})
    install_recovery_evidence(q, lib, monkeypatch, e)
    jobs = [{"name": "CI OK", "status": "completed", "conclusion": "success"}]
    jobs += [{"name": f"Other job {n}", "status": "completed", "conclusion": "success"} for n in range(70)]
    path = f"repos/{CORE}/actions/runs/{run_id}/attempts/2/jobs?per_page=100"
    results = iter(responses)
    calls = []

    def read(*args, timeout):
        assert args == ("api", "--hostname", "github.com", "--method", "GET", path)
        assert timeout == 15
        calls.append(args)
        reply = next(results)
        if reply == "timeout":
            raise subprocess.TimeoutExpired(args, timeout, stderr="secret-marker")
        if reply.startswith("transport: "):
            reply = f'Get "https://api.github.com/{path}": {reply.removeprefix("transport: ")}'
        if reply == "invalid_json":
            return subprocess.CompletedProcess(args, 0, "secret-marker", "")
        if reply not in ("full", "partial", "malformed"):
            # Failed requests can contain apparent usable JSON. The exit status still controls trust.
            return subprocess.CompletedProcess(args, 1, json.dumps({"total_count": 71, "jobs": jobs}), reply)
        rows = jobs if reply == "full" else (jobs[:-1] if reply == "partial" else [None, *jobs[1:]])
        return subprocess.CompletedProcess(args, 0, json.dumps({"total_count": 71, "jobs": rows}), "")

    monkeypatch.setattr(lib, "_gh_process", read)
    assert q.later_main_passes() is restored
    assert len(calls) == len(responses)
    logs = capsys.readouterr().err
    assert "secret-marker" not in logs
    start, end = map(json.loads, logs.splitlines())
    assert start == {"event": "queue.restoration_admission.start", "run": run_id, "attempt": 2}
    assert end == {"event": "queue.restoration_admission.outcome", "run": run_id, "attempt": 2,
                   "requests": len(responses), "outcome": "ok" if restored else "unavailable",
                   "first_failure": first_failure, "reason": reason}


def test_unknown_main_cannot_use_recovery_gate(queue, lib, monkeypatch):
    monkeypatch.setattr(queue, "main_state", lambda: "unknown")
    monkeypatch.setattr(queue, "later_main_passes", lambda: pytest.fail("unknown main must not invoke recovery"))
    assert queue.gate([{"repo": lib.CORE}], 10_000, {"last_batch": 0}) == "main CI evidence is unknown"


def test_recovery_merges_ordinary_pr_without_erasing_incident_then_holds_new_red(queue, lib, monkeypatch):
    add(queue, "393", "394")
    setup(monkeypatch, lib, queue, [view()])
    pin = recovery_evidence()["pin"]
    original = {"prs": ["380"], "shas": ["a" * 8], "at": 1}
    queue.update(main_failure=pin, red_batch=original, batch=original)
    monkeypatch.setattr(queue, "main_state", lambda: "red")
    recovered = {"value": True}
    monkeypatch.setattr(queue, "later_main_passes", lambda: recovered["value"])
    monkeypatch.setattr(queue, "core_batch", lambda ready: ready[:1])
    queue.train_tick(now=10_000)
    assert queue.merges == [(lib.CORE, "393")]
    assert queue.load()["main_failure"] == pin
    assert queue.load()["red_batch"] == original
    assert queue.load()["batch"]["prs"] == ["393"]
    recovered["value"] = False
    queue.train_tick(now=10_300)
    assert queue.merges == [(lib.CORE, "393")]
    assert queue.load()["main_failure"] == pin
    assert queue.load()["red_batch"] == original
    assert queue.load()["blocked"] == "main's latest run is not green"


@pytest.mark.parametrize("numbers", [("393",), ("393", "394")])
def test_only_a_planned_multi_core_batch_publishes_intent(queue, lib, monkeypatch, numbers):
    monkeypatch.setenv("HZ_MAIN_BATCH_PUBLISH", "1")
    add(queue, *numbers)
    setup(monkeypatch, lib, queue, [view()])
    intentions = []
    monkeypatch.setattr(queue, "publish_intent", lambda base, item: intentions.append(item["pr"]) or f"{int(item['pr']):040x}")
    monkeypatch.setattr(queue, "publish_batch", lambda base, members: True)
    monkeypatch.setattr(queue, "close_intents", lambda *args: pytest.fail("a proved batch was closed"))
    queue.train_tick(now=10_000)
    assert intentions == (list(numbers) if len(numbers) > 1 else [])


def test_partial_batch_closes_its_surviving_pending_intent(queue, lib, monkeypatch):
    monkeypatch.setenv("HZ_MAIN_BATCH_PUBLISH", "1")
    add(queue, "393", "394")
    setup(monkeypatch, lib, queue, [view()])
    train = lib.load("hz-merge-train")
    monkeypatch.setattr(train, "train", lambda repo, pr, **kwargs: (pr == "393", "abc12345"))
    monkeypatch.setattr(queue, "publish_intent", lambda base, item: "b" * 40)
    monkeypatch.setattr(queue, "publish_batch", lambda *args: pytest.fail("a single survivor received a batch proof"))
    closures = []
    monkeypatch.setattr(queue, "close_intents", lambda base, commits: closures.append(commits))
    queue.train_tick(now=10_000)
    assert closures == [["b" * 40]]


PROGRESS_ANCHOR = {"databaseId": 777, "headSha": "c" * 40, "conclusion": "failure"}


def add_progress(queue, lib, monkeypatch, number="490"):
    monkeypatch.setattr(queue, "completed_main_anchor", lambda: PROGRESS_ANCHOR.copy())
    monkeypatch.setattr(queue, "progress_anchor_unchanged", lambda expected: expected == queue.completed_main_anchor())
    monkeypatch.setattr(lib, "ghj", lambda *a, default=None: {"headRefOid": DIAGNOSTIC_HEAD})
    assert queue.main(["add", "--reviewed-progress-main", "Owner approved fast-track",
        "--progress-head", DIAGNOSTIC_HEAD, "--reviewed", "independent exact-head review", number]) == 0


@pytest.mark.parametrize("extra", [[], ["--progress-head", "short"], ["--progress-head", DIAGNOSTIC_HEAD]])
def test_progress_requires_exact_head_and_independent_review(queue, extra):
    assert queue.main(["add", "--reviewed-progress-main", "owner approved", *extra, "490"]) == 1
    assert not queue.load()["items"]


def test_progress_requires_known_main_anchor(queue, monkeypatch):
    monkeypatch.setattr(queue, "completed_main_anchor", lambda: {})
    assert queue.main(["add", "--reviewed-progress-main", "owner approved", "--progress-head", DIAGNOSTIC_HEAD,
        "--reviewed", "independent review", "490"]) == 1
    assert not queue.load()["items"]


def test_progress_merges_max_five_without_erasing_failure_attribution(queue, lib, monkeypatch):
    for number in range(490, 496):
        add_progress(queue, lib, monkeypatch, str(number))
    failed = run(777, "completed", "failure", "c" * 40)
    batch = {"prs": ["480"], "shas": ["bad12345"], "at": 9000}
    queue.update(main_failure=failed, batch=batch, red_batch=batch)
    setup(monkeypatch, lib, queue, [{**view(), "headRefOid": DIAGNOSTIC_HEAD}], main_green=False)
    queue.train_tick(now=10_000)
    assert len(queue.merges) == 5
    assert queue.load()["main_failure"] == failed
    assert queue.load()["batch"] == queue.load()["red_batch"] == batch
    assert not queue.load()["main_failure"].get("fixed_by")
    assert len(queue.load()["progress_history"]) == 5
    assert {item["status"] for item in queue.load()["items"]} == {"merged", "queued"}


@pytest.mark.parametrize("block", ["freeze", "cadence", "dependency", "CI OK", "merge-gate", "coverage-floor", "missing CI OK", "changed head", "changed main", "unknown main"] )
def test_progress_preserves_required_boundaries(queue, lib, monkeypatch, home, block):
    add_progress(queue, lib, monkeypatch)
    info = {**view(), "headRefOid": DIAGNOSTIC_HEAD}
    setup(monkeypatch, lib, queue, [info], main_green=False)
    if block == "freeze":
        (statedir.root() / "merge-freeze").touch()
    elif block == "cadence":
        queue.update(last_batch=9900)
    elif block == "dependency":
        queue.update("490", after=["480"])
    elif block == "changed head":
        info["headRefOid"] = "b" * 40
    elif block == "changed main":
        monkeypatch.setattr(queue, "completed_main_anchor", lambda: PROGRESS_ANCHOR | {"databaseId": 778})
    elif block == "unknown main":
        monkeypatch.setattr(queue, "main_state", lambda: "unknown")
    elif block == "missing CI OK":
        info["statusCheckRollup"] = [check("Lint")]
    else:
        info["statusCheckRollup"] = [check("CI OK", "", "IN_PROGRESS") if block == "CI OK" else check("CI OK"),
                                    check(block, "FAILURE") if block != "CI OK" else check("Lint")]
        monkeypatch.setattr(lib, "failing_tests", lambda *a: [])
    queue.train_tick(now=10_000)
    assert not queue.merges


def test_progress_rechecks_main_between_merges(queue, lib, monkeypatch):
    add_progress(queue, lib, monkeypatch, "490")
    add_progress(queue, lib, monkeypatch, "491")
    setup(monkeypatch, lib, queue, [{**view(), "headRefOid": DIAGNOSTIC_HEAD}], main_green=False)
    merge = lib.load("hz-merge-train")
    original = merge.train
    def change_main(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(queue, "completed_main_anchor", lambda: PROGRESS_ANCHOR | {"databaseId": 778})
        return result
    monkeypatch.setattr(merge, "train", change_main)
    queue.train_tick(now=10_000)
    assert queue.merges == [(lib.CORE, "490")]
    assert queue.load()["items"][1]["status"] == "needs work"


@pytest.mark.parametrize("rows", [None, [], {}, [None], [{"status": "completed"}]])
def test_progress_anchor_fails_closed_on_unknown_evidence(tool, lib, monkeypatch, rows):
    q = tool("hz-queue")
    monkeypatch.setattr(lib, "ghj", lambda *a, **kw: rows)
    assert q.completed_main_anchor() == {}


def test_progress_detects_older_pending_failure_behind_unchanged_newest_completion(tool, lib, monkeypatch):
    q = tool("hz-queue")
    newer = run(900, "completed", "failure", "a" * 40)
    older = run(899, "in_progress", "", "b" * 40)
    rows = [newer, older]
    monkeypatch.setattr(q, "main_observations", lambda: rows)
    anchor = q.completed_main_anchor()
    assert q.progress_anchor_unchanged(anchor)
    rows[1] = run(899, "completed", "failure", "b" * 40)
    assert not q.progress_anchor_unchanged(anchor)


def test_progress_watches_pending_run_after_it_leaves_top_ten(tool, lib, monkeypatch):
    q = tool("hz-queue")
    newer = run(900, "completed", "failure", "a" * 40)
    older = run(899, "in_progress", "", "b" * 40)
    monkeypatch.setattr(q, "main_observations", lambda: [newer, older])
    anchor = q.completed_main_anchor()
    monkeypatch.setattr(q, "main_observations", lambda: [run(901, "queued", "", "c" * 40), newer])
    monkeypatch.setattr(lib, "ghj", lambda *a, **kw: older)
    assert q.progress_anchor_unchanged(anchor)
    monkeypatch.setattr(lib, "ghj", lambda *a, **kw: run(899, "completed", "failure", "b" * 40))
    assert not q.progress_anchor_unchanged(anchor)


def test_merge_train_final_guard_runs_after_freeze_wait(tool, lib, monkeypatch):
    train = tool("hz-merge-train")
    frozen = [True]
    failed = [False]
    calls = []
    info = {"state": "OPEN", "isDraft": False, "headRefOid": DIAGNOSTIC_HEAD,
            "mergeStateStatus": "CLEAN", "statusCheckRollup": [check("CI OK")]}
    monkeypatch.setattr(train, "view", lambda *a: info)
    monkeypatch.setattr(lib, "advisory_checks", lambda *a: {"review"})
    monkeypatch.setattr(lib, "tool_required_checks", lambda *a: {"CI OK"})
    monkeypatch.setattr(lib, "merge_frozen", lambda *a: frozen[0])
    def wake(_):
        frozen[0] = False
        failed[0] = True
    monkeypatch.setattr(train.time, "sleep", wake)
    monkeypatch.setattr(train, "gh", lambda *args: (calls.append(args) or (0, "")))
    result = train.train(lib.CORE, "490", queue_core=True, expected_head=DIAGNOSTIC_HEAD,
                         pre_merge_guard=lambda: not failed[0])
    assert result[0] is False
    assert not any(call[:2] == ("pr", "merge") for call in calls)


def test_progress_refuses_disjoint_observation_window(tool, lib, monkeypatch):
    q = tool("hz-queue")
    monkeypatch.setattr(q, "main_observations", lambda: [run(900, "completed", "failure", "a" * 40)])
    anchor = q.completed_main_anchor()
    monkeypatch.setattr(q, "main_observations", lambda: [run(1000+i, "queued", "", "b" * 40) for i in range(10)])
    assert not q.progress_anchor_unchanged(anchor)


def test_progress_refuses_wrong_id_from_individual_watched_lookup(tool, lib, monkeypatch):
    q = tool("hz-queue")
    newest = run(900, "completed", "failure", "a" * 40)
    older = run(899, "in_progress", "", "b" * 40)
    monkeypatch.setattr(q, "main_observations", lambda: [newest, older])
    anchor = q.completed_main_anchor()
    monkeypatch.setattr(q, "main_observations", lambda: [newest])
    monkeypatch.setattr(lib, "ghj", lambda *a, **kw: run(898, "in_progress", "", "b" * 40))
    assert not q.progress_anchor_unchanged(anchor)


def test_progress_batch_publishes_in_merge_order_without_replacing_incidents(queue, lib, monkeypatch):
    monkeypatch.setenv("HZ_MAIN_BATCH_PUBLISH", "1")
    for number in ("490", "491", "492"):
        add_progress(queue, lib, monkeypatch, number)
    failed = run(777, "completed", "failure", "c" * 40)
    old_batch = {"prs": ["480"], "shas": ["bad12345"], "at": 9000}
    queue.update(main_failure=failed, red_batch=old_batch, batch=old_batch)
    setup(monkeypatch, lib, queue, [{**view(), "headRefOid": DIAGNOSTIC_HEAD}], main_green=False)
    events = []
    train = lib.load("hz-merge-train")
    original = train.train
    def merge(repo, number, **kwargs):
        assert kwargs.get("pre_merge_guard") is not None
        events.append(("merge", number))
        return original(repo, number, **kwargs)
    monkeypatch.setattr(train, "train", merge)
    monkeypatch.setattr(queue, "publish_intent", lambda base, item: events.append(("intent", item["pr"])) or item["pr"])
    monkeypatch.setattr(queue, "publish_batch", lambda base, numbers: events.append(("proof", numbers)) or True)
    monkeypatch.setattr(queue, "close_intents", lambda *a: pytest.fail("verified proof was closed"))
    queue.train_tick(now=10_000)
    assert events == [("merge", "490"), ("intent", "490"), ("merge", "491"), ("intent", "491"),
                      ("merge", "492"), ("intent", "492"), ("proof", ["490", "491", "492"])]
    state = queue.load()
    assert state["main_failure"] == failed
    assert state["batch"] == state["red_batch"] == old_batch
    assert [row["pr"] for row in state["progress_history"]] == ["490", "491", "492"]
    assert not state["main_failure"].get("fixed_by")


def test_progress_guard_change_finalizes_only_the_actual_merged_prefix(queue, lib, monkeypatch):
    monkeypatch.setenv("HZ_MAIN_BATCH_PUBLISH", "1")
    for number in ("490", "491", "492"):
        add_progress(queue, lib, monkeypatch, number)
    setup(monkeypatch, lib, queue, [{**view(), "headRefOid": DIAGNOSTIC_HEAD}], main_green=False)
    train = lib.load("hz-merge-train")
    original = train.train
    def merge(*args, **kwargs):
        result = original(*args, **kwargs)
        if len(queue.merges) == 2:
            monkeypatch.setattr(queue, "progress_anchor_unchanged", lambda *a: False)
        return result
    monkeypatch.setattr(train, "train", merge)
    intents, proofs = [], []
    monkeypatch.setattr(queue, "publish_intent", lambda base, item: intents.append(item["pr"]) or item["pr"])
    monkeypatch.setattr(queue, "publish_batch", lambda base, numbers: proofs.append(numbers) or True)
    queue.train_tick(now=10_000)
    assert queue.merges == [(lib.CORE, "490"), (lib.CORE, "491")]
    assert intents == ["490", "491"] and proofs == [["490", "491"]]
    assert queue.load()["items"][2]["status"] == "needs work"
    assert len(queue.load()["progress_history"]) == 2


def test_progress_anchor_keeps_raw_admission_watchset(tool, lib, monkeypatch):
    q = tool("hz-queue")
    rows = [run(900, "completed", "failure", "a" * 40), run(899, "in_progress", "", "b" * 40)]
    calls = []
    monkeypatch.setattr(lib, "ghj", lambda *a, **kw: calls.append(a) or rows)
    monkeypatch.setattr(lib, "resolve_main_runs", lambda *a: pytest.fail("raw progress watchset was delegated"))
    anchor = q.completed_main_anchor()
    assert anchor["pending"][0]["databaseId"] == 899
    assert q.progress_anchor_unchanged(anchor)
    rows = [rows[0], run(899, "completed", "success", "b" * 40)]
    assert not q.progress_anchor_unchanged(anchor)
    assert all(a[-1] == "status,conclusion,databaseId,headSha" for a in calls)


def test_failed_final_that_covers_an_older_batch_member_is_not_hidden(queue, lib, monkeypatch):
    queue.update(batch={"prs": ["12"], "shas": ["b" * 8]})
    rows = [run(100, "completed", "success", "d" * 40),
            {**run(99, "completed", "failure", "c" * 40), "batch": {"prs": [[12, "b" * 40], [13, "c" * 40]]}}]
    monkeypatch.setattr(lib, "ghj", lambda *a, **kw: rows)
    monkeypatch.setattr(lib, "resolve_main_runs", lambda rows: rows)
    monkeypatch.setattr(lib, "only_never_started_macos_jobs_failed", lambda *a: False)
    assert queue.main_state() == "red"
    assert queue.load()["main_failure"]["databaseId"] == 99


@pytest.mark.parametrize("enabled", [None, "", "0", "true"])
def test_batch_publisher_requires_explicit_enablement(queue, lib, monkeypatch, enabled):
    if enabled is None:
        monkeypatch.delenv("HZ_MAIN_BATCH_PUBLISH", raising=False)
    else:
        monkeypatch.setenv("HZ_MAIN_BATCH_PUBLISH", enabled)
    add(queue, "393", "394")
    setup(monkeypatch, lib, queue, [view()])
    monkeypatch.setattr(queue, "publish_intent", lambda *a: pytest.fail("disabled intent published"))
    monkeypatch.setattr(queue, "publish_batch", lambda *a: pytest.fail("disabled proof published"))
    queue.train_tick(now=10_000)
    assert queue.merges == [(lib.CORE, "393"), (lib.CORE, "394")]
    assert not queue.load()["batch_proof"]


@pytest.mark.parametrize("moved_base", [False, True])
def test_operator_guard_is_rechecked_in_the_final_immediate_merge(queue, lib, monkeypatch, moved_base):
    seen = []
    base = "b" * 40
    setup(monkeypatch, lib, queue, [{**view(), "headRefOid": REVIEWED_HEAD}])
    monkeypatch.setattr(queue, "guard_base", lambda *args: base)
    monkeypatch.setattr(queue, "guard_findings", lambda *args: ([("ci.yml", 4, "coe", "continue-on-error: true")], "FLAG"))
    assert queue.main(["add", "--expected-head", REVIEWED_HEAD, "--authorize-guard", "scoped fixture", "594"]) == 0
    if moved_base:
        base = "c" * 40

    def merge(repo, pr, *, immediate_merge, expected_head, pre_merge_guard, queue_core):
        assert immediate_merge and expected_head == REVIEWED_HEAD and queue_core
        allowed = pre_merge_guard()
        seen.append(allowed)
        return allowed, "merged" if allowed else "base changed"

    monkeypatch.setattr(lib.load("hz-merge-train"), "train", merge)
    queue.train_tick(now=10_000)
    assert seen == [not moved_base]
