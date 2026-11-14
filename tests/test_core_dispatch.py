"""`--format text|json` before or after the command, for every command; json format prints exactly one document."""

import json
from pathlib import Path

import pytest
import typer

from steuermaschine import API_VERSION, INTERNAL, USAGE, dispatch, helpdoc
from steuermaschine import app as app_mod
from steuermaschine.app import OUT, Runtime, command_tree
from steuermaschine.config import Project
from steuermaschine.engine import Pipeline, Refuse, Step, Subject, Wait
from steuermaschine.helpdoc import H
from steuermaschine.hookspecs import hookimpl
from steuermaschine.models import Error, Model
from steuermaschine.native import native
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke

HELP = H("A demo command.", "Use when testing.", "nothing (read-only).", "sm demo show", "sm go")


class Shown(Model):
    word: str

    def render(self) -> list[str]:
        return [f"word {self.word}"]


@native("demo show", help=HELP)
def demo_show(word: str = typer.Argument("x", metavar="WORD"), loud: bool = typer.Option(False, "--loud", help="Upper case.")) -> Shown:
    return Shown(word=word.upper() if loud else word)


@native("demo refuse", help=HELP)
def demo_refuse() -> Shown:
    raise Refuse("not now", "sm demo show")


@native("demo wait", help=HELP)
def demo_wait() -> Shown:
    raise Wait("CI is running", "CI")


@native("demo crash", help=HELP)
def demo_crash() -> Shown:
    raise RuntimeError("boom")


@native("demo tick", help=HELP, tick=True)
def demo_tick() -> Shown:
    return Shown(word="tick")


@native("demo own", help=HELP)
def demo_own(format_: str = typer.Option("wide", "--format", help="This command's own meaning of the word.")) -> Shown:
    return Shown(word=format_)


class Plugin:
    name = "demo"

    @hookimpl
    def sm_api_version(self):
        return API_VERSION

    @hookimpl
    def sm_native(self, settings):
        return [f.__sm_native__ for f in (demo_show, demo_refuse, demo_wait, demo_crash, demo_tick, demo_own)]

    @hookimpl
    def sm_pipelines(self, settings):
        return [Pipeline("head", "ship", "Ship one head.", [Step("ci", lambda ctx: "green")], lambda p, s, arg: Subject(key=arg, label=f"head {arg}"))]

    @hookimpl
    def sm_commands(self, runtime):
        app = typer.Typer()
        extra = typer.Typer(help=H("extra: a mounted group.", "Use when testing.", "nothing (read-only).", "sm extra hi", "sm go"))
        app.add_typer(extra, name="extra")

        @extra.command("hi", help=H("Say hi.", "Use when testing.", "nothing (read-only).", "sm extra hi", "sm go"))
        def hi() -> None:
            app_mod.emit(False, {"hi": True}, ["hi"])

        return app


@pytest.fixture(autouse=True)
def text_format(monkeypatch):
    monkeypatch.delenv("SM_FORMAT", raising=False)
    OUT["format"] = "text"
    yield
    OUT["format"] = "text"


def rt():
    stack = stack_of(Plugin())
    return Runtime(Project(name="proj", plugin=stack.name, root=Path("."), plugins=stack.names), stack, {}, _providers=fake_providers())


def test_scan_reads_the_last_format_and_stops_at_the_terminator():
    assert dispatch.scan_format(["pr", "status", "7"]) is None
    assert dispatch.scan_format(["--format", "text", "pr", "status", "--format=json"]) == "json"
    assert dispatch.scan_format(["pr", "--format"]) == ""
    assert dispatch.scan_format(["note", "--", "--format", "json"]) is None


def test_format_after_the_command_equals_the_root_option_for_core_native_mounted_and_pipeline_commands():
    r = rt()
    invoke(r, ["head", "ship", "k1"])  # the run exists, so both spellings below report the same state
    for words in (["demo", "show", "abc"], ["go"], ["alarm", "list"], ["self", "tree"], ["extra", "hi"], ["head", "ship", "k1"], ["head", "status"], ["tools", "gaps"]):
        before, after = invoke(r, ["--format", "json", *words]), invoke(r, [*words, "--format", "json"])
        assert json.loads(after.stdout) == json.loads(before.stdout), words
        assert after.exit_code == before.exit_code and after.stdout.count("\n") == 1, words
    assert json.loads(invoke(r, ["demo", "show", "--format=json", "abc", "--loud"]).stdout) == {"word": "ABC"}
    assert invoke(r, ["demo", "--format", "json", "show", "abc"]).stdout.strip() == '{"word": "abc"}'  # on the group in between, too
    assert invoke(r, ["demo", "show", "abc", "--format", "text"]).stdout == "word abc\n"


def test_every_command_and_group_below_the_root_has_the_option_and_shows_it_in_help():
    r = rt()
    rows = [row for row in helpdoc.tree(command_tree(r), "sm") if row["path"] != "sm"]
    assert len(rows) > 30 and all("--format" in row["options"] for row in rows)
    for words in (["demo", "show"], ["alarm", "list"], ["head", "ship"], ["extra", "hi"], ["self"]):
        shown = invoke(r, [*words, "--help"])
        assert shown.exit_code == 0 and "--format" in shown.stdout and "text|json" in shown.stdout, words


def test_a_command_with_its_own_format_option_keeps_it():
    r = rt()
    assert invoke(r, ["demo", "own", "--format", "narrow"]).stdout == "word narrow\n"
    assert json.loads(invoke(r, ["--format", "json", "demo", "own"]).stdout) == {"word": "wide"}


def test_a_wrong_format_after_the_command_is_a_usage_error():
    out = invoke(rt(), ["demo", "show", "--format", "yaml"])
    assert out.exit_code == USAGE and out.stdout == "" and "--format is text or json, not 'yaml'" in out.stderr


def test_refuse_and_wait_are_one_error_document_in_json_and_one_line_on_stderr_in_text():
    r = rt()
    refused = invoke(r, ["demo", "refuse", "--format", "json"])
    assert Error.model_validate_json(refused.stdout) == Error(error="not now", code=1, hint="sm demo show") and refused.exit_code == 1 and refused.stderr == ""
    waiting = invoke(r, ["demo", "wait", "--format", "json"])
    doc = Error.model_validate_json(waiting.stdout)
    assert (doc.code, waiting.exit_code) == (75, 75) and "waiting on CI" in doc.hint
    text = invoke(r, ["demo", "refuse"])
    assert text.stdout == "" and text.stderr.startswith("sm: not now") and text.exit_code == 1


def test_parser_errors_are_one_error_document_in_json_whatever_the_position_of_the_option():
    r = rt()
    cases = {"unknown command": ["demo", "nope", "--format", "json"], "unknown root command": ["--format", "json", "nope"], "unknown option": ["demo", "show", "--format", "json", "--nope"],
             "wrong value": ["alarm", "ack", "--format=json"], "group without a command": ["demo", "--format", "json"]}
    for name, args in cases.items():
        out = invoke(r, args)
        doc = Error.model_validate_json(out.stdout)
        assert out.exit_code == USAGE and doc.code == USAGE and doc.error and doc.hint.endswith("--help") and out.stderr == "", name
    assert Error.model_validate_json(invoke(r, ["demo", "show", "--format", "json", "--nope"]).stdout).hint == "sm demo show --help"
    text = invoke(r, ["demo", "show", "--nope"])
    assert text.exit_code == USAGE and text.stdout == "" and "Usage: sm demo show" in text.stderr and "--nope" in text.stderr


def test_an_unexpected_exception_is_an_error_document_with_code_70_in_json_and_is_raised_in_text():
    r = rt()
    out = invoke(r, ["demo", "crash", "--format", "json"])
    doc = Error.model_validate_json(out.stdout)
    assert out.exit_code == INTERNAL and doc.code == INTERNAL and doc.error == "RuntimeError: boom" and "Traceback" in out.stderr
    with pytest.raises(RuntimeError, match="boom"):
        invoke(r, ["demo", "crash"])


def test_a_tick_command_takes_the_option_next_to_its_loop_option():
    out = invoke(rt(), ["demo", "tick", "--format", "json"])
    assert json.loads(out.stdout) == {"word": "tick"} and out.exit_code == 0


def test_run_returns_the_code_of_an_exit_an_abort_and_a_plain_return():
    def tree(body):
        app = typer.Typer(add_completion=False)

        @app.callback()
        def root() -> None:
            pass

        app.command("do")(body)
        return typer.main.get_command(app)

    def aborts() -> None:
        raise typer.Abort()

    def exits() -> None:
        raise SystemExit("text is exit 1")

    said: list = []
    echo = lambda text, err=False: said.append((text, err))  # noqa: E731
    assert dispatch.run(tree(lambda: None), ["do"], "sm", lambda: False) == 0
    assert dispatch.run(tree(aborts), ["do"], "sm", lambda: False, echo) == 1 and said == [("Aborted!", True)]
    assert dispatch.run(tree(aborts), ["do"], "sm", lambda: True, echo) == 1 and json.loads(said[-1][0])["error"] == "aborted"
    assert dispatch.run(tree(exits), ["do"], "sm", lambda: False) == 1


def test_main_reads_the_format_after_the_command(tmp_path, monkeypatch, capsys):
    (tmp_path / "steuermaschine.toml").write_text('[project]\nname = "p"\nplugin = "none"\n')
    monkeypatch.setenv("STEUERMASCHINE_PROJECT", str(tmp_path))
    with pytest.raises(SystemExit) as stop:
        app_mod.main(["self", "config", "--format", "json"])
    assert stop.value.code == 0 and json.loads(capsys.readouterr().out)["project"] == "p"
    with pytest.raises(SystemExit) as stop:
        app_mod.main(["self", "nope", "--format=json"])
    assert stop.value.code == USAGE and Error.model_validate_json(capsys.readouterr().out).code == USAGE
    with pytest.raises(SystemExit) as stop:
        app_mod.main(["go", "--format", "yaml"])
    assert stop.value.code == USAGE and "--format is text or json, not 'yaml'" in capsys.readouterr().err


def test_main_reports_a_plugin_that_does_not_load_as_one_document_in_json(tmp_path, monkeypatch, capsys):
    (tmp_path / "steuermaschine.toml").write_text('[project]\nname = "p"\nplugin = "not-installed"\n')
    monkeypatch.setenv("STEUERMASCHINE_PROJECT", str(tmp_path))
    with pytest.raises(SystemExit) as stop:
        app_mod.main(["go", "--format", "json"])
    doc = Error.model_validate_json(capsys.readouterr().out)
    assert stop.value.code == INTERNAL and "NOT loaded" in doc.error and "self config" in doc.hint
