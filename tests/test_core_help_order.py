"""The help contract's order and example, as `sm self lint` (strict) checks them in every shipped project.

The lint (`helpdoc.disorder`) adds two letters to a help text whose five lines are all there: O when purpose, `Use
when`, `Changes:`, `Example:`, `Next:` are not in that order (a long line may wrap onto the next), X when the example
does not type the program (it may feed it from a pipe). Here: every shipped stack is clean, and both letters fire.
"""

import pytest

from steuermaschine import PROG, helpdoc
from steuermaschine.app import command_tree
from steuermaschine.testing import invoke
from tests.test_core_json_contract import STACKS, runtime


@pytest.mark.parametrize("name", sorted(STACKS))
def test_every_help_text_has_its_contract_lines_in_order_and_the_lint_is_clean(name):
    rt = runtime(name)
    rows = helpdoc.tree(command_tree(rt), PROG)
    bad = [finding for row in rows if (finding := helpdoc.finding(row))]
    assert len(rows) > 10 and not bad, bad
    out = invoke(rt, ["self", "lint"])
    assert out.exit_code == 0, out.stdout


def test_the_lint_finds_a_swapped_help_text_and_an_example_that_does_not_type_the_program():
    good = helpdoc.H("Do one thing.", "Use when it is due.", "nothing (read-only).", "sm do", "sm next")
    assert helpdoc.disorder(good, PROG) == ""
    swapped = good.replace("Example: sm do\nNext: sm next", "Next: sm next\nExample: sm do")
    assert helpdoc.disorder(swapped, PROG) == "O"
    assert helpdoc.disorder(good.replace("Example: sm do", "Example: do"), PROG) == "X"
    assert helpdoc.disorder(good.replace("Example: sm do", "Example: printf x | sm do"), PROG) == ""
    assert helpdoc.disorder("Do it.", PROG) == ""  # missing lines are missing() letters, not an order finding
    assert helpdoc.finding({"path": "sm do", "missing": helpdoc.missing(swapped) + helpdoc.disorder(swapped, PROG), "undocumented": []}) == "sm do: missing O"
