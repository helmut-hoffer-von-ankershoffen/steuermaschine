"""`lint.write_reach`: which functions a read command's calls are followed into, and where the search stops."""

import importlib
import sys

import pytest

from steuermaschine import lint
from steuermaschine.native import NativeCommand

WRITE = "    gh.api('repos/x/y/pulls', None, 'POST')\n"


@pytest.fixture
def package(tmp_path, monkeypatch):
    """`reachpkg`: area.py with area_lib (its unit), a sibling module and a library of another area outside it."""
    root = tmp_path / "reachpkg"
    for directory in ("area_lib", "other_lib"):
        (root / directory).mkdir(parents=True)
        (root / directory / "__init__.py").write_text("")
    (root / "__init__.py").write_text("")
    (root / "area_lib" / "table.py").write_text(
        "import subprocess\nfrom ..other_lib.edge import run as go\nfrom . import again\n\n\n"
        "def read(gh):\n    return gh.api('repos/x/y/pulls')\n\n\n"
        "def run(gh):\n" + WRITE + "\n\n"
        "def plain(gh):\n    subprocess.run(['true'])\n    return read(gh)\n\n\n"
        "def through(gh):\n    return go(gh)\n\n\n"
        "def shared(gh):\n    return again.shared(gh)\n")
    (root / "area_lib" / "again.py").write_text("def shared(gh):\n" + WRITE + "\n\ndef post(gh):\n" + WRITE)
    (root / "other_lib" / "edge.py").write_text(
        "def run(commands):\n    return commands.run(['true'])\n\n\n"  # `commands.run` is not the unit's `run`
        "def write(gh):\n" + WRITE)
    (root / "other_lib" / "front.py").write_text("from .edge import write  # noqa: F401 - passed on\n")
    (root / "sibling.py").write_text("from .other_lib import front\n\n\ndef status(gh):\n    return front.write(gh)\n")
    (root / "area.py").write_text(
        "from .area_lib import table\n\n\n"
        "def exact(gh):\n    return table.plain(gh)\n\n\n"
        "def by_object(gh, thing):\n    return thing.post(gh)\n\n\n"
        "def outside(gh):\n    return table.through(gh)\n\n\n"
        "def two_steps(gh):\n    return table.shared(gh)\n\n\n"
        "def lazy(gh):\n    from . import sibling\n\n    return sibling.status(gh)\n\n\n"
        "def unknown(gh):\n    return nowhere(gh)\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [m for m in sys.modules if m.startswith("reachpkg")]:
        del sys.modules[name]
    module = importlib.import_module("reachpkg.area")
    return lambda name: lint.write_reach(NativeCommand(f"area {name}", getattr(module, name), ""))


def test_a_call_on_an_imported_module_is_followed_into_that_file_only(package):
    assert package("exact") == []  # table.plain -> read: not the `run` that writes, and not `subprocess.run` by its name
    assert package("two_steps") == ["again.py:shared: line 2: GitHub API call with a method other than GET"]


def test_a_call_on_an_object_is_followed_by_name_into_the_whole_unit(package):
    assert package("by_object") == ["again.py:post: line 6: GitHub API call with a method other than GET"]


def test_an_import_leads_out_of_the_unit_and_there_the_net_is_that_file_alone(package):
    assert package("outside") == []  # other_lib/edge.py: `commands.run` does not come back to the unit's `run`
    # an import inside the function, a module outside the unit, and a name that module only passes on
    assert package("lazy") == ["edge.py:write: line 6: GitHub API call with a method other than GET"]


def test_a_name_nothing_defines_and_a_command_without_a_file_find_nothing(package):
    assert package("unknown") == []

    def nowhere():
        pass

    nowhere.__module__ = "not_a_loaded_module"
    assert lint.write_reach(NativeCommand("ghost read", nowhere, "")) == []
