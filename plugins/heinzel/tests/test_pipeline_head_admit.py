"""`sm head admit` step by step: check, the blocking review of protected paths, PR and queue, called with an engine context.

The composed native commands are replaced through `pipelines.common.OVERRIDE` (each has its own tests)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.engine import DIGEST_KIND, Ctx, Refuse, Subject, Wait
from steuermaschine.github.testing import FakeGh
from steuermaschine.native import NativeRuntime, using
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers
from steuermaschine.testing.fakes import FakeVcs
from steuermaschine_heinzel.native.queue import QueueEntry, QueueList
from steuermaschine_heinzel.native.ship import HeadResult
from steuermaschine_heinzel.pipelines import common, head_admit
from steuermaschine_heinzel.plugin import plugin

HEAD = "e9c943810ea75cfad155a431a2f168b2a9559828"
OTHER = "f00d943810ea75cfad155a431a2f168b2a955982"
MAIN = "1947731c" + "0" * 32
BRANCH = "fix/update-all"


class BrokenRemote(FakeVcs):
    def remote_head(self, worktree: str, branch: str) -> str | None:
        raise RuntimeError("git ls-remote: could not resolve host")


@pytest.fixture(autouse=True)
def clean_overrides() -> Iterator[None]:
    common.OVERRIDE.clear()
    yield
    common.OVERRIDE.clear()


def runtime(tmp_path: Path, gh: FakeGh | None = None, vcs: FakeVcs | None = None) -> Runtime:
    stack = stack_of(plugin)
    raw = {"heinzel": {"legacy_state_dir": str(tmp_path / "state"), "core_repo_dir": str(tmp_path / "core"), "repo": "o/core"}}
    settings, _ = stack.validate(raw, tmp_path)
    if vcs is None:
        vcs = FakeVcs(head=HEAD, branch=BRANCH, base=MAIN)
        vcs.remote["main"] = MAIN
    project = Project(name="t", plugin="heinzel", root=tmp_path, plugins=["heinzel"], allow_unreviewed=True)
    return Runtime(project, stack, settings, _providers=fake_providers(commands=gh or FakeGh(), vcs=vcs))


def context(rt: Runtime, wt: Path, inputs: dict[str, Any] | None = None, data: dict[str, Any] | None = None) -> Ctx:
    subject = Subject(key=HEAD, label=f"{BRANCH} {HEAD[:9]}", fingerprint={"head": HEAD},
                      facts={"worktree": str(wt), "branch": BRANCH, "head": HEAD, "base": MAIN, "main": MAIN})
    return Ctx(providers=rt.providers, settings={"repo": "o/core"}, subject=subject, inputs=dict(inputs or {}), data=dict(data or {}),
               live=True, author="test", say=lambda _text: None)


def native(rt: Runtime) -> using:
    return using(NativeRuntime(rt.providers, rt.settings, rt.project))




# ----------------------------------------------------------------------------- PR
def test_the_pr_probe_finds_only_the_pr_open_at_this_exact_head(tmp_path):
    rows = [{"number": 700, "headRefOid": OTHER}, {"number": 812, "headRefOid": HEAD}]
    gh = FakeGh().on(f"--head {BRANCH}", [{"number": 700, "headRefOid": OTHER}], rows)
    ctx = context(runtime(tmp_path, gh), tmp_path / "wt")
    assert head_admit.pr_probe(ctx) is None and "pr" not in ctx.data  # only an older head of the branch is open
    assert head_admit.pr_probe(ctx) == "PR #812 is open at this head" and ctx.data["pr"] == 812


@pytest.mark.parametrize("exit_code, kind, text", [(75, Wait, "sm head pr: main CI is red; wait"), (1, Refuse, "sm head pr refused: push refused; hook failed")])
def test_a_head_pr_that_did_not_open_the_pr_waits_or_refuses(tmp_path, exit_code, kind, text):
    rt = runtime(tmp_path)
    lines = ["main CI is red", "wait"] if exit_code == 75 else ["push refused"]
    common.OVERRIDE["head pr"] = lambda **kw: HeadResult(action="pr", target=kw["bead"], verdict="wait" if exit_code == 75 else "failed", lines=lines,
                                                         errors=[] if exit_code == 75 else ["hook failed"], exit=exit_code)
    ctx = context(rt, tmp_path / "wt", {"bead": "hz-412", "title": "fix(web): update all"})
    with native(rt), pytest.raises(kind, match=text):
        head_admit.pr(ctx)
    assert "pr" not in ctx.data


def test_head_pr_flagged_by_the_diff_guard_is_open_but_not_queued(tmp_path):
    gh = FakeGh().on(f"--head {BRANCH}", [{"number": 812, "headRefOid": HEAD}])
    rt = runtime(tmp_path, gh)
    seen: dict[str, Any] = {}

    def pr(**kw: Any) -> HeadResult:
        seen.update(kw)
        return HeadResult(action="pr", target=kw["bead"], verdict="needs_review", lines=["flagged"], exit=0)

    common.OVERRIDE["head pr"] = pr
    body = tmp_path / "body.md"
    ctx = context(rt, tmp_path / "wt", {"bead": "hz-412", "title": "fix(web): update all", "body_file": str(body)})
    with native(rt):
        assert head_admit.pr(ctx) == "PR #812 open; the diff guard flagged it"
    assert ctx.data["guard"] == "needs_review" and seen["body_file"] == body and seen["reason"] is None and seen["live"] is True


def test_head_pr_needs_bead_and_title(tmp_path):
    rt = runtime(tmp_path)
    with native(rt), pytest.raises(Refuse, match="the input 'title' is missing"):
        head_admit.pr(context(rt, tmp_path / "wt", {"bead": "hz-412"}))


# ----------------------------------------------------------------------------- queue
def test_the_queue_step_needs_a_pr_and_a_review_of_a_flagged_one(tmp_path):
    rt = runtime(tmp_path)
    added: list[dict[str, Any]] = []
    common.OVERRIDE["queue add"] = lambda **kw: added.append(kw) or HeadResult(action="add", target="812", verdict="done")
    no_pr = context(rt, tmp_path / "wt")
    assert head_admit.queue_probe(no_pr) is None
    with native(rt), pytest.raises(Refuse, match="no PR is known for this head") as no:
        head_admit.queue(no_pr)
    assert no.value.next_cmd == f"sm head admit {tmp_path / 'wt'}"

    flagged = context(rt, tmp_path / "wt", data={"pr": 812, "guard": "needs_review"})
    with native(rt), pytest.raises(Refuse, match="the diff guard flagged PR #812") as no:
        head_admit.queue(flagged)
    assert no.value.next_cmd == "sm head guard 812" and added == []

    reviewed = context(rt, tmp_path / "wt", {"reviewed": "review: /r/REVIEW.md", "bead": "hz-412"}, {"pr": 812, "guard": "needs_review"})
    with native(rt):
        assert head_admit.queue(reviewed) == f"PR #812 queued at {HEAD[:12]}"
    assert added == [{"pr": ["812"], "bead": "hz-412", "reviewed": "review: /r/REVIEW.md", "reviewed_head": HEAD}]


def test_the_queue_probe_adopts_a_pr_already_in_the_queue(tmp_path):
    rt = runtime(tmp_path)
    common.OVERRIDE["queue list"] = lambda **kw: QueueList(entries=[QueueEntry(key="core#700", repo="core", pr="700", status="queued"),
                                                                    QueueEntry(key="core#812", repo="core", pr="812", status="merging")])
    with native(rt):
        assert head_admit.queue_probe(context(rt, tmp_path / "wt", data={"pr": 812})) == "PR #812 is in the queue (merging)"
        assert head_admit.queue_probe(context(rt, tmp_path / "wt", data={"pr": 900})) is None


# ----------------------------------------------------------------------------- identify and check
def test_identify_binds_the_head_alone(tmp_path):
    (tmp_path / "wt").mkdir()
    subject = head_admit.identify(fake_providers(vcs=FakeVcs(head=HEAD, branch=BRANCH, base=MAIN)), {}, str(tmp_path / "wt"))
    assert subject.fingerprint == {"head": HEAD}


def test_check_adopts_a_green_receipt_and_refuses_a_red_run(tmp_path):
    rt = runtime(tmp_path)
    ctx = context(rt, tmp_path / "wt")
    assert head_admit.check_probe(ctx) is None
    common.OVERRIDE["check"] = lambda **kw: HeadResult(action="check", target="wt", verdict="failed", lines=["ruff: E501"], exit=1)
    with native(rt), pytest.raises(Refuse, match="NOT READY: ruff: E501"):
        head_admit.check(ctx)
    common.OVERRIDE["check"] = lambda **kw: HeadResult(action="check", target="wt", verdict="ok", exit=0)
    with native(rt):
        assert head_admit.check(ctx) == f"READY at {HEAD[:12]}"


# ----------------------------------------------------------------------------- the blocking review of protected paths
class DiffCommands(FakeGh):
    """Answers the two git diff calls of the protected review."""

    def __init__(self, names: list[str], pyproject: str = ""):
        super().__init__()
        self.names, self.pyproject = names, pyproject

    def run(self, argv, **kw):
        from types import SimpleNamespace

        if argv[0] == "git" and "--name-only" in argv:
            return SimpleNamespace(rc=0, out="\0".join(self.names) + "\0", err="")
        if argv[0] == "git" and "pyproject.toml" in argv:
            return SimpleNamespace(rc=0, out=self.pyproject, err="")
        return super().run(argv, **kw)


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "scripts/release.sh", "scripts/publish_pypi.py", "scripts/acceptance_run.py",
                                  "quarantine.toml", "conftest.py", "tests/conftest.py", ".coveragerc"])
def test_a_protected_path_stops_for_a_digest_of_exactly_these_paths(tmp_path, path):
    rt = runtime(tmp_path, DiffCommands(["src/a.py", path]))
    ctx = context(rt, tmp_path / "wt")
    with native(rt):
        digest, brief, under = head_admit.protected_review(ctx)
    assert len(digest) == 64 and path in brief and Path(under).read_text().count(path) == 1 and "src/a.py" not in Path(under).read_text()
    assert rt.providers.store.load(DIGEST_KIND, digest)["check"]["head"] == HEAD  # an approval needs the head's green check receipt


def test_an_ordinary_head_has_no_stop_and_a_new_head_changes_the_digest(tmp_path):
    rt = runtime(tmp_path, DiffCommands(["src/a.py", "docs/x.md"]))
    with native(rt):
        assert head_admit.protected_review(context(rt, tmp_path / "wt")) == ("", "", "")
    rt = runtime(tmp_path, DiffCommands(["conftest.py"]))
    with native(rt):
        first = head_admit.protected_review(context(rt, tmp_path / "wt"))[0]
        other = context(rt, tmp_path / "wt")
        other.subject.key = OTHER
        assert head_admit.protected_review(other)[0] != first


def test_pyproject_counts_only_when_its_diff_names_coverage(tmp_path):
    with native(runtime(tmp_path, DiffCommands(["pyproject.toml"], "+version = 2"))) as _:
        rt = runtime(tmp_path, DiffCommands(["pyproject.toml"], "+version = 2"))
        assert head_admit.protected_changes(context(rt, tmp_path / "wt")) == []
    rt = runtime(tmp_path, DiffCommands(["pyproject.toml"], "+[tool.coverage.report]"))
    assert head_admit.protected_changes(context(rt, tmp_path / "wt")) == ["pyproject.toml"]


def test_an_unreadable_diff_refuses_never_passes(tmp_path):
    class Broken(FakeGh):
        def run(self, argv, **kw):
            from types import SimpleNamespace

            return SimpleNamespace(rc=128, out="", err="bad revision") if argv[0] == "git" else super().run(argv, **kw)

    rt = runtime(tmp_path, Broken())
    with pytest.raises(Refuse, match="cannot read the changed paths"):
        head_admit.protected_changes(context(rt, tmp_path / "wt"))


def test_the_pipeline_has_no_admission_steps():
    names = [getattr(s, "name", "") for s in head_admit.pipeline().steps]
    assert names == ["check", "protected review", "pr", "queue"]
