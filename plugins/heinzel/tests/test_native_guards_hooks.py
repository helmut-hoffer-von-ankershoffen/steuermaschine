"""`sm guard git` and `sm guard heavy` through the command tree on fakes, and the moved cases of the former heavy-run hook.

The heavy-run cases are the former hook's own (its tests ran the script with the hook JSON on stdin and read the exit code
and stderr): same commands, same assertions, now through `invoke`. Changed: the hook now needs a marker file for the
management checkout (HLD.md in a temporary directory) and gated commands are also spelled `sm gate run --`.
"""

import io
import json

import guards_runtime as g
import pytest

from steuermaschine.testing import invoke
from steuermaschine_heinzel.native.guards import HookVerdict
from steuermaschine_heinzel.native.guards_lib import hooks

SKIP = "--no-" + "verify"  # spelled apart so that no hook that reads this file's text mistakes it for a command


@pytest.fixture
def run(monkeypatch, tmp_path):
    g.register(monkeypatch)

    def go(path, command, cwd=None, raw=None, fmt=None):
        text = raw if raw is not None else json.dumps({"tool_input": {"command": command}, "cwd": str(cwd or tmp_path)})
        monkeypatch.setattr("sys.stdin", io.StringIO(text))
        return invoke(g.runtime_of(tmp_path), [*(["--format", fmt] if fmt else []), *path])

    return go


def guard(run, command, cwd):
    return run(["guard", "heavy"], command, cwd)


@pytest.mark.parametrize("command", [
    "pytest tests/foo_test.py",
    "cd x && uv run --with pytest pytest -n 2",
    "npx vitest run --maxWorkers=2",
    "FOO=1 make pr",
    "python3 -m pytest tests",
    "tsc --noEmit | tail -5",
    "hz-gate pytest tests/unit/heads/desktop_installation_test.py",
    "sm gate run -- pytest tests/unit/heads/desktop_installation_test.py",
])
def test_testguard_refuses_bare_heavy_runs_and_banned_tests(run, command, tmp_path):
    done = guard(run, command, tmp_path)
    assert done.exit_code == 2 and "Blocked" in done.stderr


@pytest.mark.parametrize("command", [
    "hz-gate pytest tests/foo_test.py -n 2",
    "HZ_GATE_PRIORITY=1 hz-gate make pr",
    "sm gate run -- pytest tests/foo_test.py -n 2",
    "sm gate run --priority 1 -- make pr",
    "git commit -m 'mention pytest'",
    "grep -rn pytest tests",
    "echo make",
    "ls",
])
def test_testguard_allows_gated_and_light_commands(run, command, tmp_path):
    assert guard(run, command, tmp_path).exit_code == 0


def test_testguard_allows_bare_tests_in_the_management_checkout_but_not_the_banned_ones(run, tmp_path):
    (tmp_path / "HLD.md").write_text("")
    inside = tmp_path / "tools"
    inside.mkdir()
    assert guard(run, "uv run --quiet --with pytest python -m pytest tools/tests", inside).exit_code == 0
    assert guard(run, "pytest tests/unit/heads/desktop_installation_test.py", inside).exit_code == 2


def test_the_refusal_names_the_run_lock_and_no_old_tool(run, tmp_path):
    done = guard(run, "pytest tests", tmp_path)
    assert "sm gate run --" in done.stderr and "hz-" not in done.stderr


def test_the_heavy_guard_fails_open_on_input_it_cannot_read(run, tmp_path):
    for raw in ("", "not json", "[]", '{"tool_input": "pytest"}'):
        assert run(["guard", "heavy"], "", raw=raw).exit_code == 0, raw
    assert invoke(g.runtime_of(tmp_path), ["guard", "heavy", "--input", str(tmp_path / "missing.json")]).exit_code == 0


def test_the_heavy_guard_in_json_and_the_marker_setting(run, tmp_path, monkeypatch):
    done = run(["guard", "heavy"], "ls", fmt="json")
    assert HookVerdict.model_validate(json.loads(done.stdout)).allowed is True
    conf = g.register(monkeypatch)
    conf["guards_management_marker"] = "MARKER"
    (tmp_path / "MARKER").write_text("")
    assert guard(run, "pytest tests", tmp_path).exit_code == 0


def test_the_heavy_guard_reads_a_file_with_input(run, tmp_path):
    f = tmp_path / "hook.json"
    f.write_text(json.dumps({"tool_input": {"command": "pytest x"}, "cwd": str(tmp_path)}))
    assert invoke(g.runtime_of(tmp_path), ["guard", "heavy", "--input", str(f)]).exit_code == 2


@pytest.mark.parametrize("command", [
    f"git push {SKIP}",
    "git push --force origin main",
    "git push --force-with-lease",
    "git push -f",
    "git commit -n -m x",
    f"git commit {SKIP} -m x",
    f"cd x && git push origin HEAD {SKIP}",
    "git push origin HEAD --force=yes",
])
def test_git_guard_refuses_hook_skips_and_force_pushes(run, command):
    done = run(["guard", "git"], command)
    assert done.exit_code == 2 and "Blocked: no " in done.stderr and "hz-" not in done.stderr


@pytest.mark.parametrize("command", [
    "git push origin HEAD",
    "git commit -f -m x",
    "git status",
    "echo hi",
    "",
])
def test_git_guard_allows_the_rest(run, command):
    assert run(["guard", "git"], command).exit_code == 0


def test_git_guard_allows_a_commit_with_f_but_a_push_with_f_is_refused(run):
    assert run(["guard", "git"], "git commit -f -m x").exit_code == 0
    assert run(["guard", "git"], "git push -f").exit_code == 2
    assert run(["guard", "git"], "git commit -n -m x").exit_code == 2


def test_git_guard_fails_closed_on_input_it_cannot_read(run, tmp_path):
    for raw in ("", "not json", "[]", '{"tool_input": []}', '{"tool_input": {"command": 5}}'):
        done = run(["guard", "git"], "", raw=raw)
        assert done.exit_code == 2 and "could not be read" in done.stderr, raw
    assert run(["guard", "git"], "", raw="{}").exit_code == 0  # no command: nothing to refuse
    assert invoke(g.runtime_of(tmp_path), ["guard", "git", "--input", str(tmp_path / "missing.json")]).exit_code == 2


def test_git_guard_in_json(run):
    done = run(["guard", "git"], "git status", fmt="json")
    assert HookVerdict.model_validate(json.loads(done.stdout)).allowed is True
    refused = run(["guard", "git"], "git push -f", fmt="json")
    assert refused.exit_code == 2 and "Blocked" in json.loads(refused.stdout)["error"]


def test_library_reads():
    assert hooks.read_command('{"tool_input": {"command": "x"}}') == "x"
    assert hooks.read_heavy_input('{"tool_input": {"command": "x"}, "cwd": "/y"}') == ("x", "/y")
