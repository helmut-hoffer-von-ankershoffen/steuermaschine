"""Exercise the operator switch through the real CLI with an isolated GitHub fixture."""

import json

from ci_ops_kit import REPO, World, runtime

from steuermaschine.testing import invoke

SM = "helmut-hoffer-von-ankershoffen/steuermaschine"


def test_pause_then_resume_both_with_idempotent_readback(tmp_path, monkeypatch):
    world = World()
    values = {REPO: "true", SM: "true"}

    def read(repository):
        return lambda _args: {"name": "CLAUDE_REVIEW", "value": values[repository]}

    def write(args):
        values[args[args.index("--repo") + 1]] = args[-1]
        return ""

    for repository in values:
        world.on(f"api repos/{repository}/actions/variables/CLAUDE_REVIEW", read(repository))
    world.on("variable set CLAUDE_REVIEW", write)
    rt = runtime(tmp_path, monkeypatch, world)
    cmd = ["--format", "json", "ci", "claude-review"]
    paused = invoke(rt, [*cmd, "--pause"])
    assert paused.exit_code == 0, paused
    assert [entry["state"] for entry in json.loads(paused.stdout)["repositories"]] == ["paused", "paused"]
    assert values == {REPO: "false", SM: "false"}
    assert len([call for call in world.lines() if "variable set" in call]) == 2

    assert invoke(rt, [*cmd, "--pause"]).exit_code == 0
    assert invoke(rt, cmd).exit_code == 0
    assert len([call for call in world.lines() if "variable set" in call]) == 2
    assert invoke(rt, [*cmd, "--resume", "--repo", "sm"]).exit_code == 0
    assert values == {REPO: "false", SM: "true"}


def test_invalid_configuration_fails_closed_without_writing(tmp_path, monkeypatch):
    world = World()
    world.on(f"api repos/{REPO}/actions/variables/CLAUDE_REVIEW", {"name": "CLAUDE_REVIEW", "value": "FALSE"})
    rt = runtime(tmp_path, monkeypatch, world)
    result = invoke(rt, ["ci", "claude-review", "--repo", "core"])
    assert result.exit_code == 1 and "invalid" in result.stdout
    assert not any("variable set" in call for call in world.lines())
