# Parity: `sm ci merge-gate` against the former merge-gate tool (audit)

Same code: the command calls `status_lines()` of the moved module `queue_lib/merge_gate.py` (frozen, imported), which
is what the old tool's `audit` printed. Compared in tests/test_native_ci_evidence.py on scripted Sonar, badge and
ruleset answers: the text output is the audit line for line. Differs on purpose: `--format json` adds the repository
and its marks per line. Not ported here: `flip` and `harden` (they change a ruleset: a write command of their own,
with a review, when someone needs them; the merge queue still calls `flip` from its own module).
