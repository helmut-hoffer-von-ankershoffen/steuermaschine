"""The state machine against fakes: resumable, idempotent, dry by default, stops for an independent review."""

import pytest

from steuermaschine import OK, REFUSED, USAGE, WAITING
from steuermaschine.engine import Engine, Gate, Options, Pipeline, Refuse, ReviewStop, Step, Subject, Wait, record_head_verdict
from steuermaschine.engine import head_verdict as head_verdict_of
from steuermaschine.testing import fake_providers
from steuermaschine.testing.fakes import external_calls

DIGEST = "d" * 64


def make(p, review=True, gates=(), per_head=False, wait_next=""):
    """build -> review (STOP) -> publish* -> settle"""
    calls = []

    def identify(providers, settings, arg):
        return Subject(key=providers.vcs.head(arg), label=f"x {providers.vcs.head(arg)[:6]}",
                       fingerprint={"head": providers.vcs.head(arg), "base": providers.vcs.base(arg)}, facts={"worktree": arg})

    def build(ctx):
        calls.append("build")
        ctx.data["built"] = ctx.inputs.get("flavour", "plain")
        return f"built {ctx.data['built']}"

    def publish(ctx):
        calls.append("publish")
        ctx.providers.forge.tag("t", ctx.subject.key)
        return "published"

    def settle(ctx):
        if not ctx.providers.forge.variables.get("settled"):
            raise Wait("not settled yet", "the world", next_cmd=wait_next)
        return "settled"

    steps = [Step("build", build, binds=("head", "base"))]
    if review:
        steps.append(ReviewStop("review", lambda ctx: (DIGEST, "Review the build."), binds=("head", "base"),
                                 head=(lambda ctx: ctx.subject.key) if per_head else None))
    steps += [Step("publish", publish, "creates a tag", external=True, plan=lambda c: "tag t", gates=gates,
                   probe=lambda c: "tag exists" if "t" in c.providers.forge.tags else None),
              Step("settle", settle)]
    return Pipeline("head", "demo", "Demo.", steps, identify, inputs={"flavour": "which"}), calls


def engine(p, **kw):
    return Engine("proj", p, out=lambda line: None, **kw)


def approve(p, reviewer="reviewer-1", verdict="approve"):
    p.store.put_receipt("verdicts", DIGEST, {"digest": DIGEST, "verdict": verdict, "reviewer": reviewer, "notes": ""})


def head_verdict(p, head, reviewer="reviewer-1", verdict="approve", at="1"):
    record_head_verdict(p.store, DIGEST, head, {"digest": DIGEST, "verdict": verdict, "reviewer": reviewer, "at": at})


def test_a_reject_stays_across_heads_until_the_protected_bytes_change():
    p = fake_providers()
    pl, _ = make(p, per_head=True)
    e = engine(p)
    old = p.vcs.h
    e.advance(pl, "/wt", Options())
    head_verdict(p, old, verdict="reject")
    assert "reject" in e.advance(pl, "/wt", Options(approved=DIGEST)).phase
    p.vcs.h = "f" * 40  # the fix is a new head; the digest of the protected bytes is the same
    e.advance(pl, "/wt", Options())
    head_verdict(p, "f" * 40, reviewer="reviewer-2")
    assert "reject" in e.advance(pl, "/wt", Options(approved=DIGEST)).phase  # a reject of the digest blocks at every head


def test_an_approve_for_an_old_head_does_not_admit_a_new_head():
    p = fake_providers()
    pl, _ = make(p, per_head=True)
    e = engine(p)
    head_verdict(p, p.vcs.h)
    p.vcs.h = "f" * 40
    e.advance(pl, "/wt", Options())
    r = e.advance(pl, "/wt", Options(approved=DIGEST))
    assert r.exit_code == REFUSED and "no verdict is recorded" in r.phase and "f" * 12 in r.phase


def test_a_verdict_is_write_once_per_reviewer_and_head_and_a_reject_wins_over_every_approval():
    p = fake_providers()
    head = p.vcs.h
    head_verdict(p, head, reviewer="a", verdict="approve", at="1")
    assert head_verdict_of(p.store, DIGEST, head)["verdict"] == "approve" and head_verdict_of(p.store, DIGEST, "e" * 40) is None
    head_verdict(p, head, reviewer="b", verdict="reject", at="2")
    assert head_verdict_of(p.store, DIGEST, head)["verdict"] == "reject"
    with pytest.raises(FileExistsError):
        head_verdict(p, head, reviewer="b", verdict="approve", at="3")  # b cannot take the reject back, nor can anyone use b's name
    assert head_verdict_of(p.store, DIGEST, head)["verdict"] == "reject"
    assert head_verdict_of(p.store, DIGEST, "e" * 40)["reviewer"] == "b"  # and it blocks the digest at every head
    assert head_verdict_of(p.store, "9" * 64, head) is None  # other protected bytes (another digest) start clean


def test_stops_at_the_review_with_the_digest_and_one_next_command():
    p = fake_providers()
    pl, calls = make(p)
    r = engine(p).advance(pl, "/wt", Options(inputs={"flavour": "spicy"}))
    assert (r.status, r.exit_code) == ("review", OK)
    assert r.next == f"sm head demo /wt --approved {DIGEST}" and "reviewer" in r.waiting_on
    assert calls == ["build"] and external_calls(p) == []
    brief = next(iter(p.agents.briefs.values()))
    assert "Review the build." in brief and f"sm review verdict {DIGEST} --approve" in brief


def test_resumes_without_repeating_a_step_and_remembers_inputs():
    p = fake_providers()
    pl, calls = make(p)
    e = engine(p)
    e.advance(pl, "/wt", Options(inputs={"flavour": "spicy"}))
    r = e.advance(pl, "/wt", Options())
    assert r.status == "review" and calls == ["build"]
    assert p.store.load("runs.head.demo", "a" * 40)["inputs"] == {"flavour": "spicy"}


def test_the_digest_alone_is_not_a_review():
    p = fake_providers()
    pl, _ = make(p)
    e = engine(p)
    e.advance(pl, "/wt", Options())
    r = e.advance(pl, "/wt", Options(approved=DIGEST))
    assert (r.status, r.exit_code) == ("refused", REFUSED) and "digest alone is not a review" in r.phase


def test_a_wrong_digest_a_reject_and_a_self_review_are_refused():
    p = fake_providers()
    pl, _ = make(p)
    e = engine(p)
    e.advance(pl, "/wt", Options(author="coordinator"))
    assert e.advance(pl, "/wt", Options(approved="e" * 64)).exit_code == USAGE
    approve(p, reviewer="coordinator")
    assert "different agent" in e.advance(pl, "/wt", Options(approved=DIGEST)).phase
    p2 = fake_providers()
    pl2, _ = make(p2)
    e2 = engine(p2)
    e2.advance(pl2, "/wt", Options())
    approve(p2, verdict="reject")
    assert "reject" in e2.advance(pl2, "/wt", Options(approved=DIGEST)).phase


def test_an_approved_digest_bound_to_a_head_does_not_resume_without_the_heads_check_receipt():
    """Fail closed, for every review stop: the engine itself asks for the receipt on resume. A pipeline sets nothing
    for it, so none can leave it out; an approval recorded before the digest was bound does not pass either."""
    from steuermaschine import verdicts

    p = fake_providers()
    pl, calls = make(p)  # a plain review stop: no option names the check
    e = engine(p)
    e.advance(pl, "/wt", Options())
    approve(p)  # recorded before anything bound the digest to a head
    verdicts.need_check(p.store, DIGEST, "a" * 40, "tool check /wt", base="b" * 40)
    r = e.advance(pl, "/wt", Options(approved=DIGEST))
    assert (r.status, r.exit_code) == ("refused", REFUSED) and "no check receipt exists" in r.phase and "`tool check /wt`" in r.phase
    verdicts.record_check(p.store, "a" * 40, {"base": "c" * 40})  # measured from another base: still no
    assert "measured from cccccccccccc" in e.advance(pl, "/wt", Options(approved=DIGEST)).phase and calls == ["build"]
    p2 = fake_providers()  # the same run with the receipt from the bound base goes on
    pl2, _ = make(p2)
    e2 = engine(p2)
    e2.advance(pl2, "/wt", Options())
    approve(p2)
    verdicts.need_check(p2.store, DIGEST, "a" * 40, "tool check /wt", base="b" * 40)
    verdicts.record_check(p2.store, "a" * 40, {"base": "b" * 40})
    assert e2.advance(pl2, "/wt", Options(approved=DIGEST)).status != "refused"


def test_the_engine_reaches_the_check_receipt_without_the_verdicts_module():
    """Digest locality: engine.py is in every review digest, so it imports nothing for this (also not inside a function)."""
    import ast
    import inspect

    from steuermaschine import engine as engine_module
    from steuermaschine import verdicts

    tree = ast.parse(inspect.getsource(engine_module))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert "verdicts" not in imported and verdicts.missing_check is engine_module.missing_check and verdicts.KIND == engine_module.DIGEST_KIND


def test_after_approval_a_dry_run_stops_before_the_first_external_step():
    p = fake_providers()
    pl, calls = make(p)
    e = engine(p)
    e.advance(pl, "/wt", Options())
    approve(p)
    r = e.advance(pl, "/wt", Options(approved=DIGEST))
    assert (r.status, r.exit_code) == ("dry_run", OK) and r.next == "sm head demo /wt --live"
    assert external_calls(p) == [] and calls == ["build"]


def test_live_runs_the_external_step_then_waits_with_exit_75_then_finishes():
    p = fake_providers()
    pl, calls = make(p)
    e = engine(p)
    e.advance(pl, "/wt", Options())
    approve(p)
    r = e.advance(pl, "/wt", Options(approved=DIGEST, live=True))
    assert (r.status, r.exit_code) == ("waiting", WAITING) and external_calls(p) == [("tag", "t")]
    assert r.next == "sm head demo /wt --live" and r.waiting_on == "the world"
    p.forge.variables["settled"] = "1"
    r = e.advance(pl, "/wt", Options(live=True))
    assert (r.status, r.exit_code) == ("finished", OK) and calls == ["build", "publish"]
    assert e.advance(pl, "/wt", Options(live=True)).status == "finished" and calls == ["build", "publish"]


def test_wait_next_can_offer_an_alternate_command_without_changing_wait_state():
    p = fake_providers()
    pl, calls = make(p, review=False, wait_next="sm check /wt --allow-full")
    r = engine(p).advance(pl, "/wt", Options(live=True))
    assert (r.status, r.exit_code) == ("waiting", WAITING)
    assert "NEXT     sm check /wt --allow-full" in r.lines()
    assert r.waiting_on == "the world" and calls == ["build", "publish"]


def test_a_step_done_by_hand_is_adopted():
    p = fake_providers()
    pl, calls = make(p, review=False)
    p.forge.tags["t"] = "x"
    r = engine(p).advance(pl, "/wt", Options())  # not even --live is needed: nothing is left to write
    assert "publish" not in calls and r.steps[1]["detail"].startswith("tag exists (adopted")


def test_a_moved_base_voids_build_and_review_until_something_was_written_outside():
    p = fake_providers()
    pl, calls = make(p)
    e = engine(p)
    e.advance(pl, "/wt", Options())
    approve(p)
    e.advance(pl, "/wt", Options(approved=DIGEST))
    p.vcs.base_sha = "c" * 40
    r = e.advance(pl, "/wt", Options())
    assert calls == ["build", "build"] and r.status == "review" and any("base moved" in line for line in r.log)
    e.advance(pl, "/wt", Options(approved=DIGEST, live=True))
    p.vcs.base_sha = "e" * 40
    e.advance(pl, "/wt", Options(live=True))
    assert calls == ["build", "build", "publish"]  # after the external write nothing is voided


def test_plan_writes_nothing_and_runs_nothing():
    p = fake_providers()
    pl, calls = make(p)
    r = engine(p).advance(pl, "/wt", Options(plan=True))
    assert r.status == "plan" and calls == [] and p.store.writes == 0 and len(r.log) == 5


def test_lane_cap_refuses_a_new_head_and_names_the_occupant():
    p = fake_providers()
    pl, _ = make(p)
    e = engine(p, lanes={"default": 1})
    e.advance(pl, "/wt", Options())
    p.vcs.h = "f" * 40
    r = e.advance(pl, "/wt", Options())
    assert r.exit_code == REFUSED and "lane 'default' is full" in r.phase and "x aaaaaa" in r.phase


def test_a_gate_that_does_not_hold_refuses_and_names_the_gate():
    p = fake_providers()
    pl, calls = make(p, review=False, gates=("green",))
    gate = Gate("green", "CI is green", lambda ctx: (False, "CI is red"))
    r = engine(p, gates={"green": gate}).advance(pl, "/wt", Options(live=True))
    assert r.exit_code == REFUSED and "gate 'green' does not hold: CI is red" in r.phase and r.next == "sm self gates" and "publish" not in calls


def test_a_second_process_on_the_same_subject_waits():
    p = fake_providers()
    pl, calls = make(p)
    p.store.held.add("head.demo." + "a" * 40)
    r = engine(p).advance(pl, "/wt", Options())
    assert r.exit_code == WAITING and calls == []


def test_a_crashing_step_keeps_the_state_of_the_steps_before_it():
    p = fake_providers()
    pl, _ = make(p, review=False)
    pl.steps[1].run = lambda ctx: 1 / 0
    r = engine(p).advance(pl, "/wt", Options(live=True))
    assert r.exit_code == 70 and "ZeroDivisionError" in r.phase
    assert p.store.load("runs.head.demo", "a" * 40)["steps"]["build"]["status"] == "done"


def test_plugin_traps_override_the_steps_own():
    p = fake_providers()
    pl, _ = make(p)
    r = engine(p, traps={"head.demo.review": "Bytes change on every rebuild."}).advance(pl, "/wt", Options())
    assert r.trap == "Bytes change on every rebuild."


def test_refuse_carries_the_one_command_that_helps():
    p = fake_providers()
    pl, _ = make(p, review=False)

    def no(ctx):
        raise Refuse("not ready", "sm head status")

    pl.steps[0].run = no
    r = engine(p).advance(pl, "/wt", Options())
    assert (r.exit_code, r.next) == (REFUSED, "sm head status")


@pytest.mark.parametrize("field", ["phase", "next", "waiting_on", "trap", "status", "steps"])
def test_every_report_has_the_go_fields(field):
    p = fake_providers()
    pl, _ = make(p)
    assert field in engine(p).advance(pl, "/wt", Options()).as_json()
