"""The merge rule of a Core PR (`queue_lib/main_plan.gap`), driven at the process boundary: a scripted `gh`, the real verdict store,
the real protected-path matcher. Nothing in the queue or the rule is replaced.

Rule: the checks main's ruleset requires green on the PINNED head; main's tip an ancestor of it; a recorded protected-review
verdict for the content of the protected files the head changes.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from test_pipeline_head_admit import native, runtime

from steuermaschine_heinzel.native import protected
from steuermaschine_heinzel.native.queue_lib import hzlib, main_plan

HEAD = "a" * 40
MAIN = "b" * 40
GREEN = [{"name": n, "status": "COMPLETED", "conclusion": "SUCCESS"} for n in ("CI OK", "SonarCloud Code Analysis")]
# the legacy checks are not required by the ruleset, but a red one still holds a merge (coverage-floor was red while CI OK was green)
LEGACY_RED = [{"name": n, "status": "COMPLETED", "conclusion": "FAILURE"} for n in ("coverage-floor", "merge-gate")]
# main's ruleset today (`rules/branches/main`)
RULESET = [
    {
        "type": "required_status_checks",
        "ruleset_id": 25762292,
        "parameters": {
            "strict_required_status_checks_policy": False,
            "required_status_checks": [{"context": "CI OK", "integration_id": 15368}, {"context": "SonarCloud Code Analysis", "integration_id": 12526}],
        },
    },
    {"type": "pull_request", "parameters": {}},
]
TOML = '[project]\nname = "x"\ndependencies = ["a"]\n[tool.coverage.report]\nfail_under = 90\n[tool.pytest.ini_options]\naddopts = "-q"\n'
# Core's scripts/focused_plan.py at main (the part that names the protected files)
CORE_PLAN = """PLAN_VARIABLE = "FOCUSED_CI_PLANS"
PROTECTED_FILES = (
    ".github/workflows/ci.yml",
    "scripts/bootstrap-pyyaml.sh",
    "scripts/focused_plan.py",
    "src/heinzel/bootstrap_pyyaml.py",
    "uv.lock",
)
MAX_PLAN = 48 * 1024
"""
NOT_FOUND = (1, '{"message":"Not Found","status":"404"}')


def blob(path: str, version: str = "1") -> str:
    return hashlib.sha1(f"{path}:{version}".encode()).hexdigest()  # noqa: S324 - a stand-in for a git blob id


class World:
    """Core on GitHub, as `gh` answers: the PR, main's ref and ruleset, the compare, the changed files and raw file texts at a ref."""

    def __init__(self, files=("src/heinzel/a.py",), rollup=None, status="ahead", head=HEAD, texts=None, ruleset=None, version="1"):
        self.files, self.rollup, self.status, self.head = list(files), GREEN if rollup is None else rollup, status, head
        self.texts, self.ruleset, self.version = texts or {}, RULESET if ruleset is None else ruleset, version

    def __call__(self, *args):
        text = " ".join(args)
        if args[:2] == ("pr", "view"):
            return 0, json.dumps({"headRefOid": self.head, "statusCheckRollup": self.rollup})
        if "git/ref/heads/main" in text:
            return 0, json.dumps({"object": {"sha": MAIN}})
        if "rules/branches/main" in text:
            return (1, "") if self.ruleset == "unreadable" else (0, json.dumps(self.ruleset))
        if "/compare/" in text:
            return 0, json.dumps({"status": self.status})
        if "/pulls/" in text and "/files" in text:
            return 0, "".join(f"{name}\t{blob(name, self.version)}\tmodified\n" for name in self.files)
        if "contents/" in text:
            path, _, ref = text.split("contents/")[1].partition("?ref=")
            found = self.texts.get((path, ref))
            return (0, found) if found is not None else NOT_FOUND
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


def approve(rt, paths, version="1", reviewer="reviewer-1", verdict="approve", authors=()):
    digest, _ = protected.record({p: blob(p, version) for p in paths})
    rt.providers.store.put_receipt("verdicts", digest, {"verdict": verdict, "reviewer": reviewer})
    for author in authors:
        from steuermaschine import verdicts

        verdicts.register(rt.providers.store, digest, "sm head admit", author=author)
    return digest


def test_a_fresh_pr_with_ci_ok_and_sonar_green_is_admitted_although_the_legacy_checks_are_red(gap):
    assert gap(World()) == ""


@pytest.mark.parametrize(
    "rollup",
    [
        [GREEN[0]],  # Sonar absent
        [GREEN[0], {"name": "SonarCloud Code Analysis", "status": "COMPLETED", "conclusion": "FAILURE"}],
        [{"name": "CI OK", "status": "IN_PROGRESS", "conclusion": ""}, GREEN[1]],
        [GREEN[1]],
        [],
    ],
)
def test_a_check_the_ruleset_requires_that_is_not_green_on_the_head_refuses_and_names_it(gap, rollup):
    reason = gap(World(rollup=rollup))
    assert reason.startswith("merge rule:") and "required checks not green" in reason


@pytest.mark.parametrize(
    "ruleset",
    [
        "unreadable",
        [],
        [{"type": "pull_request", "parameters": {}}],
        [{"type": "required_status_checks", "parameters": {"required_status_checks": []}}],
        [{"type": "required_status_checks", "parameters": {}}],
    ],
)
def test_an_unreadable_or_empty_ruleset_refuses(gap, ruleset):
    reason = gap(World(ruleset=ruleset))
    assert reason.startswith("merge rule:") and "required status checks of main's ruleset" in reason


def test_a_head_behind_or_diverged_from_main_is_refused_never_merged(gap):
    for status in ("behind", "diverged", None):
        reason = gap(World(status=status))
        assert reason.startswith("merge rule:") and "update the branch" in reason
    assert gap(World(status="identical")) == ""


def test_the_pinned_head_must_be_the_current_head(gap):
    assert "not the pinned head aaaaaaaa" in gap(World(head="c" * 40), head=HEAD)


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "Makefile",
        "uv.lock",
        ".pre-commit-config.yaml",
        "sonar-project.properties",
        "scripts/merge_gate.py",
        "scripts/release_candidate.py",
        "tests/conftest.py",
        "quarantine.toml",
    ],
)
def test_a_protected_path_needs_the_recorded_verdict_of_its_content(gap, path):
    world = World(files=["src/heinzel/a.py", path])
    reason = gap(world)
    assert reason.startswith("merge rule:") and "no verdict is recorded" in reason and path in reason
    approve(gap.rt, [path], version="2")  # a verdict on other content of the same file
    assert "no verdict is recorded" in gap(world)
    approve(gap.rt, [path])
    assert gap(world) == ""


def test_an_update_of_the_branch_that_leaves_the_protected_files_alone_keeps_the_verdict(gap):
    approve(gap.rt, ["uv.lock"])
    assert gap(World(files=["uv.lock", "src/heinzel/a.py"], head=HEAD), head=HEAD) == ""
    moved = "c" * 40  # the branch was updated: another head, the same protected content
    assert gap(World(files=["uv.lock", "src/heinzel/other.py"], head=moved), head=moved) == ""


def test_a_verdict_for_other_paths_or_a_rejection_or_the_authors_own_does_not_count(gap):
    both = World(files=["uv.lock", "Makefile"])
    approve(gap.rt, ["Makefile"])  # the digest names the files: a verdict on fewer files is another digest
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


def test_core_own_protected_files_are_read_from_its_focused_plan_at_main(gap):
    """Core's real list: the two bootstrap files match no pattern of ours, only Core's own list protects them."""
    plan = {("scripts/focused_plan.py", MAIN): CORE_PLAN}
    for path in ("scripts/bootstrap-pyyaml.sh", "src/heinzel/bootstrap_pyyaml.py"):
        world = World(files=[path], texts=plan)
        assert "no verdict is recorded" in gap(world) and path in gap(world)
        approve(gap.rt, [path])
        assert gap(world) == ""
    assert gap(World(files=["src/heinzel/a.py"], texts=plan)) == ""


def test_a_focused_plan_without_the_literal_list_refuses_and_an_absent_one_protects_nothing_extra(gap):
    for text in ("PROTECTED_FILES = tuple(compute())\n", "OTHER = 1\n", "this is not python(\n", "PROTECTED_FILES = ()\n"):
        reason = gap(World(files=["src/heinzel/bootstrap_pyyaml.py"], texts={("scripts/focused_plan.py", MAIN): text}))
        assert reason.startswith("merge rule: cannot check") and "PROTECTED_FILES" in reason
    assert gap(World(files=["src/heinzel/bootstrap_pyyaml.py"])) == ""  # Core dropped the file: nothing to read, nothing to copy


def test_a_focused_plan_that_cannot_be_read_for_another_reason_refuses(gap, monkeypatch):
    world = World(files=["src/heinzel/a.py"])
    real = world.__call__

    def broken(*args):
        if "contents/scripts/focused_plan.py" in " ".join(args):
            return 1, "gateway timeout"
        return real(*args)

    reason = gap(broken)
    assert reason.startswith("merge rule:") and "protected files are unknown" in reason


def test_unreadable_facts_fail_closed_with_the_rule_named(gap):
    class Broken(World):
        def __call__(self, *args):
            return 1, ""

    assert gap(Broken()).startswith("merge rule: cannot check #803")
    assert gap(World(files=[])) == ""  # no changed file: nothing protected


def test_the_protected_matcher_and_digest_are_one_definition():
    assert protected.matches(["Makefile", "src/a.py", "docs/Makefile.md"]) == {"Makefile"}
    assert protected.execution_inputs('PROTECTED_FILES = ("a", "b")') == {"a", "b"} and protected.execution_inputs(None) == set()
    for bad in ("syntax error(", "OTHER = 1", "PROTECTED_FILES = ()", "PROTECTED_FILES = f()"):
        with pytest.raises(ValueError, match="PROTECTED_FILES"):
            protected.execution_inputs(bad)
    one, text = protected.record({"b": "2", "a": "1"})
    assert one == protected.record({"a": "1", "b": "2"})[0] and '"a": "1"' in text
    assert protected.record({"a": "1", "b": "3"})[0] != one


GATE_RULESET = [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "CI OK"}, {"context": "Gate review"}]}}]
GATE_GREEN = [{"name": n, "status": "COMPLETED", "conclusion": "SUCCESS"} for n in ("CI OK", "Gate review")]


def test_a_ruleset_that_requires_gate_review_does_not_replace_the_recorded_verdict(monkeypatch, tmp_path, real_main_plan):
    """Gate review finding 1 of PR 70: a check is matched by name and a PR can add a job of that name: the verdict is never skipped."""
    rt = runtime(tmp_path)
    world = World(files=["src/heinzel/a.py", ".github/workflows/ci.yml"], ruleset=GATE_RULESET, rollup=GATE_GREEN)
    monkeypatch.setattr(hzlib, "gh", world)
    with native(rt):
        assert "no verdict is recorded" in main_plan.gap("803", HEAD)
    approve(rt, [".github/workflows/ci.yml"])
    with native(rt):
        assert main_plan.gap("803", HEAD) == ""
