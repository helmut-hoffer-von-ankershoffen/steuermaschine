"""`sm ci flake-ledger` and `sm queue main-proof` (Core ADR 0149 g, g', i), and the merge-tier reads they share.
GitHub, git, uv and bd are the fake process runner's: nothing is posted, run or filed."""

import base64
import json
from pathlib import Path

import pytest
from coord_kit import app_runtime, ok

from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, invoke
from steuermaschine_heinzel.native import merge_ledger, merge_tier

REPO = "o/core"
HEAD = "a" * 40
GREEN = "b" * 40
PARENT = "c" * 40
PLAN = 'WHOLE_SUITES = {"web": VITEST_ALL}\nMAIN_PROOF_CONTEXT = "hz/main-proof"\n'
LINE = "passed on rerun: Focused tests (web) (attempt 1 failure, run 11, head " + HEAD + ")"
LINE2 = "passed on rerun: Coverage (attempt 2 cancelled, run 11, head " + HEAD + ")"


def contents(text: str) -> dict:
    return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


class World:
    """GitHub answers by API path (query included), plus git, uv, bd and gh run download."""

    def __init__(self, plan: str = PLAN, **api) -> None:
        self.api = {f"contents/{merge_tier.PLAN_PATH}?ref=main": contents(plan), **api}
        self.posted: list[list[str]] = []
        self.c = FakeCommands()
        self.c.on(["gh", "api"], self.gh_api)
        self.c.on(["gh", "auth", "token"], ok("tok\n"))
        self.c.on(["gh", "run", "download"], self.download)
        self.c.on(["git"], ok(""))
        self.c.on(["git", "-C"], self.git)
        self.c.on(["uv"], ok(""))
        self.c.on(["bd", "list"], ok([{"id": "heinzel-management-old", "title": "flaky: Focused tests (web)", "status": "open"}]))
        self.c.on(["bd", "create"], ok("heinzel-management-new\n"))
        self.c.on(["bd", "update"], ok("updated\n"))
        self.ancestors = {GREEN}

    def gh_api(self, argv: list[str]) -> Result:
        if "-X" in argv:
            self.posted.append(argv)
            return ok({})
        path = argv[2].removeprefix(f"repos/{REPO}/")
        for key, value in self.api.items():
            if path == key or path.split("?")[0] == key:
                return value if isinstance(value, Result) else ok(value)
        return Result(1, "", f"gh: Not Found (HTTP 404) {path}")

    def download(self, argv: list[str]) -> Result:
        target = Path(argv[argv.index("-D") + 1])
        (target / "flake-ledger.txt").write_text(f"{LINE}\n{LINE2}\nnoise\n")
        return ok("")

    def git(self, argv: list[str]) -> Result:
        if "merge-base" in argv:
            return ok("") if argv[-2] in self.ancestors else Result(1, "", "")
        if "rev-parse" in argv:
            return ok(PARENT + "\n")
        return ok("")


def rt(tmp_path, world: World, **over):
    return app_runtime(tmp_path, world.c, repo=REPO, **over)


def test_flake_lines_are_read_exactly_and_capability_is_read_from_main():
    assert [f.job for f in merge_tier.flakes(f"{LINE}\nother\n{LINE2}")] == ["Focused tests (web)", "Coverage"]
    assert merge_tier.capable(PLAN) and merge_tier.proof_capable(PLAN)
    assert not merge_tier.capable('VITEST_ALL = "web/ALL"\n') and not merge_tier.proof_capable('WHOLE_SUITES = {}\n')
    assert merge_tier.active("on", lambda: None) and not merge_tier.active("off", lambda: PLAN)
    assert merge_tier.active("auto", lambda: PLAN) and not merge_tier.active("auto", lambda: None)

    def broken() -> str:
        raise OSError("down")

    assert not merge_tier.active("auto", broken)


def test_main_plan_is_none_when_unreadable(tmp_path):
    from steuermaschine.github import Gh

    world = World()
    gh = Gh(world.c, repo=REPO)
    assert merge_tier.main_plan(gh) == PLAN and merge_tier.active_at_main(gh, {})
    world.api[f"contents/{merge_tier.PLAN_PATH}?ref=main"] = {"encoding": "none"}
    assert merge_tier.main_plan(gh) is None
    world.api[f"contents/{merge_tier.PLAN_PATH}?ref=main"] = {"encoding": "base64", "content": "/w=="}
    assert merge_tier.main_plan(gh) is None


def gate_world(**over) -> World:
    return World(**{
        f"actions/workflows/{merge_ledger.GATE_WORKFLOW}/runs": {"workflow_runs": [{"id": 11}], "total_count": 1},
        "actions/runs/11/artifacts": {"artifacts": [{"name": "flake-ledger-11-2"}, {"name": "coverage-total"},
                                                    {"name": "flake-ledger-11-1", "expired": True}]},
        **over})


def test_harvest_files_one_flaky_bead_per_job_once(tmp_path):
    world = gate_world()
    runtime = rt(tmp_path, world)
    done = merge_ledger.Harvest.model_validate(json.loads(invoke(runtime, ["--format", "json", "ci", "flake-ledger"]).stdout))
    assert done.state == "harvested" and done.ledgers == ["flake-ledger-11-2"]
    assert [(f.action, f.bead, f.job) for f in done.filed] == [("noted", "old", "Focused tests (web)"), ("added", "new", "Coverage")]
    create = next(c for c in world.c.calls if c[:2] == ["bd", "create"])
    assert create[3] == "flaky: Coverage" and "flaky" in create and LINE2 in create
    again = invoke(runtime, ["ci", "flake-ledger"])
    assert again.exit_code == 0 and "0 new, 0 flaky bead(s)" in again.stdout


def test_harvest_does_nothing_without_the_merge_tier_and_refuses_unreadable_runs(tmp_path):
    assert "has no merge tier" in invoke(rt(tmp_path, gate_world(plan='VITEST_ALL = "x"\n')), ["ci", "flake-ledger"]).stdout
    broken = gate_world(**{f"actions/workflows/{merge_ledger.GATE_WORKFLOW}/runs": Result(1, "", "boom")})
    assert invoke(rt(tmp_path, broken), ["ci", "flake-ledger"]).exit_code == 1
    no_artifacts = gate_world(**{"actions/runs/11/artifacts": Result(1, "", "boom")})
    assert "cannot list the artifacts of run 11" in invoke(rt(tmp_path, no_artifacts), ["ci", "flake-ledger"]).stderr
    world = gate_world()
    world.c.on(["gh", "run", "download"], Result(1, "", "gone"))
    assert "cannot download flake-ledger-11-2" in invoke(rt(tmp_path, world), ["ci", "flake-ledger"]).stderr
    assert invoke(rt(tmp_path, gate_world(), merge_tier_mode="off"), ["ci", "flake-ledger"]).stdout.startswith("flake ledgers: the Core")


def proof_world(conclusion="success", ci_ok="success", status=(), **over) -> World:
    return World(**{
        "git/ref/heads/main": {"object": {"sha": HEAD}},
        f"commits/{HEAD}/statuses": list(status),
        f"commits/{GREEN}/statuses": [{"context": "hz/main-proof", "state": "success"}],
        f"actions/workflows/{merge_ledger.CI_WORKFLOW}/runs?branch=main&event=push&head_sha={HEAD}&per_page=10":
            {"workflow_runs": [{"id": 21, "head_sha": HEAD, "status": "completed", "conclusion": conclusion, "html_url": "u"}]},
        f"actions/workflows/{merge_ledger.CI_WORKFLOW}/runs?branch=main&event=push&status=success&per_page=30":
            {"workflow_runs": [{"id": 21, "head_sha": HEAD}, {"id": 19, "head_sha": "d" * 40}, {"id": 20, "head_sha": GREEN}]},
        "actions/runs/21/jobs": {"jobs": [{"name": "CI OK", "conclusion": ci_ok}]},
        "actions/runs/20/jobs": {"jobs": [{"name": "CI OK", "conclusion": "success"}]},
        "actions/runs/19/jobs": {"jobs": [{"name": "CI OK", "conclusion": "success"}]},
        "actions/variables/FOCUSED_CI_PLANS": {"value": "{}"},
        **over})


def judge(world: World, proof: dict | None = None, rc: int = 0) -> None:
    body = {"type": "focused-main/v1", "head": HEAD, "run": 21, "attempt": 2, "full": False,
            "passed_on_rerun": [LINE2], **(proof or {})}
    world.c.on(["uv", "run"], Result(rc, "progress\n" + json.dumps(body) + "\n", "judge said no\n" if rc else ""))


def main_proof(tmp_path, world: World, *args: str, **over) -> merge_ledger.MainProof:
    return merge_ledger.MainProof.model_validate(json.loads(invoke(rt(tmp_path, world, **over), ["--format", "json", "queue", "main-proof", *args]).stdout))


def test_the_judge_accepts_main_and_the_status_is_posted_with_the_rerun_flakes(tmp_path):
    world = proof_world()
    judge(world)
    done = main_proof(tmp_path, world)
    assert (done.state, done.run, done.base, done.full) == ("posted", 21, GREEN, False)
    assert [f.job for f in done.filed] == ["Coverage"]
    post = world.posted[0]
    assert post[4] == f"repos/{REPO}/statuses/{HEAD}" and "context=hz/main-proof" in post and "state=success" in post
    uv = next(c for c in world.c.calls if c[0] == "uv")
    assert uv[-1] == "scripts/focused_gate.py"


def test_bootstrap_without_an_accepted_commit_uses_the_parent(tmp_path):
    world = proof_world(**{f"commits/{GREEN}/statuses": []})
    judge(world, {"full": True, "passed_on_rerun": []})
    done = main_proof(tmp_path, world)
    assert (done.state, done.base, done.full, done.filed) == ("posted", PARENT, True, [])
    assert "full set" in " ".join(world.posted[0])


@pytest.mark.parametrize(("world", "state", "reason"), [
    (lambda: proof_world(plan='VITEST_ALL = "x"\n'), "not-applicable", "no main proof"),
    (lambda: proof_world(plan='WHOLE_SUITES = {}\n'), "not-applicable", "no main proof"),
    (lambda: proof_world(status=[{"context": "hz/main-proof", "state": "success"}]), "already", ""),
    (lambda: proof_world(conclusion="failure"), "waiting", "a rerun of a red main run is a valid repair"),
    (lambda: proof_world(ci_ok="failure"), "waiting", "valid repair"),
    (lambda: proof_world(**{f"actions/workflows/{merge_ledger.CI_WORKFLOW}/runs?branch=main&event=push&head_sha={HEAD}&per_page=10":
                            {"workflow_runs": []}}), "waiting", "no push run yet"),
    (lambda: proof_world(**{f"actions/workflows/{merge_ledger.CI_WORKFLOW}/runs?branch=main&event=push&head_sha={HEAD}&per_page=10":
                            {"workflow_runs": [{"id": 21, "head_sha": HEAD, "status": "in_progress"}]}}), "waiting", "not finished"),
])
def test_nothing_is_posted_before_a_green_finished_main_run(tmp_path, world, state, reason):
    w = world()
    done = main_proof(tmp_path, w)
    assert done.state == state and reason in done.reason and not w.posted


def test_refusals_post_nothing(tmp_path):
    moved = main_proof(tmp_path, proof_world(), "--sha", GREEN)
    assert moved.state == "refused" and "main moved" in moved.reason
    dry = main_proof(tmp_path, proof_world(), "--dry-run")
    assert (dry.state, dry.base) == ("waiting", GREEN)
    for proof, rc, text in [({}, 1, "the main judge refused run 21"), ({"head": GREEN}, 0, "does not bind main")]:
        world = proof_world()
        judge(world, proof, rc)
        done = invoke(rt(tmp_path, world), ["queue", "main-proof"])
        assert done.exit_code == 1 and text in done.stderr and not world.posted
    world = proof_world()
    world.c.on(["uv", "run"], ok("no json\n"))
    assert "printed no proof" in invoke(rt(tmp_path, world), ["queue", "main-proof"]).stderr
    world = proof_world()
    world.c.on(["gh", "auth", "token"], Result(1, "", "not logged in"))
    assert "no GitHub token" in invoke(rt(tmp_path, world), ["queue", "main-proof"]).stderr
    world = proof_world()
    world.c.on(["git", "clone"], Result(128, "", "no"))
    assert "cannot prepare main" in invoke(rt(tmp_path, world), ["queue", "main-proof"]).stderr
    unread = proof_world(**{"git/ref/heads/main": Result(1, "", "down")})
    assert "cannot read main" in invoke(rt(tmp_path, unread), ["queue", "main-proof"]).stderr
    from steuermaschine.engine import Refuse
    from steuermaschine.native import NativeRuntime, using

    bare = rt(tmp_path, proof_world())
    with using(NativeRuntime(bare.providers, {**bare.settings, "core_repo_dir": None}, bare.project)), pytest.raises(Refuse, match="set core_repo_dir"):
        merge_ledger.queue_main_proof(None, False, False)
    world = proof_world(**{f"commits/{GREEN}/statuses": []})
    world.c.on(["git", "-C", str(tmp_path / "core"), "rev-parse"], Result(128, "", "unknown"))
    assert "is not in" in invoke(rt(tmp_path, world), ["queue", "main-proof"]).stderr


def test_a_failed_status_post_refuses(tmp_path):
    world = proof_world()
    judge(world)
    real = world.gh_api
    world.c.on(["gh", "api"], lambda argv: Result(1, "", "gh: Forbidden (HTTP 403)") if "-X" in argv else real(argv))
    done = invoke(rt(tmp_path, world), ["queue", "main-proof"])
    assert done.exit_code == 1 and "the status was not posted" in done.stderr


def test_the_flakes_are_filed_before_the_status_and_once(tmp_path):
    world = proof_world()
    judge(world)
    real = world.gh_api
    world.c.on(["gh", "api"], lambda argv: Result(1, "", "gh: Forbidden (HTTP 403)") if "-X" in argv else real(argv))
    runtime = rt(tmp_path, world)
    assert invoke(runtime, ["queue", "main-proof"]).exit_code == 1
    assert sum(1 for c in world.c.calls if c[:2] == ["bd", "create"]) == 1  # filed although the post failed
    world.c.on(["gh", "api"], real)
    again = merge_ledger.MainProof.model_validate(json.loads(invoke(runtime, ["--format", "json", "queue", "main-proof"]).stdout))
    assert again.state == "posted" and again.filed == [] and world.posted
    assert sum(1 for c in world.c.calls if c[:2] == ["bd", "create"]) == 1  # not filed twice


def test_a_tracker_failure_posts_nothing_and_keeps_the_flakes_for_the_next_tick(tmp_path):
    world = proof_world()
    judge(world)
    world.c.on(["bd", "create"], Result(1, "", "database locked"))
    runtime = rt(tmp_path, world)
    assert invoke(runtime, ["queue", "main-proof"]).exit_code != 0 and not world.posted
    world.c.on(["bd", "create"], ok("heinzel-management-new\n"))
    done = merge_ledger.MainProof.model_validate(json.loads(invoke(runtime, ["--format", "json", "queue", "main-proof"]).stdout))
    assert done.state == "posted" and [f.job for f in done.filed] == ["Coverage"] and world.posted


def test_merge_tier_mode_off_posts_nothing(tmp_path):
    world = proof_world()
    judge(world)
    done = main_proof(tmp_path, world, merge_tier_mode="off")
    assert done.state == "not-applicable" and not world.posted
    assert not any(c[0] in ("uv", "bd") for c in world.c.calls)
    assert not any("contents/" in " ".join(c) for c in world.c.calls)  # off reads nothing of main


def test_text_rendering(tmp_path):
    world = proof_world()
    judge(world)
    text = invoke(rt(tmp_path, world), ["queue", "main-proof"]).stdout
    assert text.startswith(f"main proof {HEAD[:9]}: posted (run 21, base {GREEN[:9]})") and "a focused selection" in text
    assert "added" in text


def test_a_second_run_waits_for_the_filing_lock_and_files_no_second_bead(tmp_path):
    """g2: a tick and a manual call must not both create `flaky: Coverage`."""
    world = proof_world()
    judge(world)
    runtime = rt(tmp_path, world)
    runtime.providers.store.held.add(merge_ledger.FILING)  # the other run holds the lock
    busy = invoke(runtime, ["queue", "main-proof"])
    assert busy.exit_code == 75 and "filing flaky beads" in busy.stderr and not world.posted
    harvest = invoke(rt(tmp_path, gate_world()), ["ci", "flake-ledger"])
    assert harvest.exit_code == 0
    gate = rt(tmp_path, gate_world())
    gate.providers.store.held.add(merge_ledger.FILING)
    assert invoke(gate, ["ci", "flake-ledger"]).exit_code == 75
    assert not any(c[:2] == ["bd", "create"] for c in gate.providers.commands.calls)
    runtime.providers.store.held.discard(merge_ledger.FILING)
    done = merge_ledger.MainProof.model_validate(json.loads(invoke(runtime, ["--format", "json", "queue", "main-proof"]).stdout))
    assert done.state == "posted" and [f.job for f in done.filed] == ["Coverage"]
    assert sum(1 for c in world.c.calls if c[:2] == ["bd", "create"]) == 1 and not runtime.providers.store.held


def test_file_once_files_a_ledger_only_once():
    from steuermaschine.testing import fake_providers

    class Rt:
        providers = fake_providers(commands=FakeCommands())

    Rt.providers.store.save(merge_ledger.SEEN, "k", {"run": 1})
    assert merge_ledger.file_once(Rt, "k", lambda: pytest.fail("read although filed"), {}) is None


def test_a_second_judge_waits_for_the_judge_lock(tmp_path):
    world = proof_world()
    judge(world)
    runtime = rt(tmp_path, world)
    runtime.providers.store.held.add(merge_ledger.JUDGE)  # the detached judge of the queue tick runs
    busy = invoke(runtime, ["queue", "main-proof"])
    assert busy.exit_code == 75 and "another main proof runs" in busy.stderr and not world.posted
    assert not any(c[0] == "uv" for c in world.c.calls)  # no second judge
    dry = merge_ledger.MainProof.model_validate(json.loads(invoke(runtime, ["--format", "json", "queue", "main-proof", "--dry-run"]).stdout))
    assert dry.base == GREEN  # the tick's read-only check needs no lock


def test_check_says_ready_without_a_base_or_the_judge(tmp_path):
    """What the queue's tick asks: a few reads, state ready only for a finished green push run with CI OK whose commit
    the Core clone holds."""
    world = proof_world()
    judge(world)
    ready = main_proof(tmp_path, world, "--check")
    assert (ready.state, ready.sha, ready.run, ready.base) == ("ready", HEAD, 21, "")
    assert not world.posted and not any(c[0] in ("uv", "bd") for c in world.c.calls)
    assert [c[3:] for c in world.c.calls if c[0] == "git"] == [["cat-file", "-e", f"{HEAD}^{{commit}}"]]  # no clone, no base search
    assert not any("status=success" in " ".join(c) for c in world.c.calls)
    assert invoke(rt(tmp_path, world), ["queue", "main-proof", "--check"]).stdout.startswith(f"main proof {HEAD[:9]}: ready (run 21): its push run is green")
    for other, state in ((proof_world(conclusion="failure"), "waiting"), (proof_world(ci_ok="failure"), "waiting"),
                         (proof_world(status=[{"context": "hz/main-proof", "state": "success"}]), "already"),
                         (proof_world(plan='VITEST_ALL = "x"\n'), "not-applicable")):
        assert main_proof(tmp_path, other, "--check").state == state
    stale = proof_world()
    stale.c.on(["git", "-C", str(tmp_path / "core"), "cat-file"], Result(1, "", ""))
    waiting = main_proof(tmp_path, stale, "--check")
    assert waiting.state == "waiting" and "not in the Core clone" in waiting.reason
    from steuermaschine.engine import Refuse
    from steuermaschine.native import NativeRuntime, using

    bare = rt(tmp_path, proof_world())
    with using(NativeRuntime(bare.providers, {**bare.settings, "core_repo_dir": None}, bare.project)), pytest.raises(Refuse, match="set core_repo_dir"):
        merge_ledger.queue_main_proof(None, False, True)


def test_the_judge_waits_a_while_for_the_filing_lock(tmp_path):
    """The judge has minutes of work behind it: a harvest that files at that moment does not cost the proof."""
    world = proof_world()
    judge(world)
    runtime = rt(tmp_path, world)
    store, clock = runtime.providers.store, runtime.providers.clock
    store.held.add(merge_ledger.FILING)
    pauses = []
    real = clock.sleep

    def sleep(seconds):
        pauses.append(seconds)
        if len(pauses) == 2:
            store.held.discard(merge_ledger.FILING)  # the other run finished
        return real(seconds)

    clock.sleep = sleep
    done = merge_ledger.MainProof.model_validate(json.loads(invoke(runtime, ["--format", "json", "queue", "main-proof"]).stdout))
    assert done.state == "posted" and [f.job for f in done.filed] == ["Coverage"] and pauses == [merge_ledger.FILING_PAUSE] * 2
