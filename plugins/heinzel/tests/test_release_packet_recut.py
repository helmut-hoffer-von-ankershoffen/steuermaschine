"""The release-main-only/v1 packet of `sm release cut` (byte-equal to the hand script's output for 0.9.42, with the home
directory in the hook paths and the commit log replaced by /Users/USER: no real path in a public repository) and the reset of
the cut state by `sm release drop`, and the choice of the release PR (open before merged, closed ignored, two open refuse)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from steuermaschine.engine import Refuse
from steuermaschine.protocols import Result
from steuermaschine.testing import fake_providers
from steuermaschine.testing.fakes import FakeCommands
from steuermaschine_heinzel.native import release
from steuermaschine_heinzel.native.release_lib import runner as runner_lib
from steuermaschine_heinzel.pipelines import release_cut as release_packet

FIX = Path(__file__).parent / "fixtures" / "release" / "packet"
EXPECTED = json.loads((FIX / "release-reservation-packet.json").read_text())
RELEASE = EXPECTED["release"]


def assembled(**over):
    args = dict(version=RELEASE["version"], head=RELEASE["head"], base=RELEASE["base"], tree=RELEASE["tree"], changelog_blob=EXPECTED["review"]["changelog_blob"],
                pair=json.loads((FIX / "pair-packet.json").read_text()), commit_log=("commit-generated.log", (FIX / "commit-generated.log").read_bytes()),
                review=("RELEASE-REVIEW.md", (FIX / "RELEASE-REVIEW.md").read_bytes()), validator_sha256=RELEASE["validator_sha256"],
                hooks={n: (h["path"], h["sha256"]) for n, h in RELEASE["hooks"].items()}, hook_runner=(RELEASE["hook_runner"]["path"], RELEASE["hook_runner"]["sha256"]))
    return release_packet.assemble(**{**args, **over})


def test_the_packet_is_byte_equal_to_the_one_the_hand_script_made_for_0_9_42():  # home path sanitized in fixture and digest
    raw = release_packet.render(assembled())
    assert raw == (FIX / "release-reservation-packet.json").read_bytes()
    assert release_packet.sha(raw) == "13b3520e0727d30ff4e853510c9d3ccd19f22f081d4d1a6679e077ff738437cb"


@pytest.mark.parametrize("over,why", [
    ({"base": "0" * 40}, "not the push plan of this release commit"),
    ({"tree": "1" * 40}, "not the push plan of this release commit"),
    ({"version": "0.9.43"}, "does not end with v0.9.43"),
    ({"pair": {"manifest": {}}}, "no main_manifest"),
])
def test_a_packet_that_does_not_match_the_release_commit_is_refused(over, why):
    with pytest.raises(Refuse, match=why):
        assembled(**over)


def test_the_evidence_comes_from_git_and_the_hooks_of_the_clone(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    prek = tmp_path / "prek"
    prek.write_bytes(b"binary")
    for name in release_packet.HOOKS:
        (hooks / name).write_text(f'#!/bin/sh\nPREK="{prek}"\nexec "$PREK" {name}\n')
    cmd = FakeCommands()
    head, base, tree = RELEASE["head"], RELEASE["base"], RELEASE["tree"]
    cmd.on(["git", "-C", "/clone", "rev-list"], Result(0, f"{head} {base}\n"))
    cmd.on(["git", "-C", "/clone", "rev-parse", "--path-format=absolute"], lambda argv: Result(0, f"{hooks}/{argv[-1].split('/')[-1]}\n"))
    cmd.on(["git", "-C", "/clone", "rev-parse", f"{head}^{{tree}}"], Result(0, tree + "\n"))
    cmd.on(["git", "-C", "/clone", "rev-parse", f"{head}:CHANGELOG.md"], Result(0, "ab" * 20 + "\n"))
    found = release_packet.evidence(fake_providers(commands=cmd), "/clone", head)
    assert found["base"] == base and found["tree"] == tree and found["changelog_blob"] == "ab" * 20
    assert found["hook_runner"] == (str(prek), release_packet.sha(b"binary"))
    assert found["hooks"]["pre-push"] == (str(hooks / "pre-push"), release_packet.sha((hooks / "pre-push").read_bytes()))


def test_a_merge_commit_or_a_hook_without_an_interpreter_is_refused(tmp_path):
    cmd = FakeCommands()
    cmd.on(["git", "-C", "/clone", "rev-list"], Result(0, f"{'a' * 40} {'b' * 40} {'c' * 40}\n"))
    with pytest.raises(Refuse, match="exactly one parent"):
        release_packet.evidence(fake_providers(commands=cmd), "/clone", "a" * 40)
    cmd = FakeCommands()
    cmd.on(["git", "-C", "/clone", "rev-list"], Result(0, f"{'a' * 40} {'b' * 40}\n"))
    (tmp_path / "pre-commit").write_text("#!/bin/sh\n")
    cmd.on(["git", "-C", "/clone", "rev-parse", "--path-format=absolute"], Result(0, f"{tmp_path}/pre-commit\n"))
    with pytest.raises(Refuse, match="names no PREK"):
        release_packet.evidence(fake_providers(commands=cmd), "/clone", "a" * 40)


def test_review_and_log_paths_stay_inside_the_review_root(tmp_path):
    for bad in ("../x", "/etc/passwd", ""):
        with pytest.raises(Refuse, match="unsafe"):
            release_packet.read_inside(tmp_path, bad, "review")
    with pytest.raises(Refuse, match="cannot read"):
        release_packet.read_inside(tmp_path, "missing.md", "review")
    shutil.copy(FIX / "RELEASE-REVIEW.md", tmp_path / "r.md")
    assert release_packet.read_inside(tmp_path, "r.md", "review") == (FIX / "RELEASE-REVIEW.md").read_bytes()


def test_the_validator_is_the_file_the_route_checks():
    from steuermaschine_heinzel.native.release_admit_lib import release_scope

    assert release_packet.validator_path() == Path(release_scope.__file__).resolve()


# ----------------------------------------------------------------------------- the release PR
def prs(monkeypatch, listed):
    monkeypatch.setattr(runner_lib.hzlib, "ghj", lambda *a, **k: listed)


def test_the_open_pr_wins_over_a_closed_one_of_a_dropped_candidate(monkeypatch):
    prs(monkeypatch, [{"number": 1, "state": "CLOSED"}, {"number": 2, "state": "OPEN"}])
    assert runner_lib.release_pr("v0.9.42") == {"number": 2, "state": "OPEN"}
    prs(monkeypatch, [{"number": 2, "state": "OPEN"}, {"number": 1, "state": "CLOSED"}])
    assert runner_lib.release_pr("v0.9.42") == {"number": 2, "state": "OPEN"}


def test_a_closed_pr_alone_is_no_release_pr_and_a_merged_one_is(monkeypatch):
    prs(monkeypatch, [{"number": 1, "state": "CLOSED"}])
    assert runner_lib.release_pr("v0.9.42") is None
    prs(monkeypatch, [{"number": 1, "state": "CLOSED"}, {"number": 3, "state": "MERGED"}])
    assert runner_lib.release_pr("v0.9.42") == {"number": 3, "state": "MERGED"}
    prs(monkeypatch, [])
    assert runner_lib.release_pr("v0.9.42") is None


def test_a_fork_pr_with_the_release_branch_name_is_not_the_release_pr(monkeypatch):
    seen = {}

    def ghj(*argv, **k):
        seen["argv"] = argv
        return [{"number": 1, "state": "CLOSED", "isCrossRepository": False}, {"number": 99, "state": "OPEN", "isCrossRepository": True}]

    monkeypatch.setattr(runner_lib.hzlib, "ghj", ghj)
    assert runner_lib.release_pr("v0.9.42") is None  # only the fork's PR is open: no release PR of ours
    assert "isCrossRepository" in seen["argv"][seen["argv"].index("--json") + 1]
    prs(monkeypatch, [{"number": 99, "state": "OPEN", "isCrossRepository": True}, {"number": 2, "state": "OPEN", "isCrossRepository": False}])
    assert runner_lib.release_pr("v0.9.42")["number"] == 2


def test_the_drop_proof_ignores_a_fork_pr_of_the_same_branch_name(monkeypatch):
    import io
    import urllib.error

    def refuse(url, timeout=0):
        raise urllib.error.HTTPError(url, 404, "nf", {}, io.BytesIO())

    nodes = [{"number": 99, "state": "OPEN", "headRefName": "release/v0.9.42", "isCrossRepository": True},
             {"number": 5, "state": "CLOSED", "headRefName": "release/v0.9.42", "isCrossRepository": False}]
    reply = json.dumps({"data": {"repository": {"ref": None, "branch": None, "release": None, "pullRequests": {"nodes": nodes}}}})
    monkeypatch.setattr(runner_lib.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(runner_lib.hzlib, "gh", lambda *a, **k: (0, reply))
    monkeypatch.setattr(runner_lib, "drop_active_runs", lambda head: [])
    assert runner_lib.drop_identity("0.9.42") == {"open": [], "closed": [5], "branch": False}  # the fork's PR would be closed by --close-pr


def test_two_open_pull_requests_on_the_release_branch_refuse(monkeypatch):
    prs(monkeypatch, [{"number": 1, "state": "OPEN"}, {"number": 2, "state": "OPEN"}])
    with pytest.raises(runner_lib.Stop, match=r"more than one open release PR for v0.9.42 \(#1, #2\)"):
        runner_lib.release_pr("v0.9.42")


def test_a_failed_merge_with_only_a_closed_pr_stops_instead_of_crashing(monkeypatch):
    prs(monkeypatch, [{"number": 1, "state": "OPEN"}])
    monkeypatch.setattr(runner_lib, "pending_waiver", lambda v: None)
    monkeypatch.setattr(runner_lib.hzlib, "load", lambda name: type("T", (), {"train": staticmethod(lambda *a, **k: (False, "red"))}))
    calls = iter([{"number": 1, "state": "OPEN"}, None])
    monkeypatch.setattr(runner_lib, "release_pr", lambda tag: next(calls))
    with pytest.raises(runner_lib.Stop, match="not merged: red"):
        runner_lib.merge_release_pr("v0.9.42")


# ----------------------------------------------------------------------------- drop resets the cut state
def test_reset_archives_the_cut_state_and_a_later_cut_starts_fresh():
    from steuermaschine.config import Project
    from steuermaschine.native import NativeRuntime, using

    providers = fake_providers()
    state = {"key": "0.9.42", "steps": {"admit prepare": {"status": "done"}}, "inputs": {"packet": "/p"}}
    providers.store.save(release.CUT_KIND, "0.9.42", state)
    project = Project(name="t", plugin="heinzel", root=Path("/"), plugins=["heinzel"], allow_unreviewed=True)
    with using(NativeRuntime(providers, {}, project)):
        where = release.reset_cut_state("0.9.42")
        assert release.reset_cut_state("0.9.42") == ""  # nothing left to archive
    assert providers.store.load(release.CUT_KIND, "0.9.42") is None
    key = where.removeprefix(release.CUT_ARCHIVE_KIND + "/")
    assert where.startswith(release.CUT_ARCHIVE_KIND + "/0.9.42-") and providers.store.load(release.CUT_ARCHIVE_KIND, key) == state
    assert json.dumps(providers.store.load(release.CUT_ARCHIVE_KIND, key))


# ----------------------------------------------------------------------------- the packet step of the pipeline
def test_the_packet_step_builds_the_reservation_next_to_the_pair_packet(tmp_path, monkeypatch):
    from test_pipeline_cov_release_cut import context, runtime

    from steuermaschine.testing.fakes import FakeVcs
    from steuermaschine_heinzel.pipelines import release_cut

    monkeypatch.setattr(release_cut, "changes_since_release", lambda core, head: (RELEASE["base"], ()))  # no CI change

    rt = runtime(tmp_path)
    root = tmp_path / "review"
    root.mkdir()
    for name in ("pair-packet.json", "commit-generated.log", "RELEASE-REVIEW.md"):
        shutil.copy(FIX / name, root / name)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    prek = tmp_path / "prek"
    prek.write_bytes(b"x")
    for name in release_packet.HOOKS:
        (hooks / name).write_text(f'PREK="{prek}"\n')
    cmd = FakeCommands()
    head = RELEASE["head"]
    cmd.on(["git", "-C"], lambda argv: Result(0, {"rev-list": f"{head} {RELEASE['base']}", "rev-parse": ""}[argv[3]] if argv[3] == "rev-list" else (
        f"{hooks}/{argv[-1].split('/')[-1]}" if "--git-path" in argv else RELEASE["tree"] if argv[-1].endswith("^{tree}") else "cd" * 20)))
    rt.providers.commands = cmd
    rt.providers.vcs = FakeVcs(head=head, base=RELEASE["base"])
    ctx = context(rt, {"packet": str(root / "pair-packet.json"), "clone": str(tmp_path / "clone"), "review_root": str(root), "commit_log": "commit-generated.log"})
    ctx.subject.facts["version"] = "0.9.42"
    detail = release_packet.packet(ctx)
    built = json.loads((root / release_packet.PACKET_NAME).read_text())
    assert built["mode"] == release_packet.MODE and built["release"]["head"] == head and built["release"]["version"] == "0.9.42"
    assert built["review"]["path"] == "RELEASE-REVIEW.md" and built["review"]["changelog_blob"] == "cd" * 20 and "built release-reservation-packet.json" in detail
    assert release_packet.reservation(ctx) == root / release_packet.PACKET_NAME and release_packet._packet_head(ctx) == head
    (root / release_packet.PACKET_NAME).rename(root / "given.json")
    ctx.inputs["packet"] = str(root / "given.json")  # an already built v1 packet is used as it is
    assert release_packet.reservation(ctx) == root / "given.json" and "used as it is" in release_packet.packet(ctx)


# ----------------------------------------------------------------------------- the drop holds the cut's lock
def test_a_drop_while_the_cut_holds_its_lock_waits_and_drops_nothing(tmp_path, monkeypatch):
    from test_native_release_writes import FakeGh, invoke, make, statedir

    gh = FakeGh()
    rt = make(tmp_path, monkeypatch, gh)
    (statedir.root() / "release.json").write_text(json.dumps({"version": "0.9.42", "freeze_token": "b" * 32}))
    before = (statedir.root() / "release.json").read_bytes()
    cut = {"key": "0.9.42", "steps": {"admit prepare": {"status": "done"}}}
    rt.providers.store.save(release.CUT_KIND, "0.9.42", cut)
    with rt.providers.store.lock("release.cut.0.9.42"):  # an advancing `sm release cut 0.9.42`
        out = invoke(rt, ["release", "drop", "0.9.42", "--reason", "candidate red twice; cut again after the fix"])
    assert out.exit_code == 75 and "nothing was dropped" in out.stderr + out.stdout
    assert gh.calls == [] and (statedir.root() / "release.json").read_bytes() == before
    assert rt.providers.store.load(release.CUT_KIND, "0.9.42") == cut and rt.providers.store.keys(release.CUT_ARCHIVE_KIND) == []
