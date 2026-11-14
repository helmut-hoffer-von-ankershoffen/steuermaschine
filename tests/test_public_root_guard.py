"""scripts/check_public_root.py: history that does not descend from the public root never reaches the public repo."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_public_root.py"


def git(repo: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "PATH": "/usr/bin:/bin", "HOME": str(repo)}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env).stdout.strip()


def layout(tmp_path: Path) -> tuple[Path, str, str, str]:
    """A repo with the public root, one commit on it, and an unrelated old-history commit; the script next to its root file."""
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "root")
    root = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "-q", "--allow-empty", "-m", "work")
    work = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "--orphan", "old")
    git(repo, "commit", "-q", "--allow-empty", "-m", "old history")
    old = git(repo, "rev-parse", "HEAD")
    scripts = repo / "scripts"
    scripts.mkdir()
    (scripts / "check_public_root.py").write_text(SCRIPT.read_text())
    (scripts / "public-root").write_text(root + "\n")
    return repo, root, work, old


def run(repo: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(repo / "scripts" / "check_public_root.py"), *args], cwd=repo, input=stdin,
                          capture_output=True, text=True, timeout=30)


def test_a_commit_on_public_history_passes_and_old_history_is_refused(tmp_path):
    repo, root, work, old = layout(tmp_path)
    assert run(repo, "--head", work).returncode == 0 and run(repo, "--head", root).returncode == 0
    refused = run(repo, "--head", old)
    assert refused.returncode == 1 and "does not descend from the public root" in refused.stderr


def test_pre_push_refuses_old_history_tags_and_the_old_private_repo(tmp_path):
    repo, _root, work, old = layout(tmp_path)
    url = "https://github.com/o/steuermaschine.git"
    assert run(repo, "--pre-push", "origin", url, stdin=f"refs/heads/main {work} refs/heads/main {'0' * 40}\n").returncode == 0
    stale = run(repo, "--pre-push", "origin", url, stdin=f"refs/heads/old {old} refs/heads/old {'0' * 40}\n")
    assert stale.returncode == 1 and "old private history" in stale.stderr
    tag = run(repo, "--pre-push", "origin", url, stdin=f"refs/tags/v1 {work} refs/tags/v1 {'0' * 40}\n")
    assert tag.returncode == 1 and "only branches" in tag.stderr
    dev = run(repo, "--pre-push", "dev", "https://github.com/o/steuermaschine-dev.git", stdin=f"refs/heads/main {work} refs/heads/main x\n")
    assert dev.returncode == 1 and "archived private repository" in dev.stderr
    assert run(repo, "--pre-push", "origin", url, stdin=f"(delete) {'0' * 40} refs/heads/gone {work}\n").returncode == 0


def test_a_missing_root_file_fails_closed(tmp_path):
    repo, _root, work, _old = layout(tmp_path)
    (repo / "scripts" / "public-root").unlink()
    assert run(repo, "--head", work).returncode != 0
