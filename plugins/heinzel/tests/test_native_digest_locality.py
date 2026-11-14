"""How local the review digests of this plugin are, as a ratchet: the numbers below may only fall.

Measured on 2026-11-14 (research note sm-refactoring-plan-2026-11-14). Seven core files are in the digest of every
write command. Until that day 23 write commands and `release cut` covered 202 to 225 files, because
`queue_lib/edge.py` imported the plugin object, and with it the registry and every area; they look it up by name now,
and the widest digest (the train) has 72 files. A change that makes a digest wider fails here; a refactoring that
makes them narrower lowers the numbers in the same commit."""

from pathlib import Path

from steuermaschine.native import _package_path
from steuermaschine.plugins import stack_of
from steuermaschine.reviewgraph import kernel, reach, voided_by
from steuermaschine_heinzel.plugin import plugin

# The core files every write command's digest covers today. Target: the small contract modules only.
KERNEL = {"steuermaschine/__init__.py", "steuermaschine/config.py", "steuermaschine/engine.py", "steuermaschine/helpdoc.py",
          "steuermaschine/models.py", "steuermaschine/native.py", "steuermaschine/protocols.py"}
WIDEST = 72  # files in the widest digest today: `daemon train` and the three `release train` writers
# The registry: the plugin object and the list of every area. A unit that reaches one of them covers the whole plugin.
REGISTRY = {"steuermaschine_heinzel/plugin.py", "steuermaschine_heinzel/native/__init__.py", "steuermaschine_heinzel/pipelines/__init__.py"}


def _units():
    """Every write path of the plugin with a review unit: the write commands and the pipelines."""
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {}}, Path("."))
    writers = [c for c in stack.native(settings) if getattr(c, "writes", False)]
    return [*writers, *(pl.unit for pl in stack.pipelines(settings) if pl.unit is not None)]


def test_the_digests_do_not_get_wider():
    units = _units()
    found = reach(units)
    assert len(found) == len(units) > 40
    assert set(kernel(found)) <= KERNEL, sorted(set(kernel(found)) - KERNEL)  # a new file in every digest
    assert not [(unit.path, len(unit.files)) for unit in found if len(unit.files) > WIDEST]
    # the walk is the digest's own: the same files, for the narrowest and for the widest unit
    by_path = {c.path: c for c in units}
    for unit in (min(found, key=lambda u: len(u.files)), max(found, key=lambda u: len(u.files))):
        assert list(unit.files) == [_package_path(p) for p in by_path[unit.path].review_files()], unit.path


def test_no_digest_reaches_the_registry():
    found = reach(_units())
    assert not [(unit.path, sorted(REGISTRY & set(unit.files))) for unit in found if REGISTRY & set(unit.files)]


def test_a_pipeline_digest_covers_its_own_code_not_the_commands_it_runs():
    by_path = {unit.path: unit for unit in reach(_units())}
    for path in ("head admit", "release cut"):
        assert "steuermaschine_heinzel/pipelines/common.py" in by_path[path].files
    # `release cut` reads the receipt with the code of `release resume`, through the registry: that module is the
    # review unit of `release resume`. An import of it put 202 files into this digest.
    assert "steuermaschine_heinzel/native/release.py" not in by_path["release cut"].files
    # Deliberate widenings (coordinator decision, 2026-11-14): native/pr_owned.py (m598, imported by
    # admission_lib/focused_slots.py) and native/release_window.py (the release-window guard, imported by window.py)
    # are in these digests on purpose; the modules stay where they are. Raised from 23 and 43.
    # core-ci-facts, 2026-11-14: admission_lib/focused_resulting.py reads the required contexts from Core's release
    # policy (queue_lib/core_policy.py and the release_candidate.py it imports, two leaf modules). Raised from 24 and 45.
    assert len(by_path["release cut"].files) <= 26 and len(by_path["head admit"].files) <= 47


def test_a_change_to_a_kernel_file_voids_every_verdict_and_a_leaf_change_one_unit():
    found = reach(_units())
    every = sorted({unit.review for unit in found})
    assert voided_by(found, ["src/steuermaschine/native.py"]) == every
    leaf = voided_by(found, ["plugins/heinzel/src/steuermaschine_heinzel/native/leakwatch_lib/watch.py"])
    assert 1 <= len(leaf) < len(every)
    # the queue's edge file is shared by the queue, release, train, ship and rehearsal units, and by nothing else
    edge = voided_by(found, ["plugins/heinzel/src/steuermaschine_heinzel/native/queue_lib/edge.py"])
    assert 1 < len(edge) < len(every) and "lim.md" not in edge
