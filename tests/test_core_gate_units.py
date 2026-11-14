"""The review gate after the 2026-11-13 review: digests that follow imports, every writer under the gate (core's own
writers and pipelines too), read-only derived from `writes=`, and the engine's review stops, running steps and
superseded runs."""

import importlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from steuermaschine import API_VERSION, INTERNAL, OK
from steuermaschine import native as native_mod
from steuermaschine.app import Runtime, core_units
from steuermaschine.config import Project, Settings
from steuermaschine.engine import Engine, Options, Pipeline, ReviewStop, Step, Subject
from steuermaschine.helpdoc import H, disagrees
from steuermaschine.hookspecs import hookimpl
from steuermaschine.native import NativeCommand, collect, unit
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke


@pytest.fixture
def package(tmp_path, monkeypatch):
    """A plugin package `unitpkg`: area.py imports area_lib, area_lib imports other_lib, and both import core."""
    root = tmp_path / "unitpkg"
    for d in ("area_lib", "other_lib", "reviews"):
        (root / d).mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (root / "area_lib" / "__init__.py").write_text("from ..other_lib import deep  # noqa\n")
    (root / "other_lib" / "__init__.py").write_text("")
    (root / "other_lib" / "deep.py").write_text("from steuermaschine.hashing import sha256_text  # noqa\nVALUE = 1\n")
    (root / "area.py").write_text(
        "import typer\nfrom steuermaschine.helpdoc import H\nfrom steuermaschine.models import Model\nfrom steuermaschine.native import native\n"
        "from . import area_lib  # noqa\n\n\nclass Done(Model):\n    did: str\n\n\n"
        "@native('unit write', writes=True, help=H('Write.', 'Use when testing.', 'writes a thing.', 'sm unit write', 'sm go'))\n"
        "def unit_write() -> Done:\n    return Done(did='written')\n\n\n"
        "@native('unit lock', writes=True, exempt='the lock itself', help=H('Lock.', 'Use when testing.', 'takes a slot.', 'sm unit lock', 'sm go'))\n"
        "def unit_lock() -> Done:\n    return Done(did='locked')\n\n\n"
        "@native('gate run', writes=True, exempt='the run lock', help=H('Run.', 'Use when testing.', 'takes a slot.', 'sm gate run', 'sm go'))\n"
        "def gate_run() -> Done:\n    return Done(did='ran')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [m for m in sys.modules if m.startswith("unitpkg")]:
        del sys.modules[name]
    module = importlib.import_module("unitpkg.area")

    class Plugin:
        name = "unitpkg"

        @hookimpl
        def sm_api_version(self):
            return API_VERSION

        @hookimpl
        def sm_native(self):
            return collect(module)

    return root, module, Plugin()


def test_the_digest_follows_imports_across_lib_packages_and_into_core(package):
    root, module, _ = package
    cmd = module.unit_write.__sm_native__
    assert [p.name for p in cmd.code_files()] == ["area.py", "__init__.py"]  # the declared unit is unchanged
    labels = cmd.labels()
    assert "other_lib/deep.py" in labels and "other_lib/__init__.py" not in labels  # a name imported from a submodule
    assert {"steuermaschine/hashing.py", "steuermaschine/native.py", "steuermaschine/helpdoc.py"} <= set(labels)
    before = cmd.code_digest()
    (root / "other_lib" / "deep.py").write_text("from steuermaschine.hashing import sha256_text  # noqa\nVALUE = 2\n")
    assert cmd.code_digest() != before  # a change two imports away voids the verdict


def test_only_the_allowlisted_run_lock_may_be_exempt_any_other_exempt_writer_stays_gated(package):
    _, _, plugin = package
    stack = stack_of(plugin)
    rt = Runtime(Project(name="p", plugin="unitpkg", root=Path("."), plugins=["unitpkg"]), stack, Settings({}), _providers=fake_providers())
    assert invoke(rt, ["unit", "write"]).exit_code == INTERNAL
    locked = invoke(rt, ["unit", "lock"])  # declares exempt=, but its path is not on the core's allowlist
    assert locked.exit_code == INTERNAL and "independent review is" in locked.stderr and locked.stdout == ""
    assert invoke(rt, ["gate", "run"]).stdout == "did: ran\n"
    rows = {r["path"]: r for r in json.loads(invoke(rt, ["--format", "json", "self", "reviews"]).stdout)}
    assert rows["gate run"]["state"] == "exempt" and "the run lock" in rows["gate run"]["detail"]
    assert rows["unit lock"]["state"] == "missing" and rows["unit write"]["state"] == "missing"
    tree = {r["path"]: r for r in json.loads(invoke(rt, ["--format", "json", "self", "tree"]).stdout)}
    assert tree["sm unit write"]["read_only"] is False and tree["sm unit lock"]["read_only"] is False and tree["sm go"]["read_only"] is True


def test_gate_problem_honours_exempt_only_for_allowlisted_paths():
    class Cmd:
        writes = True
        exempt = "any reason"

        def __init__(self, path):
            self.path = path

        def review_state(self):
            return "missing", "no review file"

    assert frozenset({"gate run"}) == native_mod.EXEMPT_PATHS
    assert native_mod.gate_problem(Cmd("gate run"), None) == ""
    assert "independent review is missing" in native_mod.gate_problem(Cmd("queue drop"), None)
    assert not native_mod.is_exempt(type("NoFlag", (), {"path": "gate run", "exempt": ""})())


def test_the_cores_own_writers_are_under_the_gate(monkeypatch):
    # The shipped review files may approve these units; the gate itself is tested against an unapproved review.
    monkeypatch.setattr(NativeCommand, "review_state", lambda self: ("open", "no verdict (test)"))
    rt = Runtime(Project(name="p", plugin="none", root=Path(".")), None, {}, {}, fake_providers())
    for argv in (["focus", "add", "x"], ["focus", "done", "x"], ["ci", "cancel", "7"]):
        done = invoke(rt, argv)
        assert done.exit_code == INTERNAL and "independent review is" in done.stderr and "nothing was run" in done.stderr, argv
    assert rt.providers.store.load("focus", "list") is None
    assert {u.path for u in core_units()} == {"review verdict", "focus add", "focus done", "alarm ack", "self migrate-state", "ci cancel"}
    for u in core_units():  # app.py (or github/cli.py) and the core files it imports, named relative to the unit's module
        assert len(u.code_digest()) == 64 and ("engine.py" in u.labels() if u.path != "ci cancel" else "cli.py" in u.labels()), u.path


def test_the_lint_finds_a_declared_flag_that_disagrees_with_the_changes_line():
    row = {"declared": True, "group": False, "read_only": True, "help": H("x", "Use when testing.", "writes the packet file.", "sm x", "sm go")}
    assert "register it with writes=True" in disagrees(row)
    assert disagrees({**row, "read_only": False, "help": H("x", "Use when testing.", "nothing (read-only).", "sm x", "sm go")})
    assert disagrees({**row, "read_only": False, "help": H("x", "Use when testing.", "nothing without --live; with --live a push.", "sm x", "sm go")}) == ""
    assert disagrees({**row, "declared": False}) == ""


# ----------------------------------------------------------------------------- the engine
def two_stops(p, digests):
    def identify(providers, settings, arg):
        return Subject(key=providers.vcs.head(arg), label="x", fingerprint={"head": providers.vcs.head(arg)}, facts={"worktree": arg})

    steps = [ReviewStop("first", lambda ctx: (digests[0], "Review one.", "/r/one.json"), binds=("head",)),
             Step("local", lambda ctx: "ok", binds=("head",)),
             ReviewStop("second", lambda ctx: (digests[1], "Review two.", "/r/two.json"), binds=("head",))]
    return Pipeline("head", "demo", "Demo.", steps, identify, unit=unit("head demo", two_stops, "head-demo.md"))


def test_each_review_stop_names_file_digest_and_brief_and_a_spent_digest_does_not_answer_the_next(tmp_path):
    p, digests = fake_providers(), ("1" * 64, "2" * 64)
    pl, eng = two_stops(p, digests), Engine("proj", p, out=lambda line: None)
    first = eng.advance(pl, "/wt", Options())
    assert first.review == {"step": "first", "file": "/r/one.json", "sha256": digests[0], "brief": first.review["brief"],
                            "record": f"sm review verdict {digests[0]} --approve --reviewer NAME", "resume": f"sm head demo /wt --approved {digests[0]}"}
    assert any(line.startswith("REVIEW   file   /r/one.json") for line in first.lines())
    assert "File under review: /r/one.json" in p.agents.briefs[first.review["brief"]]
    p.store.put_receipt("verdicts", digests[0], {"verdict": "approve", "reviewer": "reviewer-1"})
    second = eng.advance(pl, "/wt", Options(approved=digests[0]))
    assert second.status == "review" and second.review["step"] == "second" and second.exit_code == OK  # not a refusal


def test_a_step_that_acts_is_recorded_as_running_with_its_pid_before_it_runs():
    import os

    p, seen = fake_providers(), {}

    def identify(providers, settings, arg):
        return Subject(key="k" * 40, label="x", facts={"worktree": arg})

    def slow(ctx):
        seen.update(p.store.load("runs.head.demo", "k" * 40))
        return "done"

    pl = Pipeline("head", "demo", "Demo.", [Step("prepare", slow)], identify)
    Engine("proj", p, out=lambda line: None).advance(pl, "/wt", Options())
    assert seen["status"] == "running" and seen["pid"] == os.getpid() and seen["running"] == "prepare"
    final = p.store.load("runs.head.demo", "k" * 40)
    assert final["status"] == "finished" and "pid" not in final


def test_a_new_subject_for_the_same_worktree_supersedes_the_unfinished_run():
    p = fake_providers()
    pl = two_stops(p, ("1" * 64, "2" * 64))
    eng = Engine("proj", p, out=lambda line: None)
    eng.advance(pl, "/wt", Options())
    old = p.vcs.h
    p.vcs.h = "c" * 40
    eng.advance(pl, "/wt", Options())
    assert p.store.load("runs.head.demo", old)["status"] == "superseded" and p.store.load("runs.head.demo", "c" * 40)["status"] == "review"


def test_superseding_takes_the_older_runs_lock_and_waits_while_it_is_held():
    p = fake_providers()
    pl = two_stops(p, ("1" * 64, "2" * 64))
    eng = Engine("proj", p, out=lambda line: None)
    eng.advance(pl, "/wt", Options())
    old = p.vcs.h
    p.vcs.h = "c" * 40
    p.store.held.add(f"{pl.id}.{old}")  # another process advances the older run right now
    report = eng.advance(pl, "/wt", Options())
    assert report.exit_code == 75 and "the run it supersedes" in report.phase
    assert p.store.load("runs.head.demo", old)["status"] != "superseded" and p.store.load("runs.head.demo", "c" * 40) is None
    p.store.held.discard(f"{pl.id}.{old}")
    eng.advance(pl, "/wt", Options())
    assert p.store.load("runs.head.demo", old)["status"] == "superseded"


def test_a_pipeline_with_a_unit_is_shut_until_it_is_reviewed():
    p = fake_providers()
    pl = two_stops(p, ("1" * 64, "2" * 64))

    class Plugin:
        name = "demo"

        @hookimpl
        def sm_api_version(self):
            return API_VERSION

        @hookimpl
        def sm_pipelines(self, settings):
            return [pl]

    stack = stack_of(Plugin())
    shut = Runtime(Project(name="p", plugin="demo", root=Path("."), plugins=["demo"]), stack, Settings({}), _providers=p)
    done = invoke(shut, ["head", "demo", "/wt"])
    assert done.exit_code == INTERNAL and "`sm head demo` writes and its independent review is" in done.stderr
    assert invoke(shut, ["head", "demo", "/wt", "--plan"]).exit_code == OK  # the plan writes nothing and stays open
    rows = {r["path"] for r in json.loads(invoke(shut, ["--format", "json", "self", "reviews"]).stdout)}
    assert "head demo" in rows
    opened = Runtime(replace(shut.project, allow_unreviewed=True), stack, Settings({}), _providers=p)
    assert invoke(opened, ["head", "demo", "/wt"]).exit_code == OK


def test_imported_files_are_ordered_by_package_path_not_install_location(tmp_path):
    # A checkout keeps the plugin under plugins/, an install under venv/.../site-packages: sorting by the absolute path
    # put core first in one and last in the other, so the same code had two digests (promote's review gate saw stale).
    from steuermaschine.native import _package_path

    for root in ("a-install/site-packages", "z-checkout/src"):
        (tmp_path / root / "pkg").mkdir(parents=True)
        (tmp_path / root / "pkg" / "__init__.py").write_text("")
        (tmp_path / root / "pkg" / "mod.py").write_text("")
    assert _package_path(tmp_path / "z-checkout/src/pkg/mod.py") == "pkg/mod.py" == _package_path(tmp_path / "a-install/site-packages/pkg/mod.py")
    from steuermaschine.plugins import stack_of
    from steuermaschine_heinzel.plugin import plugin

    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {}}, Path("."))
    for cmd in [c for c in stack.native(settings) if getattr(c, "writes", False)][:10]:
        imported = cmd.imported_files()
        assert [_package_path(p) for p in imported] == sorted(_package_path(p) for p in imported), cmd.path
