"""The logic of `sm check` in parts: the probe that asks Core's own hook (run here against a small hook, in this
interpreter), the hook's step lines, the plan from the selector's and the hook's answer, and which tests run where."""

import contextlib
import io
import json
import sys

import pytest
from test_native_check import vitest_report

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
    import os
    from pathlib import Path

    write_text = Path.write_text

    def same_timestamp(path, text, *args, **kwargs):
        result = write_text(path, text, *args, **kwargs)
        if path.name == "scoped_checks.py":
            os.utime(path, (1_700_000_000, 1_700_000_000))
        return result

    monkeypatch.setattr(Path, "write_text", same_timestamp)
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
    full = {"full": True, "pytest": ["ALL"], "vitest": ["ALL"]}
    c, _ = context(tmp_path, {}, ["pyproject.toml"])  # no import graph in the checkout: everything
    p = cheap.plan(c, full, [])
    assert p.pytest is None and p.error == "" and "Core has no scripts/related_tests.py to narrow it: the whole unit suite runs" in p.narrowed
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/related_tests.py").write_text("")
    c, _ = context(tmp_path, {}, ["src/heinzel/x.py"])
    p = cheap.plan(c, {"full": False, "pytest": ["tests/unit/x_test.py"], "vitest": [], "e2e_lanes": ["cli"], "selector": "core"}, ["tests/unit/y_test.py"])
    assert (p.pytest, p.full, p.narrowed, p.lanes, p.extra, p.selector) == (["tests/unit/x_test.py"], False, "", ["cli"], ["tests/unit/y_test.py"], "core")
    assert p.hook == "Core has no scripts/scoped_checks.py" and p.web == [] and p.guards == []
    c, _ = context(tmp_path, {"related_tests.py": (0, "tests/unit/a_test.py\n")}, ["pyproject.toml", "web/src/a.ts"])
    p = cheap.plan(c, full, [])
    assert p.pytest == ["tests/unit/a_test.py"] and p.narrowed == "the selector selects the full set; scripts/related_tests.py narrows the unit tests to 1 file(s)"
    c, _ = context(tmp_path, {"related_tests.py": (0, "ALL\n")}, ["pyproject.toml"])
    p = cheap.plan(c, full, [])
    assert p.pytest is None and p.error == "" and p.narrowed == "the selector and scripts/related_tests.py both select everything: the whole unit suite runs"
    for answer, why in (((3, ""), "scripts/related_tests.py failed (exit 3)"), ((0, "odd line\n"), "printed something that is no test path"),
                        ((0, "ALL\ntests/unit/a_test.py\n"), "printed ALL among other lines")):
        c, _ = context(tmp_path, {"related_tests.py": answer}, ["pyproject.toml"])
        p = cheap.plan(c, full, [])
        assert why in p.error and p.pytest == [] and p.narrowed == "", answer  # a failure is an error, never "everything" and never "nothing"
    c, calls = context(tmp_path, {}, ["web/src/a.ts"])  # only web inputs: the import graph is not asked, the web check runs every Vitest file
    p = cheap.plan(c, full, [])
    assert p.pytest == [] and "only web inputs changed" in p.narrowed and not [line for line in calls if "related_tests" in line]


@pytest.mark.parametrize("picked", [["ALL"], ["web/src/b.test.ts"]])
def test_named_vitest_files_leave_the_whole_suite_to_ci_only_when_selected(tmp_path, monkeypatch, picked):
    wanted = "web/src/a.test.ts"
    expected = [wanted] if picked == ["ALL"] else [*picked, wanted]
    c, calls = context(tmp_path, {}, ["web/src/a.ts"])

    def scoped_hook(ctx, changed, files):
        assert files == expected
        return {
            "web_files": changed,
            "web": ["web-lint=web exec lint", "web-tests=web exec vitest run " + " ".join(
                "./" + file.removeprefix("web/") for file in files
            )],
        }, ""

    monkeypatch.setattr(cheap, "hook_facts", scoped_hook)
    c.plan = cheap.plan(c, {"vitest": picked, "pytest": []}, [], web_tests=[wanted])
    recorded_gate = c.gate

    def gate_with_report(argv, **kwargs):
        result = recorded_gate(argv, **kwargs)
        if "--reporter=json" in argv:
            vitest_report(c.wt, expected)(argv)
        return result

    c.gate = gate_with_report
    step = cheap.hook_step("web", "web files", "fix the web check")(c)
    assert not step.failures
    assert tuple(c.ci) == (("the whole Vitest suite",) if picked == ["ALL"] else ())
    assert wanted in step.detail
    assert any(line.startswith("web exec lint") for line in calls)
    assert any(line.startswith("web exec vitest run ./src/") for line in calls)


def test_named_vitest_files_are_refused_if_the_hook_drops_the_arguments(tmp_path, monkeypatch):
    c, _ = context(tmp_path, {}, ["web/src/a.ts"])
    monkeypatch.setattr(cheap, "hook_facts", lambda *_: ({"web": ["web-tests=web exec vitest run"]}, ""))
    p = cheap.plan(c, {"vitest": ["ALL"]}, [], web_tests=["web/src/a.test.ts"])
    assert p.hook == "the hook did not select a Vitest step for the named files"
    assert not p.web_deferred


@pytest.mark.parametrize("arguments,note", [("./src/a.test.ts ./src/extra.test.ts", ""), ("", "invalid test path")])
def test_exact_vitest_refuses_a_hook_that_widens_or_rejects_the_selection(tmp_path, monkeypatch, arguments, note):
    c, _ = context(tmp_path, {}, ["web/src/a.ts"])
    monkeypatch.setattr(cheap, "hook_facts", lambda *_: ({"web": [f"web-tests=web exec vitest run {arguments}"], "note": note}, ""))
    p = cheap.plan(c, {"vitest": ["web/src/a.test.ts", "web/src/b.test.ts"]}, [], web_tests=["web/src/a.test.ts"], vitest_exact=True)
    assert p.hook == (note or "the hook did not select a Vitest step for the named files")
    assert not p.web_deferred


def test_named_vitest_files_cannot_hide_a_dropped_selector_file(tmp_path, monkeypatch):
    c, _ = context(tmp_path, {}, ["web/src/a.ts"])
    monkeypatch.setattr(cheap, "hook_facts", lambda *_: (
        {"web": ["web-tests=web exec vitest run ./src/named.test.ts"]}, ""
    ))
    p = cheap.plan(c, {"vitest": ["web/src/a.test.ts"]}, [], web_tests=["web/src/named.test.ts"])
    assert p.hook == "the hook did not select a Vitest step for the named files"


def test_the_tests_check_fails_on_a_selection_error_and_runs_the_unit_suite_for_everything(tmp_path):
    c, calls = context(tmp_path, {}, [])
    c.plan = cheap.Plan(pytest=[], error="scripts/related_tests.py failed (exit 3)")
    s = cheap.step_tests(c)
    assert [m for m, _ in s.failures] == ["scripts/related_tests.py failed (exit 3)"] and not [line for line in calls if "pytest" in line]
    c, calls = context(tmp_path, {}, [])
    c.plan = cheap.Plan(pytest=None, narrowed="both select everything: the whole unit suite runs")
    s = cheap.step_tests(c)  # a fast check never runs the whole suite: PR CI does
    assert not s.failures and s.data["unit_suite"] is False and not [line for line in calls if "unit and not perf" in line]
    assert "left to PR CI" in s.detail and any("runs on PR CI, not here" in w for w in s.data["warnings"])
    c, calls = context(tmp_path, {}, [])
    c.full, c.plan = True, cheap.Plan(pytest=None, narrowed="both select everything: the whole unit suite runs")
    s = cheap.step_tests(c)
    assert not s.failures and s.data["unit_suite"] is True and calls[-1] == " ".join(head.UNIT_SUITE) and "the whole unit suite, " in s.detail
    for rc, outcome in ((75, "got no slot (exit 75 = WAIT)"), (2, "was refused or failed (exit 2)"), (1, "was refused or failed (exit 1)")):
        c, _ = context(tmp_path, {"unit and not perf": (rc, "out")}, [])
        c.full, c.plan = True, cheap.Plan(pytest=None, narrowed="everything")
        assert cheap.step_tests(c).failures[0][0].startswith(f"everything; it {outcome}"), rc


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


def test_the_checks_are_registered_in_their_order_and_reuse_the_preflights_standards_check_and_run_no_coverage():
    registry = cheap.checks()
    assert registry.names == cheap.STEPS == ("tree", "ruff", "types", "hook", "parity", "web", "docs", "standards", "tests", "bound")
    by_name = {check.name: check.run for check in registry.checks}
    assert by_name["types"].__wrapped__ is cheap.step_types and by_name["standards"].__wrapped__ is head.step_standards and "coverage" not in registry.names


@pytest.mark.parametrize("changed", [["web/src/a.ts"], ["docs/a.md"], ["src/heinzel/x.py"], []])
def test_a_hook_that_did_not_answer_fails_its_check_whatever_changed_and_its_steps_are_not_run(tmp_path, changed):
    c, calls = context(tmp_path, {}, changed)
    c.plan = cheap.Plan(changed=changed, hook="scripts/scoped_checks.py could not be asked (exit 1)")
    s = cheap.step_hook(c)
    assert len(s.failures) == 1 and s.failures[0][0].startswith("scripts/scoped_checks.py could not be asked (exit 1): the web steps")
    for name in ("web", "docs"):
        skipped = cheap.hook_step(name, "file(s)", "fix")(c)
        assert skipped.status == "SKIP" and not skipped.failures and "the hook did not answer" in skipped.detail
    assert calls == []


def test_the_hook_check_passes_with_what_the_hook_named_and_each_kind_runs_its_own_steps(tmp_path):
    c, calls = context(tmp_path, {}, [])
    c.plan = cheap.Plan(web=[("web-lint", ["biome", "ci"])], web_files=["web/src/a.ts"], docs=[("typos", ["typos"])], docs_files=["a.md", "b.md"], guards=[["pytest", "g_test.py"]])
    assert cheap.step_hook(c).detail == "scripts/scoped_checks.py answered: 1 web file(s), 2 document(s), 1 early guard command(s)"
    assert cheap.hook_step("web", "web file(s)", "fix")(c).detail == "1 changed web file(s): web-lint green"
    assert cheap.hook_step("docs", "document(s)", "fix")(c).detail == "2 changed document(s): typos green" and calls == ["biome ci", "typos"]
    c.plan = cheap.Plan()
    assert cheap.hook_step("docs", "document(s)", "fix")(c).detail == "no document(s) changed"


def test_parity_runs_the_lock_template_and_contract_checks_for_the_paths_each_reads(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/sync_template.py").write_text("")
    word = {"lock": "lock --check", "template": "sync_template", "contracts": "lint-imports"}
    for changed, ran in ((["README.md"], []), (["uv.lock"], ["lock"]), ([".github/workflows/ci.yml"], ["template"]), (["src/a.py"], ["contracts"]),
                         (["pyproject.toml"], ["lock", "template", "contracts"]), ([".gitignore"], ["template"]),
                         (["src/heinzel/bootstrap_pyyaml.py"], ["template", "contracts"]), (["plugins/x/pyproject.toml"], ["lock"])):
        c, calls = context(tmp_path, {}, changed)
        c.plan = cheap.Plan(changed=changed)
        assert cheap.step_parity(c).status != "FAIL"
        assert [next(n for n in word if word[n] in line) for line in calls] == ran, changed
    c, _ = context(tmp_path, {"lock --check": (1, "stale")}, ["uv.lock"])
    c.plan = cheap.Plan(changed=["uv.lock"])
    assert cheap.step_parity(c).failures[0][0].startswith("lock failed (exit 1)")
    (tmp_path / "scripts/sync_template.py").unlink()
    c, calls = context(tmp_path, {}, [".github/workflows/ci.yml"])
    c.plan = cheap.Plan(changed=[".github/workflows/ci.yml"])
    assert cheap.step_parity(c).detail.startswith("no path changed") and calls == []


PYPROJECT = '[tool.mypy]\nstrict = true\nfiles = ["src"]\n\n[tool.pyright]\ninclude = ["src/", "scripts/typed.py"]\n'


def test_the_type_checks_get_only_the_changed_files_inside_their_own_configured_scope(tmp_path):
    """Core strict-checks src only: a changed test or script passed by name was checked anyway (mypy exit 2)."""
    for name in ("src/heinzel/x.py", "scripts/contexts.py", "scripts/typed.py", "tests/unit/contexts_test.py"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("")
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    changed = ["src/heinzel/x.py", "scripts/contexts.py", "scripts/typed.py", "tests/unit/contexts_test.py"]
    c, calls = context(tmp_path, {}, changed)
    s = cheap.step_types(c)
    assert not s.failures
    assert [line for line in calls if line.startswith("uv run mypy")] == ["uv run mypy src/heinzel/x.py"]
    assert [line for line in calls if line.startswith("uv run pyright")] == ["uv run pyright src/heinzel/x.py scripts/typed.py"]
    assert s.detail.startswith("mypy, pyright clean over 2 changed Python file(s) in their scope")


def test_the_type_checks_skip_when_no_changed_file_is_in_scope_and_pass_everything_without_a_scope(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/a_test.py").write_text("")
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    c, calls = context(tmp_path, {}, ["tests/a_test.py"])
    s = cheap.step_types(c)
    assert not s.failures and "nothing to type-check here" in s.detail and not [x for x in calls if "mypy" in x or "pyright" in x]
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n")  # no scope named: every changed file
    c, calls = context(tmp_path, {}, ["tests/a_test.py"])
    cheap.step_types(c)
    assert [x for x in calls if x.startswith("uv run mypy")] == ["uv run mypy tests/a_test.py"]
    (tmp_path / "pyproject.toml").write_text("not toml [")
    assert cheap.typed_scope(tmp_path, "mypy") is None and cheap.typed_scope(tmp_path, "other") is None
    c, calls = context(tmp_path, {"uv run mypy": (1, "a.py:1: error")}, ["tests/a_test.py"])
    assert cheap.step_types(c).failures[0][0].startswith("mypy failed on the changed files (exit 1)")
