"""The merge rule of a Core PR (`queue_lib/main_plan.gap`), driven at the process boundary: a scripted `gh`, the real verdict store,
the real protected-path matcher. Nothing in the queue or the rule is replaced.

Rule: required checks green on the PINNED head; main's tip an ancestor of it; a recorded protected-review verdict for exactly
this head when it changes a protected path.
"""

from __future__ import annotations

import json

import pytest
from test_pipeline_head_admit import native, runtime

from steuermaschine_heinzel.native import protected
from steuermaschine_heinzel.native.queue_lib import hzlib, main_plan

HEAD = "a" * 40
MAIN = "b" * 40
GREEN = [{"name": n, "status": "COMPLETED", "conclusion": "SUCCESS"} for n in ("CI OK", "merge-gate", "coverage-floor")]
TOML = '[project]\nname = "x"\ndependencies = ["a"]\n[tool.coverage.report]\nfail_under = 90\n[tool.pytest.ini_options]\naddopts = "-q"\n'


class World:
    """Core on GitHub, as `gh` answers: the PR, main's ref, the compare, the changed files and raw file texts at a ref."""

    def __init__(self, files=("src/heinzel/a.py",), rollup=None, status="ahead", head=HEAD, texts=None):
        self.files, self.rollup, self.status, self.head = list(files), GREEN if rollup is None else rollup, status, head
        self.texts = texts or {}

    def __call__(self, *args):
        text = " ".join(args)
        if args[:2] == ("pr", "view"):
            return 0, json.dumps({"headRefOid": self.head, "statusCheckRollup": self.rollup})
        if "git/ref/heads/main" in text:
            return 0, json.dumps({"object": {"sha": MAIN}})
        if "/compare/" in text:
            return 0, json.dumps({"status": self.status})
        if "/pulls/" in text and "/files" in text:
            return 0, "\n".join(self.files) + "\n"
        if "contents/" in text:
            path, _, ref = text.split("contents/")[1].partition("?ref=")
            found = self.texts.get((path, ref))
            return (0, found) if found is not None else (1, "")
        return 1, ""


@pytest.fixture
def gap(monkeypatch, tmp_path, real_main_plan):
    """`gap(world)`: the rule with `world` as GitHub, under a native runtime whose store holds the verdicts."""
    rt = runtime(tmp_path)

    def run(world, head=HEAD):
        monkeypatch.setattr(hzlib, "gh", world)
        with native(rt):
            return main_plan.gap("803", head)

    run.rt = rt
    return run


def approve(rt, paths, head=HEAD, reviewer="reviewer-1", verdict="approve", authors=()):
    digest, _ = protected.record(head, paths)
    rt.providers.store.put_receipt("verdicts", digest, {"verdict": verdict, "reviewer": reviewer})
    for author in authors:
        from steuermaschine import verdicts

        verdicts.register(rt.providers.store, digest, "sm head admit", author=author)
    return digest


def test_a_fresh_green_pr_that_changes_no_protected_path_is_admitted(gap):
    assert gap(World()) == ""


@pytest.mark.parametrize("rollup", [
    [c for c in GREEN if c["name"] != "merge-gate"],  # the Sonar gate has not reported
    [*GREEN[:2], {"name": "coverage-floor", "conclusion": "FAILURE"}],
    [{"name": "CI OK", "status": "IN_PROGRESS", "conclusion": ""}, *GREEN[1:]],
    [],
])
def test_a_required_check_that_is_not_green_on_the_head_refuses_and_names_it(gap, rollup):
    reason = gap(World(rollup=rollup))
    assert reason.startswith("merge rule:") and "required checks not green" in reason


def test_a_head_behind_or_diverged_from_main_is_refused_never_merged(gap):
    for status in ("behind", "diverged", None):
        reason = gap(World(status=status))
        assert reason.startswith("merge rule:") and "update the branch" in reason
    assert gap(World(status="identical")) == ""


def test_the_pinned_head_must_be_the_current_head(gap):
    reason = gap(World(head="c" * 40), head=HEAD)
    assert "not the pinned head aaaaaaaa" in reason


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "Makefile", "uv.lock", ".pre-commit-config.yaml", "sonar-project.properties",
                                  "scripts/merge_gate.py", "scripts/release_candidate.py", "tests/conftest.py", "quarantine.toml"])
def test_a_protected_path_needs_the_recorded_verdict_of_exactly_this_head(gap, path):
    world = World(files=["src/heinzel/a.py", path])
    reason = gap(world)
    assert reason.startswith("merge rule:") and "no verdict is recorded" in reason and path in reason
    approve(gap.rt, [path], head="d" * 40)  # a verdict for another head
    assert "no verdict is recorded" in gap(world)
    approve(gap.rt, [path])
    assert gap(world) == ""


def test_a_verdict_for_other_paths_or_a_rejection_or_the_authors_own_does_not_count(gap):
    both = World(files=["uv.lock", "Makefile"])
    approve(gap.rt, ["Makefile"])  # the digest names the paths: a verdict on fewer paths is another digest
    assert "no verdict is recorded" in gap(both)
    rejected = World(files=["uv.lock"])
    approve(gap.rt, ["uv.lock"], verdict="reject")
    assert "'reject'" in gap(rejected)
    own = World(files=[".pre-commit-config.yaml"])
    approve(gap.rt, [".pre-commit-config.yaml"], reviewer="builder-1", authors=["builder-1"])
    assert "who also produced these bytes" in gap(own)
    approve(gap.rt, ["Makefile", "uv.lock"], reviewer="reviewer-1")
    assert gap(both) == ""


def test_lowering_the_coverage_floor_is_protected_and_a_dependency_bump_is_not(gap):
    lowered = World(files=["pyproject.toml"], texts={("pyproject.toml", MAIN): TOML, ("pyproject.toml", HEAD): TOML.replace("= 90", "= 50")})
    assert "no verdict is recorded" in gap(lowered) and "pyproject.toml" in gap(lowered)
    bump = World(files=["pyproject.toml"], texts={("pyproject.toml", MAIN): TOML, ("pyproject.toml", HEAD): TOML.replace('["a"]', '["a", "b"]')})
    assert gap(bump) == ""
    unreadable = World(files=["pyproject.toml"], texts={("pyproject.toml", MAIN): TOML})  # no text at the head: fail closed
    assert "no verdict is recorded" in gap(unreadable)


def test_core_own_execution_inputs_are_read_from_main_while_it_names_them(gap):
    plan = 'FULL_EXECUTION_INPUTS = ("tools/runner.py", ".github/actions/x.yml")\n'
    world = World(files=["tools/runner.py"], texts={("scripts/focused_plan.py", MAIN): plan})
    assert "no verdict is recorded" in gap(world) and "tools/runner.py" in gap(world)
    assert gap(World(files=["tools/runner.py"])) == ""  # Core no longer names it: nothing to copy, nothing protected


def test_unreadable_facts_fail_closed_with_the_rule_named(gap):
    class Broken(World):
        def __call__(self, *args):
            return 1, ""

    assert gap(Broken()).startswith("merge rule: cannot check #803")
    assert gap(World(files=[])) == ""  # no changed file: nothing protected


def test_the_protected_matcher_and_digest_are_one_definition():
    assert protected.matches(["Makefile", "src/a.py", "docs/Makefile.md"]) == {"Makefile"}
    assert protected.execution_inputs('FULL_EXECUTION_INPUTS = ("a", "b")') == {"a", "b"} and protected.execution_inputs("syntax error(") == set()
    assert protected.execution_inputs(None) == set() and protected.execution_inputs("OTHER = 1") == set()
    one, text = protected.record(HEAD, ["b", "a"])
    assert one == protected.record(HEAD, ["a", "b"])[0] and '"protected": [\n  "a",\n  "b"\n ]' in text.replace("\n  ", "\n  ")
    assert protected.record("c" * 40, ["a", "b"])[0] != one
