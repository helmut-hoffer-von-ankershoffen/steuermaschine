"""The core's own native commands (`steuermaschine.commands`): `sm gate run|status`, `sm lim`, `sm doc section`.

They work with the core alone (no plugin), take what a project adds through `sm_provider_extras` (the outermost
plugin's key wins), and are the innermost layer: a plugin's native command at the same path replaces one, and is
then gated by the plugin's own review file, never by the core's.
"""

import json
from pathlib import Path

import pytest

from steuermaschine import API_VERSION, USAGE
from steuermaschine.app import Runtime, natives, write_units
from steuermaschine.commands import doc, gate, lim
from steuermaschine.config import Project, Settings
from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.hookspecs import hookimpl
from steuermaschine.models import Model
from steuermaschine.native import native
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.runlock import Policy, Refusal
from steuermaschine.testing import FakeCommands, fake_providers, invoke


def core_alone(reviewed: bool = True, commands: FakeCommands | None = None) -> Runtime:
    project = Project(name="alone", plugin="none", root=Path("."), allow_unreviewed=reviewed)
    return Runtime(project, None, Settings({}), _providers=fake_providers(commands=commands or FakeCommands()))


def stacked(*plugins, reviewed: bool = True, commands: FakeCommands | None = None) -> Runtime:
    stack = stack_of(*plugins)
    settings = Settings({})
    project = Project(name="stacked", plugin=plugins[0].name, root=Path("."), plugins=[p.name for p in plugins], allow_unreviewed=reviewed)
    return Runtime(project, stack, settings, _providers=stack.providers(settings, fake_providers(commands=commands or FakeCommands())))


class Extras:
    """A plugin that only hands values to the core's commands."""

    def __init__(self, name: str, extras: dict):
        self.name, self.extras = name, extras

    @hookimpl
    def sm_api_version(self):
        return API_VERSION

    @hookimpl
    def sm_provider_extras(self):
        return self.extras


class Done(Model):
    """What the replacing `lim` did."""

    by: str

    def render(self) -> list[str]:
        return [f"lim of {self.by}"]


@native("lim", writes=True, help=H(
    "A project's own output limiter, replacing the core's.",
    "Use when testing the order rule.",
    "writes a log of its own.",
    "sm lim -- COMMAND",
    "sm go"))
def own_lim() -> Done:
    return Done(by="the plugin")


class Replaces:
    """A plugin whose native command `lim` replaces the core's."""

    name = "replaces"

    @hookimpl
    def sm_api_version(self):
        return API_VERSION

    @hookimpl
    def sm_native(self):
        return [own_lim.__sm_native__]


# ----------------------------------------------------------------------------- the core alone
def test_the_core_alone_has_the_generic_commands():
    out = invoke(core_alone(), ["--help"]).stdout
    assert all(f"  {word}" in out for word in ("gate", "lim", "doc"))
    paths = {c.path for c in natives(core_alone()) if hasattr(c, "fn")}
    assert {"gate run", "gate status", "lim", "doc section"} <= paths
    assert {c.path for c in write_units(core_alone()) if hasattr(c, "fn")} >= {"lim", "gate run"}


def test_lim_alone_writes_the_full_output_below_the_state_directory():
    commands = FakeCommands()
    commands.on(["make"], Result(5, "".join(f"row {i}\n" for i in range(80)), ""))
    rt = core_alone(commands=commands)
    done = invoke(rt, ["--format", "json", "lim", "-n", "2", "-t", "2", "--", "make"])
    found = lim.Limited.model_validate(json.loads(done.stdout))
    assert done.exit_code == 5 and found.cut and Path(found.log).parent == Path(rt.project.home) / "lim"
    assert len(Path(found.log).read_text().splitlines()) == 80


def test_lim_runs_without_a_per_command_review_file():
    commands = FakeCommands()
    commands.on(["true"], Result(0, "", ""))
    done = invoke(core_alone(reviewed=False, commands=commands), ["lim", "--", "true"])
    assert done.exit_code == 0, done.stderr


def test_lim_takes_the_directory_a_plugin_hands_in(tmp_path):
    commands = FakeCommands()
    commands.on(["make"], Result(0, "".join(f"row {i}\n" for i in range(80)), ""))
    rt = stacked(Extras("base", {lim.DIRECTORY: str(tmp_path / "logs")}), commands=commands)
    found = lim.Limited.model_validate(json.loads(invoke(rt, ["--format", "json", "lim", "-n", "1", "-t", "1", "--", "make"]).stdout))
    assert Path(found.log).parent == tmp_path / "logs"


def test_gate_alone_uses_the_default_policy_below_the_state_directory():
    commands = FakeCommands()
    commands.on(["true"], Result(0, ""))
    rt = core_alone(commands=commands)
    ran = gate.GateRun.model_validate(json.loads(invoke(rt, ["--format", "json", "gate", "run", "--light", "--", "true"]).stdout))
    assert (ran.status, ran.lane, ran.code) == ("ran", "light", 0) and commands.calls[-1] == ["true"]
    assert gate.default_policy(Path(rt.project.home)).directory == Path(rt.project.home) / "gate"
    assert invoke(rt, ["gate", "status"]).exit_code == 0


def refusing(word: str):
    def factory(settings, commands, clock, allow_full, cwd):
        def refuse(argv, environ):
            return Refusal(USAGE, f"{word} refuses {' '.join(argv)}") if not allow_full else None

        return Policy(directory=Path(str(cwd)) / ".never", refusals=(refuse,))

    return factory


def test_gate_takes_the_policy_of_the_outermost_plugin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rt = stacked(Extras("base", {gate.POLICY: refusing("base")}), Extras("outer", {gate.POLICY: refusing("outer")}))
    done = invoke(rt, ["gate", "run", "--", "make", "check"])
    assert done.exit_code == USAGE and "sm gate: refusing: outer refuses make check" in done.stderr and "base" not in done.stderr
    rt.providers.commands.on(["make"], Result(0, ""))
    assert invoke(rt, ["gate", "run", "--allow-full", "--light", "--", "make", "check"]).exit_code == 0  # allow_full reaches the policy


def test_doc_section_needs_a_file_without_a_default(tmp_path):
    done = invoke(core_alone(), ["doc", "section"])
    assert done.exit_code == USAGE and "names no default document" in done.stderr
    (tmp_path / "a.md").write_text("# A\n\n## 1. One\nx\n")
    assert "## 1. One" in invoke(core_alone(), ["doc", "section", str(tmp_path / "a.md")]).stdout
    missing = invoke(core_alone(), ["doc", "section", str(tmp_path / "none.md")])
    assert missing.exit_code == USAGE and "cannot read" in missing.stderr
    bad = invoke(core_alone(), ["doc", "section", str(tmp_path / "a.md"), "--grep", "("])
    assert bad.exit_code == USAGE and "not a regular expression" in bad.stderr


def test_doc_section_takes_the_default_document_and_looks_next_to_it(tmp_path, monkeypatch):
    (tmp_path / "main.md").write_text("# Main\n")
    (tmp_path / "other.md").write_text("# Other\n")
    monkeypatch.chdir(tmp_path.parent)
    rt = stacked(Extras("base", {doc.DOCUMENT: lambda: tmp_path / "main.md"}))
    assert "# Main" in invoke(rt, ["doc", "section"]).stdout
    found = doc.Section.model_validate(json.loads(invoke(rt, ["--format", "json", "doc", "section", "other.md"]).stdout))
    assert found.file == str(tmp_path / "other.md")


def test_doc_section_keeps_a_relative_file_when_the_default_is_not_configured(tmp_path, monkeypatch):
    def unconfigured():
        raise Refuse("not configured", "sm self config", code=USAGE)

    monkeypatch.chdir(tmp_path)
    rt = stacked(Extras("base", {doc.DOCUMENT: unconfigured}))
    done = invoke(rt, ["doc", "section", "nowhere.md"])
    assert done.exit_code == USAGE and "cannot read nowhere.md" in done.stderr
    assert "not configured" in invoke(rt, ["doc", "section"]).stderr


# ----------------------------------------------------------------------------- the order rule for core commands
def test_a_plugin_command_at_a_core_path_replaces_it_and_is_the_write_unit_at_that_path():
    rt = stacked(Replaces(), reviewed=False)
    (cmd,) = [c for c in natives(rt) if getattr(c, "path", "") == "lim"]
    assert cmd.fn is own_lim
    assert invoke(rt, ["lim"]).stdout.strip() == "lim of the plugin"
    assert [u.fn for u in write_units(rt) if u.path == "lim"] == [own_lim]


@pytest.mark.parametrize("path", ["gate run", "gate wait", "gate status", "lim", "doc section"])
def test_the_core_commands_follow_the_help_contract(path):
    assert invoke(core_alone(), [*path.split(), "--help"]).exit_code == 0
    lint = json.loads(invoke(core_alone(), ["--format", "json", "self", "lint"]).stdout)
    assert [row for row in lint if path in json.dumps(row)] == []


def test_gate_refuses_a_rerun_of_a_failure_on_the_same_tree_until_a_reason_is_given():
    commands = FakeCommands()
    commands.on(["git", "rev-parse"], Result(0, "TREE\n"))
    commands.on(["git", "diff"], Result(0, ""))
    commands.on(["git", "ls-files"], Result(0, ""))
    commands.on(["pytest"], Result(1, ""))
    rt = core_alone(commands=commands)
    first = invoke(rt, ["gate", "run", "--", "pytest", "tests/a_test.py"])
    assert first.exit_code == 1
    again = invoke(rt, ["gate", "run", "--", "pytest", "tests/a_test.py"])
    assert again.exit_code == USAGE and "failed" in again.stderr and "--force-rerun REASON" in again.stderr
    forced = invoke(rt, ["gate", "run", "--force-rerun", "fixture outside the tree fixed", "--", "pytest", "tests/a_test.py"])
    assert forced.exit_code == 1 and "refusing" not in forced.stderr
    blank = invoke(rt, ["gate", "run", "--force-rerun", "  ", "--", "pytest", "tests/a_test.py"])
    assert blank.exit_code == USAGE and "needs a reason" in blank.stderr
    swallowed = invoke(rt, ["gate", "run", "--force-rerun", "--", "pytest", "tests/a_test.py"])  # the option took the `--`
    assert swallowed.exit_code == USAGE and "needs a reason" in swallowed.stderr
    lanes = invoke(rt, ["gate", "status"])
    assert "light slots in use 0/4" in lanes.stdout and "heavy slots in use" in lanes.stdout
