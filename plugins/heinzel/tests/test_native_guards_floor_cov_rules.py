"""The floor rules of the Bash hook called in process (the subprocess cases in test_native_guards_floor.py go through
`sm-guard` and are not measured): how a command line is split into programs, and each rule on the shapes that only
the splitting reaches (shell -c strings, eval, wrappers, `--`, leading assignments, unbalanced quotes).

Commands are spelled apart (`SKIP`, `MK`, `OLD`) so that no hook that reads this file's text mistakes them for one.
"""

from __future__ import annotations

import pytest

from steuermaschine_heinzel import guard_floor as floor

SKIP = "--no-" + "verify"
MK = "ma" + "ke"
OLD_TOOL = "hz-" + "status"
SHA = "e9c943810ea75cfad155a431a2f168b2a9559828"


def test_words_drop_leading_assignments_and_survive_unbalanced_quotes():
    assert floor.words_of("A=1 B=two git status") == ["git", "status"]
    assert floor.words_of("echo 'unbalanced") == ["echo", "'unbalanced"]  # shlex fails: split on blanks
    assert floor.words_of("X=1") == []


def test_segments_split_shell_c_strings_and_eval_again():
    assert floor.segments("bash -lc 'git status'") == [["bash", "-lc", "git status"]]  # only exactly -c is a command string
    assert floor.segments("zsh -c 'git status'") == [["zsh", "-c", "git status"], ["git", "status"]]
    # separators split the raw line first, so a -c string with `;` reaches the rules as plain segments as well
    assert ["uv", "sync'"] in floor.segments("zsh -c 'git status; uv sync'")
    assert floor.segments("eval git log") == [["eval", "git", "log"], ["git", "log"]]
    assert floor.segments("sh") == [["sh"]]  # a bare shell: nothing to split again
    assert floor.segments(" ; && ") == []


def test_programs_follow_wrappers_their_options_and_every_double_dash():
    words = ["env", "-i", "PATH=/bin", "nice", "-n", "10", "timeout", "30", "pytest", "-q"]
    assert [words[i] for i in floor.programs(words)] == ["env", "nice", "timeout", "pytest"]
    gated = ["sm", "gate", "run", "--", "uv", "run", "pytest"]
    assert [gated[i] for i in floor.programs(gated)] == ["sm", "uv"]
    assert floor.programs(["nohup"]) == [0]  # a wrapper with nothing after it
    assert floor.programs(["sm", "lim", "--"]) == [0]  # a trailing -- names no program


@pytest.mark.parametrize("command", [
    f"git push {SKIP}", "git push --force-with-lease=main", "git commit -n -m x", "cd /r && git push -f origin x",
])
def test_the_git_rule_refuses_skipping_hooks_and_force(command):
    assert floor.git_refusal(command) == floor.GIT_BLOCKED == floor.refusal(command)


@pytest.mark.parametrize("command", ["git commit -f -m x", "git push origin HEAD:refs/heads/fix/x", "git status"])
def test_the_git_rule_allows_a_forced_commit_and_plain_pushes(command):
    assert floor.git_refusal(command) is None


@pytest.mark.parametrize("command", [
    f"nohup {OLD_TOOL} --json",
    f"bash -c '{OLD_TOOL}'",
    f"python3 ~/Code/heinzel-management/tools/{OLD_TOOL}",
    "uv run /x/hz_" + "status.py",
    f"sm gate run -- {OLD_TOOL}",
    f"uv run python ~/Code/heinzel-management/tools/{OLD_TOOL}",
    "uv run python /x/hz_" + "status.py",
    "env python3 /x/hz_" + "status.py",
    "time python3 /x/hz_" + "status.py",
    "nohup bash /x/hz_" + "status.py",
    "sm gate run -- python3 /x/hz_" + "status.py",
])
def test_the_old_tool_rule_finds_a_retired_script_wherever_it_runs(command):
    assert floor.old_tool_refusal(command) == floor.OLD_TOOL_BLOCKED


@pytest.mark.parametrize("command", [f"cat ~/.local/bin/{OLD_TOOL}", f"grep {OLD_TOOL} notes.md", f"echo {OLD_TOOL}", "sm tools map",
                                     f"git add ~/Code/x/{OLD_TOOL}", f"rm ~/Code/x/{OLD_TOOL}", f"python3 notes.py ~/.local/bin/{OLD_TOOL}"])
def test_the_old_tool_rule_lets_readers_and_mentions_pass(command):
    assert floor.old_tool_refusal(command) is None


def test_the_make_rule_refuses_the_pr_target_behind_wrappers_only():
    assert floor.make_pr_refusal(f"env CI=1 {MK} -j2 pr") == floor.MAKE_PR_BLOCKED
    assert floor.make_pr_refusal(f"sm gate run -- g{MK} pr") == floor.MAKE_PR_BLOCKED
    assert floor.make_pr_refusal(f"{MK} check") is None
    assert floor.make_pr_refusal("echo pr") is None


@pytest.mark.parametrize("command", [
    f"gh workflow run ci.yml --ref {SHA}",
    f"gh workflow run ci.yml -r {SHA[:9]}",
    "gh workflow run ci.yml --ref $(gh pr view 812 --json headRefOid -q .headRefOid)",
    f"gh workflow run ci.yml --ref={SHA}",
    "gh workflow run ci.yml -f ref=refs/pull/812/head",
    "gh api repos/o/r/actions/workflows/ci.yml/dispatches -F sha=$HEAD_SHA",
    f"gh workflow run ci.yml --ref '{SHA}",  # unbalanced quotes are still read
])
def test_the_dispatch_rule_refuses_a_pr_head_sha(command):
    assert floor.dispatch_refusal(command) == floor.DISPATCH_BLOCKED


@pytest.mark.parametrize("command", [
    "gh workflow run ci.yml --ref main",
    "gh workflow run ci.yml -f lanes=web-1",
    "gh api repos/o/r/pulls/812 -F sha=" + SHA,  # not a dispatch
    "gh api repos/o/r/actions/workflows/ci.yml/dispatches --ref " + SHA,  # --ref is not a field of gh api
    "gh workflow run ci.yml --ref",
])
def test_the_dispatch_rule_allows_branches_and_other_api_calls(command):
    assert floor.dispatch_refusal(command) is None


def test_the_switch_rule_refuses_write_steps_only_while_a_live_promote_switches(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("HOME", str(tmp_path))
    step = "sm --format json head apply /r/packet.json --approved " + "a" * 64
    assert floor.sm_step(step.split(), 0) == ("head", "apply")
    assert floor.sm_step(["sm", "--format=json", "go"], 0) is None
    assert floor.switch_refusal(step) is None  # no pending file
    pending = tmp_path.joinpath(*floor.PENDING)
    pending.parent.mkdir(parents=True)
    pending.write_text(f"{os.getpid()}\n")
    assert floor.switch_refusal(step) == floor.SWITCH_BLOCKED.format(pid=os.getpid()) == floor.refusal(step)
    assert floor.switch_refusal("sm head status") is None  # a read is never held
    pending.write_text("not-a-pid")
    assert floor.switch_pending() is None and floor.switch_refusal(step) is None
    assert floor.refusal("git status") is None
