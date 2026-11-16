"""Independent clones and explicit full CI must not masquerade as focused admission."""

import json

import policy_kit as kit
import pytest
from test_pipeline_cov_release_cut import _git, context, native, runtime

from steuermaschine.engine import Refuse
from steuermaschine.hashing import canonical_sha256
from steuermaschine_heinzel.native.admission_lib import focused_slots
from steuermaschine_heinzel.native.queue_lib import core_policy
from steuermaschine_heinzel.pipelines import common, release_cut


@pytest.fixture
def full(tmp_path, monkeypatch):
    # Reproduce the direct-full shape: one reader in CI, one in its judge. The focused
    # contract still requires two CI readers; the full route must not weaken it.
    readers = "\n".join(f'{name}: ${{{{ vars.{name} }}}}' for name in focused_slots.NAMES).encode()
    clone, base = kit.git_repo(tmp_path / "release", {
        kit.PATH: json.dumps(kit.manifest()).encode(), kit.WORKFLOW: readers, kit.JUDGE: readers,
    })
    _git(clone, "commit", "-q", "--allow-empty", "-m", "release")
    head = _git(clone, "rev-parse", "HEAD")
    tree = _git(clone, "rev-parse", "HEAD^{tree}")
    path = tmp_path / "packet.json"
    (tmp_path / "RELEASE-REVIEW.md").write_bytes(b"independent source review")
    path.write_text(json.dumps({"schema": 1, "mode": release_cut.MODE,
                               "review": {"path": "RELEASE-REVIEW.md", "sha256": release_cut.sha(b"independent source review")},
                               "release": {"version": "0.9.41", "head": head, "base": base, "tree": tree}}))
    rt = runtime(tmp_path)
    rt.providers.vcs.remote["main"] = base
    monkeypatch.setattr(rt.providers.vcs, "head", lambda worktree: head)
    monkeypatch.setattr(release_cut, "changes_since_release", lambda core, head: (base, ()))
    ctx = context(rt, {"packet": str(path), "clone": str(clone), "review_root": str(tmp_path),
                       "main_ci": "full", "intro": "Release changes."})
    ctx.subject.facts["main"] = base
    yield rt, ctx, clone, base, head
    common.OVERRIDE.clear()


def test_ci_change_reads_release_clone_not_unfetched_default(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    clone, _base = kit.git_repo(tmp_path / "release", {kit.PATH: json.dumps(kit.manifest()).encode(), "README.md": b"old"})
    _git(clone, "remote", "add", "origin", str(remote))
    _git(clone, "tag", "v0.9.40")
    _git(clone, "push", "-q", "origin", "HEAD:refs/heads/main", "refs/tags/v0.9.40")
    default = tmp_path / "default"
    _git(tmp_path, "clone", "-q", str(remote), str(default))
    (clone / "README.md").write_text("release")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-qm", "release")
    head = _git(clone, "rev-parse", "HEAD")
    with pytest.raises(core_policy.CandidateError):
        core_policy.git_read(default, "cat-file", "-e", head)
    rt = runtime(tmp_path)
    monkeypatch.setattr(rt.providers.vcs, "head", lambda worktree: head)
    ctx = context(rt, {"clone": str(clone)})
    ctx.subject.facts["core"] = str(default)
    with native(rt):
        assert "no CI path changed" in release_cut.ci_change(ctx)
    assert ctx.data["ci_change_head"] == head


def test_full_route_keeps_reader_refusal_and_failed_prepare(full, monkeypatch):
    rt, ctx, clone, base, head = full
    monkeypatch.setattr(focused_slots.a, "_run", lambda argv, cwd: core_policy.git_read(cwd, *argv[1:]))
    with pytest.raises(focused_slots.a.AdmissionError, match="trusted main lacks all registry reader injections"):
        focused_slots.reader_contract(clone, base)
    failed = clone.parent / "prepare.out"
    failed.write_text("original failed admission")
    common.OVERRIDE["release admit prepare"] = lambda **kw: pytest.fail("full mode wrote focused admission")
    common.OVERRIDE["release admit apply"] = lambda **kw: pytest.fail("full mode applied focused admission")
    with native(rt):
        assert "no focused registry" in release_cut.admit_prepare(ctx)
        digest, brief, path = release_cut.admission_review(ctx)
        plan = json.loads(release_cut.prepared(ctx).read_text())
        assert plan["head"] == head and plan["registry_write"] is False
        assert "No registry admission" in brief and path.endswith("full-ci-plan.json")
        with pytest.raises(Refuse, match="lacks approval"):
            release_cut.admit_apply(ctx)
        rt.providers.store.put_receipt("verdicts", digest, {"verdict": "approve", "reviewer": "independent"})
        assert "no focused admission applied" in release_cut.admit_apply(ctx)
        assert ctx.data["receipt"] == "" and failed.read_text() == "original failed admission"
        assert release_cut.prepare_probe(ctx) is None and release_cut.apply_probe(ctx) is None


@pytest.mark.parametrize("change", ["packet", "main", "tree", "parent", "version", "review"])
def test_full_plan_rechecks_bound_evidence(full, change):
    rt, ctx, _clone, _base, head = full
    with native(rt):
        release_cut.admit_prepare(ctx)
        digest, _, _ = release_cut.admission_review(ctx)
        rt.providers.store.put_receipt("verdicts", digest, {"verdict": "approve", "reviewer": "independent"})
        path = release_cut.reservation(ctx)
        if change == "main":
            rt.providers.vcs.remote["main"] = "a" * 40
        elif change == "packet":
            path.write_text(path.read_text() + "\n")
        elif change == "review":
            (path.parent / "RELEASE-REVIEW.md").write_text("changed review")
        elif change == "tree":
            doc = json.loads(path.read_text())
            doc["release"]["tree"] = "a" * 40
            path.write_text(json.dumps(doc))
        else:
            ctx.subject.facts["main"] = head
            rt.providers.vcs.remote["main"] = head
            doc = json.loads(path.read_text())
            doc["release"]["base"] = head
            if change == "version":
                doc["release"]["version"] = "wrong"
            path.write_text(json.dumps(doc))
        with pytest.raises(Refuse):
            release_cut.admit_apply(ctx)


def test_modes_cannot_change_or_adopt_old_receipts(full):
    _rt, ctx, *_ = full
    assert release_cut.main_ci(ctx) == "full"
    ctx.inputs["main_ci"] = "focused"
    with pytest.raises(Refuse, match="route changed"):
        release_cut.main_ci(ctx)
    ctx.data = {"receipt": "old"}
    ctx.inputs["main_ci"] = "full"
    with pytest.raises(Refuse, match="cannot be converted"):
        release_cut.main_ci(ctx)
    ctx.inputs["main_ci"] = "typo"
    with pytest.raises(Refuse, match="must be focused or full"):
        release_cut.main_ci(ctx)


def test_full_publish_plan_binds_route_and_resume_refuses_wrong_clone(full):
    rt, ctx, *_ = full
    with native(rt):
        release_cut.admit_prepare(ctx)
        digest, _, _ = release_cut.publish_review(ctx)
        assert canonical_sha256(json.dumps(ctx.data["publish_plan"])) == digest
        assert ctx.data["publish_plan"]["full_ci"]["candidate"] == "full-first-attempt"
        with pytest.raises(Refuse, match="release_dir to match"):
            release_cut.resume(ctx)


def test_full_route_rejects_unreadable_exact_source_policy(full, monkeypatch):
    rt, ctx, clone, _base, _head = full
    (clone / kit.PATH).write_text("not JSON")
    _git(clone, "add", kit.PATH)
    _git(clone, "commit", "-q", "--amend", "-m", "invalid policy")
    head = _git(clone, "rev-parse", "HEAD")
    monkeypatch.setattr(rt.providers.vcs, "head", lambda worktree: head)
    path = release_cut.reservation(ctx)
    doc = json.loads(path.read_text())
    doc["release"].update(head=head, tree=_git(clone, "rev-parse", "HEAD^{tree}"))
    path.write_text(json.dumps(doc))
    with native(rt), pytest.raises(Refuse, match=r"release policy.*not valid JSON"):
        release_cut.admit_prepare(ctx)


def test_full_route_resumes_through_existing_native_gate(full):
    from steuermaschine_heinzel.native.release import ReleaseRun

    rt, ctx, clone, *_ = full
    rt.settings["release_dir"] = clone
    calls = []

    def resume(**kwargs):
        calls.append(kwargs)
        return ReleaseRun(action="resume", version=kwargs["version"], exit=1, stopped="full candidate refused")

    common.OVERRIDE["release resume"] = resume
    with native(rt):
        release_cut.admit_prepare(ctx)
        digest, _, _ = release_cut.admission_review(ctx)
        rt.providers.store.put_receipt("verdicts", digest, {"verdict": "approve", "reviewer": "independent"})
        release_cut.admit_apply(ctx)
        digest, _, _ = release_cut.publish_review(ctx)
        rt.providers.store.put_receipt("verdicts", digest, {"verdict": "approve", "reviewer": "independent"})
        with pytest.raises(Refuse, match="full candidate refused"):
            release_cut.resume(ctx)
    assert calls == [{"version": "0.9.41", "intro": "Release changes.", "snapshot_acceptance": True, "candidate_gate": False}]
