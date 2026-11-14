"""The floor of the Bash hook through the real `sm-guard` console script (hook JSON on stdin, exit code and stderr read,
as the former hook's cases were): every case of the former git hook, the three new floor rules, the fallback when the
full rule set cannot load, and the case that made this necessary: sm itself cannot load.

The commands are spelled apart where needed (`SKIP`) so that no hook that reads this file's text mistakes them for one.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import guards_runtime as g
import pytest

from steuermaschine.testing import invoke
from steuermaschine_heinzel import guard_floor

SM_GUARD = Path(sys.executable).parent / "sm-guard"
FLOOR = "steuermaschine_heinzel.guard_floor"
SKIP = "--no-" + "verify"
OLD = "heinzel-management" + "/tools"
MK = "ma" + "ke"


def hook(command=None, raw=None, floor=FLOOR, env=None):
    text = raw if raw is not None else json.dumps({"tool_input": {"command": command}, "cwd": "/tmp"})
    return subprocess.run([str(SM_GUARD), "--floor", floor], input=text, capture_output=True, text=True, env=env, timeout=30)


# --- the former git hook, every case ------------------------------------------------------------------------------------

GIT_REFUSED = [
    f"git push {SKIP}",
    "git push --force origin main",
    "git push --force-with-lease",
    "git push -f",
    "git commit -n -m x",
    f"git commit {SKIP} -m x",
    f"cd x && git push origin HEAD {SKIP}",
    "git push origin HEAD --force=yes",
]
GIT_ALLOWED = ["git push origin HEAD", "git commit -f -m x", "git status", "echo hi", ""]


@pytest.mark.parametrize("command", GIT_REFUSED)
def test_refuses_hook_skips_and_force_pushes(command):
    done = hook(command)
    assert done.returncode == 2 and "Blocked: no " in done.stderr and "hz-" not in done.stderr


@pytest.mark.parametrize("command", GIT_ALLOWED)
def test_allows_the_rest_of_git(command):
    done = hook(command)
    assert done.returncode == 0 and done.stderr == ""


def test_a_commit_with_f_is_allowed_but_a_push_with_f_is_refused():
    assert hook("git commit -f -m x").returncode == 0
    assert hook("git push -f").returncode == 2
    assert hook("git commit -n -m x").returncode == 2


def test_fails_closed_on_input_it_cannot_read():
    for raw in ("", "not json", "[]", '{"tool_input": []}', '{"tool_input": {"command": 5}}'):
        done = hook(raw=raw)
        assert done.returncode == 2 and "could not be read" in done.stderr, raw
    assert hook(raw="{}").returncode == 0  # no command: nothing to refuse


# --- the new floor rules --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    f"~/Code/{OLD}/hz-queue run",
    f"python3 /Users/x/Code/{OLD}/hz_digest.py",
    "cd ~/Code/heinzel-management && python3 tools/hz_next.py",
    "hz-queue list",
    "~/.local/bin/hz-release status",
    "FOO=1 hz-gate pytest tests/x_test.py",
    "sm gate run -- hz-preflight-head 12",
    "nohup hz-tick >/dev/null &",
    "bash -c 'hz-train 0.9.32'",
    f"sh {OLD}/hz-alarm",
    "./hz-status",
])
def test_refuses_a_script_of_the_retired_tools_directory_run_by_hand(command):
    done = hook(command)
    assert done.returncode == 2 and "retired tools directory" in done.stderr and "hz-" not in done.stderr


@pytest.mark.parametrize("command", [
    f"cat ~/Code/{OLD}/hz-guard",
    f"grep -n tick ~/Code/{OLD}/hz-tick",
    "git -C ~/Code/heinzel-management log -- tools/hz-queue",
    "sm tools where hz-queue",
    "ls ~/.local/bin/ | grep hz-",
    "git checkout -b hz-fix",
    "hz status",
    f"sm gate run -- pytest ~/Code/{OLD}/tests/test_loadguard.py",
])
def test_reading_or_naming_an_old_tool_is_allowed(command):
    assert hook(command).returncode == 0


@pytest.mark.parametrize("command", [
    f"{MK} pr",
    f"NO_PREFLIGHT=1 sm gate run -- {MK} pr",
    f"sm gate run --priority 1 -- {MK} pr",
    f"cd x && {MK} -C web pr",
    f"env FOO=1 {MK} pr PR=1",
    f"sm lim -n 5 -- {MK} pr",
])
def test_refuses_make_pr_gated_or_not(command):
    done = hook(command)
    assert done.returncode == 2 and "never `make pr`" in done.stderr


@pytest.mark.parametrize("command", [f"{MK} check-fast", f"sm gate run -- {MK} test", f"echo {MK} pr", f"git commit -m '{MK} pr is banned'"])
def test_other_make_targets_and_mentions_are_allowed(command):
    assert hook(command).returncode == 0


@pytest.mark.parametrize("command", [
    "gh workflow run ci.yml --ref 3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39",
    "gh workflow run ci.yml -r 3f2a9c1",
    "gh workflow run ci.yml --ref=refs/pull/123/head",
    "gh workflow run ci.yml --ref $(gh pr view 123 --json headRefOid -q .headRefOid)",
    'gh workflow run ci.yml --ref "$HEAD_SHA"',
    "gh workflow run validate.yml -f sha=3f2a9c1d8e7b",
    "gh api repos/o/r/actions/workflows/ci.yml/dispatches -f ref=3f2a9c1d8e7b6a5f",
    "cd x; gh workflow run ci.yml --ref pull/7/head",
])
def test_refuses_a_dispatch_on_a_pr_head_sha(command):
    done = hook(command)
    assert done.returncode == 2 and "PR head SHA" in done.stderr


@pytest.mark.parametrize("command", [
    "gh workflow run ci.yml --ref main",
    "gh workflow run release.yml --ref release/0.9 -f version=0.9.33",
    "gh api repos/o/r/actions/workflows/ci.yml/dispatches -f ref=main",
    "gh api repos/o/r/commits/3f2a9c1d8e7b",
    "gh run view 123",
])
def test_a_dispatch_on_a_branch_and_other_gh_calls_are_allowed(command):
    assert hook(command).returncode == 0


# --- fallback and the reason for the console script ------------------------------------------------------------------------

def test_the_floor_alone_decides_when_the_full_rules_cannot_load(tmp_path):
    pkg = tmp_path / "lib" / "brokenfull"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    source = Path(guard_floor.__file__).read_text().replace(f'FULL = "{guard_floor.FULL}"', 'FULL = "brokenfull.missing:nothing"')
    assert source != Path(guard_floor.__file__).read_text()
    (pkg / "floor.py").write_text(source)
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "lib")}
    for command in [*GIT_REFUSED, f"{MK} pr", "hz-queue run"]:
        assert hook(command, floor="brokenfull.floor", env=env).returncode == 2, command
    for command in GIT_ALLOWED:
        done = hook(command, floor="brokenfull.floor", env=env)
        assert done.returncode == 0 and "not available" in done.stderr, command


def test_it_refuses_even_when_sm_and_its_dependencies_cannot_load(tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    for name in ("typer", "pydantic", "pluggy", "pydantic_settings"):
        (broken / f"{name}.py").write_text("raise ImportError('broken on purpose')\n")
    env = {**os.environ, "PYTHONPATH": str(broken)}
    assert hook(f"git push {SKIP}", env=env).returncode == 2
    assert hook(f"{MK} pr", env=env).returncode == 2
    assert hook("git status", env=env).returncode == 0
    full_sm = subprocess.run([shutil.which("sm", path=str(SM_GUARD.parent)) or "sm", "guard", "git"], input=json.dumps({"tool_input": {"command": f"git push {SKIP}"}}),
                             capture_output=True, text=True, env=env, timeout=60)
    assert full_sm.returncode not in (0, 2)  # what the console script exists for: a broken sm does not block


def test_sm_guard_git_applies_the_same_floor(monkeypatch, tmp_path):
    import io

    g.register(monkeypatch)
    for command, code in ((f"{MK} pr", 2), ("hz-queue run", 2), ("gh workflow run ci.yml --ref 3f2a9c1", 2), ("ls", 0), (GIT_REFUSED[0], 2)):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_input": {"command": command}})))
        assert invoke(g.runtime_of(tmp_path), ["guard", "git"]).exit_code == code, command


def test_the_floor_is_standard_library_only_and_names_no_old_tool_in_its_texts():
    text = Path(guard_floor.__file__).read_text()
    imports = {line.split()[1].split(".")[0] for line in text.splitlines() if line.startswith(("import ", "from "))}
    assert imports <= {"__future__", "os", "pathlib", "re", "shlex"}, imports
    for message in (guard_floor.GIT_BLOCKED, guard_floor.OLD_TOOL_BLOCKED, guard_floor.MAKE_PR_BLOCKED, guard_floor.DISPATCH_BLOCKED, guard_floor.SWITCH_BLOCKED):
        assert "hz-" not in message


# --- no new write step while bin/promote switches the live sm ------------------------------------------------------------

STEPS = ["sm head apply prepare.out", "sm --format json head prepare wt", "/Users/x/.local/bin/sm release admit 0.9.41",
         "cd /w && sm head validate . --lanes web-1", "steuermaschine release resume 0.9.41", "sm gate run -- sm head admit x", "sm release cut 0.9.42 --live",
         "sm ship ~/Code/wt --live --wait 30", "sm --format json ship wt", "cd /w && sm gate run -- sm ship wt"]


@pytest.mark.parametrize("command", STEPS)
def test_a_write_step_is_refused_while_a_live_promote_switches(command):
    pending = Path.home() / ".cache" / "sm-promote-switch-pending"
    assert guard_floor.refusal(command) is None
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(f"{os.getpid()} 2026-11-13T15:00:00Z\n")
    why = guard_floor.refusal(command)
    assert why and f"bin/promote is switching the live sm (pid {os.getpid()})" in why
    pending.write_text("999999999 2026-11-13T15:00:00Z\n")  # the promote is gone: stale, ignored
    assert guard_floor.refusal(command) is None


def test_the_floor_and_bin_promote_name_the_same_write_steps(tmp_path):
    """One list in two languages: every WRITE_STEPS pair is a busy step for bin/promote and bin/rollback, and the bash
    pattern names no step the floor lets start during a switch."""
    lib = Path(__file__).resolve().parents[3] / "bin" / "promote_lib.sh"
    pattern = re.search(r"\(head \(([a-z|]+)\)\|release \(([a-z|]+)\)\|([a-z]+)\( \|\$\)\)", lib.read_text())
    assert pattern, "busy_steps in bin/promote_lib.sh no longer has the form (head (...)|release (...)|ship( |$))"
    in_bash = {("head", v) for v in pattern.group(1).split("|")} | {("release", v) for v in pattern.group(2).split("|")}
    in_bash |= {(pattern.group(3),)}  # a top-level verb: one word
    assert in_bash == guard_floor.WRITE_STEPS


@pytest.mark.parametrize("command", ["sm head size wt", "sm head ship 801", "sm shipping", "sm release status", "sm head pr abc t --live", "cat sm head apply", "sm go"])
def test_reads_and_other_commands_run_during_a_switch(command):
    pending = Path.home() / ".cache" / "sm-promote-switch-pending"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(f"{os.getpid()}\n")
    assert guard_floor.refusal(command) is None
