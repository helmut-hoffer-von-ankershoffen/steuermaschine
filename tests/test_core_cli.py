"""The command line: go contract, help contract and lint, --format, plugin merge rules, conformance kit."""

import json
from pathlib import Path

import pytest
import typer
from pydantic import BaseModel, ConfigDict, Field

from steuermaschine import ALARM, API_VERSION, PROG, helpdoc, models
from steuermaschine.app import OUT, Runtime, command_tree
from steuermaschine.config import Project
from steuermaschine.engine import Pipeline, Step, Subject, Wait
from steuermaschine.helpdoc import H
from steuermaschine.hookspecs import hookimpl
from steuermaschine.plugins import Alarm, GoItem, Settings, stack_of
from steuermaschine.testing import check_plugin, fake_providers, invoke


def identify(providers, settings, arg):
    return Subject(key=arg, label=f"head {arg}", fingerprint={"head": arg}, facts={})


def waits(ctx):
    raise Wait("CI is running", "CI")


class DemoSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    colour: str = Field("blue", description="A colour.")


class Demo:
    name = "demo"

    @hookimpl
    def sm_api_version(self):
        return API_VERSION

    @hookimpl
    def sm_settings(self):
        return DemoSettings

    @hookimpl
    def sm_pipelines(self, settings):
        return [Pipeline("head", "ship", "Ship one head.", [Step("ci", waits)], identify)]

    @hookimpl
    def sm_commands(self, runtime):
        app = typer.Typer()
        extra = typer.Typer(help=H("extra: things of the demo plugin.", "Use when testing.", "nothing (read-only).", "sm extra hi", "sm go"))
        app.add_typer(extra, name="extra")

        @extra.command("hi", help=H("Say hi.", "Use when testing.", "nothing (read-only).", "sm extra hi", "sm go"))
        def hi() -> None:
            typer.echo("hi")

        head = typer.Typer(help=H("head: the plugin's own head group.", "Use when testing.", "nothing (read-only).", "sm head mine", "sm go"))
        app.add_typer(head, name="head")

        @head.command("ship", help=H("The plugin's own ship.", "Use when testing.", "nothing (read-only).", "sm head ship", "sm go"))
        def ship() -> None:
            typer.echo("plugin ship")

        @head.command("mine", help=H("Only the plugin has this.", "Use when testing.", "nothing (read-only).", "sm head mine", "sm go"))
        def mine() -> None:
            typer.echo("mine")

        selfish = typer.Typer(help="tries to replace a reserved word")
        app.add_typer(selfish, name="self")

        @selfish.command("config")
        def config() -> None:
            typer.echo("hijacked")

        return app

    @hookimpl
    def sm_go_items(self, providers, settings):
        return [GoItem(60, "plugin says: backlog of 2", "sm extra hi")]


class Native(Demo):
    @hookimpl
    def sm_native_paths(self, settings):
        return {"head ship"}


@pytest.fixture(autouse=True)
def text_format(monkeypatch):
    monkeypatch.delenv("SM_FORMAT", raising=False)
    OUT["format"] = "text"
    yield
    OUT["format"] = "text"


def rt(plugin=None, providers=None, **project):
    stack = stack_of(plugin or Demo())
    project.setdefault("allow_unreviewed", True)  # the core's writers (alarm ack, focus, review verdict) are under the review gate
    return Runtime(Project(name="proj", plugin=stack.name, root=Path("."), plugins=stack.names, **project), stack, Settings({"demo": {"colour": "blue"}}),
                   _providers=providers or fake_providers())


def test_go_idle_then_names_the_plugins_item_and_is_read_only():
    r = rt()
    writes = r.providers.store.writes
    out = invoke(r, ["go"])
    assert out.exit_code == 0 and "PHASE    plugin says: backlog of 2" in out.stdout and "NEXT     sm extra hi" in out.stdout
    assert r.providers.store.writes == writes and "(read-only)" in out.stdout


def test_go_gets_a_store_that_cannot_write():
    from steuermaschine.defaults import ReadOnlyStore

    with pytest.raises(PermissionError):
        ReadOnlyStore(fake_providers().store).save("x", "y", {})


def test_go_json_is_one_document_that_fits_the_model():
    r = rt(Native())
    invoke(r, ["head", "ship", "abc"])
    out = invoke(r, ["--format", "json", "go"])
    doc = models.GoReport.model_validate(json.loads(out.stdout))
    assert doc.read_only and doc.state.runs == {"waiting": 1} and doc.next == "sm extra hi"
    assert [i.phase for i in doc.also] == ["head abc: ci: CI is running"] and doc.also[0].waiting_on == "CI"


def test_a_stalled_wait_is_an_alert_first_in_go_and_exit_3_until_acknowledged():
    r = rt(Native())
    assert invoke(r, ["head", "ship", "abc"]).exit_code == 75
    r.providers.clock.t += 16 * 60
    out = invoke(r, ["go"])
    assert out.exit_code == ALARM and "PHASE    ALERT stall:head.ship:abc" in out.stdout and "has waited 16 min" in out.stdout
    assert invoke(r, ["alarm", "ack", "stall:head.ship:abc"]).exit_code == 0
    assert invoke(r, ["go"]).exit_code == 0 and "ACKED" in invoke(r, ["alarm", "list"]).stdout


def test_plugin_alarms_show_up():
    class Loud(Demo):
        @hookimpl
        def sm_alarms(self, providers, settings):
            return [Alarm("main-red", "alert", "main is red", "sm extra hi")]

    out = invoke(rt(Loud()), ["--format", "json", "alarm", "list"])
    assert out.exit_code == ALARM and json.loads(out.stdout)[0]["key"] == "main-red"


def test_focus_ranks_a_head_first():
    r = rt(Native())
    invoke(r, ["head", "ship", "abc"])
    invoke(r, ["focus", "add", "abc", "--for", "1.2.3"])
    out = invoke(r, ["--format", "json", "go"])
    assert json.loads(out.stdout)["state"]["focus"] == ["abc"]


def test_bare_command_tree_has_core_and_plugin_commands_and_the_plugin_owns_its_words():
    r = rt()
    root = command_tree(r)
    assert {"go", "head", "release", "review", "focus", "alarm", "tools", "self", "extra"} <= set(root.commands)
    assert invoke(r, ["head", "ship"]).stdout.strip() == "plugin ship"  # the plugin's head replaces the core's
    assert invoke(r, ["head", "mine"]).stdout.strip() == "mine"


def test_native_paths_keep_the_engine_command_and_merge_the_rest_of_the_group():
    r = rt(Native())
    assert invoke(r, ["head", "ship", "abc"]).exit_code == 75  # the engine's
    assert invoke(r, ["head", "mine"]).stdout.strip() == "mine"  # the plugin's, same group
    assert "abc" in invoke(r, ["head", "status"]).stdout  # the core's status stays next to it


def test_self_is_reserved():
    assert "hijacked" not in invoke(rt(), ["self", "config"]).stdout


def test_help_contract_holds_for_the_whole_tree_and_lint_finds_a_violation():
    r = rt(Native())
    assert invoke(r, ["self", "lint"]).exit_code == 0

    class Sloppy(Demo):
        @hookimpl
        def sm_commands(self, runtime):
            app = typer.Typer()

            @app.command("oops", help="Does things.")
            def oops() -> None:
                pass

            @app.callback()
            def cb() -> None:
                pass

            return app

    out = invoke(rt(Sloppy()), ["self", "lint"])
    assert out.exit_code == 1 and "sm oops: missing WCEN" in out.stdout


def test_lint_is_strict_every_argument_and_option_has_help_and_hidden_commands_count():
    class Bare(Demo):
        @hookimpl
        def sm_commands(self, runtime):
            app = typer.Typer()

            @app.command("bare", hidden=True, help=H("Do it.", "Use when needed.", "nothing (read-only).", "sm bare X", "sm go"))
            def bare(thing: str = typer.Argument(..., metavar="THING"), loud: bool = typer.Option(False, "--loud"),
                     quiet: bool = typer.Option(False, "--quiet", help="Say less.")) -> None:
                pass

            @app.callback()
            def cb() -> None:
                pass

            return app

    out = invoke(rt(Bare()), ["self", "lint"])
    assert out.exit_code == 1 and "sm bare: missing A (no help: THING, --loud)" in out.stdout


@pytest.mark.parametrize(("text", "letters"), [
    (H("Do it.", "Use when needed.", "nothing (read-only).", "sm x", "sm y"), ""),
    ("Do it.", "WCEN"), ("", "PWCEN"), ("x" * 141 + "\nUse when y\nChanges: z\nExample: a\nNext: b", "P"),
])
def test_missing_letters(text, letters):
    assert helpdoc.missing(text) == letters


def test_help_runs_nothing_and_says_sm():
    r = rt()
    out = invoke(r, ["self", "--help"])
    assert out.exit_code == 0 and out.stdout.startswith(f"Usage: {PROG} self") and r.providers.store.writes == 0


def test_tree_is_data_for_skills_and_mcp_tools():
    rows = json.loads(invoke(rt(Native()), ["--format", "json", "self", "tree"]).stdout)
    by = {r["path"]: r for r in rows}
    assert by["sm go"]["read_only"] and not by["sm head ship"]["read_only"] and "--approved" in by["sm head ship"]["options"]


def test_json_format_errors_are_one_object_with_error_code_hint():
    out = invoke(rt(), ["--format", "json", "self", "schema", "Nope"])
    doc = json.loads(out.stdout)
    assert out.exit_code == 2 and doc["code"] == 2 and "no model" in doc["error"] and "GoReport" in doc["hint"]


def test_every_core_read_command_prints_exactly_one_json_document():
    r = rt(Native())
    invoke(r, ["head", "ship", "abc"])
    for args in (["go"], ["head", "status"], ["release", "status"], ["review", "list"], ["focus", "list"], ["alarm", "list"], ["self", "gates"], ["tools", "map"],
                 ["self", "config"], ["self", "tree"], ["self", "lint"], ["self", "schema"], ["self", "schema", "GoReport"], ["head", "ship", "abc"]):
        out = invoke(r, ["--format", "json", *args])
        json.loads(out.stdout)  # one document, nothing else on stdout
    assert json.loads(invoke(r, ["head", "status", "--json"]).stdout)[0]["key"] == "abc"  # the old spelling still works


def test_schema_is_published_for_every_model():
    for name in models.MODELS:
        assert json.loads(invoke(rt(), ["self", "schema", name]).stdout)["title"] == name


def test_self_config_says_where_each_value_came_from():
    r = rt()
    r.sources = {"demo.colour": "environment SM_DEMO__COLOUR"}
    out = invoke(r, ["self", "config"])
    assert "demo.colour = 'blue'   [environment SM_DEMO__COLOUR]" in out.stdout and "project.name = 'proj'" in out.stdout


def test_conformance_kit_accepts_a_good_plugin_and_names_what_is_wrong_with_a_bad_one():
    assert check_plugin(Native(), {"colour": "blue"}) == []

    class Loose(BaseModel):
        colour: str = "blue"

    class Bad(Demo):
        @hookimpl
        def sm_settings(self):
            return Loose

        @hookimpl
        def sm_pipelines(self, settings):
            return [Pipeline("head", "ship", "Ship.", [Step("a", waits, external=True), Step("a", waits, gates=("nope",))], identify)]

        @hookimpl
        def sm_traps(self, settings):
            return {"head.ship.zzz": "x"}

        @hookimpl
        def sm_forge(self):
            return object()

    text = "\n".join(check_plugin(Bad(), {}))
    for needle in ("extra='forbid'", "needs a description", "step names repeat", "external step needs `plan`",
                   "gate 'nope' is not defined", "trap 'head.ship.zzz' names no step", "provider forge (object) does not satisfy Forge"):
        assert needle in text, needle


def test_a_plugin_fails_loudly_at_load_for_an_unknown_hook_a_wrong_signature_or_another_api_version():
    from steuermaschine.plugins import PluginError

    class Typo(Demo):
        @hookimpl
        def sm_pipeline(self, settings):  # the hook is sm_pipelines
            return []

    class WrongArguments(Demo):
        @hookimpl
        def sm_gates(self, settings, colour):
            return []

    class Old(Demo):
        @hookimpl
        def sm_api_version(self):
            return 0

    class NoVersion:
        name = "noversion"

        @hookimpl
        def sm_gates(self, settings):
            return []

    for bad, needle in ((Typo(), "unknown hook"), (WrongArguments(), "colour"), (Old(), "plugin API 0"), (NoVersion(), "sm_api_version is missing")):
        with pytest.raises(PluginError, match=needle):
            stack_of(bad)


def test_self_plugins_and_self_hooks_describe_the_stack():
    out = invoke(rt(Native()), ["--format", "json", "self", "plugins"])
    row = json.loads(out.stdout)[0]
    assert (row["order"], row["name"], row["layer"]) == (1, "demo", "base") and "sm_pipelines" in row["hooks"] and "sm_native_paths" in row["hooks"]
    hooks = {r["hook"]: r for r in json.loads(invoke(rt(), ["--format", "json", "self", "hooks"]).stdout)}
    assert hooks["sm_forge"]["kind"].startswith("provider") and hooks["sm_step_run"]["kind"] == "wrapper" and hooks["sm_traps"]["arguments"] == ["settings"]


def one_document(out, model):
    """A json-mode write: exit 0 and stdout is exactly one JSON document that fits `model`."""
    assert out.exit_code == 0, out.stdout + out.stderr
    return model.model_validate(json.loads(out.stdout))


def test_alarm_ack_in_json_prints_one_document_and_exits_0():
    r = rt(Native())
    invoke(r, ["head", "ship", "abc"])
    r.providers.clock.t += 16 * 60
    doc = one_document(invoke(r, ["--format", "json", "alarm", "ack", "stall:head.ship:abc"]), models.AlarmAcks)
    assert doc.root == {"stall:head.ship:abc": r.providers.clock.now()}


def test_review_verdict_in_json_prints_one_document_and_exits_0():
    r = rt()
    digest = "e" * 64
    r.providers.store.save("runs.head.demo", "k", {"author": "builder-1", "steps": {"review": {"digest": digest}}})
    out = invoke(r, ["--format", "json", "review", "verdict", digest, "--approve", "--reviewer", "reviewer-1"])
    doc = one_document(out, models.Verdict)
    assert (doc.verdict, doc.reviewer, doc.at) == ("approve", "reviewer-1", r.providers.clock.now())
    assert r.providers.store.get_receipt("verdicts", digest)["verdict"] == "approve"


def test_focus_add_and_done_in_json_print_one_document_and_exit_0():
    r = rt()
    doc = one_document(invoke(r, ["--format", "json", "focus", "add", "1.2.3", "--note", "cut"]), models.FocusList)
    assert "1.2.3" in doc.model_dump()
    doc = one_document(invoke(r, ["--format", "json", "focus", "done", "1.2.3"]), models.FocusList)
    assert doc.model_dump() == {}


def test_focus_and_alarm_ack_write_under_the_store_lock_and_wait_while_another_holds_it():
    r = rt(Native())
    store, seen = r.providers.store, []
    save = store.save
    store.save = lambda kind, key, data: (seen.append((kind, set(store.held))), save(kind, key, data))[1]
    assert invoke(r, ["focus", "add", "1.2.3"]).exit_code == 0 and invoke(r, ["focus", "done", "1.2.3"]).exit_code == 0
    assert seen == [("focus", {"focus.list"}), ("focus", {"focus.list"})]
    store.held.add("focus.list")
    for argv in (["focus", "add", "1.2.4"], ["focus", "done", "1.2.3"]):
        busy = invoke(r, argv)
        assert busy.exit_code == 75 and "another process is changing focus.list" in busy.stderr, argv
    assert (store.load("focus", "list") or {}) == {}
    store.held.discard("focus.list")
    invoke(r, ["head", "ship", "abc"])
    r.providers.clock.t += 16 * 60
    store.held.add("alarms.acks")
    assert invoke(r, ["alarm", "ack", "stall:head.ship:abc"]).exit_code == 75 and store.load("alarms", "acks") is None
    store.held.discard("alarms.acks")
    assert invoke(r, ["alarm", "ack", "stall:head.ship:abc"]).exit_code == 0 and seen[-1] == ("alarms", {"alarms.acks"})


class Locking(Demo):
    """A pipeline whose one step reports whether the writers' lock is held while it runs."""

    seen: list[bool] = []

    @hookimpl
    def sm_pipelines(self, settings):
        def probe(ctx):
            import fcntl

            from steuermaschine.config import state_home

            with (state_home() / "proj" / "writers.lock").open("a") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # what `sm self migrate-state` takes
                    Locking.seen.append(False)
                except BlockingIOError:
                    Locking.seen.append(True)
            raise Wait("CI is running", "CI")

        return [Pipeline("release", "cut", "Cut one release.", [Step("probe", probe)], identify)]


def test_a_pipeline_run_holds_the_writers_lock_and_waits_while_a_promote_switches(tmp_path, monkeypatch):
    """C2 of the dod13 review: `sm release cut` and `sm head admit` are pipelines, not native commands; a run takes the
    writers' lock (shared) as a typed write does, and starts nothing while bin/promote or bin/rollback switches."""
    import os

    Locking.seen = []
    r = rt(Locking())
    assert invoke(r, ["release", "cut", "1.0"]).exit_code == 75 and Locking.seen == [True]
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "sm-promote-switch-pending").write_text(f"{os.getpid()} 2026-11-13T16:00:00Z\n")
    held = invoke(r, ["release", "cut", "1.0"])
    assert held.exit_code == 75 and "is switching the live sm" in held.stderr and Locking.seen == [True]  # the step did not run
    assert invoke(r, ["release", "cut", "1.0", "--plan"]).exit_code == 0 and Locking.seen == [True]  # a plan writes nothing: not held


def test_tools_gaps_wrapped_counts_the_commands_mounted_outside_the_native_contract():
    """R6 of the dod13 review: `--wrapped` is a count, not a constant. The demo mounts four plain Typer commands."""
    from steuermaschine.app import mounted_commands

    r = rt()
    assert sorted(mounted_commands(r)) == ["extra hi", "head mine", "head ship", "self config"]
    assert invoke(r, ["tools", "gaps", "--wrapped"]).stdout == "4\n"
    assert json.loads(invoke(r, ["--format", "json", "tools", "gaps"]).stdout)["wrapped"] == 4
