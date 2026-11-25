"""Dropping a candidate: only a provably unpublished release, only its own hold, the receipt archived."""

import fcntl
import json
import urllib.error

import pytest

VERSION = "1.2.3"
REASON = "web-2 e2e failed in the candidate; cut again"
DROP = [VERSION, "--drop-candidate", "--reason", REASON]


class Remote:
    """Fake GitHub and PyPI: the typed replies the drop proves from, and a log of every write."""

    def __init__(self):
        self.tag = self.release = None
        self.branch = None
        self.prs = [{"number": 794, "state": "CLOSED", "headRefName": f"release/v{VERSION}"}]
        self.pypi = 404
        self.writes = []
        self.code = 0
        self.errors = None
        self.delete_fails = False
        self.auto_close = True
        self.before_delete = None
        self.after_delete = None
        self.lost_delete_reply = False
        self.active = {}  # status -> runs on the release branch that have not completed
        self.runs_code = 0
        self.runs_total = None

    def gh(self, *args):
        if args[:2] == ("api", "graphql") and any("updateRefs(" in arg for arg in args):
            self.writes.append(args)
            if self.before_delete:
                self.before_delete()
            sha = (self.branch or {}).get("target", {"oid": "a" * 40})["oid"]
            if self.delete_fails or self.branch is None or f"before={sha}" not in args:
                return 1, json.dumps({"errors": [{"message": "ref changed or deletion refused"}]})
            self.branch = None
            for pr in self.prs:
                if self.auto_close and pr["state"] == "OPEN":
                    pr["state"] = "CLOSED"
            if self.after_delete:
                self.after_delete()
            if self.lost_delete_reply:
                return 1, ""
            return 0, json.dumps({"data": {"updateRefs": {"clientMutationId": None}}})
        if args[0] == "api" and "/actions/runs?" in args[1]:
            assert f"branch=release/v{VERSION}&status=" in args[1] and len(args) == 2  # a read, never a cancel
            runs = self.active.get(args[1].split("status=")[1].split("&")[0], [])
            return self.runs_code, json.dumps({"total_count": len(runs) if self.runs_total is None else self.runs_total, "workflow_runs": runs})
        assert args[:2] == ("api", "graphql"), args
        assert f"tag=v{VERSION}" in args and f"head=release/v{VERSION}" in args
        prs = ([{"isCrossRepository": False, "baseRefName": "main", "headRefOid": "a" * 40, "autoMergeRequest": None} | pr
                for pr in self.prs] if self.prs is not None else None)
        branch = None if self.branch is None else {"target": {"oid": "a" * 40}} | self.branch
        reply = {"data": {"repository": {"id": "fixture-repository", "ref": self.tag, "branch": branch, "release": self.release,
                                         "pullRequests": {"nodes": prs}}}}
        if self.errors:
            reply["errors"] = self.errors
        return self.code, json.dumps(reply)

    def urlopen(self, url, **_kw):
        assert url == f"https://pypi.org/pypi/heinzel/{VERSION}/json"
        if isinstance(self.pypi, BaseException):
            raise self.pypi
        if self.pypi != 200:
            raise urllib.error.HTTPError(url, self.pypi, "reply", {}, None)

        class Reply:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        return Reply()


@pytest.fixture
def candidate(tool, home, monkeypatch):
    rel = tool("hz-release")
    rel.remember(version=VERSION)
    rel.acquire_snapshot_freeze(VERSION)
    remote = Remote()
    monkeypatch.setattr(rel.hzlib, "gh", remote.gh)
    monkeypatch.setattr(rel.urllib.request, "urlopen", remote.urlopen)
    return rel, remote


def active_run(run_id, status="in_progress"):
    return {"id": run_id, "head_branch": f"release/v{VERSION}", "status": status}


def archives(rel):
    root = rel.dropped_dir()
    return sorted(root.iterdir()) if root.exists() else []


def untouched(rel, before):
    return rel.paths()[1].read_bytes() == before and rel.snapshot_hold_path().read_text() == VERSION and archives(rel) == []


def test_drop_releases_only_its_own_hold_archives_the_receipt_and_a_fresh_release_starts_clean(candidate, capsys):
    rel, remote = candidate
    token = rel.state()["freeze_token"]
    own = rel.snapshot_hold_path()
    incident = rel.paths()[0]
    incident.write_text("incident")
    other = own.parent / ("b" * 32)
    other.write_text("9.9.9")
    assert rel.main(DROP) == 0
    assert not own.exists() and not rel.paths()[1].exists()
    assert incident.read_text() == "incident" and other.read_text() == "9.9.9"
    assert remote.writes == []  # nothing closed, deleted or cancelled
    (archive,) = archives(rel)
    assert archive.name.startswith(f"v{VERSION}-") and archive.suffix == ".json"
    kept = json.loads(archive.read_text())
    assert kept["version"] == VERSION and kept["freeze_token"] == token
    assert kept["drop"]["reason"] == REASON and kept["drop"]["hold"] == "owned" and kept["drop"]["token"] == token
    assert kept["drop"]["evidence"] == {"tag": False, "github_release": False, "pypi": 404, "merged_pr": False,
                                        "closed_prs": [794], "closed_by_drop": [], "branch": False,
                                        "branch_deleted_by_drop": False}
    out = capsys.readouterr()
    assert f"DROPPED {VERSION}" in out.out and "No workflow run was cancelled" in out.out and "other merge holds remain" in out.out
    other.unlink()
    incident.unlink()
    assert rel.state() == {}  # a fresh `hz-release 1.2.3` reads no receipt and finds no hold
    assert not rel.hzlib.release_holds_present()


def test_drop_of_a_paused_candidate_with_a_closed_pr_is_supported(candidate):
    """The 2026-11-12 case: paused record, PR closed, branch deleted; --pause and resume both refuse it."""
    rel, remote = candidate
    remote.prs = [{"number": 792, "state": "OPEN", "headRefName": f"release/v{VERSION}", "baseRefName": "main",
                   "headRefOid": "a" * 40, "isDraft": True, "autoMergeRequest": None}]
    assert rel.main([VERSION, "--pause"]) == 0
    assert not rel.snapshot_hold_path().exists()
    remote.prs = [{"number": 792, "state": "CLOSED", "headRefName": f"release/v{VERSION}"}]
    assert rel.main(DROP) == 0
    (archive,) = archives(rel)
    kept = json.loads(archive.read_text())
    assert kept["drop"]["hold"] == "paused" and [e["action"] for e in kept["hold_events"]] == ["pause-request", "pause"]
    assert rel.state() == {}


def test_drop_without_any_release_pr_or_branch_is_supported(candidate):
    rel, remote = candidate
    remote.prs = []
    assert rel.main(DROP) == 0
    assert json.loads(archives(rel)[0].read_text())["drop"]["evidence"]["closed_prs"] == []


@pytest.mark.parametrize("case, unmet", [
    ("tag", "tag v1.2.3 exists on the remote"),
    ("release", "GitHub release v1.2.3 exists"),
    ("pypi-published", "heinzel 1.2.3 is on PyPI"),
    ("pypi-500", "cannot prove 1.2.3 is absent from PyPI (HTTP 500)"),
    ("pypi-offline", "cannot prove 1.2.3 is absent from PyPI"),
    ("merged", "release PR #794 is merged"),
    ("open", "release PR #794 is open"),
    ("branch", "remote branch release/v1.2.3 exists"),
    ("gh-failed", "cannot prove the release state on GitHub"),
    ("gh-errors", "cannot prove the release state on GitHub"),
    ("gh-garbage", "cannot prove the release state on GitHub"),
    ("pr-truncated", "cannot prove the release state on GitHub"),
    ("pr-foreign-head", "cannot prove the release state on GitHub"),
    ("run-in-progress", "candidate run 4301 on release/v1.2.3 is still active (2 in all)"),
    ("run-queued", "candidate run 4302 on release/v1.2.3 is still active (1 in all)"),
    ("runs-unreadable", "cannot prove that no candidate run on release/v1.2.3 is still active"),
    ("runs-truncated", "cannot prove that no candidate run on release/v1.2.3 is still active"),
    ("runs-foreign-branch", "cannot prove that no candidate run on release/v1.2.3 is still active"),
])
def test_every_unproven_or_published_state_refuses_with_the_exact_condition(candidate, capsys, case, unmet):
    rel, remote = candidate
    if case == "tag":
        remote.tag = {"name": "v1.2.3"}
    elif case == "release":
        remote.release = {"tagName": "v1.2.3"}
    elif case == "pypi-published":
        remote.pypi = 200
    elif case == "pypi-500":
        remote.pypi = 500
    elif case == "pypi-offline":
        remote.pypi = urllib.error.URLError("offline")
    elif case == "merged":
        remote.prs[0]["state"] = "MERGED"
    elif case == "open":
        remote.prs[0]["state"] = "OPEN"
    elif case == "branch":
        remote.branch = {"name": "release/v1.2.3"}
    elif case == "gh-failed":
        remote.code = 1
    elif case == "gh-errors":
        remote.errors = [{"message": "rate limited"}]
    elif case == "gh-garbage":
        remote.prs = None
    elif case == "pr-truncated":
        remote.prs = remote.prs * 20
    elif case == "run-in-progress":
        remote.active = {"in_progress": [active_run(4301)], "queued": [active_run(4302, "queued")]}
    elif case == "run-queued":
        remote.active = {"queued": [active_run(4302, "queued")]}
    elif case == "runs-unreadable":
        remote.runs_code = 1
    elif case == "runs-truncated":
        remote.runs_total = 101
    elif case == "runs-foreign-branch":
        remote.active = {"in_progress": [active_run(4301) | {"head_branch": "release/v1.2.30"}]}
    else:
        remote.prs[0]["headRefName"] = "release/v1.2.30"
    before = rel.paths()[1].read_bytes()
    assert rel.main(DROP) == 1
    err = capsys.readouterr().err
    assert f"drop refused: {unmet}" in err and "run the same drop command again" in err
    assert untouched(rel, before) and remote.writes == []


@pytest.mark.parametrize("case, unmet", [
    ("other-version", "the release receipt is for 1.2.4, not 1.2.3"),
    ("no-receipt", "the release receipt is for no release, not 1.2.3"),
    ("finished", "release 1.2.3 is finished"),
    ("pypi", "records steps after the tag"),
    ("compose", "records steps after the tag"),
    ("acceptance_run", "records steps after the tag"),
    ("unknown_future_stage", "records steps after the tag"),
    ("tail", "is past its tag"),
    ("tag-on-main", "tag-on-green-main"),
    ("foreign-drop", "drop record of another identity"),
    ("hold-missing", "owned release hold is missing without a recorded pause"),
    ("hold-other-version", "release hold is not a regular file owned by this version"),
    ("hold-symlink", "release hold is not a regular file owned by this version"),
    ("bad-token", "release hold identity is missing or invalid"),
    ("bad-events", "release hold history is invalid"),
])
def test_local_state_that_is_not_an_unpublished_candidate_refuses_before_any_network(candidate, capsys, monkeypatch, case, unmet):
    rel, remote = candidate
    monkeypatch.setattr(rel.hzlib, "gh", lambda *a: pytest.fail("must refuse before the network"))
    monkeypatch.setattr(rel.urllib.request, "urlopen", lambda *a, **k: pytest.fail("must refuse before the network"))
    hold = rel.snapshot_hold_path()
    if case == "other-version":
        hold.write_text("1.2.4")
        rel.remember(version="1.2.4")
    elif case == "no-receipt":
        hold.unlink()
        rel.paths()[1].unlink()
    elif case == "finished":
        rel.remember(finished=True)
    elif case in ("pypi", "compose", "acceptance_run", "unknown_future_stage"):
        rel.remember(**{case: "2026-11-12T18:00:00Z"})
    elif case == "tail":
        rel.tail_dir().mkdir(parents=True)
        rel.tail_receipt(VERSION).write_text(json.dumps({"version": VERSION}))
    elif case == "tag-on-main":
        rel.release_flow.write({"version": VERSION, "flow": rel.release_flow.TAG_ON_MAIN})
    elif case == "foreign-drop":
        rel.remember(drop={"version": VERSION, "token": "c" * 32})
    elif case == "hold-missing":
        hold.unlink()
    elif case == "hold-other-version":
        hold.write_text("9.9.9")
    elif case == "hold-symlink":
        target = hold.with_name("elsewhere")
        target.write_text(VERSION)
        hold.unlink()
        hold.symlink_to(target)
    elif case == "bad-token":
        rel.remember(freeze_token="not-a-token")
    else:
        rel.remember(hold_events=[{"action": "pause", "version": "9.9.9"}])
    receipt = rel.paths()[1].read_bytes() if rel.paths()[1].exists() else None
    assert rel.main(DROP) == 1
    assert f"{unmet}" in capsys.readouterr().err
    assert archives(rel) == []
    assert (rel.paths()[1].read_bytes() if rel.paths()[1].exists() else None) == receipt
    if case in ("hold-other-version", "hold-symlink"):
        assert hold.is_symlink() or hold.read_text() == "9.9.9"  # a foreign hold is never removed


def test_close_pr_closes_the_open_release_pr_and_deletes_its_branch_then_proves_again(candidate):
    rel, remote = candidate
    remote.prs = [{"number": 792, "state": "CLOSED", "headRefName": "release/v1.2.3"},
                  {"number": 794, "state": "OPEN", "headRefName": "release/v1.2.3"}]
    remote.branch = {"name": "release/v1.2.3"}
    assert rel.main([*DROP, "--close-pr"]) == 0
    [mutation] = remote.writes
    assert mutation[:2] == ("api", "graphql") and f"before={'a' * 40}" in mutation
    evidence = json.loads(archives(rel)[0].read_text())["drop"]["evidence"]
    assert evidence["closed_prs"] == [792, 794] and evidence["closed_by_drop"] == [794] and evidence["branch_deleted_by_drop"] is True
    assert not rel.paths()[1].exists() and not rel.hzlib.release_holds_present()


@pytest.mark.parametrize("case, unmet", [
    ("pr-stays-open", "release PR #794 is still open"),
    ("delete-fails", "GitHub did not confirm conditional deletion"),
    ("merged-meanwhile", "release PR #794 is merged"),
    ("tagged-meanwhile", "tag v1.2.3 exists on the remote"),
])
def test_close_pr_that_cannot_be_proven_keeps_hold_and_receipt(candidate, capsys, case, unmet):
    rel, remote = candidate
    remote.prs[0]["state"] = "OPEN"
    remote.branch = {"name": "release/v1.2.3"}
    if case == "pr-stays-open":
        remote.auto_close = False
    elif case == "delete-fails":
        remote.delete_fails = True
    elif case == "merged-meanwhile":
        remote.after_delete = lambda: remote.prs[0].update(state="MERGED")
    else:
        remote.after_delete = lambda: setattr(remote, "tag", {"name": "v1.2.3"})
    before = rel.paths()[1].read_bytes()
    assert rel.main([*DROP, "--close-pr"]) == 1
    assert unmet in capsys.readouterr().err
    assert rel.state() == json.loads(before) | {"drop_attempt": {"pr": 794, "head": "a" * 40}}
    assert rel.snapshot_hold_path().read_text() == VERSION and archives(rel) == []


def test_replacement_during_legacy_drop_preserves_branch_and_holds(candidate):
    rel, remote = candidate
    remote.prs[0].update(state="OPEN", headRefOid="a" * 40)
    remote.branch = {"name": f"release/v{VERSION}", "target": {"oid": "a" * 40}}

    def push_replacement():
        remote.branch["target"]["oid"] = remote.prs[0]["headRefOid"] = "f" * 40

    remote.before_delete = push_replacement
    assert rel.main([*DROP, "--close-pr"]) == 1
    assert rel.main([*DROP, "--close-pr"]) == 1
    assert remote.branch["target"]["oid"] == "f" * 40 and remote.prs[0]["state"] == "OPEN"
    assert rel.snapshot_hold_path().read_text() == VERSION and archives(rel) == []
    assert rel.state()["drop_attempt"] == {"pr": 794, "head": "a" * 40}
    assert len(remote.writes) == 1 and f"before={'a' * 40}" in remote.writes[0]


def test_durable_drop_record_precedes_the_unlink_and_a_failed_unlink_is_retried(candidate, monkeypatch, capsys):
    rel, remote = candidate
    original = rel.unlink_pinned_hold
    seen = []

    def failing(*args):
        seen.append(rel.state()["drop"]["reason"])
        raise rel.Stop("cannot remove pinned release hold; pause evidence retained for retry")

    monkeypatch.setattr(rel, "unlink_pinned_hold", failing)
    assert rel.main(DROP) == 1
    assert seen == [REASON] and rel.snapshot_hold_path().exists() and archives(rel) == []
    # An unfinished drop blocks every other release command, by name.
    assert rel.main([VERSION]) == 1 and rel.main([VERSION, "--pause"]) == 1 and rel.main(["--plugin", "resource"]) == 1
    assert f"sm release drop {VERSION} --reason" in capsys.readouterr().err  # the hint names the command that finishes it
    monkeypatch.setattr(rel, "unlink_pinned_hold", original)
    assert rel.main([VERSION, "--drop-candidate", "--reason", "a different reason on retry"]) == 0
    kept = json.loads(archives(rel)[0].read_text())
    assert kept["drop"]["reason"] == REASON and not rel.hzlib.release_holds_present()


def test_retry_after_the_hold_was_removed_archives_without_a_hold(candidate, monkeypatch):
    rel, remote = candidate

    def fail(*_):
        raise rel.Stop("cannot archive the dropped receipt; run the same drop command again")

    original = rel.archive_dropped
    monkeypatch.setattr(rel, "archive_dropped", fail)
    assert rel.main(DROP) == 1
    assert not rel.hzlib.release_holds_present() and rel.state()["drop"]["hold"] == "owned"
    monkeypatch.setattr(rel, "archive_dropped", original)
    assert rel.main(DROP) == 0
    assert json.loads(archives(rel)[0].read_text())["drop"]["hold"] == "owned" and rel.state() == {}


@pytest.mark.skipif("sys.platform == 'linux'", reason="Linux reuses the freed inode at once, so the replaced hold looks unchanged; the race is provable on macOS (APFS)")
def test_hold_replaced_during_the_remote_proof_is_never_removed(candidate, monkeypatch):
    rel, remote = candidate
    hold = rel.snapshot_hold_path()
    proof = remote.gh

    def raced(*args):
        hold.unlink()
        hold.write_text(VERSION)  # same name and content, another inode
        return proof(*args)

    monkeypatch.setattr(rel.hzlib, "gh", raced)
    assert rel.main(DROP) == 1
    assert hold.read_text() == VERSION and archives(rel) == [] and rel.paths()[1].exists()


def test_receipt_without_an_owned_hold_never_touches_the_shared_sentinel(tool, home, monkeypatch, capsys):
    rel = tool("hz-release")
    rel.remember(version=VERSION)
    sentinel = rel.paths()[0]
    sentinel.write_text("")
    remote = Remote()
    monkeypatch.setattr(rel.hzlib, "gh", remote.gh)
    monkeypatch.setattr(rel.urllib.request, "urlopen", remote.urlopen)
    assert rel.main(DROP) == 0
    assert sentinel.exists() and "other merge holds remain" in capsys.readouterr().out
    assert json.loads(archives(rel)[0].read_text())["drop"]["hold"] == "none"


def test_waiver_of_the_dropped_version_moves_to_the_archive_and_another_stays(candidate):
    rel, _ = candidate
    rel.waiver_path().write_text(json.dumps({"version": "9.9.9", "job": "x", "bead": "abc", "owner_quote": "waive it please"}))
    assert rel.main(DROP) == 0
    assert rel.waiver_path().exists() and len(archives(rel)) == 1


def test_waiver_of_the_dropped_version_is_archived(candidate):
    rel, _ = candidate
    rel.waiver_path().write_text(json.dumps({"version": VERSION, "job": "x", "bead": "abc", "owner_quote": "waive it please"}))
    assert rel.main(DROP) == 0
    names = [path.name for path in archives(rel)]
    assert not rel.waiver_path().exists() and len(names) == 2 and any(name.endswith(".waiver.json") for name in names)
    assert rel.pending_waiver(VERSION) is None


def test_two_drops_in_the_same_second_keep_both_archives(candidate, monkeypatch):
    rel, remote = candidate
    assert rel.main(DROP) == 0
    first = archives(rel)[0]
    rel.remember(version=VERSION)
    rel.acquire_snapshot_freeze(VERSION)
    stamp = first.name.removeprefix(f"v{VERSION}-").removesuffix(".json")

    class Frozen(rel.datetime):
        @classmethod
        def now(cls, tz=None):
            return rel.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=tz)

    monkeypatch.setattr(rel, "datetime", Frozen)
    assert rel.main(DROP) == 0
    assert len(archives(rel)) == 2 and first.exists()


def test_shared_lock_blocks_a_drop_during_any_release_command(candidate, monkeypatch):
    rel, _ = candidate
    monkeypatch.setattr(rel.hzlib, "gh", lambda *a: pytest.fail("must refuse before the network"))
    before = rel.paths()[1].read_bytes()
    with rel.paths()[1].with_suffix(".lock").open("a") as other_command:
        fcntl.flock(other_command, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert rel.main(DROP) == 1
    assert untouched(rel, before)


@pytest.mark.parametrize("argv", [
    [VERSION, "--drop-candidate"],
    ["patch", "--drop-candidate", "--reason", REASON],
    ["--drop-candidate", "--reason", REASON, "--plugin", "resource"],
    [VERSION, "--drop-candidate", "--reason", REASON, "--pause"],
    [VERSION, "--drop-candidate", "--reason", REASON, "--intro", "x"],
    [VERSION, "--drop-candidate", "--reason", REASON, "--snapshot-acceptance"],
    [VERSION, "--close-pr"],
    [VERSION, "--reason", REASON],
])
def test_drop_takes_only_an_exact_version_a_reason_and_close_pr(tool, home, argv):
    rel = tool("hz-release")
    with pytest.raises(SystemExit) as stop:
        rel.main(argv)
    assert stop.value.code == 2


def test_a_reason_too_short_to_explain_anything_is_refused(candidate, capsys):
    rel, remote = candidate
    before = rel.paths()[1].read_bytes()
    assert rel.main([VERSION, "--drop-candidate", "--reason", "red"]) == 1
    assert "at least 10 characters" in capsys.readouterr().err and untouched(rel, before) and remote.writes == []


def test_close_pr_never_closes_the_pr_of_a_candidate_that_still_runs(candidate, capsys):
    rel, remote = candidate
    remote.prs[0]["state"] = "OPEN"
    remote.branch = {"name": f"release/v{VERSION}"}
    remote.active = {"in_progress": [active_run(4301)]}
    before = rel.paths()[1].read_bytes()
    assert rel.main([*DROP, "--close-pr"]) == 1
    assert "candidate run 4301 on release/v1.2.3 is still active" in capsys.readouterr().err
    assert untouched(rel, before) and remote.writes == [] and remote.prs[0]["state"] == "OPEN"
    remote.active = {}
    assert rel.main([*DROP, "--close-pr"]) == 0 and len(archives(rel)) == 1


# ---------------------------------------------------------------- bead oevt: a release PR that `sm release pr` opened
OPENED = {"pr": 794, "head": "a" * 40, "at": "2026-11-15T01:00:00Z"}


def test_a_recorded_open_release_pr_is_pre_tag_and_close_pr_closes_it(candidate):
    rel, remote = candidate
    rel.remember(release_pr_opened=OPENED)
    remote.prs = [{"number": 794, "state": "OPEN", "headRefName": f"release/v{VERSION}"}]
    remote.branch = {"name": f"release/v{VERSION}"}
    before = rel.paths()[1].read_bytes()
    refused = rel.main(DROP)  # open PR without --close-pr: refused, nothing written
    assert refused != 0 and remote.writes == [] and rel.paths()[1].read_bytes() == before
    assert rel.main([*DROP, "--close-pr"]) == 0
    assert remote.writes[0][:2] == ("api", "graphql")
    evidence = json.loads(archives(rel)[0].read_text())["drop"]["evidence"]
    assert evidence["recorded_pr"] == 794 and evidence["closed_by_drop"] == [794] and evidence["pypi"] == 404 and evidence["tag"] is False


def test_a_recorded_release_pr_is_judged_by_github_not_by_the_receipt(candidate, capsys):
    rel, remote = candidate
    rel.remember(release_pr_opened=OPENED)
    before = rel.paths()[1].read_bytes()
    remote.prs = [{"number": 794, "state": "MERGED", "headRefName": f"release/v{VERSION}"}]
    assert rel.main([*DROP, "--close-pr"]) != 0 and "release PR #794 is merged" in capsys.readouterr().err
    remote.prs = [{"number": 801, "state": "OPEN", "headRefName": f"release/v{VERSION}"}]  # GitHub knows no #794
    assert rel.main([*DROP, "--close-pr"]) != 0 and "cannot prove exact release PR #794" in capsys.readouterr().err
    remote.prs, remote.tag = [{"number": 794, "state": "CLOSED", "headRefName": f"release/v{VERSION}"}], {"name": "v1.2.3"}
    assert rel.main(DROP) != 0 and "tag v1.2.3 exists" in capsys.readouterr().err
    remote.tag, remote.pypi = None, 200
    assert rel.main(DROP) != 0 and "is on PyPI" in capsys.readouterr().err
    assert remote.writes == [] and rel.paths()[1].read_bytes() == before and archives(rel) == []


@pytest.mark.parametrize("bad", [{"pr": "794", "head": "a" * 40, "at": "2026-11-15T01:00:00Z"}, {"pr": 794}, "794",
                                 {"pr": 794, "head": "a" * 40, "at": "yesterday"}])
def test_a_malformed_release_pr_record_refuses_before_any_network(candidate, capsys, bad):
    rel, remote = candidate
    rel.remember(release_pr_opened=bad)
    assert rel.main([*DROP, "--close-pr"]) != 0 and "release_pr_opened record is malformed" in capsys.readouterr().err
    assert remote.writes == []


ADOPT = [*DROP, "--close-pr", "--adopt-pr", "794", "--adopt-head", OPENED["head"]]


@pytest.fixture
def unrecorded(tool, home, monkeypatch):
    rel, remote = tool("hz-release"), Remote()
    branch = f"release/v{VERSION}"
    remote.prs = [{"number": 794, "state": "OPEN", "headRefName": branch, "isCrossRepository": False,
                   "baseRefName": "main", "headRefOid": OPENED["head"], "autoMergeRequest": None}]
    remote.branch = {"name": branch, "target": {"oid": OPENED["head"]}}
    monkeypatch.setattr(rel.hzlib, "gh", remote.gh)
    monkeypatch.setattr(rel.urllib.request, "urlopen", remote.urlopen)
    return rel, remote


def test_adopt_only_the_exact_unrecorded_candidate_then_finish_after_interruption(unrecorded):
    rel, remote = unrecorded
    remote.lost_delete_reply = True
    assert rel.main(ADOPT) == 1
    assert rel.state()["release_pr_opened"]["head"] == OPENED["head"]
    assert remote.prs[0]["state"] == "CLOSED" and archives(rel) == []
    assert rel.main([*DROP, "--close-pr"]) == 1
    assert remote.branch is None
    assert rel.main(ADOPT) == 0
    record = json.loads(archives(rel)[0].read_text())
    assert record["release_pr_opened"]["pr"] == 794 and record["drop"]["hold"] == "none"
    assert [write[:2] for write in remote.writes] == [("api", "graphql")]
    assert rel.state() == {}


@pytest.mark.parametrize(("target", "key", "value"), [
    ("repo", "tag", {"name": "v1.2.3"}), ("repo", "release", {}), ("repo", "pypi", 200),
    ("repo", "active", {"in_progress": [active_run(4301)]}), ("repo", "branch", None),
    ("pr", "headRefOid", "f" * 40), ("pr", "number", 795), ("pr", "state", "CLOSED"),
    ("pr", "autoMergeRequest", {}), ("pr", "isCrossRepository", True), ("pr", "isCrossRepository", None),
    ("pr", "baseRefName", "other"), ("branch", "target", {"oid": "f" * 40}),
])
def test_adoption_false_proof_never_writes(unrecorded, target, key, value):
    rel, remote = unrecorded
    if target == "repo":
        setattr(remote, key, value)
    else:
        (remote.prs[0] if target == "pr" else remote.branch)[key] = value
    assert rel.main(ADOPT) == 1
    assert not rel.paths()[1].exists() and not archives(rel) and not remote.writes


def test_adoption_does_not_delete_a_replacement_branch_after_initial_proof(unrecorded):
    rel, remote = unrecorded
    remote.before_delete = lambda: remote.branch["target"].update(oid="f" * 40)
    assert rel.main(ADOPT) == 1
    assert [write[:2] for write in remote.writes] == [("api", "graphql")]
    assert remote.branch["target"]["oid"] == "f" * 40 and remote.prs[0]["state"] == "OPEN"
    assert archives(rel) == []

    assert rel.state()["release_pr_opened"]["head"] == OPENED["head"]
    assert rel.main(ADOPT) == 1 and len(remote.writes) == 1


def test_adoption_refuses_incident_hold_and_other_receipt(unrecorded):
    rel, remote = unrecorded
    incident = rel.paths()[0]
    incident.parent.mkdir(parents=True, exist_ok=True)
    incident.write_text("incident")
    assert rel.main(ADOPT) == 1 and not rel.paths()[1].exists()
    incident.unlink()
    rel.remember(version=VERSION, release_pr_opened=OPENED | {"head": "f" * 40})
    before = rel.paths()[1].read_bytes()
    assert rel.main(ADOPT) == 1 and rel.paths()[1].read_bytes() == before and remote.writes == []


@pytest.mark.parametrize("args", [
    ["--adopt-pr", "794", "--adopt-head", OPENED["head"]],
    ["--close-pr", "--adopt-pr", "794"],
    ["--close-pr", "--adopt-head", OPENED["head"]],
    ["--close-pr", "--adopt-pr", "0", "--adopt-head", OPENED["head"]],
    ["--close-pr", "--adopt-pr", "794", "--adopt-head", "a"],
])
def test_adoption_needs_both_pins_and_close_pr(unrecorded, args):
    rel, remote = unrecorded
    assert rel.main([*DROP, *args]) == 1
    assert not rel.paths()[1].exists() and not archives(rel) and not remote.writes


def test_adoption_records_confirmed_branch_deletion_and_pr_closure(unrecorded):
    rel, remote = unrecorded
    assert rel.main(ADOPT) == 0
    record = json.loads(archives(rel)[0].read_text())["drop"]["evidence"]
    assert record["branch_deleted_by_drop"] and record["closed_by_drop"] == [794]
