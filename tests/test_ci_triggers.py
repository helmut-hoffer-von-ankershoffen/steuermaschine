"""ci.yml runs each PR head once: push is limited to main, pull_request stays, PR runs are cancelled by a newer push."""

from pathlib import Path

CI = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml").read_text()
HEAD = CI.split("permissions:", 1)[0]


def test_push_runs_only_on_main():
    assert "  push:\n    branches: [main]\n" in HEAD


def test_pull_request_trigger_stays():
    assert "  pull_request:\n" in HEAD


def test_pr_runs_are_cancelled_by_a_newer_push():
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in HEAD
