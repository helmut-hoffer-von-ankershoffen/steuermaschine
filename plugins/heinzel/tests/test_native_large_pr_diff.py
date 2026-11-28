"""A large PR is classified only from exact Git objects and stable forge identities."""

import copy
import subprocess

import pytest

from steuermaschine_heinzel.native.queue_lib import diff_guard as guard

REPO = guard.hzlib.CORE
LIMIT = "could not find pull request diff: HTTP 406: Sorry, the diff exceeded the maximum number of files (300).\nPullRequest.diff too_large\n"


@pytest.fixture
def exact_diff(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", *args], text=True)

    git("init", "-q")
    git("remote", "add", "origin", f"https://github.com/{REPO}.git")
    text = tmp_path / "test_gate.py"
    text.write_text("import pytest\n" + "def example():\n    return True\n" * 3000)
    git("add", ".")
    git("commit", "-qm", "base")
    merge_base = git("rev-parse", "HEAD").strip()
    git("checkout", "-qb", "feature")
    for index in range(300):
        (tmp_path / f"file-{index}.md").write_text(f"{index}\n")
    (tmp_path / "art.bin").write_bytes(bytes(range(256)))
    with text.open("a") as file:
        file.write("\n@pytest.mark.skip\ndef test_example():\n    pass\n")
    git("add", ".")
    git("commit", "-qm", "head")
    head = git("rev-parse", "HEAD").strip()
    git("checkout", "-qb", "fixture-main", merge_base)
    (tmp_path / "base-only.md").write_text("current base, absent from head\n")
    git("add", ".")
    git("commit", "-qm", "base moved")
    base = git("rev-parse", "HEAD").strip()
    git("checkout", "-q", "feature")
    pr = {"state": "open", "number": 997, "changed_files": 302,
          "base": {"sha": base, "ref": "main", "repo": {"full_name": REPO}},
          "head": {"sha": head, "ref": "feature", "repo": {"full_name": REPO}}}
    replies = [copy.deepcopy(pr), copy.deepcopy(pr)]
    comparison = {"base_commit": {"sha": base}, "merge_base_commit": {"sha": merge_base}}
    calls = []

    def api(*args, **kwargs):
        calls.append(args)
        if args == ("api", f"repos/{REPO}/pulls/997"):
            return replies.pop(0)
        if args == ("api", f"repos/{REPO}/compare/{base}...{head}?per_page=1"):
            return comparison
        raise AssertionError(f"Unexpected GitHub API call {args}")

    run = guard.edge.run

    def process(argv, **kwargs):
        if argv[:3] == ["gh", "pr", "diff"]:
            return subprocess.CompletedProcess(argv, 1, "", LIMIT)
        assert kwargs["env"]["GIT_ATTR_NOSYSTEM"] == "1"
        return run(argv, **kwargs)

    monkeypatch.setattr(guard.edge, "setting", lambda name: tmp_path)
    monkeypatch.setattr(guard.hzlib, "ghj", api)
    monkeypatch.setattr(guard.edge, "run", process)
    return git, base, head, replies, comparison, calls


def test_oversize_diff_uses_complete_verified_git_patch(exact_diff, tmp_path):
    git, _, head, _, _, calls = exact_diff
    (tmp_path / "test_gate.py").write_text("# uncommitted\n")
    status = git("status", "--porcelain")
    patch = guard.diff_text(REPO, "997")
    assert patch is not None and "GIT binary patch" in patch and "# uncommitted" not in patch and "base-only.md" not in patch
    assert [(path, kind) for path, _, kind, _ in guard.scan(patch)] == [("test_gate.py", "skip")]
    assert git("rev-parse", "HEAD").strip() == head and git("status", "--porcelain") == status
    assert len(calls) == 3 and all("/files" not in arg for call in calls for arg in call)


def test_oversize_diff_refuses_untracked_attributes_that_hide_a_source_hunk(exact_diff, tmp_path):
    git, base, head, replies, _, _ = exact_diff
    attrs = tmp_path / ".git" / "info" / "attributes"
    attrs.write_text("test_gate.py -diff\n")
    assert "GIT binary patch" in git(f"--attr-source={head}", "diff", "--binary", f"{base}...{head}", "--", "test_gate.py")
    assert guard.diff_text(REPO, "997") is None
    attrs.write_text("")
    replies.append(copy.deepcopy(replies[0]))
    patch = guard.diff_text(REPO, "997")
    assert patch is not None and [(p, kind) for p, _, kind, _ in guard.scan(patch)] == [("test_gate.py", "skip")]


def test_oversize_diff_refuses_local_attributes_added_while_reading(exact_diff, tmp_path, monkeypatch):
    run = guard.edge.run

    def process(argv, **kwargs):
        if "--binary" in argv:
            (tmp_path / ".git" / "info" / "attributes").write_text("test_gate.py -diff\n")
        return run(argv, **kwargs)

    monkeypatch.setattr(guard.edge, "run", process)
    assert guard.diff_text(REPO, "997") is None


@pytest.mark.parametrize("mutation", ["head", "base", "base_ref", "closed", "file_count", "compare"])
def test_oversize_diff_refuses_moved_or_incomplete_source(exact_diff, mutation):
    _, base, head, replies, comparison, _ = exact_diff
    if mutation in {"head", "base"}:
        replies[-1][mutation]["sha"] = base if mutation == "head" else head
    elif mutation == "base_ref":
        replies[-1]["base"]["ref"] = "other"
    elif mutation == "closed":
        replies[-1]["state"] = "closed"
    elif mutation == "file_count":
        replies[0]["changed_files"] += 1
    else:
        comparison["merge_base_commit"]["sha"] = head
    assert guard.diff_text(REPO, "997") is None


@pytest.mark.parametrize("mutation", ["missing", "invalid", "blob", "wrong_repo", "wrong_origin"])
def test_oversize_diff_refuses_missing_or_untrusted_objects(exact_diff, mutation):
    git, _, _, replies, _, _ = exact_diff
    if mutation in {"missing", "invalid"}:
        replies[0]["head"]["sha"] = "e" * 40 if mutation == "missing" else "refs/heads/main"
    elif mutation == "blob":
        replies[0]["head"]["sha"] = git("rev-parse", "HEAD:art.bin").strip()
    elif mutation == "wrong_repo":
        replies[0]["head"]["repo"]["full_name"] = "elsewhere/heinzel"
    else:
        git("remote", "set-url", "origin", f"https://example.test/{REPO}")
    assert guard.diff_text(REPO, "997") is None


@pytest.mark.parametrize(("code", "output", "error", "expected"), [
    (0, "normal GitHub patch", "", "normal GitHub patch"),
    (0, "", "", None),
    (1, "", "HTTP 406: Not Acceptable", None),
    (1, "", "HTTP 403: Resource not accessible", None),
    (1, "", "HTTP 502: failed", None),
])
def test_other_gh_responses_do_not_try_the_local_fallback(monkeypatch, code, output, error, expected):
    monkeypatch.setattr(guard.edge, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, code, output, error))
    monkeypatch.setattr(guard, "large_core_diff", lambda *args: pytest.fail("unexpected object fallback"))
    assert guard.diff_text(REPO, "997") == expected


def test_diff_transport_timeout_refuses_without_using_local_objects(monkeypatch):
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(guard.edge, "run", timeout)
    monkeypatch.setattr(guard, "large_core_diff", lambda *args: pytest.fail("unexpected object fallback"))
    assert guard.diff_text(REPO, "997") is None
