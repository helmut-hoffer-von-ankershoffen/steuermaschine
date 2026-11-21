"""The visible surface (surface.py), the one parser for list options (listopts.py), trusted call counts, the journal of
old runs, `self doc` and `self tokens`: on a small plugin that declares a surface over its own commands."""

import json
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer

from steuermaschine import API_VERSION, PROG, go, helpdoc, journal, listopts, statsreport, surface, usagelog
from steuermaschine.app import OUT, Runtime, command_tree
from steuermaschine.config import Project
from steuermaschine.helpdoc import H
from steuermaschine.hookspecs import hookimpl
from steuermaschine.models import Model, Tokens
from steuermaschine.native import Default, Renamed, Surface, Verb, collect, native
from steuermaschine.plugins import Settings, stack_of
from steuermaschine.testing import fake_providers, invoke


class Seen(Model):
    what: str
    items: list[str] = []

    def render(self) -> list[str]:
        return [f"{self.what} {','.join(self.items)}".strip()]


@native("box list", help=H("List the box.", "Use when testing.", "nothing (read-only).", "sm box list", "sm go"))
def box_list(kind: str = typer.Argument("all", help="Which kind."), tag: list[str] | None = typer.Option(None, "--tag", metavar="TAG[,TAG...]", help="A tag."),
             text: list[str] | None = typer.Option(None, "--text", metavar="TEXT", help="A text that may hold a comma.")) -> Seen:
    return Seen(what=f"list {kind}", items=[*(tag or []), *(text or [])])


@native("box put", writes=True, help=H("Put into the box.", "Use when testing.", "writes the box.", "sm box put x", "sm go"))
def box_put(item: str = typer.Argument(..., metavar="ITEM", help="The item.")) -> Seen:
    return Seen(what=f"put {item}")


@native("box run", writes=True, help=H("Run the box.", "Use when testing.", "writes the box.", "sm box run 1.2.3", "sm go"))
def box_run(version: str = typer.Argument(..., metavar="VERSION", help="The version.")) -> Seen:
    return Seen(what=f"run {version}")


@native("box plumbing", help=H("A job's tick.", "Use when testing.", "nothing (read-only).", "sm box plumbing", "sm go"))
def box_plumbing() -> Seen:
    return Seen(what="plumbing")


area = types.ModuleType("surface_area")
area.__dict__.update(
    SURFACE=Surface(verbs=(("", "the next step"), ("stuff list|put", "the items"), ("box [VERSION]", "a run")),
                    visible=("stuff", "stuff list", "stuff put", "box"), rules=("  ITEM            one thing.",),
                    helps=(("box", H("box: a run of one version.", "Use when testing.", "nothing by itself.", "sm box 1.2.3", "sm go")),)),
    LIST=Verb("stuff list", "box list"), PUT=Verb("stuff put", "box put"),
    RENAMES=[Renamed("box list", "stuff list"), Renamed("box put", "stuff put"), Renamed("box run", "box", stub=False)],
    DEFAULT=Default("box", "run", empty="list", metavar="[VERSION]"))
for fn in (box_list, box_put, box_run, box_plumbing):
    fn.__module__ = "surface_area"
    setattr(area, fn.__name__, fn)
sys.modules["surface_area"] = area


class Boxes:
    name = "boxes"

    @hookimpl
    def sm_api_version(self):
        return API_VERSION

    @hookimpl
    def sm_native(self):
        return collect(area)


@pytest.fixture(autouse=True)
def text_format(monkeypatch):
    monkeypatch.delenv("SM_FORMAT", raising=False)
    OUT["format"] = "text"


def rt(tmp_path=None):
    stack = stack_of(Boxes())
    return Runtime(Project(name="p", plugin="boxes", root=Path(tmp_path or "."), plugins=["boxes"], allow_unreviewed=True), stack, Settings({}),
                   _providers=fake_providers())


def visible(r):
    return {row["path"] for row in helpdoc.tree(command_tree(r), PROG) if not row["hidden"]}


def test_only_the_declared_commands_are_visible_and_hidden_ones_keep_working():
    r = rt()
    assert {"sm stuff", "sm stuff list", "sm stuff put", "sm box"} <= visible(r)
    assert not {"sm box list", "sm box put", "sm box plumbing", "sm go", "sm self", "sm review"} & visible(r)
    assert invoke(r, ["box", "plumbing"]).stdout == "plumbing\n"
    assert invoke(r, ["stuff", "put", "x"]).stdout == "put x\n"


def test_a_read_only_stub_runs_the_new_command_and_says_so_on_stderr():
    out = invoke(rt(), ["box", "list", "red"])
    assert (out.exit_code, out.stdout) == (0, "list red\n") and "Renamed: sm stuff list (was: sm box list)" in out.stderr


def test_a_writing_stub_runs_nothing_and_exits_2_naming_the_new_command():
    out = invoke(rt(), ["box", "put", "x"])
    assert out.exit_code == 2 and out.stdout == "" and "Renamed: sm stuff put (was: sm box put)" in out.stderr
    doc = json.loads(invoke(rt(), ["--format", "json", "box", "put", "x"]).stdout)
    assert doc["code"] == 2 and "stuff put" in doc["error"]


def test_a_default_routes_a_word_that_is_no_subcommand_and_bare_group_runs_its_empty_line():
    assert invoke(rt(), ["box", "1.2.3"]).stdout == "run 1.2.3\n"
    out = invoke(rt(), ["box"])
    assert out.stdout == "list all\n" and "Renamed" in out.stderr
    assert "[VERSION]" in invoke(rt(), ["box", "-h"]).stdout and "box: a run of one version." in invoke(rt(), ["box", "-h"]).stdout


def test_the_resolver_names_one_match_several_or_a_close_one_and_never_runs():
    one = invoke(rt(), ["plumbing"])
    assert one.exit_code == 2 and "Did you mean: sm box plumbing" in one.stderr
    many = invoke(rt(), ["list"])
    assert many.exit_code == 2 and "Did you mean one of: sm stuff list" in many.stderr
    close = invoke(rt(), ["plumbin"])
    assert close.exit_code == 2 and "sm box plumbing" in close.stderr
    joined = invoke(rt(), ["box-put"])
    assert "Did you mean: sm stuff put ITEM" in joined.stderr
    assert invoke(rt(), ["zzzzzz"]).exit_code == 2


def test_root_help_follows_the_contract_and_the_lint_checks_it():
    out = invoke(rt(), ["-h"]).stdout
    assert out.startswith("sm (steuermaschine)") and "Do what NEXT says" in out and "sm stuff list|put" in out and "ITEM" in out
    assert len(out.splitlines()) <= surface.HELP_MAX_LINES and surface.help_findings(out) == []
    assert invoke(rt(), ["self", "lint"]).exit_code == 0
    bad = surface.help_findings("x\n" * 61 + "the release train candidate")
    assert any("lines, more than 60" in b for b in bad) and any("'train'" in b for b in bad) and any("'candidate'" in b for b in bad)
    assert any("Exit codes" in b for b in bad)


def test_translate_and_dry_run():
    renames = [Renamed("box list", "stuff list"), Renamed("box run", "box", stub=False), Renamed("go", "", stub=False)]
    assert surface.translate("then: sm box list red && sm go", renames) == "then: sm stuff list red && sm"
    assert surface.translate("sm box listing", renames) == "sm box listing"
    root = command_tree(rt())
    assert surface.commands_in("cd x && sm stuff put y; sm box 1.2.3 (soon)") == [["stuff", "put", "y"], ["box", "1.2.3"]]
    assert surface.dry_run(root, ["stuff", "put", "y"]) == "" and surface.dry_run(root, ["box", "1.2.3"]) == ""
    assert surface.dry_run(root, ["box", "plumbing"]) == "sm box plumbing is hidden"
    assert "Missing argument" in surface.dry_run(root, ["stuff", "put"]) or "ITEM" in surface.dry_run(root, ["stuff", "put"])
    assert surface.dry_run(root, ["stuff"]) == "sm stuff: needs a command" and surface.dry_run(root, []) == ""
    assert surface.dry_run(root, ["stuff", "list", "--help"]) == ""
    assert "No such command" in surface.dry_run(root, ["nope"])


def test_a_project_without_a_surface_is_unchanged(tmp_path):
    from tests.test_core_native import rt as demo

    assert "sm demo list" in visible(demo())
    assert "Did you mean" in invoke(demo(), ["lst-demo"]).stderr or invoke(demo(), ["lst-demo"]).exit_code == 2


def test_list_options_take_repeats_and_comma_lists_through_one_parser():
    assert json.loads(invoke(rt(), ["--format", "json", "stuff", "list", "--tag", "a,b", "--tag", "c"]).stdout)["items"] == ["a", "b", "c"]
    kept = json.loads(invoke(rt(), ["--format", "json", "stuff", "list", "--text", "test_x[a,b]", "--text", "c"]).stdout)["items"]
    assert kept == ["test_x[a,b]", "c"]  # not a key list: a comma inside a value stays
    assert listopts.split(["a, b", "", "c", 3]) == ["a", "b", "c", 3]
    root = command_tree(rt())
    assert "Repeat it, or give A,B." in next(p.help for p in surface.find(root, "stuff list").params if "--tag" in getattr(p, "opts", []))
    assert listopts.findings(root) == []

    app = typer.Typer()

    @app.command()
    def one(labels: str = typer.Option("", "--labels", help="Labels, comma-separated.")) -> None:
        """One."""

    @app.command()
    def two() -> None:
        """Two."""

    assert listopts.findings(typer.main.get_command(app)) == ["sm one: --labels says comma-separated: make it a list option (repeat it, or give A,B)"]


def test_trusted_call_counts(tmp_path):
    now = datetime(2026, 11, 21, 12, tzinfo=UTC)
    rows = [("2026-11-20", "box list"), ("2026-11-21", "box list"), ("2026-10-01", "stuff put")]
    found = statsreport.calls(rows, {"box list": True, "stuff put": False, "box plumbing": True}, 30, now)
    assert found.trusted and found.calls == 2 and next(c.model_dump() for c in found.commands) == {"command": "box list", "calls": 2, "last": "2026-11-21", "hidden": True}
    assert {c.command: c.calls for c in found.commands} == {"box list": 2, "stuff put": 0, "box plumbing": 0}
    assert "TRUSTED" in found.render()[0]
    young = statsreport.calls(rows[:2], {"box list": True}, 30, now)
    assert not young.trusted and "NOT TRUSTED" in young.render()[0]
    usagelog.count_call(tmp_path, "box list", 0, now.timestamp())
    (tmp_path / usagelog.CALLS / "2026-01.tsv").write_text("2026-01-02\told\t0\tx\nbroken\n")
    usagelog.count_call(tmp_path, "box list", 1, datetime(2026, 12, 1, tzinfo=UTC).timestamp())  # a new month drops files over three months old
    assert sorted(p.name for p in (tmp_path / usagelog.CALLS).iterdir()) == ["2026-11.tsv", "2026-12.tsv"]
    assert statsreport.read_calls(tmp_path / usagelog.CALLS) == [("2026-11-21", "box list"), ("2026-12-01", "box list")]
    assert statsreport.read_calls(tmp_path / "missing") == []


def test_only_real_calls_are_counted(tmp_path, monkeypatch):
    monkeypatch.setenv("STEUERMASCHINE_HOME", str(tmp_path))
    r = rt(tmp_path)
    invoke(r, ["stuff", "list"])
    invoke(r, ["stuff", "list", "-h"])
    invoke(r, ["nothing-here"])
    invoke(r, ["stuff"])
    counted = statsreport.read_calls(r.project.home / usagelog.CALLS)
    assert [c for _, c in counted] == ["stuff list"]
    out = invoke(r, ["--format", "json", "self", "stats", "--calls"])
    doc = json.loads(out.stdout)
    assert doc["calls"] == 1 and {"command": "box plumbing", "calls": 0, "last": "", "hidden": True} in doc["commands"]


def test_history_is_finished_or_untouched_for_days():
    now = datetime(2026, 11, 21, 12, tzinfo=UTC).timestamp()
    assert go.is_history({"status": "finished"}, now, 2) and go.is_history({"updated": "2026-11-01T10:00:00+0000"}, now, 2)
    assert not go.is_history({"updated": "2026-11-21T10:00:00+0000"}, now, 2) and not go.is_history({"updated": "nonsense"}, now, 2)


def test_rewrite_keeps_the_moved_text_first(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("a\nb\n")
    kept: list[str] = []
    assert journal.rewrite(doc, lambda t: (t.replace("b\n", ""), "b"), kept.append, [tmp_path / "lock"]) == ("a\nb\n", "a\n")
    assert doc.read_text() == "a\n" and kept == ["b"]
    assert journal.rewrite(doc, lambda t: (t, ""), kept.append) == ("a\n", "a\n") and kept == ["b"]


def test_self_doc_and_tokens(tmp_path):
    out = invoke(rt(), ["self", "doc"]).stdout
    assert out.startswith("# `sm` command reference") and "## `sm stuff`" in out and "### `sm stuff put` (writes)" in out and "## `sm stuff` (writes)" not in out and "box plumbing" not in out
    assert json.loads(invoke(rt(), ["--format", "json", "self", "doc"]).stdout).startswith("# `sm`")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join([json.dumps({"message": {"usage": {"input_tokens": 3, "output_tokens": 4, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 6}}}),
                                     json.dumps({"message": {"usage": {"input_tokens": True}}}), "not json", json.dumps({"type": "user"})]))
    found = Tokens.count(transcript.read_text().splitlines())
    assert (found.answers, found.total) == (2, 18)
    assert "2 answers: 18 tokens" in invoke(rt(), ["self", "tokens", str(transcript)]).stdout
    assert invoke(rt(), ["self", "tokens", str(tmp_path / "none.jsonl")]).exit_code == 2


def test_tree_all_lists_hidden_commands():
    rows = json.loads(invoke(rt(), ["--format", "json", "self", "tree", "--all"]).stdout)
    hidden = {r["path"] for r in rows if r["hidden"]}
    assert "sm box plumbing" in hidden and "sm self tree" in hidden and "sm stuff list" not in hidden
    assert "WH sm box put:" in invoke(rt(), ["self", "tree", "--all"]).stdout or "RH sm box put:" in invoke(rt(), ["self", "tree", "--all"]).stdout
