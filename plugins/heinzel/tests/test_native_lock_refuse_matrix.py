"""The full-gate refusal of `sm gate run`, case by case: what is refused and what passes, through every wrapper
(env assignments, uv run, shells, nice/time) the lock unwraps."""

import pytest

from steuermaschine_heinzel.native.lock_lib.refuse import NEVER_MAKE_PR, refusal

REFUSED = [
    (["make", "pr"], NEVER_MAKE_PR),
    (["env", "NO_PREFLIGHT=1", "make", "pr"], NEVER_MAKE_PR),
    (["make", "-j4", "coverage"], "`make coverage` runs"),
    (["make", "test-unit"], "`make test-unit` runs"),
    (["pytest"], "without a specific test path"),
    (["pytest", "-n", "4", "tests"], "without a specific test path"),
    (["pytest", "-k", "foo", "."], "without a specific test path"),
    (["python3", "-m", "pytest", "-x"], "without a specific test path"),
    (["uv", "run", "--with", "x", "pytest"], "without a specific test path"),
    (["uv", "run", "--", "pytest", "-q"], "without a specific test path"),
    (["nice", "pytest"], "without a specific test path"),
    (["time", "make", "check"], "`make check` runs"),
    (["bash", "-c", "cd web && pnpm test"], "`pnpm test` runs the whole JS suite"),
    (["sh", "-ec", "echo hi; make preflight"], "`make preflight` runs"),
    (["zsh", "-c", "true || pytest"], "without a specific test path"),
    (["pnpm", "exec", "vitest", "run"], "`vitest` without a file filter"),
    (["npx", "vitest", "run", "src/"], "`vitest` without a file filter"),
    (["npm", "run", "test"], "`npm test` runs the whole JS suite"),
    (["yarn", "t"], "`yarn test` runs the whole JS suite"),
    (["A=1", "B=2", "pytest"], "without a specific test path"),
]

ALLOWED = [
    ["pytest", "tests/unit/x_test.py"],
    ["pytest", "-n", "2", "tests/unit/x_test.py::test_a"],
    ["pytest", "--maxfail=1", "x_test.py"],
    ["python", "-m", "pytest", "tests/a/b_test.py"],
    ["uv", "run", "--script", "tool.py"],
    ["uv", "run", "--project", "p", "pytest", "tests/a_test.py"],
    ["uv", "run"],
    ["make", "check-fast"],
    ["make"],
    ["git", "push", "origin", "HEAD"],
    ["pnpm", "exec", "vitest", "run", "src/a.test.ts"],
    ["pnpm", "test", "src/a.test.ts"],
    ["pnpm", "install"],
    ["bash", "-c", "echo 'unbalanced"],
    ["bash", "-c"],
    ["bash", "script.sh"],
    ["env"],
    ["nice"],
]


@pytest.mark.parametrize(("argv", "reason"), REFUSED)
def test_full_gates_are_refused_with_their_reason(argv, reason):
    assert reason in (refusal(argv, {}) or "")


@pytest.mark.parametrize("argv", ALLOWED)
def test_focused_and_other_commands_pass(argv):
    assert refusal(argv, {"NO_PREFLIGHT": "1"}) is None


@pytest.mark.xfail(strict=True, reason="gap in the reviewed head-validate unit: `nice -n 10` leaves 10 as the command word, so the "
                   "full suite behind it is not refused; fix it with the unit's next review")
def test_an_option_value_of_nice_hides_the_command():
    assert refusal(["nice", "-n", "10", "pytest"], {})
