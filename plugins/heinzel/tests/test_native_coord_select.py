"""`sm head select`. The former selector's tests, on the moved module: the built-in cases against a core worktree, and
the output shapes. Needs a core worktree with its import graph (scripts/related_tests.py) on this machine, read only;
skipped without one, as the former tests were. Old and new output were compared on 2026-11-12 (parity/head-select.md)."""

import json
import os
import pwd
from pathlib import Path

import pytest
from coord_kit import app_runtime, ok

from steuermaschine.engine import Refuse
from steuermaschine.native import NativeRuntime, using
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, invoke
from steuermaschine_heinzel.native import affected
from steuermaschine_heinzel.native.affected_lib import select

CODE = Path(pwd.getpwuid(os.getuid()).pw_dir) / "Code"  # the real home: the root conftest sandboxes HOME
CORE = next((p.parent.parent for p in sorted(CODE.glob("heinzel*/scripts/related_tests.py")) if (p.parent.parent / "Makefile").is_file()), None)
needs_core = pytest.mark.skipif(CORE is None, reason="no core worktree with scripts/related_tests.py under ~/Code")


def run(tmp_path, *args, commands=None, **over):
    return invoke(app_runtime(tmp_path, commands, **over), ["--format", "json", "head", "select", "--repo", str(CORE), *args])


@needs_core
def test_self_test_runs_every_case_and_knows_every_e2e_lane(tmp_path):
    done = invoke(app_runtime(tmp_path), ["head", "select", "--repo", str(CORE), "--self-test"])
    lines = done.stdout.splitlines()
    assert len([line for line in lines if line.startswith(("ok ", "FAIL "))]) >= len(select.CASES)
    assert "e2e files without a lane" not in done.stdout
    assert done.exit_code == (1 if "FAIL" in done.stdout else 0) and (lines[-1].endswith("cases pass") or lines[-1].endswith("failing"))


@needs_core
def test_json_shape_and_full_for_kernel(tmp_path):
    data = json.loads(run(tmp_path, "src/heinzel/kernel/_errors.py").stdout)
    assert data["full"] is True
    assert data["pytest"] == ["ALL"] and data["vitest"] == ["ALL"]
    assert set(data) >= {"files", "full", "pytest", "vitest", "e2e_lanes", "jobs", "reasons"}
    affected.Selection.model_validate(data)


@needs_core
def test_web_only_skips_python_matrix(tmp_path, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("web/src/layout/TopBar.tsx\nweb/src/layout/TopBar.test.tsx\n"))
    data = json.loads(run(tmp_path, "--stdin").stdout)
    assert data["full"] is False
    assert "test-linux" not in data["jobs"] and "coverage" not in data["jobs"] and "e2e-cli" not in data["jobs"]
    assert "web/src/layout/TopBar.test.tsx" in data["vitest"]
    assert data["e2e_lanes"] == ["web-1", "web-2"]


@needs_core
def test_text_output_mentions_reasons(tmp_path):
    done = invoke(app_runtime(tmp_path), ["head", "select", "--repo", str(CORE), "docs/guides/install.md"])
    assert done.exit_code == 0
    assert "full: no" in done.stdout and "site tests" in done.stdout


@needs_core
def test_python_sources_tests_desktop_and_unknown_paths(tmp_path):
    data = json.loads(run(tmp_path, "src/heinzel/modules/heinzel/_autonomous_schedule.py", "tests/unit/standards_test.py", "web/e2e/mobile-responsive.mjs").stdout)
    assert data["full"] is False and "tests/unit/standards_test.py" in data["pytest"] and "test-linux" in data["jobs"]
    assert json.loads(run(tmp_path, "desktop/src/main.js").stdout)["jobs"].count("electron") == 1
    assert json.loads(run(tmp_path, "something/new.txt").stdout)["full"] is True
    assert json.loads(run(tmp_path, "web/openapi.json", "web/src/api/schema.d.ts", "src/heinzel/heads/cli/_overview.py", "tests/e2e/audio_visualizer_test.py").stdout)["vitest"] == ["ALL"]


@needs_core
def test_base_adds_the_three_dot_diff_and_the_working_tree(tmp_path):
    c = FakeCommands()
    c.on(["git", "-C", str(CORE), "diff"], ok("docs/guides/install.md\n"))
    c.on(["git", "-C", str(CORE), "status"], ok(" M README.md\nR  old.md -> docs/new.md\n"))
    data = json.loads(run(tmp_path, "--base", "origin/main", commands=c).stdout)
    assert data["files"] == ["README.md", "docs/guides/install.md", "docs/new.md"] and data["full"] is False
    c.on(["git", "-C", str(CORE), "diff"], Result(128, "", "fatal: bad revision"))
    done = run(tmp_path, "--base", "nope", commands=c)
    assert done.exit_code == 2 and "git diff failed: fatal: bad revision" in done.stdout


def test_a_directory_that_is_no_core_checkout_and_no_configured_checkout_are_usage_errors(tmp_path):
    done = invoke(app_runtime(tmp_path), ["head", "select", "--repo", str(tmp_path), "x.py"])
    assert done.exit_code == 2 and "is not a core checkout" in done.stderr
    rt = app_runtime(tmp_path)
    with using(NativeRuntime(rt.providers, {}, rt.project)), pytest.raises(Refuse) as stop:
        affected.head_select(["x.py"], None, None, False, False)
    assert stop.value.code == 2 and "no core checkout is configured" in stop.value.message


def test_render_shortens_long_lists():
    text = select.render({"full": False, "jobs": [], "e2e_lanes": [], "pytest": [f"t{i}" for i in range(14)], "vitest": [f"v{i}" for i in range(13)], "reasons": ["r"]})
    assert "pytest (14): t0" in text and "... +2" in text and "... +1" in text and text.splitlines()[-1] == "  - r"


GRAPH_WITHOUT_TREES = '''
from pathlib import Path
ROOT = Path(".")


class Graph:
    """The current core's graph: `known` and `files`, no `trees` (heinzel-management-grjz)."""

    def __init__(self):
        self.files = ["src/heinzel/modules/system/update/_all.py", "tests/unit/update_all_test.py", "tests/unit/other_test.py"]
        self.known = frozenset(self.files)
        self.imports = {"tests/unit/update_all_test.py": {"src/heinzel/modules/system/update/_all.py"}}

    def dependents(self, changed):
        return set(changed) | {f for f, targets in self.imports.items() if targets & set(changed)}


def graph():
    return Graph()
'''


def test_python_tests_for_reads_a_graph_without_trees(tmp_path):
    """Regression (grjz): the update-all head crashed with 'Graph' object has no attribute 'trees'."""
    core = tmp_path / "core"
    for rel, text in {"Makefile": "", "scripts/related_tests.py": GRAPH_WITHOUT_TREES, "src/heinzel/modules/system/update/_all.py": "",
                      "tests/unit/update_all_test.py": "", "tests/unit/other_test.py": ""}.items():
        (core / rel).parent.mkdir(parents=True, exist_ok=True)
        (core / rel).write_text(text, encoding="utf-8")
    repo = select.Repo(core)
    assert repo.python_tests_for(["src/heinzel/modules/system/update/_all.py"]) == ["tests/unit/update_all_test.py"]
    assert repo.related.ROOT == core and Path(repo.related.__file__) == core / "scripts/related_tests.py"


def test_graph_files_prefers_known_then_trees_then_files():
    class Old:
        trees = {"a.py": None}
        files = ["b.py"]

    class Bare:
        pass

    assert select.Repo.graph_files(Old()) == {"a.py"} and select.Repo.graph_files(Bare()) == set()


UPDATE_ALL = CODE / "heinzel-update-all-0941b"


@pytest.mark.skipif(not (UPDATE_ALL / "scripts/related_tests.py").is_file(), reason="no update-all worktree on this machine")
def test_update_all_head_selects_without_crashing(tmp_path):
    changed = ["src/heinzel/modules/system/_plugin_releases.py"]
    data = select.classify(select.Repo(UPDATE_ALL), changed)
    assert data["files"] == changed and data["pytest"] != ["ALL"] and data["pytest"]
