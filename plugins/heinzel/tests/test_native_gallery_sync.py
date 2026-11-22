"""sm check regenerates a stale plugin gallery and sm ship takes exactly those files into the shipped commit
(bead heinzel-management-0tkk8). Real git in a temporary repository; the repository's freshness check and the capture
are scripted. No browser, uv or agent runs."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from steuermaschine.engine import Refuse
from steuermaschine.testing import MemoryStore
from steuermaschine.testing.fakes import Result
from steuermaschine_heinzel.native import check as check_module
from steuermaschine_heinzel.native import gallery_sync as gs
from steuermaschine_heinzel.native import plugin_gallery as pg

GALLERY = "plugins/heinzel-clock/docs/assets/screenshots"
IDENTITY = ["-c", "user.name=Mira Example", "-c", "user.email=mira@example.com"]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *IDENTITY, *args], check=True, capture_output=True, text=True).stdout.strip()


class Commands:
    """Real git; the frozen freshness check answers from `answers` (one JSON report per call, the last one repeats)."""

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def run(self, argv, cwd=None, timeout=None, env=None):
        self.calls.append(list(argv))
        if argv[: len(pg.FROZEN)] == pg.FROZEN:
            answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
            return Result(0 if answer.get("ok") else 1, "progress\n" + json.dumps(answer) + "\n", "")
        assert Path(argv[0]).name not in {"codex", "claude"}
        if argv[:2] == ["git", "-C"]:
            argv = [argv[0], argv[1], argv[2], *IDENTITY, *argv[3:]]
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return Result(done.returncode, done.stdout, done.stderr)


FRESH = {"ok": True, "fresh": ["heinzel-clock"], "stale": [], "failed": []}
STALE = {"ok": False, "fresh": [], "stale": [{"plugin": "heinzel-clock", "command": "make screenshots NAME=heinzel-clock", "error": "stale"}], "failed": []}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "plugins-wt"
    (root / GALLERY).mkdir(parents=True)
    (root / GALLERY / "ui.png").write_bytes(b"old")
    (root / "plugins/heinzel-clock/web.css").write_text("main {}\n")
    subprocess.run(["git", "init", "-q", "-b", "feat/clock", str(root)], check=True)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "feat(clock): a new face")
    return root


def runtime(answers):
    return SimpleNamespace(providers=SimpleNamespace(commands=Commands(answers), store=MemoryStore()))


def capture_that_writes(monkeypatch, root, code=0):
    def gallery(name, repo):
        assert repo == root and name == "heinzel-clock"
        if code:
            return pg.Gallery(plugin="clock", repo=str(root), live=True, ran=True, exit=code, error="Scene today failed")
        (root / GALLERY / "ui.png").write_bytes(b"new")
        (root / GALLERY / "cli.png").write_bytes(b"added")
        return pg.Gallery(plugin="clock", repo=str(root), live=True, ran=True, exit=0, changed=["a changed", "b added"])

    monkeypatch.setattr(pg, "plugin_gallery", gallery)


def test_the_plugins_repository_is_recognised_by_its_origin():
    assert gs.is_plugins("helmut-hoffer-von-ankershoffen/heinzel-plugins") and gs.is_plugins("x/Heinzel-Plugins")
    assert not gs.is_plugins("helmut-hoffer-von-ankershoffen/heinzel") and not gs.is_plugins("x/heinzel-plugins-extra")


def test_the_freshness_check_is_the_own_repository_offline_command(repo):
    rt = runtime([FRESH])
    assert gs.stale(rt.providers.commands, repo) == []
    assert rt.providers.commands.calls[0] == [*pg.FROZEN, "screenshots-check", "--all", "--json"]


@pytest.mark.parametrize("answer, why", [({"ok": False}, "no readable answer"),
                                         ({"ok": False, "stale": [], "failed": [{"plugin": "heinzel-clock", "error": "image changed"}]}, "broken, not stale")])
def test_an_unreadable_or_broken_check_refuses(repo, answer, why):
    with pytest.raises(Refuse, match=why):
        gs.stale(runtime([answer]).providers.commands, repo)


def test_check_regenerates_and_ship_amends_exactly_the_regenerated_files_into_the_head(repo, monkeypatch):
    capture_that_writes(monkeypatch, repo)
    rt = runtime([STALE, FRESH])
    report = check_module.plugins_check(rt, repo)
    assert report.ready and "regenerated" in report.steps[0].detail and not report.receipt
    before = git(repo, "rev-parse", "HEAD")
    lines = gs.into_head(rt, repo)
    assert lines and "amended into the head commit" in lines[0]
    assert git(repo, "status", "--porcelain") == "" and git(repo, "rev-list", "--count", "HEAD") == "1"
    assert git(repo, "rev-parse", "HEAD") != before and git(repo, "log", "-1", "--format=%s") == "feat(clock): a new face"
    assert set(git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()) >= {f"{GALLERY}/ui.png", f"{GALLERY}/cli.png"}
    assert rt.providers.store.load(gs.KIND, str(repo)) is None


def test_a_pushed_head_gets_one_docs_commit_never_a_rewrite(repo, tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "feat/clock")
    capture_that_writes(monkeypatch, repo)
    rt = runtime([STALE, FRESH])
    lines = gs.into_head(rt, repo)
    assert any("the head was already pushed" in line for line in lines)
    assert git(repo, "log", "-1", "--format=%s") == gs.SUBJECT and git(repo, "rev-list", "--count", "HEAD") == "2"


def test_other_uncommitted_changes_are_never_committed_by_sm(repo, monkeypatch):
    capture_that_writes(monkeypatch, repo)
    rt = runtime([STALE, FRESH])
    check_module.plugins_check(rt, repo)
    (repo / "plugins/heinzel-clock/web.css").write_text("main { color: teal; }\n")
    lines = gs.fold(rt, repo)
    assert "besides the regenerated gallery" in lines[0]
    assert git(repo, "rev-list", "--count", "HEAD") == "1" and git(repo, "status", "--porcelain")


def test_a_gallery_that_cannot_be_regenerated_fails_check_and_refuses_ship_with_the_exact_command(repo, monkeypatch):
    capture_that_writes(monkeypatch, repo, code=1)
    rt = runtime([STALE])
    report = check_module.plugins_check(rt, repo)
    assert not report.ready and report.failures[0].fix == f"cd {repo} && make screenshots NAME=heinzel-clock"
    with pytest.raises(Refuse, match="could not be regenerated") as refused:
        gs.into_head(rt, repo)
    assert refused.value.next_cmd.startswith(f"cd {repo} && make screenshots NAME=heinzel-clock")


def test_a_fresh_clean_worktree_ships_unchanged(repo):
    rt = runtime([FRESH])
    assert gs.into_head(rt, repo) == []
    assert check_module.plugins_check(rt, repo).steps[0].detail.startswith("every screenshot gallery is fresh")


def test_the_freshness_report_is_the_last_readable_json_line_and_anything_else_refuses(repo):
    ok = gs.stale(Commands([STALE]), repo)
    assert ok[0]["plugin"] == "heinzel-clock"

    class Noise(Commands):
        def run(self, argv, cwd=None, timeout=None, env=None):
            if argv[: len(pg.FROZEN)] == pg.FROZEN:
                return Result(1, "{broken\nnot a report\n", "")
            return super().run(argv, cwd=cwd, timeout=timeout, env=env)

    with pytest.raises(Refuse, match="no readable answer"):
        gs.stale(Noise([FRESH]), repo)


def test_dirty_lists_the_changed_paths_and_refuses_outside_a_repository(repo, tmp_path):
    (repo / "plugins/heinzel-clock/web.css").write_text("main { color: red }\n")
    (repo / "new file.txt").write_text("x")
    assert gs.dirty(Commands([FRESH]), repo) == ["new file.txt", "plugins/heinzel-clock/web.css"]
    with pytest.raises(Refuse, match="cannot read the status"):
        gs.dirty(Commands([FRESH]), tmp_path / "nowhere")


def test_a_record_with_nothing_left_to_commit_is_dropped_and_a_record_of_another_head_is_never_folded(repo, monkeypatch):
    capture_that_writes(monkeypatch, repo)
    rt = runtime([STALE, FRESH])
    check_module.plugins_check(rt, repo)
    git(repo, "checkout", "--", ".")
    git(repo, "clean", "-fdq")
    assert gs.fold(rt, repo) == [] and rt.providers.store.load(gs.KIND, str(repo)) is None  # nothing dirty: the record goes
    capture_that_writes(monkeypatch, repo)
    rt.providers.commands = Commands([STALE, FRESH])  # the check answers again from the start
    check_module.plugins_check(rt, repo)
    git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "--allow-empty", "-m", "chore: the head moved")
    assert "did not regenerate for this head" in gs.fold(rt, repo)[0]
    assert git(repo, "rev-list", "--count", "HEAD") == "2"


def test_a_commit_hook_that_refuses_the_gallery_commit_is_a_refusal_with_the_hook_output(repo, monkeypatch):
    capture_that_writes(monkeypatch, repo)
    rt = runtime([STALE, FRESH])
    check_module.plugins_check(rt, repo)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho hook says no >&2\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(Refuse, match="the commit of the regenerated gallery failed"):
        gs.fold(rt, repo)


def test_a_gallery_still_stale_after_the_regeneration_refuses_and_names_the_command(repo, monkeypatch):
    capture_that_writes(monkeypatch, repo)
    rt = runtime([STALE])  # the freshness check keeps saying stale
    with pytest.raises(Refuse, match="still stale after regeneration") as refused:
        gs.into_head(rt, repo)
    assert "make screenshots NAME=heinzel-clock" in refused.value.next_cmd
