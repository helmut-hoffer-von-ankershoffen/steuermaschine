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


def run(repo: Path, who: tuple[str, str], *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    env = {"GIT_AUTHOR_NAME": who[0], "GIT_AUTHOR_EMAIL": who[1], "GIT_COMMITTER_NAME": who[0], "GIT_COMMITTER_EMAIL": who[1],
           "PATH": "/usr/bin:/bin", "HOME": str(repo)}
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=repo, capture_output=True, text=True, timeout=30, env=env, input=stdin)


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


BOT = ("claude", "noreply@anthropic.com")
GITHUB = ("GitHub", "noreply@github.com")
ZERO = "0" * 40


def push_line(sha: str) -> str:
    return f"refs/heads/work {sha} refs/heads/work {ZERO}\n"


def test_a_push_is_read_from_the_stdin_refs_and_only_commits_on_no_remote(tmp_path):
    repo = repo_with(tmp_path)
    git(repo, (NAME, EMAIL), "commit", "-q", "--allow-empty", "-m", "mine")
    mine = git(repo, (NAME, EMAIL), "rev-parse", "HEAD")
    assert run(repo, (NAME, EMAIL), "--pre-push", stdin=push_line(mine)).returncode == 0
    git(repo, BOT, "commit", "-q", "--allow-empty", "-m", "bot")
    bot = git(repo, BOT, "rev-parse", "HEAD")
    done = run(repo, (NAME, EMAIL), "--pre-push", stdin=push_line(bot))
    assert done.returncode == 1 and "the author is claude" in done.stderr and "the committer is claude" in done.stderr
    assert run(repo, (NAME, EMAIL), "--pre-push", stdin=push_line(mine)).returncode == 0  # the tip decides, not HEAD
    assert run(repo, (NAME, EMAIL), "--pre-push", stdin=f"refs/heads/gone {ZERO} refs/heads/gone {mine}\n").returncode == 0  # a deletion brings nothing


def test_a_commit_github_made_is_on_the_remote_and_never_read(tmp_path):
    repo = repo_with(tmp_path)
    git(repo, GITHUB, "commit", "-q", "--allow-empty", "-m", "Merge branch main (update-branch)")  # committer GitHub
    made = git(repo, GITHUB, "rev-parse", "HEAD")
    assert run(repo, (NAME, EMAIL), "--pre-push", stdin=push_line(made)).returncode == 1  # not on a remote yet: refused
    git(repo, GITHUB, "update-ref", "refs/remotes/origin/work", made)  # GitHub made it on the branch: it is there already
    git(repo, (NAME, EMAIL), "commit", "-q", "--allow-empty", "-m", "mine")
    mine = git(repo, (NAME, EMAIL), "rev-parse", "HEAD")
    assert run(repo, (NAME, EMAIL), "--pre-push", stdin=push_line(mine)).returncode == 0


def test_without_stdin_the_runner_ref_or_head_is_used(tmp_path):
    repo = repo_with(tmp_path)
    git(repo, BOT, "commit", "-q", "--allow-empty", "-m", "bot")
    assert run(repo, (NAME, EMAIL), "--pre-push").returncode == 1


def test_ranges_are_read(tmp_path):
    repo = repo_with(tmp_path)
    git(repo, BOT, "commit", "-q", "--allow-empty", "-m", "bot")
    assert run(repo, (NAME, EMAIL), "--range", "origin/main..HEAD").returncode == 1
    assert run(repo, (NAME, EMAIL), "--range", "origin/main..origin/main").returncode == 0


def test_usage_and_unreadable_git_fail_closed(tmp_path):
    assert run(tmp_path, (NAME, EMAIL), "--bogus").returncode == 2
    assert run(tmp_path, (NAME, EMAIL), "--pre-push").returncode == 1
