"""`sm head preflight` parity with the Core pre-push hook and the first CI lane: the hook's type checks, the
repository-wide standards tests and the unit tests the hook's own script selects."""
import pytest
from test_native_preflight_head import CI, PYPROJECT, Fake, go, result, step

from steuermaschine_heinzel.native.preflight_lib import head as p

SRC = "src/heinzel/modules/m/model.py"


def tree(core, files):
    for path, text in files.items():
        (core / path).parent.mkdir(parents=True, exist_ok=True)
        (core / path).write_text(text)


@pytest.fixture
def core(tmp_path):
    wt = tmp_path / "core"
    tree(wt, {"pyproject.toml": PYPROJECT, ".github/workflows/ci.yml": CI, ".sonarcloud.properties": "", "tests/unit/standards_test.py": ""})  # no Core list: the setting applies
    return wt


def fails(data, name):
    return [f["message"] for f in data["failures"] if f["step"] == name]


def hook_answer(lines, rc=0):
    """The Core script through `sh -c 'exec ... 2>/dev/null'`: its standard output only."""
    return lambda cmd: (rc, "\n".join(lines) + "\n" if lines else "") if "scripts/related_tests.py" in cmd else (0, "")


# ---- the fallback import scan (a Core without scripts/related_tests.py) -----------------------------------------------

def test_import_names_resolve_relative_imports_lists_and_continuations():
    text = "import os, heinzel.m.model\nfrom . import model\nfrom .sibling import a\nfrom heinzel.q import (r,\n  s)\nfrom heinzel.x import a, \\\n  b\n"
    got = p.import_names(text, "heinzel.m.chat")
    assert {"heinzel.m.model", "heinzel.m", "heinzel.m.sibling", "heinzel.q.r", "heinzel.q.s", "heinzel.x.b"} <= got


@pytest.mark.parametrize(("files", "expected"), [
    ({"src/heinzel/m/chat.py": "from .model import x\n", "tests/unit/chat_test.py": "from heinzel.m import chat\n"}, ["tests/unit/chat_test.py"]),  # a relative import
    ({"src/heinzel/m/__init__.py": "from .model import x\n", "tests/unit/pkg_test.py": "from heinzel.m import x\n"}, ["tests/unit/pkg_test.py"]),  # a re-export
    ({"tests/unit/many_test.py": "import os, heinzel.m.model\n"}, ["tests/unit/many_test.py"]),  # a comma list
    ({"tests/unit/cont_test.py": "from heinzel.m import a, \\\n    model\n"}, ["tests/unit/cont_test.py"]),  # a continuation line
    ({"src/heinzel/m/b.py": "from heinzel.m import model\n", "src/heinzel/m/a.py": "from heinzel.m import b\n", "tests/unit/two_test.py": "from heinzel.m import a\n"},
     ["tests/unit/two_test.py"]),  # two hops
    ({"tests/unit/model_test.py": "import json\n"}, ["tests/unit/model_test.py"]),  # selected by its name
])
def test_the_fallback_scan_finds_what_the_hook_graph_finds(core, files, expected):
    tree(core, {"src/heinzel/m/model.py": "x = 1\n", "src/heinzel/m/__init__.py": "", **files})
    assert p.importers(core, ["src/heinzel/m/model.py"]) == expected


# ---- related: the hook's own selection -------------------------------------------------------------------------------

def test_related_runs_exactly_the_selection_of_the_core_script_uncapped(core, capsys):
    many = [f"tests/unit/t{i:02d}_test.py" for i in range(p.RELATED_CAP + 5)]
    tree(core, {SRC: "x = 1\n", "scripts/related_tests.py": "", **{t: "" for t in many}})
    fake = Fake(core, changed=[SRC], **{"sh": hook_answer(many)})
    _, rc = go(core, fake, "--skip", "coverage")
    related = step(result(capsys), "related")
    assert rc == 0 and related["data"]["hook"] == many and related["data"]["tests"] == many  # the cap does not apply to the hook's selection
    asked = next(c for c in fake.calls if "related_tests.py" in " ".join(c))
    assert asked[:3] == ["sh", "-c", 'exec "$@" 2>/dev/null'] and asked[-2:] == ["scripts/related_tests.py", SRC]  # standard output only


def test_related_fails_when_the_core_script_fails_or_cannot_tell_and_the_suite_is_refused(core, capsys):
    tree(core, {SRC: "x = 1\n", "scripts/related_tests.py": ""})
    _, rc = go(core, Fake(core, changed=[SRC], **{"sh": hook_answer([], rc=1)}), "--skip", "coverage")
    assert rc == 1 and "related_tests.py failed" in fails(result(capsys), "related")[0]
    refused = {"sh": hook_answer(["ALL"]), "gate uv run": lambda cmd: (2, "sm gate: refusing\n") if "unit and not perf" in cmd else (0, "")}
    fake = Fake(core, changed=[SRC], **refused)
    _, rc = go(core, fake, "--skip", "coverage")
    assert rc == 1 and "ALL" in fails(result(capsys), "related")[0] and any("unit and not perf" in c for c in fake.calls)


def test_related_runs_the_unit_suite_when_the_hook_cannot_tell(core, capsys):
    tree(core, {SRC: "x = 1\n", "scripts/related_tests.py": ""})
    fake = Fake(core, changed=[SRC], **{"sh": hook_answer(["ALL"])})
    _, rc = go(core, fake, "--skip", "coverage")
    assert rc == 0 and step(result(capsys), "related")["data"]["selection"] == "ALL" and any("unit and not perf" in c for c in fake.calls)


def test_related_without_the_core_script_falls_back_to_the_scan_with_a_warning(core, capsys):
    tree(core, {SRC: "x = 1\n", "src/heinzel/modules/m/__init__.py": "", "tests/unit/chat_test.py": "from heinzel.modules.m import model\n",
                "tests/unit/listed_test.py": "from heinzel.modules.m import model\n"},
    )
    fake = Fake(core, changed=[SRC])
    _, rc = go(core, fake, "--skip", "coverage", sel={"python": ["tests/unit/listed_test.py"]})
    related = step(result(capsys), "related")
    assert rc == 0 and related["status"] == "WARN" and related["data"]["tests"] == ["tests/unit/chat_test.py"]


def test_the_extras_add_access_integration_e2e_rules_and_the_documentation_test(core):
    tree(core, {"src/heinzel/heads/cli/restart.py": "x = 1\n", "tests/integration/cli_restart_test.py": "run('restart')\n", "tests/integration/unrelated_test.py": "run('x')\n",
                "web/e2e/sw.mjs": "//\n", "tests/unit/e2e_browser_output_test.py": "", "tests/unit/documentation_test.py": ""})
    assert p.extra_tests(core, ["src/heinzel/heads/cli/restart.py", "web/e2e/sw.mjs"]) == ["tests/integration/cli_restart_test.py", p.DOCS_TEST, p.E2E_RULES]


def test_the_extras_are_capped_and_the_skipped_ones_named(core, capsys):
    names = [f"tests/integration/cli_t{i:02d}_test.py" for i in range(p.RELATED_CAP + 3)]
    tree(core, {"src/heinzel/heads/cli/restart.py": "x = 1\n", "scripts/related_tests.py": "", **{n: "run('restart')\n" for n in names}})
    fake = Fake(core, changed=["src/heinzel/heads/cli/restart.py"], **{"sh": hook_answer([])})
    _, rc = go(core, fake, "--skip", "coverage")
    related = step(result(capsys), "related")
    assert rc == 0 and related["status"] == "WARN" and len(related["data"]["tests"]) == p.RELATED_CAP and related["data"]["skipped"] == names[p.RELATED_CAP:]


# ---- types: the hook's commands, over the project --------------------------------------------------------------------

def test_the_type_commands_are_read_from_the_hook_script(core):
    assert [n for n, _ in p.hook_types(core)[0]] == ["mypy", "pyright"] and p.hook_types(core)[1]  # no hook script: the defaults, nothing to warn about
    tree(core, {p.HOOK_SCRIPT: "TYPES = '\"&mypy=uv run mypy --strict\" \"&ty=uv run ty check\"'\n"})
    assert p.hook_types(core) == ([("mypy", ["uv", "run", "mypy", "--strict"]), ("ty", ["uv", "run", "ty", "check"])], True)
    tree(core, {p.HOOK_SCRIPT: "TYPES = compute()\n"})
    assert p.hook_types(core)[1] is False


@pytest.mark.parametrize("failing", ["mypy", "pyright"])
def test_either_type_checker_failing_fails_the_head_over_the_whole_project(core, capsys, failing):
    tree(core, {"src/heinzel/a.py": "x = 1\n"})
    fake = Fake(core, changed=["src/heinzel/a.py"], **{"gate uv run": lambda cmd: (1, f"{failing}: error\n") if failing in cmd else (0, "")})
    _, rc = go(core, fake, "--skip", "coverage")
    data = result(capsys)
    assert rc == 1 and step(data, "types")["status"] == "FAIL" and f"{failing} failed over the project" in fails(data, "types")[0]
    assert ["gate", "uv", "run", "mypy"] in fake.calls and ["gate", "uv", "run", "pyright"] in fake.calls  # no file arguments: what the hook runs


def test_a_head_without_python_files_runs_no_type_check(core, capsys):
    fake = Fake(core, changed=["web/src/a.ts"])
    go(core, fake)
    assert step(result(capsys), "types")["status"] == "PASS" and not [c for c in fake.calls if "mypy" in c or "pyright" in c]


# ---- standards -------------------------------------------------------------------------------------------------------

def test_standards_run_whatever_the_selection_and_fail_the_head(core, capsys):
    tree(core, {"tests/unit/standards_test.py": "", "web/src/test/terminal-standards.test.ts": ""})
    fake = Fake(core, **{"gate uv run": lambda cmd: (1, "FAILED standards_test\n") if "pytest" in cmd else (0, "")})
    _, rc = go(core, fake)
    data = result(capsys)
    assert rc == 1 and step(data, "standards")["status"] == "FAIL"
    vitest = next(c for c in fake.calls if "vitest" in c)
    assert "src/test/terminal-standards.test.ts" in vitest and "scripts/web_build.py" in vitest
    assert fake.cwds[fake.calls.index(vitest)] == core  # the repository root: the script path is relative to it


def test_an_empty_standards_set_fails_in_every_form(core, capsys):
    (core / "tests/unit/standards_test.py").unlink()
    _, rc = go(core, Fake(core))
    assert rc == 1 and "no standards test to run" in fails(result(capsys), "standards")[0]
    tree(core, {p.STANDARDS_FILE: "# nothing\n"})
    _, rc = go(core, Fake(core))
    assert rc == 1 and "lists no pattern" in fails(result(capsys), "standards")[0]


def test_a_core_standards_file_with_a_stale_pattern_fails_naming_it(core, capsys):
    tree(core, {"tests/unit/standards_test.py": "", p.STANDARDS_FILE: "tests/unit/standards_test.py\ntests/unit/gone_test.py  # stale\n"})
    _, rc = go(core, Fake(core))
    assert rc == 1 and "tests/unit/gone_test.py" in fails(result(capsys), "standards")[0]


def test_default_patterns_that_match_nothing_warn_visibly(core, capsys):
    tree(core, {"tests/unit/standards_test.py": ""})
    _, rc = go(core, Fake(core))
    standards = step(result(capsys), "standards")
    assert rc == 0 and standards["status"] == "WARN" and standards["warnings"] if "warnings" in standards else rc == 0 and standards["status"] == "WARN"


def test_the_core_standards_file_replaces_the_setting(core):
    tree(core, {"tests/unit/standards_test.py": "", p.STANDARDS_FILE: "# only this\ntests/unit/standards_test.py  # one\n"})
    assert p.standards_globs(core, []) == (["tests/unit/standards_test.py"], True)


def test_a_web_change_warns_that_the_hook_runs_the_whole_gate_and_asks_the_script_nothing(core, capsys):
    tree(core, {"scripts/related_tests.py": ""})
    fake = Fake(core, changed=["web/src/a.ts"])
    _, rc = go(core, fake)
    related = step(result(capsys), "related")
    assert rc == 0 and related["status"] == "WARN" and not [c for c in fake.calls if "related_tests.py" in " ".join(c)]


def test_all_runs_the_unit_suite_and_a_notice_on_standard_error_cannot_hide_it(core, capsys):
    """The reviewer's reproduction: uv's VIRTUAL_ENV notice next to `ALL` made zero tests pass. The script is asked through
    `2>/dev/null`, so the notice is gone; the fake answers with what stdout then holds."""
    tree(core, {"pyproject.toml": PYPROJECT, "scripts/related_tests.py": ""})
    fake = Fake(core, changed=["pyproject.toml"], **{"sh": hook_answer(["ALL"])})
    _, rc = go(core, fake, "--skip", "coverage")
    related = step(result(capsys), "related")
    assert rc == 0 and related["data"]["selection"] == "ALL" and any("unit and not perf" in c for c in fake.calls)


def test_a_notice_on_standard_output_next_to_all_fails_instead_of_passing_zero_tests(core, capsys):
    tree(core, {"scripts/related_tests.py": ""})
    _, rc = go(core, Fake(core, changed=["pyproject.toml"], **{"sh": hook_answer(["warning: x", "ALL"])}), "--skip", "coverage")
    assert rc == 1 and "ALL among other lines" in fails(result(capsys), "related")[0]


def test_an_unrecognised_line_or_an_empty_answer_with_a_failing_run_is_never_zero_tests(core, capsys):
    tree(core, {SRC: "x = 1\n", "scripts/related_tests.py": ""})
    _, rc = go(core, Fake(core, changed=[SRC], **{"sh": hook_answer(["warning: something"])}), "--skip", "coverage")
    assert rc == 1 and "no test path" in fails(result(capsys), "related")[0]


def test_an_empty_clean_selection_runs_the_hooks_own_pin_test(core, capsys):
    tree(core, {SRC: "x = 1\n", "scripts/related_tests.py": "", p.PIN_TEST: ""})
    fake = Fake(core, changed=[SRC], **{"sh": hook_answer([])})
    _, rc = go(core, fake, "--skip", "coverage")
    assert rc == 0 and step(result(capsys), "related")["data"]["hook"] == [p.PIN_TEST]


@pytest.mark.parametrize("path", ["pyproject.toml", "uv.lock", "tests/unit/conftest.py", ".github/workflows/ci.yml"])
def test_types_run_for_config_only_changes(core, capsys, path):
    fake = Fake(core, changed=[path])
    go(core, fake, "--skip", "coverage")
    assert ["gate", "uv", "run", "mypy"] in fake.calls and ["gate", "uv", "run", "pyright"] in fake.calls


@pytest.mark.parametrize("path", ["README.md", "docs/a.md", "web/src/a.ts", "plugins/x/a.py"])
def test_types_are_left_out_for_what_the_hook_leaves_out(core, capsys, path):
    fake = Fake(core, changed=[path])
    go(core, fake, "--skip", "coverage")
    assert not [c for c in fake.calls if "mypy" in c or "pyright" in c]


def test_the_web_warning_names_the_validation_lanes(core, capsys):
    tree(core, {"scripts/related_tests.py": ""})
    go(core, Fake(core, changed=["web/src/a.ts"]))
    assert step(result(capsys), "related")["data"]["warnings"] == ["the push hook runs the whole web gate; hosted PR CI runs the web lanes"]


def test_unreadable_types_line_warns_that_the_defaults_ran(core, capsys):
    tree(core, {"src/heinzel/a.py": "x = 1\n", p.HOOK_SCRIPT: "TYPES = compute()\n"})
    go(core, Fake(core, changed=["src/heinzel/a.py"]), "--skip", "coverage")
    assert "could not read TYPES" in step(result(capsys), "types")["data"]["warnings"][0]
