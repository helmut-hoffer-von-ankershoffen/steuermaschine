"""`--format json` prints exactly one document from a typed model, for every command of every shipped project.

The test walks the built command tree. A native command is checked by its return annotation: models only (a list,
a union or `| None` of them; None prints `Nothing`). A core command prints through `emit`, whose model is required.
A command a plugin adds as a plain Typer app has no contract and fails the test.

Every command, native ones too, is then run in json format with a filler value for each required argument and option
(fake providers): stdout must be one JSON document that its own result model parses (a native command's annotated
models, the model a core command hands to `emit`), or `Error` when the exit code is non-zero.
"""

import copy
import enum
import inspect
import io
import json
import sys
import typing
from pathlib import Path

import pytest
from pydantic import RootModel

from steuermaschine import PROG, app, helpdoc, models
from steuermaschine.app import Runtime, command_tree
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel.plugin import plugin as heinzel
from steuermaschine_layer.plugin import plugin as layer
from steuermaschine_toy.plugin import plugin as toy

STACKS = {"heinzel": (heinzel,), "toy": (toy,), "layered": (toy, layer)}


def runtime(name):
    stack = stack_of(*STACKS[name])
    settings, _ = stack.validate({}, Path("."))
    names = [p.name for p in STACKS[name]]
    return Runtime(Project(name=name, plugin=names[0], root=Path("."), plugins=names), stack, settings, _providers=fake_providers())


def leaves(rt):
    for path, command, is_group in helpdoc.walk(command_tree(rt), PROG):
        if not is_group:
            yield path, command


def native_of(command):
    return getattr(getattr(command.callback, "__wrapped__", None), "__sm_native__", None)


def only_models(hint) -> bool:
    if hint is type(None):
        return True
    if isinstance(hint, type):
        return issubclass(hint, models.Model)
    args = typing.get_args(hint)
    return bool(args) and all(only_models(a) for a in args)


def test_emit_needs_a_model():
    assert inspect.signature(app.emit).parameters["model"].default is inspect.Parameter.empty


@pytest.mark.parametrize("name", sorted(STACKS))
def test_every_command_is_native_with_a_model_or_a_core_command(name):
    rt = runtime(name)
    bad = []
    for path, command in leaves(rt):
        cmd = native_of(command)
        if cmd is not None:
            hint = typing.get_type_hints(cmd.fn).get("return")
            if not only_models(hint) or not cmd.models:
                bad.append(f"{path}: returns {hint}")
        elif not command.callback.__module__.startswith("steuermaschine."):
            bad.append(f"{path}: a plain Typer command of {command.callback.__module__}; make it @native with a result model")
    assert not bad, bad


@pytest.mark.parametrize("name", sorted(STACKS))
def test_self_schema_lists_every_result_model_and_prints_each(name):
    rt = runtime(name)
    listed = set(json.loads(invoke(rt, ["--format", "json", "self", "schema"]).stdout))
    wanted = set(models.MODELS) | {m.__name__ for c in rt.plugin.native(rt.settings) if hasattr(c, "fn") for m in c.models}
    assert wanted <= listed
    for model in sorted(listed):
        out = invoke(rt, ["--format", "json", "self", "schema", model])
        assert out.exit_code == 0 and isinstance(json.loads(out.stdout), dict), model


def test_a_native_command_that_returns_none_prints_nothing_typed(capsys):
    app.OUT["format"] = "json"
    try:
        app.emit_result(None)
    finally:
        app.OUT["format"] = "text"
    assert json.loads(capsys.readouterr().out) == models.Nothing().model_dump()


def filler(command) -> list[str]:
    """One plain value for each required argument and option: the first choice, 1 for a number, else `x`."""
    words: list[str] = []
    for param in command.params:
        if not param.required:
            continue
        hint = param.type_descriptor.annotation
        if isinstance(hint, type) and issubclass(hint, enum.Enum):
            value = str(next(iter(hint)).value)
        elif typing.get_origin(hint) is typing.Literal:
            value = str(typing.get_args(hint)[0])
        else:
            value = "1" if hint in (int, float) else "x"
        words += [param.opts[0], value] if param.param_type_name == "option" else [value]
    return words


def parses(model, doc) -> bool:
    try:
        rows = isinstance(doc, list) and not issubclass(model, RootModel)
        _ = [model.model_validate(d) for d in doc] if rows else model.model_validate(doc)
    except ValueError:
        return False
    return True


@pytest.fixture
def module_state():
    """Running every command sets module globals that other tests rely on (a command calls `configure(...)` of its
    library with the settings' defaults). Put every first-party module's globals back afterwards: rebound names to
    their old object, changed dicts, lists and sets to their old contents."""
    first_party = ("steuermaschine", "steuermaschine_heinzel", "steuermaschine_toy", "steuermaschine_layer")
    saved = {}
    for name, module in list(sys.modules.items()):
        if name.split(".")[0] in first_party and module is not None:
            saved[name] = {k: (v, copy.copy(v) if isinstance(v, (dict, list, set)) else None) for k, v in vars(module).items()}
    yield
    for name, values in saved.items():
        module = sys.modules[name]
        for key, (value, contents) in values.items():
            if getattr(module, key, None) is not value:
                setattr(module, key, value)
            if contents is not None and value != contents:
                value.clear()
                value.update(contents) if isinstance(value, (dict, set)) else value.extend(contents)


@pytest.mark.parametrize("name", sorted(STACKS))
def test_every_command_prints_one_document_its_model_parses(name, monkeypatch, module_state):
    rt = runtime(name)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # the hook commands read stdin; never wait for a terminal
    handed: list[type] = []
    real_emit = app.emit
    monkeypatch.setattr(app, "emit", lambda as_json, data, lines, model: (handed.append(model), real_emit(as_json, data, lines, model))[1])
    bad = []
    for path, command in leaves(rt):
        handed.clear()
        out = invoke(rt, ["--format", "json", *path.split()[1:], *filler(command)])
        cmd = native_of(command)
        wanted = (cmd.models or [models.Nothing]) if cmd is not None else handed[-1:]
        try:
            doc = json.loads(out.stdout)  # exactly one document: two would not parse
        except ValueError:
            bad.append(f"{path}: exit {out.exit_code}, not one JSON document: {out.stdout[:120]!r}")
            continue
        if not any(parses(m, doc) for m in [*wanted, *([models.Error] if out.exit_code else [])]):
            bad.append(f"{path}: exit {out.exit_code}, {[m.__name__ for m in wanted]} do not parse {out.stdout[:120]!r}")
    assert not bad, bad
