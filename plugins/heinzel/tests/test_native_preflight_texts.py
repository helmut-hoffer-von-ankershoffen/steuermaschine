"""`texts_check`: the title and body of a pull request, checked before they are published. The head's term checker
is a fake that answers in the checker's own words (`where:LINE: confidential term #N` on stderr, exit 1); the term
of these tests is an invented word. No process, no network."""

from __future__ import annotations

from pathlib import Path

import pytest

from steuermaschine.engine import Refuse
from steuermaschine.native import NativeRuntime
from steuermaschine.protocols import Result
from steuermaschine.testing import fake_providers
from steuermaschine_heinzel.native.preflight import texts_check
from steuermaschine_heinzel.native.preflight_lib import texts

TERM = "zorblat"  # invented


class Checker:
    """The head's checker: reads the file it is given, as the real one does, and names line and term number only."""

    def __init__(self, terms: bool = True, crash: bool = False):
        self.terms, self.crash, self.calls = terms, crash, []

    def run(self, argv, cwd=None, timeout=None, env=None):
        self.calls.append((list(argv), cwd, env))
        if self.crash:
            return Result(2, "", f"Traceback ... {TERM}")
        if not self.terms:
            return Result(1, "", "No confidential terms configured; set CONFIDENTIAL_TERMS.") if "--require" in argv else Result(
                0, "Confidentiality check skipped: no terms configured (see scripts/check_confidentiality.py).")
        text = Path(argv[-1]).read_text()
        found = [f"commit message:{n}: confidential term #1" for n, line in enumerate(text.splitlines(), 1) if TERM in line]
        return Result(1, "", "\n".join([*found, f"{len(found)} confidential term(s) found; remove them before committing."])) if found else Result(0)


@pytest.fixture
def tree(tmp_path) -> Path:
    wt = tmp_path / "wt"
    (wt / "scripts").mkdir(parents=True)
    (wt / "scripts" / "check_confidentiality.py").write_text("")
    (tmp_path / "terms").write_text(f"{TERM}\n")
    return wt


def runtime_of(commands, terms: Path | None) -> NativeRuntime:
    return NativeRuntime(fake_providers(commands=commands), {"preflight_terms_file": str(terms)} if terms else {}, None)


def test_a_clean_title_and_body_have_no_problem_and_the_texts_never_reach_an_argument(tree, tmp_path):
    checker = Checker()
    assert texts_check(runtime_of(checker, tmp_path / "terms"), tree, title="fix(web): short", body="Why.\n\nWhat.") == []
    assert len(checker.calls) == 2 and all(argv[:4] == ["uv", "run", "--quiet", "--script"] and "--require" in argv and argv[-2] == "--commit-msg" for argv, _, _ in checker.calls)
    assert all(env["CONFIDENTIAL_TERMS_FILE"] == str(tmp_path / "terms") and cwd == str(tree) for _, cwd, env in checker.calls)
    assert not any("short" in word or "Why" in word for argv, _, _ in checker.calls for word in argv)


def test_the_title_rule_is_the_commit_subject_rule_and_does_not_repeat_the_title(tree, tmp_path):
    rt, long = runtime_of(Checker(), tmp_path / "terms"), "feat(cli): " + "x" * 70
    assert texts_check(rt, tree, title=long, body="") == ["PR title: 81 chars > 72 -> shorten the summary to <= 65 chars (a squash adds ' (#N)')"]
    problems = texts_check(rt, tree, title="Update stuff", body="")
    assert len(problems) == 1 and problems[0].startswith("PR title: not conventional -> 'type(scope): summary'") and "Update stuff" not in problems[0]


def test_a_term_is_named_by_its_line_only_never_by_itself(tree, tmp_path):
    problems = texts_check(runtime_of(Checker(), tmp_path / "terms"), tree, title=f"fix(web): about {TERM}", body=f"fine\nmore\nabout {TERM} here\n{TERM} again")
    assert problems == ["PR title: contains a confidential term (line 1) -> reword that line", "PR body: contains a confidential term (line 3) -> reword that line",
                        "PR body: contains a confidential term (line 4) -> reword that line"]
    assert TERM not in "\n".join(problems)


def test_a_configured_terms_file_that_cannot_be_read_refuses(tree, tmp_path):
    checker = Checker()
    with pytest.raises(Refuse) as missing:
        texts_check(runtime_of(checker, tmp_path / "gone"), tree, title="fix(web): short", body="x")
    assert "cannot be read" in missing.value.message and "nothing may be published" in missing.value.message and not checker.calls
    locked = tmp_path / "terms"
    locked.chmod(0)
    try:
        with pytest.raises(Refuse):
            texts_check(runtime_of(checker, locked), tree, title="fix(web): short", body="x")
    finally:
        locked.chmod(0o600)
    assert not checker.calls


def test_no_terms_no_checker_and_a_checker_without_a_verdict_refuse_and_say_nothing_of_the_output(tree, tmp_path):
    with pytest.raises(Refuse) as unset:  # no terms file configured and the checker's own sources are empty
        texts_check(runtime_of(Checker(terms=False), None), tree, title="fix(web): short", body="")
    assert "set preflight_terms_file" in unset.value.message
    with pytest.raises(Refuse) as empty:  # a configured file without a term
        texts_check(runtime_of(Checker(terms=False), tmp_path / "terms"), tree, title="fix(web): short", body="")
    assert "has no term" in empty.value.message
    with pytest.raises(Refuse) as crashed:
        texts_check(runtime_of(Checker(crash=True), tmp_path / "terms"), tree, title="fix(web): short", body="")
    assert "exit 2 and no verdict" in crashed.value.message and TERM not in crashed.value.message
    with pytest.raises(Refuse) as bare:
        texts_check(runtime_of(Checker(), tmp_path / "terms"), tmp_path, title="fix(web): short", body="")
    assert "has no scripts/check_confidentiality.py" in bare.value.message


def test_without_a_configured_file_the_checker_runs_with_its_own_sources(tree):
    checker = Checker()
    assert texts.check(checker, tree, "fix(web): short", f"about {TERM}") == ["PR body: contains a confidential term (line 1) -> reword that line"]
    assert all(env is None and "--require" not in argv for argv, _, env in checker.calls)
