"""Write units: what a write path's code covers (imports followed into core), every writer running without a
per-command review file (one verdict per promoted commit replaced it), `sm self reviews` naming the write paths a diff
reaches, read-only derived from `writes=`, and the engine's review stops, running steps and superseded runs."""

import importlib
import json
import sys
from pathlib import Path

import pytest

from steuermaschine import API_VERSION, OK, USAGE
from steuermaschine import native as native_mod
from steuermaschine.app import Runtime, core_units, write_units
from steuermaschine.config import Project, Settings
from steuermaschine.engine import Engine, Options, Pipeline, ReviewStop, Step, Subject
from steuermaschine.helpdoc import H, disagrees
from steuermaschine.hookspecs import hookimpl
from steuermaschine.native import collect, unit
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, fake_providers, invoke


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


def git_fake(changed: str, rc: int = 0) -> FakeCommands:
    """git in /repo: BASE resolves to b*40, HEAD to h*40, and the diff names `changed`."""
    commands = FakeCommands()
    commands.on(["git", "-C", "/repo", "rev-parse"], lambda argv: Result(rc, ("h" if argv[-1] == "HEAD^{commit}" else "b") * 40 + "\n", "bad revision" if rc else ""))
    commands.on(["git", "-C", "/repo", "diff"], Result(0, changed, ""))
    return commands


def runtime_of(plugin, commands: FakeCommands | None = None) -> Runtime:
    return Runtime(Project(name="p", plugin="unitpkg", root=Path("."), plugins=["unitpkg"]), stack_of(plugin), Settings({}),
                   _providers=fake_providers(commands=commands or FakeCommands()))


def test_every_writer_runs_without_a_review_file_and_only_the_allowlisted_run_lock_is_exempt(package):
    _, _, plugin = package
    rt = runtime_of(plugin)
    assert invoke(rt, ["unit", "write"]).stdout == "did: written\n"  # no review file exists in the package
    assert invoke(rt, ["unit", "lock"]).stdout == "did: locked\n"
    assert invoke(rt, ["gate", "run"]).stdout == "did: ran\n"
    tree = {r["path"]: r for r in json.loads(invoke(rt, ["--format", "json", "self", "tree"]).stdout)}
    assert tree["sm unit write"]["read_only"] is False and tree["sm unit lock"]["read_only"] is False and tree["sm go"]["read_only"] is True
    assert frozenset({"gate run"}) == native_mod.EXEMPT_PATHS  # exempt from the writers' lock, unchanged
    lock, gate = (next(c for c in native_mod.registered(rt) if c.path == p) for p in ("unit lock", "gate run"))
    assert native_mod.is_exempt(gate) and not native_mod.is_exempt(lock)
    assert not hasattr(native_mod, "gate_problem")


def test_self_reviews_names_the_write_paths_the_diff_reaches(package):
    _, _, plugin = package
    rt = runtime_of(plugin, git_fake("plugins/x/src/unitpkg/other_lib/deep.py\ndocs/README.md\n"))
    done = invoke(rt, ["--format", "json", "self", "reviews", "--repo", "/repo", "--base", "v1"])
    found = json.loads(done.stdout)
    assert done.exit_code == OK and (found["base"], found["head"]) == ("b" * 40, "h" * 40)
    reached = {r["path"]: r["files"] for r in found["reached"]}
    assert reached["unit write"] == ["plugins/x/src/unitpkg/other_lib/deep.py"]  # two imports away from area.py
    assert found["unreached"] == ["docs/README.md"] and "focus add" not in reached
    text = invoke(rt, ["self", "reviews", "--repo", "/repo", "--base", "v1"]).stdout
    assert "write path(s) reached" in text and "reached  sm unit write" in text and "in no write path (1): docs/README.md" in text


def test_self_reviews_lists_changes_to_the_promote_gate_first(package):
    _, _, plugin = package
    rt = runtime_of(plugin, git_fake("bin/promote\nbin/promote_lib.sh\nbin/promote_verdict.py\nbin/rollback\nbin/sm-dev\n"))
    found = json.loads(invoke(rt, ["--format", "json", "self", "reviews", "--repo", "/repo", "--base", "v1"]).stdout)
    assert found["gate"] == ["bin/promote", "bin/promote_lib.sh", "bin/promote_verdict.py", "bin/rollback"] and found["unreached"] == ["bin/sm-dev"]
    text = invoke(rt, ["self", "reviews", "--repo", "/repo", "--base", "v1"]).stdout.splitlines()
    assert text[1].startswith("GATE CODE (read first") and "bin/promote_verdict.py" in text[1]


def test_self_reviews_says_when_the_diff_reaches_no_write_path(package):
    _, _, plugin = package
    rt = runtime_of(plugin, git_fake("docs/README.md\n"))
    found = json.loads(invoke(rt, ["--format", "json", "self", "reviews", "--repo", "/repo", "--base", "v1"]).stdout)
    assert found["reached"] == [] and found["unreached"] == ["docs/README.md"]
    assert "reaches no write path: CI covers it; the promote still needs one verdict" in invoke(rt, ["self", "reviews", "--repo", "/repo", "--base", "v1"]).stdout


def test_self_reviews_starts_at_the_live_release_and_refuses_without_one(package, tmp_path, monkeypatch):
    _, _, plugin = package
    commands = git_fake("")
    rt = runtime_of(plugin, commands)
    monkeypatch.delenv("STEUERMASCHINE_RELEASE", raising=False)
    done = invoke(rt, ["self", "reviews", "--repo", "/repo"])
    assert done.exit_code == USAGE and "give --base REV" in done.stderr
    (tmp_path / "stable" / "releases" / "0123456789ab").mkdir(parents=True)
    (tmp_path / "stable" / "current").symlink_to("releases/0123456789ab")
    monkeypatch.setenv("STEUERMASCHINE_RELEASE", str(tmp_path / "stable" / "current"))
    assert invoke(rt, ["self", "reviews", "--repo", "/repo"]).exit_code == OK
    assert ["git", "-C", "/repo", "rev-parse", "--verify", "0123456789ab^{commit}"] in commands.calls
    bad = invoke(runtime_of(plugin, git_fake("", rc=128)), ["self", "reviews", "--repo", "/repo", "--base", "nope"])
    assert bad.exit_code == USAGE and "cannot resolve nope and HEAD" in bad.stderr


def test_the_cores_own_writers_run_without_a_review_file_and_are_write_units():
    rt = Runtime(Project(name="p", plugin="none", root=Path(".")), None, {}, {}, fake_providers())
    assert invoke(rt, ["focus", "add", "x"]).exit_code == OK
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


def test_a_pipeline_with_a_unit_runs_without_a_review_file_and_is_a_write_unit():
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

    rt = Runtime(Project(name="p", plugin="demo", root=Path("."), plugins=["demo"]), stack_of(Plugin()), Settings({}), _providers=p)
    assert invoke(rt, ["head", "demo", "/wt"]).exit_code == OK  # it stops at its first review stop, a planned stop
    assert "head demo" in {u.path for u in write_units(rt)}


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
