"""The logic of `sm check` in parts: the probe that asks Core's own hook (run here against a small hook, in this
interpreter), the hook's step lines, the plan from the selector's and the hook's answer, and which tests run where."""

import contextlib
import io
import json
import sys

import pytest

from steuermaschine_heinzel.native.check_lib import cheap
from steuermaschine_heinzel.native.preflight_lib import head

HOOK = '''
def is_document(path):
    return path.endswith(".md")
def is_docs_tooling(path):
    return path == "scripts/site_build.py"
def web_test_step(files):
    if files is None:
        return "&web-tests=web exec vitest run"
    if "bad" in files:
        raise ValueError("WEB_TEST_FILES must list web tests")
    return "&web-tests=web exec vitest run --maxWorkers=2 " + files
def scoped_steps(changed, *, web_test_files=None):
    web = [p for p in changed if p.startswith("web/") and not is_document(p)]
    docs = [p for p in changed if is_document(p) or is_docs_tooling(p)]
    if not changed or len(web) + len(docs) != len(changed):
        return None
    return (["&typos=uv run typos"] if docs else []) + (["web-lint=web exec biome ci .", web_test_step(web_test_files)] if web else [])
def early_guard_commands(changed):
    return [["uv", "run", "pytest", "tests/unit/ci_test.py"]] if "Makefile" in changed else []
'''


def probe(tmp_path, monkeypatch, changed, vitest, hook=HOOK):
    """Run the probe as the worktree's interpreter would: its two arguments in, one JSON line out."""
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts/scoped_checks.py").write_text(hook)
    monkeypatch.setattr(sys, "argv", ["-c", str(tmp_path), json.dumps({"changed": changed, "vitest": vitest})])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(cheap.PROBE, "probe", "exec"), {"__name__": "probe"})
    return json.loads(out.getvalue())


def test_the_probe_sorts_the_changed_paths_with_the_hooks_own_predicates(tmp_path, monkeypatch):
    found = probe(tmp_path, monkeypatch, ["web/src/a.ts", "web/README.md", "docs/adr/0001.md", "scripts/site_build.py", "src/heinzel/x.py", "Makefile"], ["web/src/a.test.ts"])
    assert found["web_files"] == ["web/src/a.ts"] and found["doc_files"] == ["web/README.md", "docs/adr/0001.md", "scripts/site_build.py"]
    assert found["web"] == ["web-lint=web exec biome ci .", "&web-tests=web exec vitest run --maxWorkers=2 web/src/a.test.ts"]
    assert found["docs"] == ["&typos=uv run typos"] and found["guards"] == [["uv", "run", "pytest", "tests/unit/ci_test.py"]] and found["note"] == ""


def test_the_probe_runs_every_vitest_file_for_all_none_for_an_empty_selection_and_says_when_the_hook_refuses_the_files(tmp_path, monkeypatch):
    assert probe(tmp_path, monkeypatch, ["web/src/a.ts"], None)["web"][-1] == "&web-tests=web exec vitest run"
    assert probe(tmp_path, monkeypatch, ["web/src/a.ts"], [])["web"] == ["web-lint=web exec biome ci ."]
    refused = probe(tmp_path, monkeypatch, ["web/src/a.ts"], ["bad.ts"])
    assert refused["web"][-1] == "&web-tests=web exec vitest run" and "every Vitest file ran" in refused["note"]
    assert probe(tmp_path, monkeypatch, ["src/heinzel/x.py"], []) == {"web": [], "docs": [], "guards": [], "web_files": [], "doc_files": [], "note": ""}


def test_the_probe_answers_with_an_error_for_a_hook_it_cannot_ask(tmp_path, monkeypatch):
    assert probe(tmp_path, monkeypatch, ["a.md"], [], hook="X = 1\n")["error"].startswith("AttributeError")
    assert "SyntaxError" in probe(tmp_path, monkeypatch, ["a.md"], [], hook="def (\n")["error"]


def test_steps_of_reads_the_hooks_step_lines():
    assert cheap.steps_of(["web-prepare=uv run --script scripts/web_build.py prepare", "&docs=env -u HZ_X uv run python scripts/generate_docs.py --check", "broken", "&empty="]) == [
        ("web-prepare", ["uv", "run", "--script", "scripts/web_build.py", "prepare"]), ("docs", ["env", "-u", "HZ_X", "uv", "run", "python", "scripts/generate_docs.py", "--check"])]


def context(tmp_path, answers, changed):
    """A context whose processes are scripted: `answers` maps a needle in the command line to (exit code, output)."""
    calls = []

    def sh(argv, cwd=None, input=None, env=None):
        line = " ".join(str(word) for word in argv)
        calls.append(line)
        if line.startswith("git diff --name-only"):
            return 0, "\n".join(changed)
        return next((answer for needle, answer in answers.items() if needle in line), (0, ""))

    return cheap.Ctx(wt=tmp_path, sh=sh, gate=sh), calls


def test_hook_facts_names_why_the_hook_could_not_be_read(tmp_path):
    c, _ = context(tmp_path, {}, [])
    assert cheap.hook_facts(c, [], []) == ({}, "Core has no scripts/scoped_checks.py")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/scoped_checks.py").write_text("")
    for answer, why in (((1, ""), "could not be asked (exit 1)"), ((0, "uv notice\nnot json"), "could not be asked (exit 0)"), ((0, "[]"), "could not be asked"),
                        ((0, '{"error": "boom"}'), "does not answer: boom")):
        c, _ = context(tmp_path, {"core_scoped_checks": answer}, [])
        assert why in cheap.hook_facts(c, [], [])[1], answer
    c, calls = context(tmp_path, {"core_scoped_checks": (0, 'warning from a tool\n{"web": [], "note": "n"}\n')}, [])
    assert cheap.hook_facts(c, ["a.md"], None) == ({"web": [], "note": "n"}, "")  # the last line is the answer
    assert calls[-1].startswith('sh -c exec "$@" 2>/dev/null sh uv run --quiet python -c') and calls[-1].endswith('{"changed": ["a.md"], "vitest": null}')


def test_plan_takes_the_selectors_tests_or_narrows_a_full_selection_with_the_hooks_graph(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/related_tests.py").write_text("")
    c, _ = context(tmp_path, {}, ["src/heinzel/x.py"])
    p = cheap.plan(c, {"full": False, "pytest": ["tests/unit/x_test.py"], "vitest": [], "e2e_lanes": ["cli"], "selector": "core"}, ["tests/unit/y_test.py"])
    assert (p.pytest, p.full, p.narrowed, p.lanes, p.extra, p.selector) == (["tests/unit/x_test.py"], False, "", ["cli"], ["tests/unit/y_test.py"], "core")
    assert p.hook == "Core has no scripts/scoped_checks.py" and p.web == [] and p.guards == []
    c, _ = context(tmp_path, {"related_tests.py": (0, "tests/unit/a_test.py\n")}, ["pyproject.toml", "web/src/a.ts"])
    p = cheap.plan(c, {"full": True, "pytest": ["ALL"], "vitest": ["ALL"]}, [])
    assert p.pytest == ["tests/unit/a_test.py"] and p.narrowed == "the selector selects the full set; scripts/related_tests.py narrows the unit tests to 1 file(s)"
    c, calls = context(tmp_path, {"related_tests.py": (0, "ALL\n")}, ["pyproject.toml"])
    p = cheap.plan(c, {"full": True, "pytest": ["ALL"], "vitest": ["ALL"]}, [])
    assert p.pytest is None and p.narrowed == "the selector selects the full set and nothing narrows it"
    c, _ = context(tmp_path, {"related_tests.py": (3, "")}, ["pyproject.toml"])
    assert "scripts/related_tests.py failed (exit 3)" in cheap.plan(c, {"full": True, "pytest": ["ALL"]}, []).narrowed
    c, calls = context(tmp_path, {}, ["web/src/a.ts"])  # only web inputs: the import graph is not asked
    assert cheap.plan(c, {"full": True, "pytest": ["ALL"]}, []).pytest is None and not [line for line in calls if "related_tests" in line]


def test_selected_tests_leaves_out_what_a_hook_step_runs_and_what_is_not_in_the_checkout(tmp_path):
    for name in ("a_test.py", "b_test.py", "c_test.py"):
        (tmp_path / name).write_text("")
    c, _ = context(tmp_path, {}, [])
    c.plan = cheap.Plan(pytest=["a_test.py", "b_test.py", "gone_test.py"], extra=["c_test.py", "a_test.py"], docs=[("tests", ["uv", "run", "pytest", "b_test.py"])])
    assert cheap.selected_tests(c) == ["a_test.py", "c_test.py"]
    c.plan.guards = [["uv", "run", "pytest", "c_test.py"]]
    assert cheap.selected_tests(c) == ["a_test.py"]


def test_the_steps_stop_at_no_slot_and_name_each_failed_command(tmp_path):
    c, calls = context(tmp_path, {"biome": (75, ""), "tsc": (1, "TS1")}, [])
    s = cheap.Step("web")
    cheap.run_steps(c, s, [("web-lint", ["biome", "ci"]), ("web-types", ["tsc"])], "fix")
    assert [m for m, _ in s.failures] == ["the run lock gave no slot for web-lint (exit 75 = WAIT)"] and calls == ["biome ci"]  # nothing after a WAIT
    s = cheap.Step("web")
    cheap.run_steps(c, s, [("web-types", ["tsc", "-p", "a b"]), ("ok", ["true"])], "fix it")
    assert s.failures[0][0].startswith("web-types failed (exit 1): `tsc -p 'a b'`") and s.failures[0][1] == "fix it" and calls[-1] == "true"


def test_ruff_and_tree_and_bound_steps(tmp_path):
    (tmp_path / "x.py").write_text("")
    c, calls = context(tmp_path, {"ruff format": (1, "would reformat x.py"), "rev-list": (0, "0"), "status --porcelain": (0, "")}, ["x.py", "gone.py", "a.md"])
    s = cheap.step_ruff(c)
    assert len(s.failures) == 1 and "ruff format failed on the changed files" in s.failures[0][0] and next(line for line in calls if "ruff" in line).endswith("-- x.py")
    assert "no commit of its own" in cheap.step_tree(c).failures[0][0]
    c.sha = "a" * 40
    c2, _ = context(tmp_path, {"rev-parse HEAD": (0, "a" * 40)}, [])
    c2.sha = "a" * 40
    assert cheap.step_bound(c2).detail == "HEAD is still aaaaaaaaa, the tree is clean"
    c3, _ = context(tmp_path, {}, ["a.md"])
    assert cheap.step_ruff(c3).detail == "no Python file changed"


def test_the_checks_are_registered_in_their_order_and_reuse_the_preflights_type_and_standards_checks():
    registry = cheap.checks()
    assert registry.names == cheap.STEPS == ("tree", "ruff", "types", "web", "docs", "standards", "tests", "coverage", "bound")
    by_name = {check.name: check.run for check in registry.checks}
    assert by_name["types"] is head.step_types and by_name["standards"] is head.step_standards


@pytest.mark.parametrize(("changed", "fails"), [(["web/src/a.ts"], True), (["docs/a.md"], True), (["src/heinzel/x.py"], False)])
def test_an_unread_hook_fails_only_when_its_files_changed(tmp_path, changed, fails):
    c, _ = context(tmp_path, {}, changed)
    c.plan = cheap.Plan(changed=changed, hook="scripts/scoped_checks.py could not be asked (exit 1)")
    s = cheap.hook_step("web", "web file(s)", "fix")(c)
    assert bool(s.failures) is fails and (fails or "no web file or document changed" in s.detail)
