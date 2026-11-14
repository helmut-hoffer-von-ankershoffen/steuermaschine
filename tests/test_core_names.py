"""No old tool name and no old command name in anything a command shows: help, argument and option help, metavars
and defaults, result models, setting descriptions and defaults, for every shipped project.
The tool map is the one place that names old tools: it is the map of them.

COMPAT lists the old names that stay on purpose: files and variables other programs (the old tools still running on the
machine, the product's own release receipts) read or write. Renaming one silently would split the state in two; each
goes when its last old reader is gone. A new entry needs a reason here."""

import re
from pathlib import Path

import pytest

from steuermaschine import PROG, helpdoc
from steuermaschine.app import Runtime, command_tree
from steuermaschine.config import Project
from steuermaschine.models import Model
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers
from steuermaschine_heinzel.plugin import plugin

OLD = re.compile(r"\bhz-[a-z]|\bhz" + r"c\b|\.cache/hz")
WORD = re.compile(r"\bhz-[a-z][\w.-]*|\bhz" + r"c\b|\.cache/hz[\w.-]*")  # HZ_* variables are the product's: see test_core_settings
COMPAT: dict[str, set[str]] = {
    "heinzel.skills_allow_words": {"hz-builder", "hz-chore"},  # agent types and skill names, not tools
}


def words(text: str) -> set[str]:
    return set(WORD.findall(text))


def runtime():
    stack = stack_of(plugin)
    settings, _ = stack.validate({}, Path("."))
    return Runtime(Project(name="names", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, settings, _providers=fake_providers())


def test_help_of_every_command_but_the_tool_map_is_free_of_old_names():
    rt = runtime()
    for row in helpdoc.tree(command_tree(rt), PROG):
        if row["path"].startswith(f"{PROG} tools"):
            continue
        assert not OLD.search(row["help"]), (row["path"], OLD.search(row["help"])[0])


@pytest.mark.parametrize("name", ["heinzel", "toy", "layered"])
def test_arguments_options_and_settings_are_free_of_old_names_but_the_compat_list(name):
    from test_core_json_contract import runtime as project

    rt = project(name)
    found = {}
    for path, command, _ in helpdoc.walk(command_tree(rt), PROG):
        if path.startswith(f"{PROG} tools"):
            continue
        texts = [command.help or "", command.short_help or ""]
        texts += [f"{getattr(p, 'help', '') or ''} {p.metavar or ''} {p.default}" for p in command.params]
        found[path[len(PROG) + 1:]] = words(" ".join(texts))
    for plugin_name, model in rt.plugin.settings_models().items():
        for field, info in model.model_fields.items():
            found[f"{plugin_name}.{field}"] = words(f"{info.description} {info.default}")
    for key, seen in found.items():
        assert seen <= COMPAT.get(key, set()), (key, seen - COMPAT.get(key, set()))


def test_the_compat_list_has_no_stale_entry():
    from test_core_json_contract import runtime as project

    rt = project("heinzel")
    models = rt.plugin.settings_models()
    for key in COMPAT:
        if key.startswith("heinzel."):
            assert key.split(".", 1)[1] in models["heinzel"].model_fields, key


def test_result_models_of_native_commands_are_free_of_old_names():
    rt = runtime()
    for cmd in rt.plugin.native(rt.settings):
        model = getattr(cmd, "model", None)
        if isinstance(model, type) and issubclass(model, Model):
            text = str(model.model_json_schema())
            assert not OLD.search(text), (cmd.path, OLD.search(text)[0])


def test_the_core_names_no_project():
    import steuermaschine

    for path in Path(steuermaschine.__file__).parent.rglob("*.py"):
        text = path.read_text().lower()
        for word in ("heinzel", "hh" + "va", "helmut"):
            assert word not in text, (path.name, word)


def test_no_string_of_the_plugin_names_an_old_tool_or_state_path():
    """Strict at zero (bin/debt.py counts it): output, help and state paths name sm and the state directory. The maps of
    old names (tool map, moved maps, the output translation, the state moves) and a line that says why it keeps a name
    (`# old name kept: ...`) are the only places with one."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
    import debt

    assert debt.old_names() == 0
