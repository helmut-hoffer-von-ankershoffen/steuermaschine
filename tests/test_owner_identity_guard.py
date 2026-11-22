"""scripts/check_owner_identity.py: a commit whose author or committer is not the owner is refused (bead i3hq1)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_owner_identity.py"
NAME, EMAIL = "helmut-hoffer-von-ankershoffen", "1826050+helmut-hoffer-von-ankershoffen@users.noreply.github.com"


def git(repo: Path, who: tuple[str, str], *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": who[0], "GIT_AUTHOR_EMAIL": who[1], "GIT_COMMITTER_NAME": who[0], "GIT_COMMITTER_EMAIL": who[1],
           "PATH": "/usr/bin:/bin", "HOME": str(repo)}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env).stdout.strip()


def run(repo: Path, who: tuple[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    env = {"GIT_AUTHOR_NAME": who[0], "GIT_AUTHOR_EMAIL": who[1], "GIT_COMMITTER_NAME": who[0], "GIT_COMMITTER_EMAIL": who[1],
           "PATH": "/usr/bin:/bin", "HOME": str(repo)}
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=repo, capture_output=True, text=True, timeout=30, env=env)


def repo_with(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    owner = (NAME, EMAIL)
    git(repo, owner, "init", "-q", "-b", "main")
    git(repo, owner, "commit", "-q", "--allow-empty", "-m", "root")
    git(repo, owner, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def test_the_next_commit_must_be_the_owner(tmp_path):
    repo = repo_with(tmp_path)
    assert run(repo, (NAME, EMAIL)).returncode == 0
    bot = run(repo, ("claude", "noreply@anthropic.com"))
    assert bot.returncode == 1 and "the author is claude <noreply@anthropic.com>, not the owner" in bot.stderr and "git config user.email" in bot.stderr
    assert run(repo, (NAME, "x@example.invalid")).returncode == 1


def test_unpushed_commits_and_ranges_are_read(tmp_path):
    repo = repo_with(tmp_path)
    git(repo, (NAME, EMAIL), "commit", "-q", "--allow-empty", "-m", "mine")
    assert run(repo, (NAME, EMAIL), "--unpushed").returncode == 0
    git(repo, ("claude", "noreply@anthropic.com"), "commit", "-q", "--allow-empty", "-m", "bot")
    done = run(repo, (NAME, EMAIL), "--unpushed")
    assert done.returncode == 1 and "the author is claude" in done.stderr and "the committer is claude" in done.stderr
    assert run(repo, (NAME, EMAIL), "--range", "origin/main..HEAD").returncode == 1
    assert run(repo, (NAME, EMAIL), "--range", "origin/main..origin/main").returncode == 0


def test_usage_and_unreadable_git_fail_closed(tmp_path):
    assert run(tmp_path, (NAME, EMAIL), "--bogus").returncode == 2
    assert run(tmp_path, (NAME, EMAIL), "--unpushed").returncode == 1
