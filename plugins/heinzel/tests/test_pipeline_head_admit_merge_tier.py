"""`sm head admit` at a merge-tier main (Core ADR 0149): no manifest unless a protected file changed or a widening
is asked for; an older Core keeps every step. Git reads are scripted (FakeGh as the process runner)."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_pipeline_cov_head_admit import HEAD, MAIN, context, runtime

from steuermaschine.github.testing import FakeGh
from steuermaschine_heinzel.pipelines import head_admit


@pytest.fixture(autouse=True)
def fresh_decisions():
    head_admit._DECIDED.clear()
    yield
    head_admit._DECIDED.clear()

PLAN = ('WHOLE_SUITES = {"web": VITEST_ALL}\n'
        'PROTECTED_FILES = (".github/workflows/ci.yml", "Makefile")\n'
        'PROTECTED_PATTERNS = ("tests/_*.py", "tests/e2e/_*.py")\n')


def gh(plan: str | int = PLAN, changed: str | int = "src/heinzel/a.py\ntests/unit/a_test.py\n") -> FakeGh:
    return FakeGh().on(f"show {MAIN}:scripts/focused_plan.py", plan).on(f"diff --name-only --no-renames {MAIN}...{HEAD}", changed)


def ctx(tmp_path: Path, fake: FakeGh, **inputs: str):
    (tmp_path / "wt").mkdir(exist_ok=True)
    return context(runtime(tmp_path, gh=fake), tmp_path / "wt", inputs=inputs)


def test_a_merge_tier_main_skips_packet_reviews_prepare_and_apply(tmp_path):
    c = ctx(tmp_path, gh())
    for probe in (head_admit.packet_probe, head_admit.prepare_probe, head_admit.apply_probe):
        assert (probe(c) or "").startswith("not needed: merge tier")
    assert head_admit.packet_review(c) == ("", "", "")
    assert head_admit.apply_review(c) == ("", "", "")
    shows = [line for line in c.providers.commands.lines() if " show " in f" {line} "]
    assert len(shows) == 1  # decided once per head and main


def test_a_protected_change_or_a_widening_keeps_the_manifest_steps(tmp_path):
    for changed in ("Makefile\n", "tests/e2e/_driver.py\n", ".github/workflows/ci.yml\n"):
        head_admit._DECIDED.clear()
        assert head_admit.manifest_skip(ctx(tmp_path, gh(changed=changed))) is None
    assert head_admit.manifest_skip(ctx(tmp_path, gh(), widen="yes")) is None
    head_admit._DECIDED.clear()
    assert head_admit.manifest_skip(ctx(tmp_path, gh(changed="tests/unit/_deep_test.py\n"))) is not None


def test_an_older_core_or_an_unreadable_main_keeps_every_step(tmp_path):
    for fake in (gh(plan='VITEST_ALL = "web/ALL"\n'), gh(plan=128), gh(changed=128)):
        head_admit._DECIDED.clear()
        assert head_admit.manifest_skip(ctx(tmp_path, fake)) is None
    head_admit._DECIDED.clear()
    assert head_admit.prepare_probe(ctx(tmp_path, gh(plan=128))) is None


def test_protected_changes_reads_both_literals_from_the_base():
    assert head_admit.protected_changes(PLAN, ["Makefile", "tests/_x.py", "tests/unit/_y.py", "src/a.py"]) == ["Makefile", "tests/_x.py"]
    assert head_admit.protected_changes(PLAN.replace("PROTECTED_PATTERNS", "PROTECTED_GLOBS"), ["src/a.py"]) is None


@pytest.mark.parametrize("files", [
    "tuple(x)", "BASE + (\"Makefile\",)", "frozenset({\"Makefile\"})", "(1, 2)", "\"Makefile\"", "(\"Makefile\",)\nPROTECTED_FILES += (\"x\",)",
])
def test_a_protected_list_that_is_no_plain_literal_fails_closed(tmp_path, files):
    plan = PLAN.replace('(".github/workflows/ci.yml", "Makefile")', files)
    assert head_admit.protected_changes(plan, ["Makefile"]) is None
    assert head_admit.manifest_skip(ctx(tmp_path, gh(plan=plan))) is None


def test_a_missing_protected_list_or_unparsable_plan_fails_closed(tmp_path):
    for plan in ('WHOLE_SUITES = {"web": VITEST_ALL}\n', PLAN + "def broken(:\n"):
        head_admit._DECIDED.clear()
        assert head_admit.protected_changes(plan, ["src/a.py"]) is None
        assert head_admit.manifest_skip(ctx(tmp_path, gh(plan=plan))) is None


def test_an_annotated_literal_is_read():
    plan = PLAN.replace("PROTECTED_FILES =", "PROTECTED_FILES: tuple[str, ...] =")
    assert head_admit.protected_changes(plan, ["Makefile", "src/a.py"]) == ["Makefile"]


def test_whole_suites_only_in_a_comment_or_string_is_an_older_core(tmp_path):
    plan = PLAN.replace("WHOLE_SUITES = ", "# WHOLE_SUITES = ") + 'NOTE = "WHOLE_SUITES"\n'
    assert head_admit.manifest_skip(ctx(tmp_path, gh(plan=plan))) is None


def test_merge_tier_mode_off_keeps_every_step_and_on_still_checks_protected_files(tmp_path):
    off = ctx(tmp_path, gh())
    off.settings["merge_tier_mode"] = "off"
    assert head_admit.manifest_skip(off) is None
    head_admit._DECIDED.clear()
    on = ctx(tmp_path, gh(plan='VITEST_ALL = "web/ALL"\n' + PLAN.split("\n", 1)[1]))
    on.settings["merge_tier_mode"] = "on"
    assert (head_admit.manifest_skip(on) or "").startswith("not needed: merge tier")
    head_admit._DECIDED.clear()
    unreadable = ctx(tmp_path, gh(plan=128))
    unreadable.settings["merge_tier_mode"] = "on"
    assert head_admit.manifest_skip(unreadable) is None  # forced on, but no protected list to check: manifest
