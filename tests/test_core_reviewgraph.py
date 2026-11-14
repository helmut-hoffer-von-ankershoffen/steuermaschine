"""`steuermaschine.reviewgraph`: what a unit's digest reaches, the files in every digest, and what a change voids.
The walk must name exactly the files `NativeCommand.review_files()` hashes."""

import importlib
import sys

import pytest

from steuermaschine.native import _package_path, unit
from steuermaschine.reviewgraph import Reach, fan_in, kernel, parsed_once, reach, voided_by


@pytest.fixture
def units(tmp_path, monkeypatch):
    """A package `graphpkg` with two units: `left` (left.py + left_lib, imports shared.py and core's hashing) and
    `right` (right.py, imports shared.py only). `lonely.py` is imported by nothing."""
    root = tmp_path / "graphpkg"
    (root / "left_lib").mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (root / "shared.py").write_text("VALUE = 1\n")
    (root / "lonely.py").write_text("VALUE = 2\n")
    (root / "left_lib" / "__init__.py").write_text("from steuermaschine.hashing import canonical_sha256  # noqa\nfrom .. import shared  # noqa\n")
    (root / "left.py").write_text("from . import left_lib  # noqa\n\n\ndef anchor():\n    pass\n")
    (root / "right.py").write_text("from .shared import VALUE  # noqa\n\n\ndef anchor():\n    pass\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [m for m in sys.modules if m.startswith("graphpkg")]:
        del sys.modules[name]
    left, right = importlib.import_module("graphpkg.left"), importlib.import_module("graphpkg.right")
    return [unit("left write", left.anchor, "left.md"), unit("right write", right.anchor, "right.md")]


def test_reach_names_the_unit_and_exactly_the_files_the_digest_covers(units):
    left, right = reach(units)
    assert (left.path, left.review, left.own) == ("left write", "left.md", ("graphpkg/left.py", "graphpkg/left_lib/__init__.py"))
    assert left.imported == ("graphpkg/shared.py", "steuermaschine/hashing.py")
    assert (right.own, right.imported) == (("graphpkg/right.py",), ("graphpkg/shared.py",))
    for found, cmd in zip((left, right), units, strict=True):  # the same files as the digest, by package path
        assert list(found.files) == [_package_path(p) for p in cmd.review_files()]


def test_a_unit_without_code_has_no_reach():
    def nowhere():
        pass

    nowhere.__module__ = "not_a_loaded_module"
    assert reach([unit("ghost write", nowhere, "ghost.md")]) == []


def test_fan_in_puts_the_file_in_the_most_digests_first(units):
    table = fan_in(reach(units))
    assert next(iter(table)) == "graphpkg/shared.py" and table["graphpkg/shared.py"] == ["left.md", "right.md"]
    assert table["steuermaschine/hashing.py"] == ["left.md"] and "graphpkg/lonely.py" not in table


def test_kernel_is_what_every_digest_covers(units):
    assert kernel(reach(units)) == ["graphpkg/shared.py"]
    assert kernel([]) == []
    assert kernel([Reach("a", "a.md", ("p/a.py",), ()), Reach("b", "b.md", ("p/b.py",), ())]) == []


def test_voided_by_names_the_verdicts_a_change_voids(units):
    found = reach(units)
    assert voided_by(found, ["src/steuermaschine/hashing.py"]) == ["left.md"]  # a path as git prints it
    assert voided_by(found, ["graphpkg/shared.py"]) == ["left.md", "right.md"]  # a package path
    assert voided_by(found, ["plugins/x/src/graphpkg/right.py", "README.md"]) == ["right.md"]
    assert voided_by(found, ["graphpkg/lonely.py", "other/hashing.py", "xsteuermaschine/hashing.py"]) == []


def test_reach_parses_a_shared_file_once_for_all_units(units, monkeypatch):
    from steuermaschine import native

    parsed = []
    real = native._imports_of
    monkeypatch.setattr(native, "_imports_of", lambda path, roots: parsed.append(path) or real(path, roots))
    reach(units)
    assert [p.name for p in parsed].count("shared.py") == 1 and len(parsed) == len(set(parsed))  # two units import it


def test_parsed_once_gives_the_same_digests_with_one_parse_per_file_and_restores_the_walk(units, monkeypatch):
    from steuermaschine import native

    plain = [(cmd.code_digest(), cmd.labels()) for cmd in units]
    parsed = []
    real = native._imports_of
    counting = lambda path, roots: parsed.append(path) or real(path, roots)  # noqa: E731
    monkeypatch.setattr(native, "_imports_of", counting)
    with parsed_once():
        assert [(cmd.code_digest(), cmd.labels()) for cmd in units] == plain  # two walks per unit
        assert len(parsed) == len(set(parsed)) > 0
    assert native._imports_of is counting
    with pytest.raises(RuntimeError), parsed_once():
        raise RuntimeError("the listing failed")
    assert native._imports_of is counting  # restored after a failure too
