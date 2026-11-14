"""`sm head select` prints the checkout's own selector (Core ADR 0149 item a): its classify, its focused_lanes when it has
them (the merge tier), and the moved copy only for a checkout without scripts/affected.py. Scratch checkouts, no Git."""

import json
from pathlib import Path

from coord_kit import app_runtime

from steuermaschine.testing import invoke
from steuermaschine_heinzel.native import affected
from steuermaschine_heinzel.native.affected_lib import select

CLASSIFY = '''
import json, pathlib
class Repo:
    def __init__(self, root):
        self.root = root
        self.lanes = {"web-1": ["tests/e2e/web_ui_test.py"], "web-2": []}
def classify(repo, changed):
    graph = repo.related  # the moved Repo's loader stands in for Repo.related
    full = any(p.startswith("desktop/managed/") or p == "desktop/package.json" for p in changed)
    desktop = ["ALL"] if full else (["desktop/test/window.test.js"] if any(p.startswith("desktop/") and not p.endswith(".md") for p in changed) else [])
    return {"files": sorted(changed), "full": full, "pytest": ["ALL"] if full else [], "vitest": ["ALL"] if full else [],
            "desktop": desktop, "e2e_lanes": [], "jobs": ["electron"] if any(p.startswith("desktop/") for p in changed) else [],
            "reasons": ["stub " + graph.NAME]}
'''
LANES = '''
def focused_lanes(result, lane_files):
    if result["full"]:
        return None, "the selector selects the full set"
    lanes = {"desktop": ["desktop/ALL"]} if result["desktop"] else {}
    return (lanes or None), ("focused" if lanes else "the selector selects no tests")
'''


def checkout(tmp_path: Path, script: str | None) -> Path:
    root = tmp_path / "core"
    (root / "src/heinzel").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "tests/e2e").mkdir(parents=True)
    (root / "Makefile").write_text("all:\n")
    (root / "scripts/related_tests.py").write_text("NAME = 'graph'\n")
    if script is not None:
        (root / "scripts/affected.py").write_text(script)
    return root


def select_json(tmp_path, root, *files):
    done = invoke(app_runtime(tmp_path), ["--format", "json", "head", "select", "--repo", str(root), *files])
    return done, (json.loads(done.stdout) if done.stdout.strip().startswith("{") else {})


def test_the_merge_tier_checkout_prints_its_desktop_tests_and_lanes(tmp_path):
    root = checkout(tmp_path, CLASSIFY + LANES)
    done, data = select_json(tmp_path, root, "desktop/src/main.js")
    assert done.exit_code == 0 and data["selector"] == "core" and data["reasons"] == ["stub graph"]
    assert data["desktop"] == ["desktop/test/window.test.js"] and data["lanes"] == {"desktop": ["desktop/ALL"]} and data["lanes_reason"] == "focused"
    text = invoke(app_runtime(tmp_path), ["head", "select", "--repo", str(root), "desktop/src/main.js"]).stdout
    assert "desktop (1): desktop/test/window.test.js" in text and "focused CI lanes: desktop (desktop/ALL)" in text and "selector: core" in text
    full = invoke(app_runtime(tmp_path), ["head", "select", "--repo", str(root), "desktop/managed/x.ts"]).stdout
    assert "none, CI runs the FULL set: the selector selects the full set" in full


def test_an_older_core_has_no_lanes_and_a_checkout_without_a_selector_uses_the_moved_copy(tmp_path):
    root = checkout(tmp_path, CLASSIFY)
    _, data = select_json(tmp_path, root, "desktop/src/main.js")
    assert data["selector"] == "core" and data["lanes"] is None and data["lanes_reason"] == ""
    other = checkout(tmp_path / "x", None)
    _, data = select_json(tmp_path, other, "docs/a.md")
    assert data["selector"] == "port" and data["full"] is False


def test_a_broken_selector_is_a_refusal(tmp_path):
    done = invoke(app_runtime(tmp_path), ["head", "select", "--repo", str(checkout(tmp_path, "raise RuntimeError('boom')\n")), "x.py"])
    assert done.exit_code == 2 and "scripts/affected.py failed: boom" in done.stderr


def test_self_test_adds_the_desktop_cases_for_a_merge_tier_selector(tmp_path):
    root = checkout(tmp_path, CLASSIFY + LANES)
    done = invoke(app_runtime(tmp_path), ["head", "select", "--repo", str(root), "--self-test"])
    lines = done.stdout.splitlines()
    names = [name for name, _files, _want in affected.DESKTOP_CASES]
    assert all(any(f" {name}:" in line for line in lines) for name in names)
    assert any(line.startswith("ok  desktop managed = full") for line in lines) and done.exit_code == 1  # the stub fails other cases
    assert "CI lanes FULL" in done.stdout


def test_self_test_of_an_older_selector_skips_the_desktop_cases_and_the_port_keeps_its_own(tmp_path, monkeypatch):
    done = invoke(app_runtime(tmp_path), ["head", "select", "--repo", str(checkout(tmp_path, CLASSIFY.replace('"desktop": desktop, ', ''))), "--self-test"])
    assert "desktop app file" not in done.stdout and done.stdout.splitlines()[-1].endswith(("cases pass", "failing"))
    monkeypatch.setattr(select, "self_test", lambda repo: (0, ["port self test"]))
    port = invoke(app_runtime(tmp_path), ["head", "select", "--repo", str(checkout(tmp_path / "p", None)), "--self-test"])
    assert port.stdout == "port self test\n"


def test_problems_reads_every_expectation_kind():
    got = {"full": False, "pytest": ["ALL"], "jobs": ["a"], "desktop": [], "lanes": {"web": []}}
    found = affected._problems(got, {"jobs_has": ["b"], "jobs_not": ["a"], "desktop_some": True, "lanes_has": ["desktop"], "full": True})
    assert found == ["jobs lacks ['b']", "jobs should not have ['a']", "desktop is empty", "lanes lack desktop", "full = False, want True",
                     "pytest ALL without full"]
