"""The plugin as a whole: conformant, no old command or script name in what it prints, a complete tool map."""

from pathlib import Path

import steuermaschine_heinzel
from steuermaschine_heinzel.plugin import plugin
from steuermaschine_heinzel.toolmap import ROWS

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.testing import assert_conformant, fake_providers, invoke

OLD_COMMAND = "hz" + "c"


def rt():
    stack = stack_of(plugin)
    settings, _ = stack.validate({}, Path("."))
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, settings, _providers=fake_providers())


def test_conformance():
    assert_conformant(plugin, {})


def test_the_old_command_name_is_gone_from_the_plugins_code():
    package = Path(steuermaschine_heinzel.__file__).parent
    assert all(OLD_COMMAND not in p.read_text() for p in package.rglob("*.py"))


def test_nothing_in_the_plugin_starts_an_old_script():
    package = Path(steuermaschine_heinzel.__file__).parent
    for path in package.rglob("*.py"):
        text = path.read_text()
        assert "tools_dir" not in text and "heinzel-management/tools" not in text, path


def test_every_old_tool_has_exactly_one_row_and_a_missing_one_names_its_owner():
    tools = [r.tool for r in ROWS]
    assert len(tools) == len(set(tools)) and len(tools) >= 84
    for row in ROWS:
        assert row.kind in ("command", "internal", "retired")
        assert (row.kind == "command") == bool(row.path), row.tool
        assert row.kind != "command" or row.owner.startswith(("heinzel-native-", "core-")), row.tool
        assert not row.path.startswith("sm ")


def test_the_rows_the_core_serves_carry_the_flag_in_the_tool_map_itself():
    """One place for the data: toolmap.py marks a row the core's own command serves; the hook returns the rows as they are."""
    served = {row.path for row in ROWS if row.core}
    assert {"doc section", "gate run", "gate status", "lim", "self lint"} <= served
    assert plugin.sm_tool_map() == ROWS


def test_tools_gaps_counts_the_project_pipelines_as_ported():
    r = rt()
    assert {"head admit", "release cut"} <= {f"{pl.subject} {pl.verb}" for pl in r.pipelines()}
    gaps = invoke(r, ["tools", "gaps"]).stdout
    assert "MISSING hz-focused-admit" not in gaps and "0 missing" in gaps
    where = invoke(r, ["tools", "where", "hz-focused-admit"])
    assert where.exit_code == 0 and where.stdout.startswith("hz-focused-admit -> sm head admit ")


def test_tools_gaps_lists_what_is_not_ported_with_its_owner_and_where_says_not_ported_yet():
    r = rt()
    out = invoke(r, ["tools", "gaps"])
    missing = [line for line in out.stdout.splitlines() if line.startswith("MISSING ")]
    native = len([row for row in ROWS if row.kind == "command"]) - len(missing)
    assert out.exit_code == (1 if missing else 0) and out.stdout.splitlines()[-1].startswith(f"{len(missing)} missing, {native} native")
    assert invoke(r, ["tools", "gaps", "--wrapped"]).stdout.strip() == "0"
    retired = invoke(r, ["tools", "where", "hz-card"])
    assert retired.exit_code == 0 and "retired" in retired.stdout
    for line in missing[:1]:
        tool = line.split()[1].rstrip(":")
        where = invoke(r, ["tools", "where", tool])
        assert where.exit_code == 1 and "not ported yet" in where.stdout and "owner: heinzel-native-" in where.stdout
