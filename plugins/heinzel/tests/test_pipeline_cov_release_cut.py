"""`sm release cut` step by step, and the shared step helpers of `pipelines.common`: refusals, waits, adoptions.

The composed native commands are replaced through `pipelines.common.OVERRIDE` (each has its own tests); one helper
test runs a real read-only command (`head hash`) through the registry instead.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.engine import Ctx, Refuse, Subject, Wait
from steuermaschine.native import NativeRuntime, using
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers
from steuermaschine.testing.fakes import FakeVcs
from steuermaschine_heinzel.native import release
from steuermaschine_heinzel.native.admission_lib.ops import canonical_sha256
from steuermaschine_heinzel.native.release import ReleaseRun
from steuermaschine_heinzel.native.release_read import ReleaseVerify, SurfaceCheck
from steuermaschine_heinzel.native.ship import HeadResult
from steuermaschine_heinzel.pipelines import common, release_cut
from steuermaschine_heinzel.plugin import plugin

HEAD = "e9c943810ea75cfad155a431a2f168b2a9559828"
MAIN = "1947731c" + "0" * 32
OTHER_MAIN = "bb56886" + "1" * 33


class BrokenRemote(FakeVcs):
    def remote_head(self, worktree: str, branch: str) -> str | None:
        raise RuntimeError("git ls-remote: timed out")


@pytest.fixture(autouse=True)
def clean_overrides() -> Iterator[None]:
    common.OVERRIDE.clear()
    yield
    common.OVERRIDE.clear()


def runtime(tmp_path: Path) -> Runtime:
    stack = stack_of(plugin)
    raw = {"heinzel": {"legacy_state_dir": str(tmp_path / "state"), "core_repo_dir": str(tmp_path / "core"), "repo": "o/core"}}
    settings, _ = stack.validate(raw, tmp_path)
    vcs = FakeVcs(head=HEAD, base=MAIN)
    vcs.remote["main"] = MAIN
    project = Project(name="t", plugin="heinzel", root=tmp_path, plugins=["heinzel"], allow_unreviewed=True)
    return Runtime(project, stack, settings, _providers=fake_providers(vcs=vcs))


def context(rt: Runtime, inputs: dict[str, Any] | None = None, data: dict[str, Any] | None = None) -> Ctx:
    subject = Subject(key="0.9.41", label="v0.9.41", fingerprint={"main": MAIN}, facts={"version": "0.9.41", "main": MAIN, "core": "/core"})
    return Ctx(providers=rt.providers, settings={}, subject=subject, inputs=dict(inputs or {}), data=dict(data or {}), live=True,
               author="test", say=lambda _text: None)


def native(rt: Runtime) -> using:
    return using(NativeRuntime(rt.providers, rt.settings, rt.project))


# ----------------------------------------------------------------------------- identify
@pytest.mark.parametrize("version", ["0.9", "v0.9.41", "0.9.41rc1", ""])
def test_identify_refuses_what_is_not_a_version(version):
    with pytest.raises(Refuse, match=r"is not a version X.Y.Z") as no:
        release_cut.identify(fake_providers(), {"core_repo_dir": "/core"}, version)
    assert no.value.code == 2


def test_identify_refuses_without_a_core_repository():
    with pytest.raises(Refuse, match="no Core repository: set core_repo_dir") as no:
        release_cut.identify(fake_providers(), {}, "0.9.41")
    assert no.value.next_cmd == "sm self config" and no.value.code == 2


def test_identify_refuses_when_main_cannot_be_read_exactly_and_binds_main_otherwise(tmp_path):
    with pytest.raises(Refuse, match="cannot read main on the remote: git ls-remote: timed out"):
        release_cut.identify(fake_providers(vcs=BrokenRemote()), {"core_repo_dir": str(tmp_path)}, "0.9.41")
    vcs = FakeVcs()
    with pytest.raises(Refuse, match="a cut binds one main"):
        release_cut.identify(fake_providers(vcs=vcs), {"core_repo_dir": str(tmp_path)}, "0.9.41")
    vcs.remote["main"] = MAIN
    subject = release_cut.identify(fake_providers(vcs=vcs), {"core_repo_dir": str(tmp_path)}, "0.9.41")
    assert (subject.key, subject.label, subject.fingerprint) == ("0.9.41", "v0.9.41", {"main": MAIN})
    assert subject.facts == {"version": "0.9.41", "main": MAIN, "core": str(tmp_path)}


# ----------------------------------------------------------------------------- the CI-change gate
TAG = "7" * 40


def gate(rt, monkeypatch, changes, tmp_path):
    monkeypatch.setattr(release_cut, "changes_since_release", lambda core, head: (TAG, changes))
    return context(rt, {"clone": str(tmp_path / "clone")})


def record(rt, kind):
    from steuermaschine import verdicts

    data = {"digest": HEAD, "verdict": "approve", "reviewer": "reviewer-fable", "model": "m", "notes": "ci.yml read", "at": 0}
    rt.providers.store.put_receipt("verdicts", HEAD, data | ({"kind": kind} if kind else {}))
    return verdicts


def test_a_release_without_ci_changes_needs_no_ci_change_verdict(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    with native(rt):
        assert release_cut.ci_change(gate(rt, monkeypatch, (), tmp_path)) == f"no CI path changed since {TAG[:12]}: no ci_change verdict needed"


def test_a_ci_change_without_a_verdict_refuses_and_names_the_command(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    with native(rt), pytest.raises(Refuse, match=f"no verdict is recorded for {HEAD}") as no:
        release_cut.ci_change(gate(rt, monkeypatch, (".github/workflows/ci.yml: CI path",), tmp_path))
    assert no.value.next_cmd.startswith(f"sm review verdict {HEAD} --approve --kind ci_change --reviewer NAME --model M --notes")
    assert ".github/workflows/ci.yml" in no.value.next_cmd
    assert rt.providers.store.load("review.digests", HEAD)["sources"] == ["release ci_change"]  # reviewable now


def test_a_plain_verdict_does_not_cover_a_ci_change(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    record(rt, "")
    with native(rt), pytest.raises(Refuse, match="is not a ci_change verdict"):
        release_cut.ci_change(gate(rt, monkeypatch, ("scripts/x.py: CI path",), tmp_path))


def test_a_ci_change_verdict_on_the_release_commit_passes(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    record(rt, "ci_change")
    with native(rt):
        found = release_cut.ci_change(gate(rt, monkeypatch, ("scripts/x.py: CI path", "a.py: CI path"), tmp_path))
    assert found == f"2 CI change(s) since {TAG[:12]}: ci_change verdict for {HEAD[:12]} recorded"


def test_unreadable_rules_or_no_tag_count_as_a_ci_change(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    monkeypatch.setattr(release_cut, "_last_tag", lambda core, head: "")
    assert release_cut.changes_since_release(str(tmp_path / "nowhere"), HEAD) == ("", ("no published release tag before the release commit",))
    monkeypatch.setattr(release_cut, "_last_tag", lambda core, head: TAG)
    base, changes = release_cut.changes_since_release(str(tmp_path / "nowhere"), HEAD)
    assert base == TAG and changes[0].startswith(f"no reviewed release policy at base {TAG}")
    monkeypatch.undo()
    assert release_cut._last_tag(str(tmp_path / "nowhere"), HEAD) == ""
    with native(rt), pytest.raises(Refuse, match="no exact head"):
        ctx = context(rt, {"clone": str(tmp_path / "clone")})
        monkeypatch.setattr(rt.providers.vcs, "head", lambda worktree: "")
        release_cut.ci_change(ctx)


def test_the_last_tag_is_the_newest_release_tag_before_the_release_commit(tmp_path):
    import subprocess

    import policy_kit as kit

    repo, first = kit.git_repo(tmp_path / "core", {"a": b"1"})
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True, env=env | {"PATH": __import__("os").environ["PATH"]}).stdout.strip()  # noqa: E731
    git("tag", "v0.9.1", first)
    git("commit", "-q", "--allow-empty", "-m", "next")
    git("tag", "not-a-release")
    git("commit", "-q", "--allow-empty", "-m", "release")
    head = git("rev-parse", "HEAD")
    assert release_cut._last_tag(str(repo), head) == first


# ----------------------------------------------------------------------------- admission
def packet(tmp_path: Path, head: str = HEAD) -> Path:
    path = tmp_path / "review" / "release-0.9.41" / "packet.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mode": "release-main-only/v1", "release": {"head": head}}))
    return path


def test_a_prepared_admission_at_this_main_is_adopted(tmp_path):
    rt, path = runtime(tmp_path), packet(tmp_path)
    ctx = context(rt, {"packet": str(path)})
    assert release_cut.prepare_probe(ctx) is None  # nothing prepared
    other = {"head": HEAD, "main": OTHER_MAIN, "slot": "RELEASE_MAIN"}  # the transaction names its main; the arguments file does not
    (path.parent / "prepare.out").write_text(json.dumps(other))
    (path.parent / "prepare.args.json").write_text(json.dumps({"schema": 1, "sha256": canonical_sha256(other)}))
    assert release_cut.prepare_probe(ctx) is None  # prepared at another main
    doc = {"head": HEAD, "main": MAIN, "slot": "RELEASE_MAIN"}
    (path.parent / "prepare.out").write_text(json.dumps(doc))
    assert release_cut.prepare_probe(ctx) is None  # the arguments belong to the other transaction
    (path.parent / "prepare.args.json").write_text(json.dumps({"schema": 1, "sha256": canonical_sha256(doc)}))
    assert release_cut.prepare_probe(ctx) == f"prepared {canonical_sha256(doc)[:12]} at main {MAIN[:8]}"


def test_admit_prepare_passes_the_reviewed_nonconsumers(tmp_path):
    rt, path = runtime(tmp_path), packet(tmp_path)
    seen: dict[str, Any] = {}

    def prepare(**kw: Any) -> Any:
        seen.update(kw)
        return type("Prepared", (), {"sha256": "d" * 64, "exit_code": 0})()

    common.OVERRIDE["release admit prepare"] = prepare
    receipts = tmp_path / "stuck.json"
    ctx = context(rt, {"packet": str(path), "clone": str(tmp_path / "clone"), "review_root": str(tmp_path / "review"), "nonconsumers": str(receipts)})
    with native(rt):
        assert release_cut.admit_prepare(ctx) == f"prepared {'d' * 12}"
    assert seen == {"packet": path, "core_repo": tmp_path / "clone", "review_root": tmp_path / "review", "nonconsumers": receipts, "wait_": True}


def test_an_unreadable_packet_has_no_head_and_no_receipt_is_adopted(tmp_path):
    rt, path = runtime(tmp_path), packet(tmp_path)
    ctx = context(rt, {"packet": str(path)})
    with native(rt):
        assert release_cut.apply_probe(ctx) is None  # a head, but no receipt for it
    receipts = tmp_path / "state" / "focused-admission-receipts"
    receipts.mkdir(parents=True)
    (receipts / f"head-{HEAD}-1.json").write_text("{}")
    with native(rt):
        assert release_cut.apply_probe(ctx) == "a receipt for the release head exists"
    path.write_text("{ not json")
    assert release_cut._packet_head(ctx) == ""
    with native(rt):
        assert release_cut.apply_probe(ctx) is None


# ----------------------------------------------------------------------------- publish
def test_the_publish_review_refuses_without_the_intro(tmp_path):
    rt = runtime(tmp_path)
    ctx = context(rt, {"review_root": str(tmp_path / "review")})
    with pytest.raises(Refuse, match="the input 'intro' is missing") as no:
        release_cut.publish_review(ctx)
    assert no.value.code == 2 and not release_cut.plan_file(ctx).exists()


def test_the_publish_plan_names_the_resume_flags(tmp_path):
    rt = runtime(tmp_path)
    ctx = context(rt, {"review_root": str(tmp_path / "review"), "intro": "Faster ticks.", "snapshot": "no", "candidate_gate": "YES"})
    sha, brief, file = release_cut.publish_review(ctx)
    plan = json.loads(Path(file).read_text())
    assert plan["command"] == "sm release resume 0.9.41 --intro INTRO --candidate-gate"
    assert plan["snapshot_acceptance"] is False and plan["candidate_gate"] is True and sha == canonical_sha256(plan)
    assert file == str(tmp_path / "review" / "release-v0.9.41" / "cut-plan.json") and "v0.9.41" in brief


def test_a_finished_accepted_receipt_is_adopted_by_the_resume_probe(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    receipts = {"0.9.41": ("tail", {"version": "0.9.41", "finished": True, "accepted": False})}
    monkeypatch.setattr(release, "runner", lambda: None)
    monkeypatch.setattr(release, "receipt_of", lambda _module, version: receipts[version])
    assert release_cut.resume_probe(context(rt)) is None  # finished, but not accepted
    receipts["0.9.41"] = ("tail", {"version": "0.9.41", "finished": True, "accepted": True})
    assert release_cut.resume_probe(context(rt)) == "ACCEPTED (finished receipt)"


def approved(rt: Runtime, tmp_path: Path) -> Ctx:
    """A context whose publishing plan has an approving verdict: resume takes its inputs from that plan only."""
    ctx = context(rt, {"intro": "Faster ticks.", "review_root": str(tmp_path / "review")})
    sha, _brief, _file = release_cut.publish_review(ctx)
    rt.providers.store.put_receipt("verdicts", sha, {"digest": sha, "verdict": "approve", "reviewer": "reviewer-1"})
    return ctx


def test_resume_without_an_approved_plan_does_not_run(tmp_path):
    rt = runtime(tmp_path)
    common.OVERRIDE["release resume"] = lambda **kw: pytest.fail("resume ran without an approved plan")
    with native(rt), pytest.raises(Refuse, match="no approved publishing plan"):
        release_cut.resume(context(rt, {"intro": "Faster ticks."}))


def test_a_release_finished_not_accepted_is_refused_with_absorb(tmp_path):
    rt = runtime(tmp_path)
    common.OVERRIDE["release resume"] = lambda **kw: ReleaseRun(action="resume", version=kw["version"], exit=1, finished=True, accepted=False)
    ctx = approved(rt, tmp_path)
    with native(rt), pytest.raises(Refuse, match=r"v0.9.41 finished NOT ACCEPTED") as no:
        release_cut.resume(ctx)
    assert no.value.next_cmd.startswith("sm release resume 0.9.41 --absorb") and "stopped" not in ctx.data


def test_resume_stopped_without_a_reason_names_its_exit_code(tmp_path):
    rt = runtime(tmp_path)
    common.OVERRIDE["release resume"] = lambda **kw: ReleaseRun(action="resume", version=kw["version"], exit=3)
    ctx = approved(rt, tmp_path)
    with native(rt), pytest.raises(Refuse, match="resume stopped: exit 3"):
        release_cut.resume(ctx)
    assert ctx.data["stopped"] == "exit 3"
    common.OVERRIDE["release resume"] = lambda **kw: ReleaseRun(action="resume", version=kw["version"], exit=0, finished=True, accepted=True)
    with native(rt):
        assert release_cut.resume(ctx) == "ACCEPTED"


def test_surfaces_not_serving_yet_are_a_wait_not_a_refusal(tmp_path):
    rt = runtime(tmp_path)
    common.OVERRIDE["release verify"] = lambda **kw: HeadResult(action="verify", target=kw["version"], verdict="failed",
                                                                lines=["pypi heinzel 0.9.41 missing"], exit=1)
    with native(rt), pytest.raises(Wait, match=r"published surfaces not all serving yet: .*pypi heinzel 0.9.41 missing") as wait:
        release_cut.verify(context(rt))
    assert wait.value.waiting_on == "PyPI and the GitHub release"
    common.OVERRIDE["release verify"] = lambda **kw: ReleaseVerify(version=kw["version"], checks=[SurfaceCheck(passed=True, text="pypi")])
    with native(rt):
        assert release_cut.verify(context(rt)) == "1 surface checks pass"


# ----------------------------------------------------------------------------- common
def test_a_step_that_is_not_a_command_of_this_build_is_refused():
    with pytest.raises(Refuse, match="`sm head teleport` is not a command of this build") as no:
        common.command("head teleport")
    assert no.value.next_cmd == "sm self tree"


def test_run_without_an_override_calls_the_registered_command(tmp_path):
    rt = runtime(tmp_path)
    doc = {"head": HEAD, "main": MAIN}
    prepared = tmp_path / "prepare.out"
    prepared.write_text(json.dumps(doc))
    with native(rt):
        result = common.run("head hash", prepared=prepared)
    assert result.sha256 == canonical_sha256(doc) == common.prepared_digest(prepared)
    assert common.lines(result) == "; ".join(x for x in result.render() if x.strip())[:300] and canonical_sha256(doc) in common.lines(result)
    assert common.lines(object()) == ""


@pytest.mark.parametrize("exit_code, lines, kind, text, code", [
    (75, ["the run lock is held"], Wait, "sm release notes-audit: the run lock is held", None),
    (75, [], Wait, "sm release notes-audit: waiting", None),
    (2, ["  ", "no such ref"], Refuse, "sm release notes-audit exit 2: no such ref", 2),
    (4, [], Refuse, "sm release notes-audit exit 4: no detail", 4),
])
def test_ok_turns_an_exit_code_into_a_wait_or_a_refusal(tmp_path, exit_code, lines, kind, text, code):
    rt = runtime(tmp_path)
    common.OVERRIDE["release notes-audit"] = lambda **kw: HeadResult(action="notes", target="x", verdict="failed", lines=lines, exit=exit_code)
    with native(rt), pytest.raises(kind, match=text) as caught:
        common.ok("release notes-audit", base="origin/main")
    if code is not None:
        assert caught.value.code == code


def test_slug_keeps_a_safe_name():
    assert common.slug("fix/update all!") == "fix-update-all" and common.slug("///") == "head"
