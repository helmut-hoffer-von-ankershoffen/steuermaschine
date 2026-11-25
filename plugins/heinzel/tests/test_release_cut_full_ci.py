"""Independent clones and explicit full CI must not masquerade as focused admission."""

import json

import policy_kit as kit
import pytest
from test_pipeline_cov_release_cut import _git, context, native, runtime

from steuermaschine.engine import Refuse
from steuermaschine.hashing import canonical_sha256
from steuermaschine_heinzel.native.admission_lib import focused_slots
from steuermaschine_heinzel.native.queue_lib import core_policy
from steuermaschine_heinzel.native.release_admit_lib import release_scope
from steuermaschine_heinzel.pipelines import common, full_ci, release_cut


def direct_workflow():
    """Runnable selection graph; workload bodies are fixtures, never execution proof."""
    jobs = {f"job-{index}": {"name": name, "runs-on": "ubuntu-latest", "steps": [{"run": "true"}]}
            for index, (name, result) in enumerate(kit.expected_jobs("pull_request").items()) if result == "success"}
    jobs = {key: job for key, job in jobs.items() if job["name"] not in ("Changed paths", "Main batch admission")}
    jobs["admission"] = {"name": "Main batch admission", "runs-on": "ubuntu-latest",
                         "outputs": {"full": "${{ steps.batch.outputs.full }}"},
                         "steps": [{"id": "batch", "run": "uv run --no-build --script scripts/main_batch.py"}]}
    jobs["changes"] = {
        "name": "Changed paths", "runs-on": "ubuntu-latest", "needs": "admission",
        "if": "github.event_name != 'push' || needs.admission.outputs.full != 'false'",
        "outputs": {"full_ci": "${{ steps.full.outputs.selected }}", "focused_mode": "", "focused_base": "",
                    "focused_matrix": '{"include":[]}'},
        "steps": [{"id": "full", "if": "github.event_name != 'schedule'", "run": 'echo \'selected=true\' >> "$GITHUB_OUTPUT"'}],
    }
    jobs["workload"] = {"needs": "changes", "if": "needs.changes.outputs.full_ci == 'true'",
                        "runs-on": "ubuntu-latest", "steps": [{"run": "true"}]}
    return {"on": {"push": {"branches": ["main"]}, "pull_request": None},
            "env": {name: f"${{{{ vars.{name} }}}}" for name in focused_slots.NAMES}, "jobs": jobs}


@pytest.fixture
def full(tmp_path, monkeypatch, request):
    # Reproduce the direct-full shape: one reader in CI, one in its judge. The focused
    # contract still requires two CI readers; the full route must not weaken it.
    readers = "\n".join(f'{name}: ${{{{ vars.{name} }}}}' for name in focused_slots.NAMES).encode()
    old, new = "0.9.40", "0.9.41"
    metadata = {
        "pyproject.toml": f'[project]\nname="heinzel"\nversion="{old}"\n[tool.bumpversion]\ncurrent_version="{old}"\n',
        "uv.lock": f'[[package]]\nname="heinzel"\nversion="{old}"\n',
        release_scope.PLUGIN: json.dumps({"version": old, "name": "heinzel"}),
        "copier.yml": f'heinzel_version:\n  type: str\n  default: "{old}" # bumped by the release process\n  when: false\n',
        release_scope.ACCEPTANCE: f'FOREGROUND_UI = {{\n    "{old}": ("ui", "serve"),\n}}\n',
        "CHANGELOG.md": "# Changelog\n",
        "Makefile": "test-parallel:\n\tpytest tests\n",
    }
    workflow = direct_workflow()
    target = next(job for job in workflow["jobs"].values() if job.get("name") == "Coverage")
    if getattr(request, "param", None) == "disabled":
        target["if"] = "false"
    elif getattr(request, "param", None) == "empty-matrix":
        target["strategy"] = {"matrix": {"include": []}}
    clone, base = kit.git_repo(tmp_path / "release", {
        kit.PATH: json.dumps(kit.manifest()).encode(), kit.WORKFLOW: json.dumps(workflow).encode(), kit.JUDGE: readers,
        kit.RECORDER: kit.recorder_bytes(), "scripts/main_batch.py": (kit.FIXTURES / "main_batch_direct_full.py").read_bytes(),
    }, {path: value.encode() for path, value in metadata.items()})
    _git(clone, "remote", "add", "origin", f"https://github.com/{kit.REPOSITORY}.git")
    for path, value in metadata.items():
        if path == release_scope.ACCEPTANCE:
            value = value.replace("}\n", f'    "{new}": ("ui", "serve"),\n}}\n')
        elif path == "CHANGELOG.md":
            value += f"\n## {new}\n"
        else:
            value = value.replace(old, new)
        (clone / path).write_text(value)
    _git(clone, "commit", "-qam", "release")
    head = _git(clone, "rev-parse", "HEAD")
    tree = _git(clone, "rev-parse", "HEAD^{tree}")
    path = tmp_path / "packet.json"
    (tmp_path / "RELEASE-REVIEW.md").write_bytes(b"independent source review")
    path.write_text(json.dumps({"schema": 1, "mode": release_cut.MODE,
                               "review": {"path": "RELEASE-REVIEW.md", "sha256": release_cut.sha(b"independent source review")},
                               "release": {"version": "0.9.41", "head": head, "base": base, "tree": tree}}))
    rt = runtime(tmp_path)
    rt.settings["repo"] = kit.REPOSITORY
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
    git_read = core_policy.git_read
    monkeypatch.setattr(core_policy, "git_read", lambda repo, *args, **kw:
                        b"https://github.com/o/core.git\n" if args == ("remote", "get-url", "origin") else git_read(repo, *args, **kw))
    ctx = context(rt, {"clone": str(clone)})
    ctx.subject.facts["core"] = str(default)
    with native(rt):
        assert "no CI path changed" in release_cut.ci_change(ctx)
    assert ctx.data["ci_change_head"] == head


@pytest.mark.parametrize("cached", [False, True])
def test_same_main_fork_origin_cannot_supply_a_different_tag_map(full, monkeypatch, cached):
    rt, ctx, clone, _base, head = full
    if cached:
        with native(rt):
            release_cut.ci_change(ctx)
    _git(clone, "remote", "set-url", "origin", "https://github.com/fork/core.git")
    _git(clone, "tag", "v99.0.0", head)
    monkeypatch.setattr(release_cut, "changes_since_release", lambda *args: pytest.fail("read fork's remote tags"))
    with native(rt), pytest.raises(Refuse, match="not the official Core"):
        release_cut.ci_change(ctx)


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
        assert "validation requested" in release_cut.admit_prepare(ctx)
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
        assert ctx.data["publish_plan"]["full_ci"]["source_review"]["validation_requested"] == "full-first-attempt"
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


def test_full_route_resumes_through_existing_native_gate(full, monkeypatch):
    from types import SimpleNamespace

    from steuermaschine_heinzel.native import release
    from steuermaschine_heinzel.native.release import ReleaseRun

    rt, ctx, clone, *_ = full
    rt.settings["release_dir"] = clone
    monkeypatch.setattr(release, "runner", lambda: SimpleNamespace(pending_waiver=lambda version: None))
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




@pytest.mark.parametrize("path", [kit.WORKFLOW, kit.RECORDER, kit.JUDGE, "scripts/main_batch.py"])
def test_release_head_cannot_replace_protected_execution_sources(full, path):
    _rt, _ctx, clone, base, _head = full
    target = clone / path
    target.write_bytes(target.read_bytes() + b"\n# changed execution source\n")
    _git(clone, "add", path)
    _git(clone, "commit", "-q", "--amend", "-m", "changed source")
    with pytest.raises(Refuse, match="changes protected workflow"):
        full_ci.source_request(str(clone), base, _git(clone, "rev-parse", "HEAD"), "0.9.41")


@pytest.mark.parametrize(("rename", "error"), [(False, "unsupported path"), (True, "modify existing regular files")])
def test_hosted_registration_refuses_a_neutered_or_renamed_make_target(full, monkeypatch, rename, error):
    from steuermaschine_heinzel.native import release_read
    from steuermaschine_heinzel.native.release_lib import exact_queue

    rt, _ctx, clone, base, _head = full
    if rename:
        _git(clone, "mv", "Makefile", "other.txt")
    else:
        (clone / "Makefile").write_text("test-parallel:\n\ttrue\n")
        _git(clone, "add", "Makefile")
    _git(clone, "commit", "-q", "--amend", "-m", "different workload")
    head, tree = _git(clone, "rev-parse", "HEAD"), _git(clone, "rev-parse", "HEAD^{tree}")
    # A local replacement would otherwise make git read the original harmless tree under the untrusted head SHA.
    _git(clone, "replace", head, _head)
    proof = release_read.CandidateProof(pr=12, head=head, base=base, tree=tree, run_id=99, run_attempt=1, job_count=12)
    monkeypatch.setattr(release_read, "release_candidate_proof", lambda *args: proof)
    monkeypatch.setattr(release_read, "release_dir", lambda: clone)
    monkeypatch.setattr(exact_queue, "source_producers", lambda *args: ("0.9.41", {"builder"}))
    with native(rt), pytest.raises(Refuse, match=error):
        release_read.prepare_protected(12, head, base, tree, "d" * 64)


def test_generated_release_validation_preserves_transitive_execution_inputs(full):
    _rt, _ctx, clone, base, _head = full
    (clone / "unexpected.py").write_text("print('changed execution input')\n")
    _git(clone, "add", "unexpected.py")
    _git(clone, "commit", "-q", "--amend", "-m", "unexpected source")
    with pytest.raises(Refuse, match="release diff must modify existing regular files"):
        full_ci.source_request(str(clone), base, _git(clone, "rev-parse", "HEAD"), "0.9.41")


@pytest.mark.parametrize("full", ["disabled", "empty-matrix"], indirect=True)
def test_unreviewed_partial_workflow_is_only_a_request_and_cannot_apply(full):
    rt, ctx, clone, base, head = full
    with native(rt):
        release_cut.admit_prepare(ctx)
        digest, brief, _ = release_cut.admission_review(ctx)
        plan = json.loads(release_cut.prepared(ctx).read_text())
        source = plan["source_review"]
        assert plan["mode"] == "full-candidate-request/v1" and source["execution_evidence"] is None
        assert source["base"] == base and source["head"] == head
        assert source["source_sha256"][kit.WORKFLOW] == release_cut.sha(core_policy.git_read(clone, "show", f"{base}:{kit.WORKFLOW}"))
        assert "every required branch condition and matrix" in brief
        with pytest.raises(Refuse, match="lacks approval"):
            release_cut.admit_apply(ctx)
        rt.providers.store.put_receipt("verdicts", digest, {"verdict": "reject", "reviewer": "independent"})
        with pytest.raises(Refuse, match="lacks approval"):
            release_cut.admit_apply(ctx)


def test_full_request_refuses_pending_same_version_waiver(full, monkeypatch):
    from types import SimpleNamespace

    from steuermaschine_heinzel.native import release

    rt, ctx, clone, *_ = full
    rt.settings["release_dir"] = clone
    monkeypatch.setattr(release, "runner", lambda: SimpleNamespace(pending_waiver=lambda version: {"job": "any"}))
    common.OVERRIDE["release resume"] = lambda **kw: pytest.fail("started with pending waiver")
    with native(rt), pytest.raises(Refuse, match="pending same-version candidate waiver"):
        release_cut.resume(ctx)
