"""`steuermaschine.reviewgraph`: the files a write path reaches and the write paths a change reaches."""

import importlib
import sys

import pytest

from steuermaschine.native import unit
from steuermaschine.reviewgraph import parsed_once, reach, reached_by


@pytest.fixture
def units(tmp_path, monkeypatch):
    """A package `graphpkg` with two units: `left` (left.py + left_lib, imports shared.py and core's hashing) and
    `right` (right.py, imports shared.py and one module of tools_lib). `lonely.py` is imported by nothing;
    tools_lib holds a module nothing imports, a data file in a directory without `__init__.py`, and a cache."""
    root = tmp_path / "graphpkg"
    for directory in ("left_lib", "tools_lib/data", "tools_lib/__pycache__", "tools_lib/.ruff_cache"):
        (root / directory).mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (root / "shared.py").write_text("VALUE = 1\n")
    (root / "lonely.py").write_text("VALUE = 2\n")
    (root / "left_lib" / "__init__.py").write_text("from steuermaschine.hashing import canonical_sha256  # noqa\nfrom .. import shared  # noqa\n")
    (root / "left.py").write_text("from . import left_lib  # noqa\n\n\ndef anchor():\n    pass\n")
    (root / "tools_lib" / "__init__.py").write_text("")
    (root / "tools_lib" / "named.py").write_text("VALUE = 3\n")
    (root / "tools_lib" / "by_string.py").write_text("VALUE = 4\n")  # loaded with importlib at run time: no import names it
    (root / "tools_lib" / "data" / "ci.yml").write_text("on: push\n")
    (root / "tools_lib" / "__pycache__" / "named.cpython-315.pyc").write_bytes(b"\0")
    (root / "tools_lib" / ".ruff_cache" / "tag").write_text("")
    (root / "right.py").write_text("from .shared import VALUE  # noqa\nfrom .tools_lib import named  # noqa\n\n\ndef anchor():\n    pass\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [m for m in sys.modules if m.startswith("graphpkg")]:
        del sys.modules[name]
    left, right = importlib.import_module("graphpkg.left"), importlib.import_module("graphpkg.right")
    return [unit("left write", left.anchor), unit("right write", right.anchor)]


def test_reach_names_the_unit_and_the_files_it_imports(units):
    left, right = reach(units)
    assert (left.path, left.own) == ("left write", ("graphpkg/left.py", "graphpkg/left_lib/__init__.py"))
    assert left.imported == ("graphpkg/shared.py", "steuermaschine/hashing.py")
    assert right.own == ("graphpkg/right.py",) and "graphpkg/shared.py" in right.imported
    assert left.files == (*left.own, *left.imported)


def test_one_file_of_a_lib_package_brings_the_whole_package_with_its_data_files_and_no_cache(units):
    _, right = reach(units)
    assert right.imported == ("graphpkg/shared.py", "graphpkg/tools_lib/__init__.py", "graphpkg/tools_lib/by_string.py",
                              "graphpkg/tools_lib/data/ci.yml", "graphpkg/tools_lib/named.py")
    assert "graphpkg/__init__.py" not in right.files  # not the packages above a module: a registry there would reach everything


def test_a_unit_without_code_has_no_reach():
    def nowhere():
        pass

    nowhere.__module__ = "not_a_loaded_module"
    assert reach([unit("ghost write", nowhere)]) == []


def test_reached_by_names_the_write_paths_a_change_reaches(units):
    found = reach(units)
    assert reached_by(found, ["src/steuermaschine/hashing.py"]) == {"left write": ["src/steuermaschine/hashing.py"]}  # a path as git prints it
    assert list(reached_by(found, ["graphpkg/shared.py"])) == ["left write", "right write"]  # a package path
    assert reached_by(found, ["plugins/x/src/graphpkg/right.py", "README.md"]) == {"right write": ["plugins/x/src/graphpkg/right.py"]}
    assert reached_by(found, ["graphpkg/lonely.py", "other/hashing.py", "xsteuermaschine/hashing.py"]) == {}
    assert list(reached_by(found, ["src/graphpkg/tools_lib/by_string.py"])) == ["right write"]  # no import names it
    assert list(reached_by(found, ["src/graphpkg/tools_lib/data/ci.yml"])) == ["right write"]
    assert reached_by(found, [".github/workflows/ci.yml", "src/graphpkg/tools_lib/.ruff_cache/tag"]) == {}  # another file of that name; a cache


def test_reach_parses_a_shared_file_once_for_all_units(units, monkeypatch):
    from steuermaschine import native

    parsed = []
    real = native._imports_of
    monkeypatch.setattr(native, "_imports_of", lambda path, roots: parsed.append(path) or real(path, roots))
    reach(units)
    assert [p.name for p in parsed].count("shared.py") == 1 and len(parsed) == len(set(parsed))  # two units import it


def test_parsed_once_gives_the_same_reach_with_one_parse_per_file_and_restores_the_walk(units, monkeypatch):
    from steuermaschine import native

    plain = reach(units)
    parsed = []
    real = native._imports_of
    counting = lambda path, roots: parsed.append(path) or real(path, roots)  # noqa: E731
    monkeypatch.setattr(native, "_imports_of", counting)
    with parsed_once():
        assert [reach([cmd]) for cmd in units] == [[found] for found in plain]  # one walk per unit, each with a cache of its own
        assert len(parsed) == len(set(parsed)) > 0
    assert native._imports_of is counting
    with pytest.raises(RuntimeError), parsed_once():
        raise RuntimeError("the listing failed")
    assert native._imports_of is counting  # restored after a failure too
