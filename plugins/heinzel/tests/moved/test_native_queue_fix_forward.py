"""hz-queue fix-forward lane (bead ox6): one fix PR may merge while main is red, only under every rule; refusals named."""

import json
import sys

import pytest
from steuermaschine_heinzel import state as statedir

CORE = "helmut-hoffer-von-ankershoffen/heinzel"
HEAD = "5f68da5d5ad99de85c3a310c90c6e5193c356d47"
RED = {"run": 42377404946, "sha": "e" * 40, "jobs": ["End-to-end web-2 (ubuntu-24.04)"]}


def check(name, conclusion="SUCCESS", status="COMPLETED", run=7):
    return {"name": name, "status": status, "conclusion": conclusion, "detailsUrl": f"https://github.com/o/r/actions/runs/{run}/job/1"}


def pr(title="test(web): assert the measured spectrum", body="Bead 9kk: fixes the flake.", rollup=None, head=HEAD, commits=None):
    return {"state": "OPEN", "isDraft": False, "headRefOid": head, "title": title, "body": body, "labels": [],
            "commits": [{"messageHeadline": h} for h in (commits or [title])],
            "statusCheckRollup": rollup if rollup is not None else [check("CI OK"), check("Lint"), check("Site tests", "SKIPPED"),
                                                                    check("Claude review", "NEUTRAL"), check("review", "FAILURE")]}


BEADS = {"heinzel-management-9kk": {"id": "heinzel-management-9kk", "labels": ["flaky"], "notes": ""},
         "heinzel-management-ft1": {"id": "heinzel-management-ft1", "labels": ["ui"], "notes": ""},
         "heinzel-management-old": {"id": "heinzel-management-old", "labels": [], "issue_type": "bug", "notes": "10:00 PR #590 open"}}


@pytest.fixture
def lib(tool):
    sys.path.insert(0, str(tool.__globals__["TOOLS"]))
    from steuermaschine_heinzel.native.queue_lib import hzlib

    return hzlib


@pytest.fixture
def q(tool, lib, home, monkeypatch):
    monkeypatch.setenv("HZ_QUEUE_STATE", str(statedir.root() / "queue.json"))
    q = tool("hz-queue")
    notes, merges = [], []
    q.notes, q.merges, q.pr_reply = notes, merges, pr()
    monkeypatch.setattr(lib, "note", notes.append)
    monkeypatch.setattr(lib, "sleep", lambda s: None)
    monkeypatch.setattr(q, "guard_findings", lambda repo, number: ([], "PASS"))
    monkeypatch.setattr(q, "pr_facts", lambda number: q.pr_reply)
    monkeypatch.setattr(q, "main_red_jobs", lambda: dict(RED))
    monkeypatch.setattr(q, "bead_info", lambda bead: BEADS.get(bead, {}))
    monkeypatch.setattr(q, "pr_state", lambda n: {"state": "MERGED", "title": "fix: x", "body": ""} if n == "590" else {"state": "OPEN"})
    monkeypatch.setattr(q, "release_active", lambda: "")
    train = tool("hz-merge-train")

    def fake_train(repo, number, ignore_freeze=False, queue_core=False, expected_head="", pre_merge_guard=None):
        merges.append((repo, number, expected_head))
        return True, "abc12345"

    monkeypatch.setattr(train, "train", fake_train)
    monkeypatch.setattr(lib, "load", lambda name: train)
    return q


def run_add(q, *extra, number="598"):
    return q.main(["add", "--fix-forward", *extra, number])


def queued(q):
    return {i["key"]: i for i in q.load()["items"]}


def test_dry_run_admits_prints_mapping_and_queues_nothing(q, capsys):
    assert run_add(q, "--for", "web-2=9kk", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "job 'End-to-end web-2 (ubuntu-24.04)' -> bead heinzel-management-9kk addressed by #598" in out
    assert "admissible (dry run, nothing queued)" in out
    assert not queued(q) and not q.notes


def test_add_queues_pinned_head_and_notes_the_mapping(q):
    assert run_add(q, "--for", "web-2=9kk") == 0
    item = queued(q)["598"]
    assert item["fix_forward"] and item["reviewed_head"] == HEAD and not item["fixes_main"]
    assert item["fix_forward_map"] == [{"job": "End-to-end web-2 (ubuntu-24.04)", "bead": "heinzel-management-9kk", "pr": "598"}]
    assert any("fix-forward #598 queued at 5f68da5d for red main run 42377404946" in n for n in q.notes)


@pytest.mark.parametrize(("reply", "why"), [
    (pr(title="feat(web): new pane"), "not a fix|test|ci"),
    (pr(title="fix!: break the api"), "not a fix|test|ci"),
    (pr(commits=["test: a", "feat: sneak a feature"]), "feature or breaking"),
    (pr(rollup=[check("CI OK"), check("E2E", "CANCELLED")]), "E2E CANCELLED"),
    (pr(rollup=[check("CI OK"), check("E2E", "", "IN_PROGRESS")]), "E2E IN_PROGRESS"),
    (pr(rollup=[check("CI OK", "FAILURE")]), "CI OK FAILURE"),
    (pr(rollup=[check("Lint")]), "CI OK is not SUCCESS"),
    (pr(body="no bead named here"), "does not address heinzel-management-9kk"),
])
def test_refusals_name_the_rule_and_queue_nothing(q, capsys, reply, why):
    q.pr_reply = reply
    assert run_add(q, "--for", "web-2=9kk") == 1
    assert why in capsys.readouterr().err
    assert not queued(q) and not q.notes


def test_a_run_deferred_by_hz_ci_defer_is_not_green(q, home, capsys):
    (statedir.root() / "ci-deferred.txt").write_text("7 feat/x CI\n")
    assert run_add(q, "--for", "web-2=9kk") == 1
    assert "deferred by sm ci defer" in capsys.readouterr().err


def test_every_failing_main_job_needs_a_bead_and_every_entry_a_job(q, monkeypatch, capsys):
    monkeypatch.setattr(q, "main_red_jobs", lambda: {**RED, "jobs": ["End-to-end web-2 (ubuntu-24.04)", "End-to-end web-1 (ubuntu-24.04)"]})
    assert run_add(q, "--for", "web-2=9kk") == 1
    assert "'End-to-end web-1 (ubuntu-24.04)' (run 42377404946) is not attributed" in capsys.readouterr().err
    assert run_add(q, "--for", "web-2=9kk,web-1=9kk,electron=9kk") == 1
    assert "--for electron=heinzel-management-9kk matches no failing job" in capsys.readouterr().err


def test_bead_must_be_a_flake_or_bug_and_exist(q, capsys):
    q.pr_reply = pr(body="Bead ft1 and bead heinzel-management-zzz")
    assert run_add(q, "--for", "web-2=ft1") == 1
    assert "is not a flake or known bug" in capsys.readouterr().err
    assert run_add(q, "--for", "web-2=zzz") == 1
    assert "bead heinzel-management-zzz not found" in capsys.readouterr().err


def test_an_earlier_merged_pr_may_address_the_bead_an_open_one_may_not(q, capsys):
    assert run_add(q, "--for", "web-2=old@590", "--dry-run") == 0  # bead notes carry `PR #590` (hz-tasks pr old 590)
    assert run_add(q, "--for", "web-2=old@591") == 1
    assert "#591 (addresses heinzel-management-old) is not merged" in capsys.readouterr().err


def test_guard_flag_needs_an_independent_review(q, monkeypatch, capsys):
    monkeypatch.setattr(q, "guard_findings", lambda repo, number: ([("a", 1, "raised timeout")], "FLAG"))
    assert run_add(q, "--for", "web-2=9kk") == 1
    assert "sm head guard did not PASS" in capsys.readouterr().err
    assert run_add(q, "--for", "web-2=9kk", "--reviewed", "independent Fable review at 5f68da5d: no weakening") == 0
    assert queued(q)["598"]["fix_forward_reviewed"].startswith("independent Fable")


def test_refused_during_a_freeze_or_release(q, monkeypatch, capsys):
    monkeypatch.setattr(q, "release_active", lambda: "release/* run active: release/0.9.30 1")
    assert run_add(q, "--for", "web-2=9kk") == 1
    assert "REFUSE: release/* run active" in capsys.readouterr().err


def test_release_active_reads_freeze_runs_and_receipt(tool, lib, home, monkeypatch):
    q = tool("hz-queue")
    rows = [{"headBranch": "feat/x", "status": "in_progress", "databaseId": 1}]
    monkeypatch.setattr(lib, "merge_frozen", lambda *a: False)
    monkeypatch.setattr(lib, "ghj", lambda *a, default=None: rows)
    assert q.release_active() == ""
    rows.append({"headBranch": "release/0.9.30", "status": "queued", "databaseId": 2})
    assert q.release_active().startswith("release/* run active: release/0.9.30 2")
    rows.pop()
    (statedir.root() / "release.json").write_text(json.dumps({"version": "0.9.30"}))
    assert q.release_active() == "sm release resume 0.9.30 unfinished"
    monkeypatch.setattr(lib, "ghj", lambda *a, default=None: None)
    assert "cannot read CI runs" in q.release_active()
    monkeypatch.setattr(lib, "merge_frozen", lambda *a: True)
    assert q.release_active().startswith("merge freeze is on")


def test_misuse_is_refused(q, capsys):
    assert q.main(["add", "--for", "web-2=9kk", "598"]) == 1
    assert run_add(q) == 1
    assert run_add(q, "--for", "web-2=9kk", number="plugins 70") == 1
    assert run_add(q, "--for", "web-2=b@d=x") == 1
    with pytest.raises(SystemExit):
        q.main(["add", "--fix-forward", "--fixes-main", "--for", "web-2=9kk", "598"])


def test_help_documents_the_lane(q, capsys):
    with pytest.raises(SystemExit):
        q.main(["--help"])
    assert "FIX-FORWARD LANE" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        q.main(["add", "--help"])
    out = capsys.readouterr().out
    assert "--fix-forward" in out and "--for JOB=BEAD[@PR]" in out and "--dry-run" in out


# the tick


def tick_setup(q, lib, monkeypatch, views):
    monkeypatch.setattr(lib, "ghj", lambda *a, default=None: views.pop(0) if len(views) > 1 else views[0])
    monkeypatch.setattr(lib, "gh", lambda *a: (1, ""))
    monkeypatch.setattr(q, "main_state", lambda: "red")
    monkeypatch.setattr(q, "later_main_passes", lambda: False)


def view(head=HEAD):
    return {"state": "OPEN", "isDraft": False, "headRefOid": head, "mergeStateStatus": "CLEAN", "statusCheckRollup": [check("CI OK")]}


def plain(q, number):
    data = q.load()
    data["items"].append({"key": number, "repo": CORE, "pr": number, "after": [], "status": "queued", "detail": "", "reruns": 0, "noted": "", "added": 1})
    q.save(data)


def test_red_main_fix_forward_goes_alone_once_per_main_run(q, lib, monkeypatch):
    assert run_add(q, "--for", "web-2=9kk") == 0
    plain(q, "599")
    tick_setup(q, lib, monkeypatch, [view()])
    q.train_tick(now=10_000)
    assert q.merges == [(CORE, "598", HEAD)]
    assert queued(q)["599"]["status"] == "queued" and queued(q)["598"]["status"] == "merged"
    assert q.load()["fix_forward_runs"] == {"42377404946": "598"}
    assert any("fix-forward #598 merged at 5f68da5d while main run 42377404946 is red" in n and "pin clears only" in n for n in q.notes)
    assert q.load()["batch"]["prs"] == ["598"] and not q.load()["main_failure"].get("fixed_by")
    # a second fix-forward against the same red run waits
    q.pr_reply = pr(head="a" * 40)
    assert run_add(q, "--for", "web-2=9kk", number="602") == 0  # queues (one-per-run is a hold, not a refusal)
    tick_setup(q, lib, monkeypatch, [view("a" * 40)])
    q.train_tick(now=20_000)
    assert len(q.merges) == 1 and queued(q)["602"]["status"] == "queued"


def test_tick_refuses_a_changed_head_or_a_new_unattributed_failure(q, lib, monkeypatch):
    assert run_add(q, "--for", "web-2=9kk") == 0
    tick_setup(q, lib, monkeypatch, [view("b" * 40)])
    q.train_tick(now=10_000)
    assert not q.merges and "fix-forward head changed" in queued(q)["598"]["detail"]
    assert run_add(q, "--for", "web-2=9kk") == 0
    monkeypatch.setattr(q, "main_red_jobs", lambda: {**RED, "run": 1, "jobs": ["Test (ubuntu-24.04, Python 3.12)"]})
    tick_setup(q, lib, monkeypatch, [view()])
    q.train_tick(now=20_000)
    assert not q.merges and "is not attributed" in queued(q)["598"]["detail"]


def test_tick_holds_during_a_release(q, lib, monkeypatch):
    assert run_add(q, "--for", "web-2=9kk") == 0
    monkeypatch.setattr(q, "release_active", lambda: "sm release resume 0.9.30 unfinished")
    tick_setup(q, lib, monkeypatch, [view()])
    q.train_tick(now=10_000)
    assert not q.merges and queued(q)["598"]["status"] == "queued"


def test_main_red_jobs_uses_latest_red_run_then_the_pin(tool, lib, home, monkeypatch):
    monkeypatch.setenv("HZ_QUEUE_STATE", str(statedir.root() / "queue.json"))
    q = tool("hz-queue")
    sha = "e" * 40
    runs = [{"status": "completed", "conclusion": "failure", "databaseId": 5, "headSha": sha}]
    jobs = {"jobs": [{"name": "CI OK", "conclusion": "failure"}, {"name": "E2E web-2", "conclusion": "failure"}, {"name": "Lint", "conclusion": "success"}]}
    monkeypatch.setattr(lib, "ghj", lambda *a, default=None: runs if a[0] == "run" else jobs)
    assert q.main_red_jobs() == {"run": 5, "sha": sha, "jobs": ["E2E web-2"]}
    runs[0]["conclusion"] = "success"
    assert q.main_red_jobs() == {}
    q.save({**q.load(), "main_failure": {"status": "completed", "conclusion": "failure", "databaseId": 3, "headSha": sha}})
    assert q.main_red_jobs()["run"] == 3
