"""nn5m: one writer per branch. The owner is recorded per branch of a repository; another agent is refused with the owner's name."""

import json
import subprocess

import pytest

from steuermaschine.defaults import Subprocess
from steuermaschine.engine import Refuse
from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native import branch_owner


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(statedir, "path", lambda name, under=None: tmp_path / "state" / name)
    main = tmp_path / "main"
    main.mkdir()
    git = lambda *a, cwd=main: subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)  # noqa: E731
    git("init", "-q", "-b", "main")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    git("worktree", "add", "-q", "-b", "fix/topic", str(tmp_path / "wt-a"))
    return tmp_path


def test_the_first_claim_owns_the_branch_and_a_second_agent_is_refused_by_name(repo):
    commands = Subprocess()
    wt = repo / "wt-a"
    mine = branch_owner.claim(commands, wt, "agent-1")
    assert mine["agent"] == "agent-1" and mine["branch"] == "fix/topic"
    assert branch_owner.claim(commands, wt, "agent-1") == mine  # its owner again: a no-op
    with pytest.raises(Refuse) as why:
        branch_owner.claim(commands, wt, "agent-2")
    assert "belongs to agent agent-1" in str(why.value) and "agent-2" in str(why.value)
    with pytest.raises(Refuse, match="belongs to agent agent-1"):
        branch_owner.check(commands, wt, "agent-2")
    assert branch_owner.check(commands, wt, "agent-1")[1]["agent"] == "agent-1"
    assert branch_owner.claim(commands, wt, "agent-2", take=True)["agent"] == "agent-2"  # the coordinator's hand-over


def test_without_an_agent_name_nothing_is_refused_or_recorded_and_main_is_never_owned(repo, monkeypatch):
    monkeypatch.delenv("SM_AGENT", raising=False)
    commands = Subprocess()
    branch_owner.claim(commands, repo / "wt-a", "agent-1")
    branch_owner.guard_write(commands, repo / "wt-a")  # no name: no read, no refusal
    assert branch_owner.check(commands, repo / "wt-a", "")[1]["agent"] == "agent-1"
    with pytest.raises(Refuse, match="no agent name"):
        branch_owner.claim(commands, repo / "wt-a", "")
    with pytest.raises(Refuse, match="not a work branch"):
        branch_owner.claim(commands, repo / "main", "agent-1")
    branch_owner.guard_write(commands, repo / "main", "agent-2")  # main is not owned


def test_the_guard_of_a_write_step_claims_an_unowned_branch_for_the_first_named_writer(repo, monkeypatch):
    commands = Subprocess()
    branch_owner.guard_write(commands, repo / "wt-a", "agent-1", record=False)  # a dry run records nothing
    assert not (repo / "state" / branch_owner.FOLDER).exists()
    monkeypatch.setenv("SM_AGENT", "agent-1")
    branch_owner.guard_write(commands, repo / "wt-a")
    monkeypatch.setenv("SM_AGENT", "agent-2")
    with pytest.raises(Refuse, match="belongs to agent agent-1"):
        branch_owner.guard_write(commands, repo / "wt-a")
    files = list((repo / "state" / branch_owner.FOLDER).glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["agent"] == "agent-1"
