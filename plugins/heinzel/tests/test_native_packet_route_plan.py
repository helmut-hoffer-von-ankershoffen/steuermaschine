"""`sm head plan` and `sm ship --plan`: the route of one head, read at the start from the base's own policy.

A real Git repository in a temporary directory holds the base's policy files; the selector and the protected-blob
reader of the base are replaced (each has its own tests). No value of Core is written into the tool: every test that
checks a limit changes it in the base commit and sees the plan follow.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_pipeline_cov_head_admit import native, runtime

from steuermaschine.engine import Refuse
from steuermaschine_heinzel.native import packet, ship_driver
from steuermaschine_heinzel.native.packet_lib import focused_manifest as fm
from steuermaschine_heinzel.native.packet_lib import route_plan
from steuermaschine_heinzel.native.release_admit_lib import release_scope
from steuermaschine_heinzel.pipelines import common

# The limit constants and the lines that apply them are Core's own, as they stand at its main (scripts/focused_plan.py and
# scripts/focused_job.py); only the functions around them are shortened. Nothing here is executed: the plan reads text.
PLAN = '''import re
from scripts.focused_job import MAX_IDS, PYTHON_CI_MAX_IDS
PROTECTED_FILES = ("Makefile", "scripts/focused_plan.py")
PLAN_VARIABLE = "PLANS"
LANES = {"python": ("a", "b", "pytest"), "web": ("a", "b", "vitest")}
PATH = re.compile(r"[A-Za-z0-9_./-]+")
MAX_PLAN = 48 * 1024
MAX_SLOT = 40_000
MAX_SLOT_DECODED = 1024 * 1024
MAX_SLOT_ENTRIES = 256
MAX_DIFF = 1024 * 1024
MAX_COVERED_FILES = 100
max_lower = 1
MAXIMUM = 7


def changed_paths(raw):
    require(len(raw) <= MAX_DIFF and (not raw or raw.endswith(b"\\0")), "unsupported focused diff")


def _selections(plan, commit):
    for lane, tests in plan["selection"].items():
        limit = PYTHON_CI_MAX_IDS if lane == "python" else MAX_IDS
        require(isinstance(tests, list) and 0 < len(tests) <= limit, f"empty or broad focused lane {lane}")


def decode_slot(raw):
    require(len(raw) <= MAX_SLOT_DECODED, "focused slot exceeds its decoded limit")


def _scope(plan, production):
    return len(registry) <= MAX_SLOT_ENTRIES and len(production) <= MAX_COVERED_FILES
'''
JOB = 'import re\nTOKEN = re.compile(r"[a-z]+")\nMAX_IDS = 10\nPYTHON_CI_MAX_IDS = 4096\nMAX_REPEAT = 60\n'
BASE_FILES = {"scripts/focused_plan.py": PLAN, "scripts/focused_job.py": JOB, "scripts/release_candidate.py": 'REPOSITORY = "o/core"\n',
              "Makefile": "all:\n", "src/heinzel/a.py": "A = 1\n", "docs/a.md": "a\n", "pyproject.toml": "[project]\n", "uv.lock": "lock\n"}


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
                          capture_output=True, text=True, check=True)
    return done.stdout.strip()


def commit(repo: Path, files: dict[str, str | None], message: str) -> str:
    for name, text in files.items():
        path = repo / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def repository(tmp_path: Path, plan: str = PLAN, job: str = JOB) -> Path:
    repo = tmp_path / "core"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    commit(repo, {**BASE_FILES, "scripts/focused_plan.py": plan, "scripts/focused_job.py": job}, "base")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "-q", "-b", "fix/topic")
    return repo


def selector(pytest_ids: list[str] | None = None, *, full: bool = False, e2e: list[str] | None = None, vitest: list[str] | None = None) -> Any:
    def select(_repo: Any, _base: str, _tree: str, _changed: dict[str, str]) -> dict[str, Any]:
        return {"full": full, "pytest": ["ALL"] if full else list(pytest_ids if pytest_ids is not None else ["tests/unit/a_test.py"]), "vitest": list(vitest or []),
                "e2e_lanes": list(e2e or []), "lane_files": {lane: [f"tests/e2e/{lane}_test.py"] for lane in e2e or []},
                "reasons": ["uv.lock: shared/sensitive path, selects the full set"] if full else []}
    return select


def plan_of(repo: Path, select: Any = None, mode: str = "off") -> route_plan.RoutePlan:
    return route_plan.plan(fm.Repo(repo), "HEAD", "origin/main", worktree=str(repo), branch="fix/topic", merge_tier_mode=mode,
                           select=select or selector(), pins=lambda *_a: None)


def route(got: route_plan.RoutePlan, name: str) -> route_plan.Route:
    return next(r for r in got.routes if r.name == name)


def limit(got: route_plan.RoutePlan, name: str) -> route_plan.Limit:
    return next(item for item in got.limits if item.name == name)


# ----------------------------------------------------------------------------- tier, size, route, stops
def test_an_ordinary_head_takes_focused_admit_with_two_review_stops(tmp_path):
    repo = repository(tmp_path)
    head = commit(repo, {"src/heinzel/a.py": "A = 2\n", "tests/unit/a_test.py": "def test_a(): pass\n"}, "fix")
    got = plan_of(repo)
    assert (got.tier, got.head, got.changed, got.production, got.selection, got.protected) == ("focused", head, 2, 1, {"python": 1}, [])
    assert got.route == "focused admit" and got.exit_code == 0
    assert route(got, "protected-producer route").state == "OPEN" and route(got, "release path").state == "CLOSED"
    assert [stop.split(":")[0] for stop in got.review_stops] == ["packet review", "apply review", "at the pr step"]
    text = "\n".join(got.render())
    assert "TIER     focused" in text and "SIZE     2 changed file(s), 1 production; test ids: python 1" in text
    assert "ROUTE    focused admit: TAKEN." in text and f"sm ship {repo}" in text and "REFUSED" not in text


def test_cores_real_constants_leave_an_ordinary_small_head_open(tmp_path):
    """The review's probe: MAX_SLOT_ENTRIES 256, MAX_IDS 10, PYTHON_CI_MAX_IDS 4096 as Core has them, and a small head."""
    repo = repository(tmp_path)
    commit(repo, {"src/heinzel/a.py": "A = 2\n" + "# a comment line that makes the diff larger\n" * 40, "tests/unit/a_test.py": "def test_a(): pass\n"}, "fix")
    got = plan_of(repo, selector([f"tests/unit/t{n}_test.py" for n in range(11)]))  # 11 Python ids: over MAX_IDS, which Core never applies to Python
    values = {item.name: (item.value, item.binds) for item in got.limits}
    assert values["MAX_SLOT_ENTRIES"] == (256, "unknown") and values["MAX_IDS"] == (10, "no") and values["PYTHON_CI_MAX_IDS"] == (4096, "no")
    assert [item.name for item in got.limits if item.binds == "yes"] == []
    assert route(got, "focused admit").state == "TAKEN" and got.route == "focused admit" and got.exit_code == 0
    assert "REFUSED" not in "\n".join(got.render())


def test_every_limit_is_read_from_the_base_and_only_the_measured_ones_can_bind(tmp_path):
    repo = repository(tmp_path)
    commit(repo, {"src/heinzel/a.py": "A = 2\n"}, "fix")
    got = plan_of(repo, selector(["tests/a_test.py", "tests/b_test.py"], vitest=["web/a.test.ts"]))
    names = [item.name for item in got.limits]
    assert names[:-1] == ["MAX_PLAN", "MAX_SLOT", "MAX_SLOT_DECODED", "MAX_SLOT_ENTRIES", "MAX_DIFF", "MAX_COVERED_FILES",
                          "MAX_IDS", "PYTHON_CI_MAX_IDS", "MAX_REPEAT"]  # max_lower and MAXIMUM are no limits
    for name in ("MAX_PLAN", "MAX_SLOT", "MAX_SLOT_DECODED", "MAX_SLOT_ENTRIES", "MAX_REPEAT"):  # counted in entries, encoded bytes, repeats
        assert (limit(got, name).measured, limit(got, name).binds, limit(got, name).what) == (None, "unknown", route_plan.NOT_MEASURED)
    assert (limit(got, "PYTHON_CI_MAX_IDS").measured, limit(got, "PYTHON_CI_MAX_IDS").what) == (2, "test ids in lane python")
    assert (limit(got, "MAX_IDS").measured, limit(got, "MAX_IDS").what) == (1, "test ids in lane web")
    assert (limit(got, "MAX_COVERED_FILES").measured, limit(got, "MAX_COVERED_FILES").binds) == (1, "no")
    assert limit(got, "MAX_DIFF").measured == got.limits[-1].measured > 0 and limit(got, "MAX_DIFF").binds == "no"
    assert limit(got, "MAX_IDS").source == "scripts/focused_job.py at the base" and limit(got, "MAX_PLAN").value == 48 * 1024
    assert got.limits[-1].source.startswith("sm ") and got.limits[-1].value == fm.MAX_DIFF  # the tool's own bound, named as such
    assert "MAX_SLOT_ENTRIES = 256 (scripts/focused_plan.py at the base): not measured" in "\n".join(got.render())


def test_a_binding_limit_closes_the_route_at_the_start_and_follows_the_base(tmp_path):
    repo = repository(tmp_path)
    commit(repo, {"src/heinzel/a.py": "A = 2\n"}, "fix")
    eleven = selector(vitest=[f"web/t{n}.test.ts" for n in range(11)])
    got = plan_of(repo, eleven)
    assert limit(got, "MAX_IDS").binds == "yes" and limit(got, "PYTHON_CI_MAX_IDS").binds == "no"
    closed = route(got, "focused admit")
    assert closed.state == "CLOSED" and "MAX_IDS = 10" in closed.why and "this head has 11 test ids in lane web" in closed.why
    assert "split the head" in closed.alternative and got.route == "" and got.exit_code == 1
    assert got.review_stops == [] and got.render()[-1].startswith("REFUSED  no route is open")
    assert plan_of(repo, selector(vitest=[f"web/t{n}.test.ts" for n in range(10)])).route == "focused admit"  # at the limit is not over it

    lifted = repository(tmp_path / "lifted", job=JOB.replace("MAX_IDS = 10\n", "MAX_IDS = 30\n", 1))
    commit(lifted, {"src/heinzel/a.py": "A = 2\n"}, "fix")
    free = plan_of(lifted, eleven)
    assert free.route == "focused admit" and limit(free, "MAX_IDS").value == 30


def test_a_limit_the_base_applies_in_another_way_is_unknown_and_closes_nothing(tmp_path):
    plan = PLAN.replace("0 < len(tests) <= limit", "0 < count(tests) <= limit").replace("len(raw) <= MAX_DIFF", "size(raw) <= MAX_DIFF")
    plan = plan.replace("len(production) <= MAX_COVERED_FILES", "len(set(production)) <= MAX_COVERED_FILES")
    repo = repository(tmp_path, plan, JOB.replace("MAX_IDS = 10\n", "MAX_IDS = 1\n", 1).replace("4096", "1"))
    commit(repo, {"src/heinzel/a.py": "A = 2\n"}, "fix")
    got = plan_of(repo, selector(["tests/a_test.py", "tests/b_test.py"], vitest=["web/a.test.ts", "web/b.test.ts"]))
    assert {item.binds for item in got.limits[:-1]} == {"unknown"} and got.route == "focused admit" and got.exit_code == 0


def test_the_constant_names_come_from_the_bases_own_lines(tmp_path):
    plan = PLAN.replace("PYTHON_CI_MAX_IDS", "PY_MAX").replace("MAX_COVERED_FILES", "MAX_SOURCES").replace('lane == "python"', "lane == 'web'")
    repo = repository(tmp_path, plan, JOB.replace("PYTHON_CI_MAX_IDS = 4096", "PY_MAX = 1"))
    commit(repo, {"src/heinzel/a.py": "A = 2\n"}, "fix")
    got = plan_of(repo, selector(["tests/a_test.py"], vitest=["web/a.test.ts", "web/b.test.ts"]))
    assert (limit(got, "PY_MAX").measured, limit(got, "PY_MAX").what, limit(got, "PY_MAX").binds) == (2, "test ids in lane web", "yes")
    assert (limit(got, "MAX_IDS").measured, limit(got, "MAX_IDS").what) == (1, "test ids in lane python")
    assert limit(got, "MAX_SOURCES").measured == 1 and got.exit_code == 1


def test_the_merge_tier_needs_no_plan_and_its_plan_limits_do_not_apply(tmp_path):
    repo = repository(tmp_path)
    commit(repo, {"src/heinzel/a.py": "A = 2\n"}, "fix")
    got = plan_of(repo, selector(["a"], vitest=[f"web/t{n}.test.ts" for n in range(11)]), mode="on")
    assert got.tier == "merge" and got.route == "focused admit" and "no plan" in route(got, "focused admit").why
    assert (limit(got, "MAX_IDS").measured, limit(got, "MAX_IDS").binds) == (11, "no") and "not applied" in limit(got, "MAX_IDS").what
    assert limit(got, "MAX_SLOT_ENTRIES").binds == "unknown"
    assert [stop.split(":")[0] for stop in got.review_stops] == ["at the pr step"]


def test_the_tier_is_detected_from_the_base_itself(tmp_path):
    repo = repository(tmp_path, PLAN + 'WHOLE_SUITES = {"web": "web/ALL"}\n')
    commit(repo, {"src/heinzel/a.py": "A = 2\n"}, "fix")
    assert plan_of(repo, mode="auto").tier == "merge"
    older = repository(tmp_path / "older")
    commit(older, {"src/heinzel/a.py": "A = 2\n"}, "fix")
    assert plan_of(older, mode="auto").tier == "focused"


def test_a_full_selection_is_closed_without_the_merge_tier_and_taken_with_it(tmp_path):
    repo = repository(tmp_path)
    commit(repo, {"uv.lock": "lock 2\n", "src/heinzel/a.py": "A = 2\n"}, "deps")
    got = plan_of(repo, selector(full=True))
    closed = route(got, "focused admit")
    assert got.tier == "full" and closed.state == "CLOSED" and "full set" in closed.why and "--set sel=SEL.json" in closed.alternative
    assert got.exit_code == 1
    tiered = plan_of(repo, selector(full=True), mode="on")
    assert tiered.tier == "full" and tiered.route == "focused admit" and "CI runs the FULL set" in route(tiered, "focused admit").why


def test_hosted_lanes_are_named_as_a_stop_before_admission(tmp_path):
    repo = repository(tmp_path)
    commit(repo, {"src/heinzel/a.py": "A = 2\n"}, "fix")
    got = plan_of(repo, selector(e2e=["web-1"]))
    assert got.hosted_lanes == ["web-1"] and any("hosted validation of web-1" in stop for stop in got.review_stops)


# ----------------------------------------------------------------------------- the protected-producer route
@pytest.mark.parametrize("mode", ["off", "on"])
def test_a_protected_change_takes_the_protected_producer_route_in_either_tier(tmp_path, mode):
    repo = repository(tmp_path)
    commit(repo, {"Makefile": "all: test\n", "src/heinzel/a.py": "A = 2\n"}, "ci")
    got = plan_of(repo, mode=mode)
    assert got.protected == ["Makefile"] and got.route == "protected-producer route" and got.exit_code == 0
    closed = route(got, "focused admit")
    assert closed.state == "CLOSED" and "Makefile" in closed.why and "takes it by itself" in closed.alternative
    assert "paired main plan" in route(got, "protected-producer route").why
    assert got.review_stops[0].startswith("packet review: an independent verdict on protected-producer.json")
    assert got.review_stops[1].startswith("apply review: a separate independent verdict on prepare.out")
    assert limit(got, "MAX_IDS").binds == "no" and "not applied" not in limit(got, "MAX_IDS").what  # a plan is built again: the limits apply


@pytest.mark.parametrize(("path", "old", "new", "name"), [
    ("scripts/focused_plan.py", 'PLAN_VARIABLE = "PLANS"', 'PLAN_VARIABLE = "OTHER"', "PLAN_VARIABLE"),
    ("scripts/focused_plan.py", '("Makefile", "scripts/focused_plan.py")', '("scripts/focused_plan.py",)', "PROTECTED_FILES"),
])
def test_a_producer_change_of_the_identity_constants_has_no_route(tmp_path, path, old, new, name):
    repo = repository(tmp_path)
    commit(repo, {path: PLAN.replace(old, new)}, "ci")
    got = plan_of(repo)
    closed = route(got, "protected-producer route")
    assert closed.state == "CLOSED" and name in closed.why and "none in sm" in closed.alternative and got.exit_code == 1


def test_a_changed_repository_identity_is_seen_when_its_file_is_protected(tmp_path):
    plan = PLAN.replace('"scripts/focused_plan.py")', '"scripts/focused_plan.py", "scripts/release_candidate.py")')
    repo = repository(tmp_path, plan)
    commit(repo, {"scripts/release_candidate.py": 'REPOSITORY = "x/y"\n'}, "ci")
    got = plan_of(repo)
    assert got.protected == ["scripts/release_candidate.py"] and "REPOSITORY" in route(got, "protected-producer route").why


def test_a_reformatted_identity_constant_is_not_a_change(tmp_path):
    repo = repository(tmp_path)
    commit(repo, {"scripts/focused_plan.py": PLAN.replace('PLAN_VARIABLE = "PLANS"', "PLAN_VARIABLE   =   'PLANS'  # same")}, "ci")
    assert plan_of(repo).route == "protected-producer route"


def test_a_binding_limit_closes_the_protected_producer_route_too(tmp_path):
    repo = repository(tmp_path)
    commit(repo, {"Makefile": "all: test\n"}, "ci")
    got = plan_of(repo, selector(vitest=[f"web/t{n}.test.ts" for n in range(11)]), mode="on")
    assert route(got, "protected-producer route").state == "CLOSED" and "a limit binds: MAX_IDS = 10" in route(got, "protected-producer route").why


def test_the_bases_own_protected_blobs_decide_when_it_has_them(tmp_path):
    repo = repository(tmp_path)
    commit(repo, {"src/heinzel/a.py": "A = 2\n"}, "fix")
    seen = []

    def pins(_repo: Any, base: str, treeish: str) -> dict[str, str]:
        seen.append(base == treeish)
        return {"tests/_driver.py": "1" * 40} if base == treeish else {"tests/_driver.py": "2" * 40, "tests/_new.py": "3" * 40}

    got = route_plan.plan(fm.Repo(repo), "HEAD", "origin/main", select=selector(), pins=pins, merge_tier_mode="off")
    assert got.protected == ["tests/_driver.py", "tests/_new.py"] and seen == [False, True]
    gone = route_plan.pins_changed(None, "b", "t", None, lambda _r, b, t: {"kept": "1" * 40, "gone": "2" * 40} if b == t else {"kept": "1" * 40})
    assert gone == ["gone"]  # a pinned file the head deletes


# ----------------------------------------------------------------------------- the release path, no route, nothing yet
def test_the_release_path_is_open_only_for_the_generated_release_commit(tmp_path):
    repo = repository(tmp_path)
    commit(repo, {"pyproject.toml": "[project]\nversion = '1'\n", "uv.lock": "lock 2\n"}, "release")
    got = plan_of(repo, selector(full=True))
    release = route(got, "release path")
    assert set(release_scope.SCOPE) >= {"pyproject.toml", "uv.lock"} and release.state == "OPEN" and release.command == "sm release cut VERSION"
    commit(repo, {"src/heinzel/a.py": "A = 2\n", "docs/a.md": "b\n"}, "more")
    closed = route(plan_of(repo, selector(full=True)), "release path")
    assert closed.state == "CLOSED" and "2 changed path(s) are outside it (docs/a.md, src/heinzel/a.py)" in closed.why and "sm ship" in closed.alternative


def test_a_head_that_conflicts_with_the_base_has_no_route(tmp_path):
    repo = repository(tmp_path)
    commit(repo, {"src/heinzel/a.py": "A = 2\n"}, "mine")
    git(repo, "checkout", "-q", "main")
    moved = commit(repo, {"src/heinzel/a.py": "A = 3\n"}, "theirs")
    git(repo, "update-ref", "refs/remotes/origin/main", moved)
    git(repo, "checkout", "-q", "fix/topic")
    got = plan_of(repo, lambda *_a: pytest.fail("no selection of an unmerged tree"))
    assert got.exit_code == 1 and "conflicts with the base in src/heinzel/a.py" in got.routes[0].why and "merge origin/main" in got.routes[0].alternative


def test_a_head_without_a_commit_is_planned_later_and_not_refused(tmp_path):
    repo = repository(tmp_path)
    got = plan_of(repo, lambda *_a: pytest.fail("nothing to select"))
    assert (got.tier, got.routes, got.exit_code) == ("none", [], 0) and "nothing is committed" in got.render()[1]


def test_a_diff_past_the_tools_own_bound_is_closed_with_that_bound(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    commit(repo, {"src/heinzel/a.py": "A = 2\n"}, "fix")
    monkeypatch.setattr(fm, "MAX_DIFF", 10)
    got = plan_of(repo, lambda *_a: pytest.fail("not selected"))
    assert got.exit_code == 1 and got.limits[-1].binds == "yes" and "split the head" in got.routes[0].alternative


# ----------------------------------------------------------------------------- the readers
def test_only_integer_constants_named_max_are_limits_and_nothing_is_evaluated():
    source = b"MAX_A = 2 * 3 + 1 - 2\nMAX_B = int('4')\nMAX_C = 'x'\nA_MAX = True\nMAX_D: int = 5\nMAX_E = 2 ** 3\n"
    assert route_plan.core_limits(lambda path: source if path.endswith("plan.py") else None) == [
        ("MAX_A", "scripts/focused_plan.py", 5), ("MAX_D", "scripts/focused_plan.py", 5)]
    assert route_plan.core_limits(lambda _path: b"def (:\n") == []
    assert route_plan._number(ast.parse("1 + x", mode="eval").body) is None


def test_a_base_without_the_id_rule_or_with_no_lane_selected_measures_what_it_can():
    facts = {"ids": {}, "diff_bytes": 100, "production": 3}
    diff = 'require(len(raw) <= MAX_DIFF and (not raw or raw.endswith(b"\\0")), "x")\n'
    assert route_plan.applied("require(len(raw) <= MAX_SLOT_DECODED)\n" + diff, facts) == {"MAX_DIFF": (100, "bytes of the raw diff")}
    assert route_plan.applied(diff + diff.replace("MAX_DIFF", "MAX_OTHER"), facts) == {}  # two different constants in the same form: ambiguous
    assert route_plan.applied("len(production) <= A\nlen(production) <= A\n", facts) == {"A": (3, "changed production files")}
    rule = 'limit = A if lane == "python" else B\nrequire(len(tests) <= limit)\n'
    assert route_plan.applied(rule, facts) == {"A": (0, "test ids in lane python"), "B": (0, "test ids in a lane other than python (none selected)")}
    assert route_plan.applied(rule, {**facts, "ids": {"python": 4, "web": 9, "browser": 2}}) == {"A": (4, "test ids in lane python"), "B": (9, "test ids in lane web")}
    assert route_plan.applied("", facts) == {}


def test_the_identity_constants_are_the_ones_prepare_compares():
    """route_plan.IDENTITY has no copy to drift from silently: the gate's own comparison names the same three."""
    import inspect

    from steuermaschine_heinzel.native.admission_lib import focused_admit

    gate = inspect.getsource(focused_admit.validate_canonical)
    for path, name in route_plan.IDENTITY:
        assert f'"{name}"' in gate and (path in gate or name in ("PROTECTED_FILES", "PLAN_VARIABLE")), name
    assert gate.count("_literal(") == len(route_plan.IDENTITY)


def test_an_identity_constant_missing_on_one_side_is_a_change():
    base = {"scripts/focused_plan.py": b"PROTECTED_FILES = ('a',)\nPLAN_VARIABLE = 'P'\n", "scripts/release_candidate.py": b"REPOSITORY = 'o/c'\n"}
    tree = {**base, "scripts/release_candidate.py": b"def (:\n"}
    assert route_plan.identity_changed(base.get, base.get) == [] and route_plan.identity_changed(base.get, tree.get) == ["REPOSITORY"]
    assert route_plan.identity_changed(base.get, {"scripts/focused_plan.py": base["scripts/focused_plan.py"]}.get) == ["REPOSITORY"]


# ----------------------------------------------------------------------------- the commands
def test_head_plan_reads_the_committed_head_of_the_worktree_and_the_setting(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    commit(repo, {"src/heinzel/a.py": "A = 2\n"}, "fix")
    real, seen = route_plan.plan, {}

    def planned(*args: Any, **kw: Any) -> route_plan.RoutePlan:
        seen.update(kw)
        return real(*args, **kw, select=selector(), pins=lambda *_a: None)

    monkeypatch.setattr(route_plan, "plan", planned)
    with native(runtime(tmp_path)):
        got = packet.head_plan(repo, base="origin/main")
        assert got.branch == "fix/topic" and got.route == "focused admit" and seen["merge_tier_mode"] == "auto"
        with pytest.raises(Refuse, match="is not a directory"):
            packet.head_plan(tmp_path / "missing", base="origin/main")
        with pytest.raises(Refuse, match="cannot plan the route"):
            packet.head_plan(repo, base="origin/nowhere")


def test_ship_plan_prints_the_route_and_runs_nothing(tmp_path):
    common.OVERRIDE.clear()
    plan = route_plan.RoutePlan(head="a" * 40, base="b" * 40, tier="full", lines=["PLAN x", "REFUSED  no route"],
                                routes=[route_plan.Route(name="focused admit", state="CLOSED", why="full")])
    common.OVERRIDE["head plan"] = lambda **kw: plan if kw == {"worktree": tmp_path} else pytest.fail(str(kw))
    try:
        with native(runtime(tmp_path)):
            got = ship_driver.ship(str(tmp_path), live=False, approved="", author="", set_=None, wait=None, plan=True)
            assert (got.status, got.phase, got.exit_code, got.render()) == ("plan", "route: none", 1, ["PLAN x", "REFUSED  no route"])
            for extra in ({"live": True}, {"approved": "a" * 64}, {"wait": 5}, {"set_": ["bead=x"]}):
                with pytest.raises(Refuse, match="--plan only reads"):
                    ship_driver.ship(str(tmp_path), **{"live": False, "approved": "", "author": "", "set_": None, "wait": None, **extra}, plan=True)
    finally:
        common.OVERRIDE.clear()
