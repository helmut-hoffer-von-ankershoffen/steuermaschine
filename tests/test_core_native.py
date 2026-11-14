"""Native commands: registered with @native, built into the tree, rendered in text and json, gated by a review."""

import json
import sys
import types
from pathlib import Path

import pytest
import typer

from steuermaschine import API_VERSION
from steuermaschine.app import OUT, Runtime
from steuermaschine.config import Project
from steuermaschine.engine import Pipeline, Refuse, Step, Subject, Wait
from steuermaschine.helpdoc import H
from steuermaschine.hookspecs import hookimpl
from steuermaschine.models import Model, ToolMapRow
from steuermaschine.native import Group, NativeRuntime, collect, native, runtime, using
from steuermaschine.plugins import Settings, stack_of
from steuermaschine.testing import fake_providers, invoke

HELP = H("Do one thing.", "Use when testing.", "nothing (read-only).", "sm demo list", "sm go")


class Listing(Model):
    """What `demo list` prints."""

    items: list[str]
    colour: str

    def render(self) -> list[str]:
        return [f"{self.colour} {item}" for item in self.items]

    @property
    def exit_code(self) -> int:
        return 3 if "alarm" in self.items else 0


class Done(Model):
    did: str


area = types.ModuleType("demo_area")
area.__dict__["QUEUE"] = Group("demo", H("demo: the demo area.", "Use when testing.", "nothing by itself.", "sm demo list", "sm go"))


PEERS: list[str] = []


@native("demo list", help=HELP, replaces=("old-list",))
def demo_list(alarm: bool = typer.Option(False, "--alarm", help="Raise an alarm."), fail: str = typer.Option("", "--fail", help="refuse or wait.")) -> Listing:
    rt = runtime()
    rt.say("reading")
    PEERS[:] = [c.path for c in rt.commands() if hasattr(c, "fn")]
    if fail == "refuse":
        raise Refuse("not allowed", "sm demo fix")
    if fail == "wait":
        raise Wait("CI is running", "CI")
    return Listing(items=["a", "alarm"] if alarm else ["a", "b"], colour=rt.settings["colour"])


@native("demo write", writes=True, help=H("Write one thing.", "Use when testing.", "writes one thing on this machine.", "sm demo write x", "sm go"))
def demo_write(what: str = typer.Argument(..., help="What to write.")) -> Done:
    return Done(did=what)


@native("demo list more", help=HELP)
def demo_list_more() -> Done:  # makes `demo list` a command that also has a command below it
    return Done(did="more")


TICKS = []


@native("daemon demo", tick=True, help=HELP)
def daemon_demo() -> Done:
    TICKS.append(runtime().providers.clock.now())
    if len(TICKS) >= 3:
        raise KeyboardInterrupt
    return Done(did=f"tick {len(TICKS)}")


for fn in (demo_list, demo_list_more, demo_write, daemon_demo):
    fn.__module__ = "demo_area"
    setattr(area, fn.__name__, fn)
sys.modules["demo_area"] = area


class Demo:
    name = "demo"

    @hookimpl
    def sm_api_version(self):
        return API_VERSION

    @hookimpl
    def sm_native(self):
        return collect(area)

    @hookimpl
    def sm_tool_map(self):
        return [ToolMapRow(tool="old-list", path="demo list"), ToolMapRow(tool="old-push", path="demo push", owner="slice-x"),
                ToolMapRow(tool="old-lib", kind="internal", note="inside demo list"), ToolMapRow(tool="old-gone", kind="retired", note="no caller"),
                ToolMapRow(tool="old-go", path="go", owner="slice-y"),
                ToolMapRow(tool="old-lint", path="self lint", core=True),
                ToolMapRow(tool="old-area", path="demo", requires=["demo list"]),  # a group: every old subcommand is ported
                ToolMapRow(tool="old-half", path="demo", requires=["demo list", "demo push"], owner="slice-x"),  # partial port
                ToolMapRow(tool="old-bare", path="demo", owner="slice-x")]  # a group without its list of subcommands


@pytest.fixture(autouse=True)
def text_format(monkeypatch):
    monkeypatch.delenv("SM_FORMAT", raising=False)
    OUT["format"] = "text"


def rt(**project):
    stack = stack_of(Demo())
    return Runtime(Project(name="p", plugin="demo", root=Path("."), plugins=["demo"], **project), stack, Settings({"demo": {"colour": "blue"}}), _providers=fake_providers())


def test_text_is_render_and_progress_goes_to_stderr():
    out = invoke(rt(), ["demo", "list"])
    assert (out.exit_code, out.stdout, out.stderr) == (0, "blue a\nblue b\n", "reading\n")


def test_json_is_exactly_one_document_of_the_model():
    out = invoke(rt(), ["--format", "json", "demo", "list"])
    assert Listing.model_validate(json.loads(out.stdout)).items == ["a", "b"] and out.stdout.count("\n") == 1


def test_the_models_exit_code_is_the_commands():
    assert invoke(rt(), ["demo", "list", "--alarm"]).exit_code == 3


def test_refuse_and_wait_become_exit_codes_and_in_json_an_error_object():
    out = invoke(rt(), ["demo", "list", "--fail", "refuse"])
    assert out.exit_code == 1 and "not allowed" in out.stderr and "sm demo fix" in out.stderr and out.stdout == ""
    out = invoke(rt(), ["--format", "json", "demo", "list", "--fail", "wait"])
    assert out.exit_code == 75 and json.loads(out.stdout)["code"] == 75 and "CI" in json.loads(out.stdout)["hint"]


def test_a_write_command_runs_without_a_per_command_review_file():
    """Review is one verdict per promoted commit (bin/promote_verdict.py); nothing checks a review file at run time."""
    out = invoke(rt(), ["demo", "write", "x"])
    assert (out.exit_code, out.stdout) == (0, "did: x\n")


@pytest.fixture
def reviewed_package(tmp_path, monkeypatch):
    """A real package on disk with one write command, so its review file and code digest are real."""
    package = tmp_path / "gated_pkg"
    (package / "reviews").mkdir(parents=True)
    (package / "area_lib").mkdir()
    (package / "__init__.py").write_text("")
    (package / "area_lib" / "__init__.py").write_text("VALUE = 1\n")
    (package / "area.py").write_text(
        "from steuermaschine.helpdoc import H\nfrom steuermaschine.models import Model\nfrom steuermaschine.native import native\n\n\n"
        "class Wrote(Model):\n    did: str\n\n\n"
        "@native('gated write', writes=True, help=H('Write.', 'Use when testing.', 'writes a thing.', 'sm gated write', 'sm go'))\n"
        "def gated_write() -> Wrote:\n    return Wrote(did='written')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [m for m in sys.modules if m.startswith("gated_pkg")]:
        del sys.modules[name]
    import importlib

    module = importlib.import_module("gated_pkg.area")

    class Gated:
        name = "gated"

        @hookimpl
        def sm_api_version(self):
            return API_VERSION

        @hookimpl
        def sm_native(self):
            return collect(module)

    return package, module.gated_write.__sm_native__, Gated()


def test_no_review_file_is_read_at_run_time(reviewed_package):
    package, cmd, _ = reviewed_package
    (package / "reviews" / "gated-write.md").write_text("VERDICT: REJECT\n")  # a stale or rejecting file changes nothing
    assert not hasattr(cmd, "review_state") and not hasattr(cmd, "reviewed")
    assert len(cmd.code_digest()) == 64 and [p.name for p in cmd.code_files()] == ["area.py", "__init__.py"]


def test_an_explicit_review_unit_limits_the_digest_to_the_files_the_command_executes(reviewed_package):
    package, cmd, _ = reviewed_package
    (package / "area_lib" / "other.py").write_text("X = 1\n")
    (package / "area_lib" / "used.py").write_text("Y = 1\n")
    from dataclasses import replace

    narrow = replace(cmd, files=("area_lib/used.py",))
    assert [p.name for p in narrow.code_files()] == ["area.py", "used.py"]
    before = narrow.code_digest()
    (package / "area_lib" / "other.py").write_text("X = 2\n")  # another command's file changes: this approval stays
    assert narrow.code_digest() == before and cmd.code_digest() != before
    (package / "area_lib" / "used.py").write_text("Y = 2\n")
    assert narrow.code_digest() != before
    with pytest.raises(FileNotFoundError):
        replace(cmd, files=("area_lib/gone.py",)).code_files()
    assert [p.name for p in replace(cmd, files=("area_lib",)).code_files()] == ["area.py", "__init__.py", "other.py", "used.py"]  # a directory


def test_a_write_command_that_imports_a_library_outside_its_review_unit_is_found(reviewed_package):
    from dataclasses import replace

    from steuermaschine import lint

    package, cmd, _ = reviewed_package
    (package / "other_lib").mkdir()
    (package / "other_lib" / "__init__.py").write_text("")
    (package / "area.py").write_text((package / "area.py").read_text() + "\nfrom .other_lib import nothing  # noqa\n".replace("nothing", "__name__"))
    assert lint.review_unit_gaps(cmd) == ['area.py imports other_lib, which is not in the review unit of `gated write`: add files=("other_lib", ...) to its @native']
    assert lint.review_unit_gaps(replace(cmd, files=("area_lib", "other_lib"))) == []


def test_a_write_command_runs_through_the_real_entry_point_without_a_review_file(reviewed_package, tmp_path, monkeypatch, capsys):
    from steuermaschine import app, plugins

    _, _, plugin = reviewed_package
    plugins.register(plugin)
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "xdg" / "steuermaschine").mkdir(parents=True)
    (tmp_path / "xdg" / "steuermaschine" / "config.toml").write_text('[project]\nname = "p"\nplugin = "gated"\n')
    monkeypatch.chdir(work)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("STEUERMASCHINE_HOME", str(tmp_path / "state"))
    for var in ("STEUERMASCHINE_PLUGIN", "STEUERMASCHINE_PROJECT", "SM_FORMAT"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit) as stop:
        app.main(["gated", "write"])
    assert stop.value.code == 0 and "written" in capsys.readouterr().out


def test_a_tick_command_gets_loop_and_repeats_with_the_clock():
    TICKS.clear()
    r = rt()
    assert "--loop" in invoke(r, ["daemon", "demo", "--help"]).stdout
    TICKS.clear()
    out = invoke(r, ["daemon", "demo", "--loop", "30"])  # the third tick is interrupted, as a service manager stops it
    assert out.exit_code == 130 and out.stdout.splitlines()[:2] == ["did: tick 1", "did: tick 2"]
    assert len(TICKS) == 3 and r.providers.clock.slept == [30.0, 30.0]


def test_a_write_command_waits_while_a_promote_switches_the_live_sm(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".cache").mkdir()
    pending = tmp_path / ".cache" / "sm-promote-switch-pending"
    pending.write_text(f"{os.getpid()} 2026-11-13T16:00:00Z\n")  # a live pid: the switch is pending
    r = rt(allow_unreviewed=True)
    held = invoke(r, ["demo", "write", "x"])
    assert held.exit_code == 75 and "is switching the live sm" in held.stderr and held.stdout == ""
    assert invoke(r, ["demo", "list"]).exit_code == 0  # read commands are not held
    pending.write_text("999999999 2026-11-13T16:00:00Z\n")  # a dead pid: stale, ignored
    assert invoke(r, ["demo", "write", "x"]).stdout == "did: x\n"


def test_a_pending_file_whose_pid_was_reused_by_a_later_process_is_ignored(tmp_path, monkeypatch):
    import os

    from steuermaschine import native as native_mod
    from steuermaschine.protocols import Result
    from steuermaschine.testing import FakeCommands

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".cache").mkdir()
    pending = tmp_path / ".cache" / "sm-promote-switch-pending"
    pending.write_text(f"{os.getpid()} 2026-11-13T16:00:00Z\n")
    written = 1_794_585_600.0  # 2026-11-13T16:00:00Z
    commands = FakeCommands()
    commands.on(["ps"], Result(0, "   05:00\n", ""))  # started five minutes ago
    assert native_mod.switch_pending(commands, written + 60) == os.getpid()  # started before the file: the promote
    assert native_mod.switch_pending(commands, written + 3600) is None  # started 55 min after the file: a reused pid
    assert commands.calls[-1] == ["ps", "-o", "etime=", "-p", str(os.getpid())]
    commands.on(["ps"], Result(0, "1-02:03:04\n", ""))
    assert native_mod.switch_pending(commands, written + 3600) == os.getpid()  # started a day earlier
    for unreadable in (Result(1, "", "no such process"), Result(0, "soon\n", ""), Result(0, "x-01:02\n", ""), Result(0, "", "")):
        commands.on(["ps"], unreadable)
        assert native_mod.switch_pending(commands, written + 3600) == os.getpid()  # unknown start: the file holds (fail closed)
    pending.write_text(f"{os.getpid()} not-a-time\n")
    calls = len(commands.calls)
    assert native_mod.switch_pending(commands, written + 3600) == os.getpid() and len(commands.calls) == calls
    r = rt(allow_unreviewed=True)
    r.providers.clock.t = written + 3600
    r.providers.commands.on(["ps"], Result(0, "05:00\n", ""))
    pending.write_text(f"{os.getpid()} 2026-11-13T16:00:00Z\n")
    assert invoke(r, ["demo", "write", "x"]).stdout == "did: x\n"  # a stale file no longer blocks a write


def test_a_path_can_be_a_command_and_a_group():
    r = rt()
    assert invoke(r, ["demo", "list", "--alarm"]).stdout.splitlines() == ["blue a", "blue alarm"]
    assert invoke(r, ["demo", "list", "more"]).stdout == "did: more\n"
    assert "Do one thing." in invoke(r, ["demo", "list", "--help"]).stdout  # the group shows the command's help


def test_a_loop_pinned_to_an_older_release_ends_after_its_first_tick(tmp_path, monkeypatch):
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    monkeypatch.setenv("STEUERMASCHINE_PINNED", str(tmp_path / "old"))  # this process runs the old release
    monkeypatch.setenv("STEUERMASCHINE_RELEASE", str(tmp_path / "new"))  # the pointer already names the new one
    TICKS.clear()
    out = invoke(rt(), ["daemon", "demo", "--loop", "30"])
    assert out.exit_code == 0 and len(TICKS) == 1 and "another release is live now" in out.stderr


def test_help_contract_and_schema_cover_native_commands():
    r = rt()
    assert invoke(r, ["self", "lint"]).exit_code == 0
    assert "demo: the demo area." in invoke(r, ["demo", "--help"]).stdout
    assert json.loads(invoke(r, ["self", "schema", "Listing"]).stdout)["title"] == "Listing"


def test_a_native_function_can_be_called_directly_in_a_test():
    with using(NativeRuntime(fake_providers(), {"colour": "red"}, None, say=lambda text: None)):
        assert demo_list(alarm=False, fail="").render() == ["red a", "red b"]
    with pytest.raises(RuntimeError):
        runtime()


def test_tool_map_status_comes_from_the_command_tree():
    r = rt()
    rows = {row["tool"]: row["status"] for row in json.loads(invoke(r, ["--format", "json", "tools", "map"]).stdout)}
    assert rows == {"old-list": "native", "old-push": "missing", "old-lib": "internal", "old-gone": "retired",
                    "old-go": "missing",  # the core has a `go`, but only a plugin's command knows what the old tool knew
                    "old-lint": "native",  # declared: the core command replaces it
                    "old-area": "native", "old-half": "missing", "old-bare": "missing"}
    gaps = invoke(r, ["tools", "gaps"])
    assert gaps.exit_code == 1 and gaps.stdout.splitlines() == ["MISSING old-push: becomes `sm demo push`   owner: slice-x", "MISSING old-go: becomes `sm go`   owner: slice-y",
                                                           "MISSING old-half: becomes `sm demo`   owner: slice-x", "MISSING old-bare: becomes `sm demo`   owner: slice-x",
                                                           "4 missing, 3 native, 1 internal, 1 retired; 0 wrapped"]
    assert invoke(r, ["tools", "gaps", "--wrapped"]).stdout == "0\n"
    assert invoke(r, ["tools", "where", "old-list"]).stdout == "old-list -> sm demo list\n"
    missing = invoke(r, ["tools", "where", "push"])
    assert missing.exit_code == 1 and missing.stdout == "old-push: not ported yet; it becomes `sm demo push` (owner: slice-x)\n"
    assert "retired" in invoke(r, ["tools", "where", "old-gone"]).stdout and invoke(r, ["tools", "where", "nope"]).exit_code == 1


class WithPipeline(Demo):
    @hookimpl
    def sm_pipelines(self, settings):
        return [Pipeline("head", "admit", "Admit one head.", [Step("ci", lambda ctx: "green")], lambda p, s, arg: Subject(key=arg, label=arg))]

    @hookimpl
    def sm_tool_map(self):
        return [ToolMapRow(tool="old-admit", path="head admit", owner="slice-z"), ToolMapRow(tool="old-cut", path="release cut", owner="slice-z")]


def test_a_pipeline_of_the_project_counts_as_ported():
    stack = stack_of(WithPipeline())
    r = Runtime(Project(name="p", plugin="demo", root=Path("."), plugins=["demo"]), stack, Settings({"demo": {"colour": "blue"}}), _providers=fake_providers())
    assert invoke(r, ["tools", "where", "old-admit"]).stdout == "old-admit -> sm head admit\n"
    assert invoke(r, ["tools", "where", "old-cut"]).exit_code == 1  # no such pipeline: still missing


def test_a_native_command_finds_the_registered_commands_through_its_runtime():
    assert invoke(rt(), ["demo", "list"]).exit_code == 0
    assert {"demo list", "demo write", "daemon demo"} <= set(PEERS)  # the stack's registry, without importing it
    assert NativeRuntime(fake_providers(), {}, None).commands() == []  # a bare runtime knows no commands


def test_an_alias_mounts_a_group_under_a_second_name_and_skips_a_missing_target():
    import typer.main

    from steuermaschine.native import Alias, mount_aliases

    app, beads, focus = typer.Typer(), typer.Typer(), typer.Typer()
    beads.command("run")(lambda: None)
    focus.command("list")(lambda: None)
    app.add_typer(beads, name="beads")
    app.add_typer(focus, name="focus")
    root = typer.main.get_command(app)
    run = root.commands["beads"].commands["run"]
    mount_aliases(root, [Alias("work", "beads", "work: both.\nmore"), Alias("work focus", "focus"), Alias("gone", "nothing")])
    assert root.commands["work"].commands["run"] is run and set(root.commands["work"].commands) == {"run", "focus"}
    assert root.commands["work"].short_help == "work: both."
    assert set(root.commands["beads"].commands) == {"run"} and "gone" not in root.commands
