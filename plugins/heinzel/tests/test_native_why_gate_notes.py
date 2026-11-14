"""Core ADR 0149 d' and h: the merge gate's verdict may end with a reviewed workflow change, advisory lanes (consumers:
N failing: a, b) and reruns. `sm why` shows them as information; the check's conclusion alone decides red."""

from steuermaschine_heinzel.native import merge_tier


def test_the_verdict_clauses_are_information_and_the_rerun_tail_is_counted():
    v = merge_tier.verdict("PR scoped coverage passed; reviewed workflow change: Makefile; "
                           "consumers advisory: 2 failing: heinzel-a, heinzel-b; 1 job(s) passed on rerun")
    assert v.passed_on_rerun == 1 and v.advisory_failing == {"consumers": ["heinzel-a", "heinzel-b"]}
    assert v.info == ["1 job(s) passed on rerun", "reviewed workflow change: Makefile", "consumers advisory: 2 failing: heinzel-a, heinzel-b"]
    assert merge_tier.verdict("x; consumers advisory: passed").info == ["consumers advisory: passed"]
    assert merge_tier.verdict("x; consumers advisory: did not finish").advisory_failing == {}


def test_why_shows_the_gate_notes_as_information():
    from steuermaschine_heinzel.native import why

    runs = [{"name": "coverage-floor", "completed_at": "2", "summary": "PR scoped coverage passed; consumers advisory: 1 failing: heinzel-a"},
            {"name": "coverage-floor", "completed_at": "1", "summary": "old"}, {"name": "CI OK", "completed_at": "3"}]
    notes = why.gate_notes(runs)
    assert notes.advisory_failing == {"consumers": ["heinzel-a"]} and why.gate_notes([]).info == []
    shown = why.PullRequestWhy(number=1, gate_notes=notes.info).render()
    assert "  gate note (not red): consumers advisory: 1 failing: heinzel-a" in shown
