"""`sm head admit` and `sm release cut`: the engine pipelines over the native steps, replayed on today's flows.

The native steps are replaced by recorded results (pipelines.common.OVERRIDE): each has its own tests. What is
tested here is the composition: the order, the two kinds of stop (review, --live), the exact digest handed from a
stop to the write after it, drift (main or head moved), adoption of what is already true, the review gate of the
pipeline itself and of each composed command, and what bare `sm go` says from the resulting state.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from steuermaschine import verdicts
from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine.testing.fakes import FakeVcs
from steuermaschine_heinzel.native.admission_lib.ops import ApplyResult, canonical_sha256
from steuermaschine_heinzel.native.go_lib import decide as rules
from steuermaschine_heinzel.native.preflight import HeadPreflight
from steuermaschine_heinzel.native.release import ReleaseRun
from steuermaschine_heinzel.native.release_admit_lib.release_ops import ReleasePrepareResult
from steuermaschine_heinzel.native.release_read import NotesAudit, ReleaseVerify, SurfaceCheck
from steuermaschine_heinzel.pipelines import common, release_cut
from steuermaschine_heinzel.plugin import plugin

HEAD = "e9c943810ea75cfad155a431a2f168b2a9559828"
NEXT_HEAD = "f00d943810ea75cfad155a431a2f168b2a955982"
MAIN = "1947731c" + "0" * 32
MOVED_MAIN = "bb56886" + "1" * 33
BRANCH = "fix/update-all"


@pytest.fixture(autouse=True)
def clean_overrides():
    common.OVERRIDE.clear()
    changes = release_cut.changes_since_release
    yield
    common.OVERRIDE.clear()
    release_cut.changes_since_release = changes


def runtime(tmp_path: Path, gh: FakeGh | None = None, vcs: FakeVcs | None = None, reviewed: bool = True, core: str | None = None) -> Runtime:
    stack = stack_of(plugin)
    raw = {"heinzel": {"legacy_state_dir": str(tmp_path / "state"), "core_repo_dir": str(tmp_path / "core") if core is None else core, "repo": "o/core",
                       "dispatch_state_file": str(tmp_path / "dispatch" / "focused-runs.jsonl")}}
    settings, _ = stack.validate(raw, tmp_path)
    if vcs is None:
        vcs = FakeVcs(head=HEAD, branch=BRANCH, base=MAIN)
        vcs.remote["main"] = MAIN
    project = Project(name="t", plugin="heinzel", root=tmp_path, plugins=["heinzel"], allow_unreviewed=reviewed)
    return Runtime(project, stack, settings, _providers=fake_providers(commands=gh or FakeGh(), vcs=vcs))


def sha_of(path: Path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def report(out) -> dict:
    assert out.stdout.strip(), out
    return json.loads(out.stdout)


def verdict(rt: Runtime, digest: str) -> None:
    need = (rt.providers.store.load(verdicts.KIND, digest) or {}).get("check")
    if need:  # a packet digest binds its head: `sm check` was green on it, measured from the main the packet is built on
        verdicts.record_check(rt.providers.store, need["head"], {"base": need["base"]})
    done = invoke(rt, ["review", "verdict", digest, "--approve", "--reviewer", "reviewer-fable", "--notes", "recomputed"])
    assert done.exit_code == 0, done


# ----------------------------------------------------------------------------- head admit: the update-all admission
def read(kw) -> str:
    """The sha256 of the selection bytes the (fake) preflight read, as the real one reports it."""
    return hashlib.sha256(Path(kw["sel"]).read_bytes()).hexdigest()




def admit(rt: Runtime, wt: Path, *more: str) -> dict:
    return report(invoke(rt, ["--format", "json", "head", "admit", str(wt), *more]))


def inputs(tmp_path: Path) -> list[str]:
    (tmp_path / "sel.json").write_text("{}")
    return ["--set", f"review_root={tmp_path / 'review'}", "--set", f"sel={tmp_path / 'sel.json'}", "--set", "junit=python=j.xml",
            "--set", "pr=812", "--set", "bead=hz-412", "--set", "title=fix(web): update all plugins in one click"]










# ----------------------------------------------------------------------------- release cut: the 0.9.40 recut
class CutFakes:
    def __init__(self, tmp_path: Path):
        self.tmp, self.calls, self.resumes = tmp_path, [], []
        release_cut.changes_since_release = self.ci  # restored by clean_overrides
        common.OVERRIDE.update({"release notes-audit": self.notes, "release admit prepare": self.prepare,
                                "release admit apply": self.apply, "release resume": self.resume, "release verify": self.verify})

    def ci(self, core, head):
        self.calls.append("ci change")
        return "7" * 40, ()  # no CI path changed since the last tag: no ci_change verdict needed

    def notes(self, **kw):
        self.calls.append("notes")
        return NotesAudit(checkout=str(kw["checkout"]), base=kw["base"], missing=[])

    def prepare(self, **kw):
        self.calls.append("admit prepare")
        packet = Path(kw["packet"])
        doc = {"head": HEAD, "main": MAIN, "slot": "RELEASE_MAIN", "version": "0.9.40"}
        (packet.parent / "prepare.out").write_text(json.dumps(doc))
        (packet.parent / "prepare.args.json").write_text(json.dumps({"schema": 1, "packet": str(packet), "sha256": canonical_sha256(doc)}))  # as the real file: no main
        return ReleasePrepareResult(sha256=canonical_sha256(doc), version="0.9.40", head=HEAD, main=MAIN, slot="RELEASE_MAIN", recovery=False,
                                    prepared_path=str(packet.parent / "prepare.out"), args_path="", prepared=doc)

    def apply(self, **kw):
        self.calls.append(("admit apply", kw["approved"]))
        return ApplyResult(receipt_path=str(self.tmp / "receipt.json"), sha256=kw["approved"], head=HEAD)

    def resume(self, **kw):
        self.calls.append(("resume", kw["version"], kw["intro"], kw["snapshot_acceptance"]))
        return self.resumes.pop(0)

    def verify(self, **kw):
        self.calls.append("verify")
        return ReleaseVerify(version=kw["version"], checks=[SurfaceCheck(passed=True, text="pypi heinzel 0.9.40"), SurfaceCheck(passed=True, text="release v0.9.40")])


def cut(rt: Runtime, *more: str) -> dict:
    return report(invoke(rt, ["--format", "json", "release", "cut", "0.9.40", *more]))


def test_the_0940_recut_stops_twice_survives_a_failed_compose_and_verifies(tmp_path):
    rt, fakes = runtime(tmp_path), CutFakes(tmp_path)
    (tmp_path / "core").mkdir()
    rt.providers.vcs.remote["main"] = MAIN
    packet = tmp_path / "review" / "release-0.9.40" / "packet.json"
    packet.parent.mkdir(parents=True)
    packet.write_text(json.dumps({"mode": "release-main-only/v1", "release": {"head": HEAD}}))
    sets = ["--set", f"packet={packet}", "--set", f"clone={tmp_path / 'clone'}", "--set", f"review_root={tmp_path / 'review'}",
            "--set", "intro=Plugins update in one click."]

    first = cut(rt, *sets)
    assert first["status"] == "review" and first["review"]["step"] == "admission review" and first["review"]["file"].endswith("prepare.out")
    assert fakes.calls == ["ci change", "notes", "admit prepare"]
    verdict(rt, first["review"]["sha256"])
    applied = cut(rt, "--approved", first["review"]["sha256"], "--live")
    assert applied["status"] == "review", applied["phase"]
    assert applied["status"] == "review" and applied["review"]["step"] == "publish review"
    assert ("admit apply", first["review"]["sha256"]) in fakes.calls
    plan = json.loads(Path(applied["review"]["file"]).read_text())
    assert plan["intro"] == "Plugins update in one click." and plan["snapshot_acceptance"] is True and applied["review"]["sha256"] == canonical_sha256(plan)

    verdict(rt, applied["review"]["sha256"])
    fakes.resumes = [ReleaseRun(action="resume", version="0.9.40", exit=1, stopped="compose run 19234 failed: plugin heinzel-audio 0.9.12 not on PyPI"),
                     ReleaseRun(action="resume", version="0.9.40", exit=0, finished=True, accepted=True, acceptance_run=19301)]
    failed = cut(rt, "--approved", applied["review"]["sha256"], "--live")
    assert failed["status"] == "refused" and "compose" in failed["phase"] and failed["trap"].startswith("First-attempt rule")

    state = rt.providers.store.load("runs.release.cut", "0.9.40")  # bare `sm` from this state: the failed compose
    d = rules.decide(go_state(MAIN, dict(state, external_done=True)), 0)
    assert d["kind"] == "pipeline-failed" and d["next"] == "sm release status" and "re-adopted by every resume" in d["trap"]

    done = cut(rt, "--live")  # the same command continues after the fix is published
    assert done["status"] == "finished" and fakes.calls[-1] == "verify"
    assert [c[0] for c in fakes.calls if isinstance(c, tuple) and c[0] == "resume"] == ["resume", "resume"]


# ----------------------------------------------------------------------------- bare `sm go` from the pipeline states
def go_state(main: str, *runs: dict) -> dict:
    """What the probes of `go` collected, quiet everywhere but the pipeline runs (as in test_native_go.state)."""
    return {"main": {"sha": main}, "remote_heads": {"main": main}, "runs": [], "prs": [], "releases": {"latest": "0.9.39"},
            "queue": {"queued": [], "freeze": False, "verdict": "nothing queued", "main_runs": [], "added": {}}, "train": {"trains": [], "tails": []},
            "release": {"active": None, "dropped": []}, "holds": {"files": [], "prs": {}}, "procs": [], "receipts": [], "packets": [],
            "slots": {}, "mini": {"cli": "0.9.39", "live": None, "live_ok": None}, "tools": {}, "since_tag": {"count": 0}, "pipelines": list(runs)}


def run_state(**over) -> dict:
    base = {"pipeline": "head.admit", "key": HEAD, "label": f"{BRANCH} {HEAD[:9]}", "arg": "/wt", "status": "review", "phase": "apply review: waiting",
            "next": f"sm head admit /wt --approved {'a' * 64}", "trap": "The digest is of canonical JSON.", "fingerprint": {"head": HEAD, "main": MAIN},
            "review": {"brief": "brief:reviewer:x", "file": "/r/prepare.out", "sha256": "a" * 64, "record": "sm review verdict aaa --approve --reviewer NAME"},
            "external_done": False, "worktree_head": HEAD}
    return {**base, **over}


@pytest.mark.parametrize("case, kind, next_, trap", [
    ("running prepare", "pipeline-running", "", "Start nothing else"),
    ("waiting review", "pipeline-review", "", "canonical JSON"),
    ("stale plan", "pipeline-stale", "sm head admit /wt", "binds one head and one main"),
    ("interrupted", "pipeline-interrupted", "sm head admit /wt", "continues from the saved state"),
])
def test_go_names_phase_next_and_trap_from_the_pipeline_state(case, kind, next_, trap):
    run = {"running prepare": run_state(status="running", running="prepare", pid=4242, alive=True, phase="prepare: running (pid 4242)"),
           "waiting review": run_state(),
           "stale plan": run_state(fingerprint={"head": HEAD, "main": MAIN}),
           "interrupted": run_state(status="running", running="prepare", pid=4242, alive=False)}[case]
    main = MOVED_MAIN if case == "stale plan" else MAIN
    d = rules.decide(go_state(main, run), 0)
    assert d["kind"] == kind and (d["next"] or "") == next_ and trap in d["trap"], d
    if case == "waiting review":
        assert "brief:reviewer:x" in d["waiting"] and "/r/prepare.out" in d["waiting"] and d["then"].endswith("--approved " + "a" * 64)
    if case == "running prepare":
        assert "pid 4242" in d["waiting"]




# ----------------------------------------------------------------------------- the Fable sweep: P1, P2, P3
TREE = "7ee" + "a" * 37
VALIDATION_SHA = "c0de" + "b" * 36


def hosted_preflight(wt, lanes=("web-2",)):
    return lambda **kw: HeadPreflight(title="head", ready=True, verdict="READY", head=HEAD, base=MAIN, hosted_lanes=list(lanes), selection_sha256=read(kw),
                                      next=f"sm head validate {wt} --lanes {','.join(lanes)}")


def lane_ref(lane: str) -> str:
    from steuermaschine_heinzel.native.window import lane_branch

    return lane_branch(BRANCH, lane, HEAD)


def receipt(tmp_path: Path, run: int, lane: str, **over) -> None:
    """The line `sm head validate` (dispatch_lib.focused.record) leaves for one lane run."""
    path = tmp_path / "dispatch" / "focused-runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"run": run, "kind": "dispatch", "what": f"dispatch e2e {lane} x1 on ubuntu-24.04 @ {lane_ref(lane)}", "branch": lane_ref(lane),
           "attempt": 1, "jobs": 1, "arm_jobs": 0, "result": None, **over}
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


def validation_gh(runs: dict | None = None, **run) -> FakeGh:
    """GitHub as `sm head validate` leaves it: the focused job dispatched on the lane's ref, on a throwaway commit of this tree."""
    gh = FakeGh().on(f"rev-parse {HEAD}^{{tree}}", TREE)
    for rid, fields in (runs or {9101: {"lane": "web-2", **run}}).items():
        lane = fields.pop("lane")
        gh.on(f"repos/o/core/actions/runs/{rid}", {"id": rid, "head_branch": lane_ref(lane), "head_sha": VALIDATION_SHA, "status": "completed",
                                                   "conclusion": "success", "event": "workflow_dispatch",
                                                   "path": ".github/workflows/focused-job.yml", **fields})
    gh.on(f"repos/o/core/git/commits/{VALIDATION_SHA}", {"sha": VALIDATION_SHA, "tree": {"sha": TREE}})
    gh.on(f"repos/o/core/git/commits/{HEAD}", {"sha": HEAD, "tree": {"sha": TREE}})
    gh.on("repos/o/core/git/commits/" + "d" * 40, {"sha": "d" * 40, "tree": {"sha": "f" * 40}})
    return gh








def release_packet(tmp_path: Path) -> tuple[Path, list[str]]:
    packet = tmp_path / "review" / "release-0.9.40" / "packet.json"
    packet.parent.mkdir(parents=True)
    packet.write_text(json.dumps({"mode": "release-main-only/v1", "release": {"head": HEAD}}))
    return packet, ["--set", f"packet={packet}", "--set", f"clone={tmp_path / 'clone'}", "--set", f"review_root={tmp_path / 'review'}",
                    "--set", "intro=Plugins update in one click."]


def test_p2_main_moving_before_the_release_admission_prepares_again_at_the_new_main(tmp_path):
    rt, fakes = runtime(tmp_path), CutFakes(tmp_path)
    rt.providers.vcs.remote["main"] = MAIN
    _, sets = release_packet(tmp_path)
    first = cut(rt, *sets)
    assert first["review"]["step"] == "admission review"

    rt.providers.vcs.remote["main"] = MOVED_MAIN  # a merge landed before the apply

    def prepare_at_moved_main(**kw):
        fakes.calls.append("admit prepare")
        packet = Path(kw["packet"])
        doc = {"head": HEAD, "main": MOVED_MAIN, "slot": "RELEASE_MAIN", "version": "0.9.40"}
        (packet.parent / "prepare.out").write_text(json.dumps(doc))
        (packet.parent / "prepare.args.json").write_text(json.dumps({"schema": 1, "packet": str(packet)}))  # the real file names no main
        return ReleasePrepareResult(sha256=canonical_sha256(doc), version="0.9.40", head=HEAD, main=MOVED_MAIN, slot="RELEASE_MAIN", recovery=False,
                                    prepared_path=str(packet.parent / "prepare.out"), args_path="", prepared=doc)

    common.OVERRIDE["release admit prepare"] = prepare_at_moved_main
    again = cut(rt)
    assert fakes.calls.count("admit prepare") == 2  # the old prepare.out (main 1947731c) was not adopted
    assert again["status"] == "review" and again["review"]["step"] == "admission review" and again["review"]["sha256"] != first["review"]["sha256"]
    assert json.loads(Path(again["review"]["file"]).read_text())["main"] == MOVED_MAIN


def test_p2_a_prepare_at_the_same_main_is_adopted(tmp_path):
    rt, fakes = runtime(tmp_path), CutFakes(tmp_path)
    rt.providers.vcs.remote["main"] = MAIN
    packet, sets = release_packet(tmp_path)
    doc = {"head": HEAD, "main": MAIN, "slot": "RELEASE_MAIN"}
    (packet.parent / "prepare.out").write_text(json.dumps(doc))
    (packet.parent / "prepare.args.json").write_text(json.dumps({"schema": 1, "packet": str(packet), "sha256": canonical_sha256(doc)}))
    done = cut(rt, *sets)
    assert "admit prepare" not in fakes.calls and done["review"]["step"] == "admission review"
    assert next(s for s in done["steps"] if s["name"] == "admit prepare")["detail"].endswith("(adopted: already true)")


@pytest.mark.parametrize("result", [
    ReleaseRun(action="resume", version="0.9.40", exit=0),  # exit 0, no receipt read: no verdict
    ReleaseRun(action="resume", version="0.9.40", exit=0, finished=True),  # finished, verdict unset
    ReleaseRun(action="resume", version="0.9.40", exit=0, finished=False, accepted=True),  # accepted but not finished
])
def test_p3_resume_without_an_accepted_verdict_is_not_accepted(tmp_path, result):
    rt, fakes = runtime(tmp_path), CutFakes(tmp_path)
    rt.providers.vcs.remote["main"] = MAIN
    _, sets = release_packet(tmp_path)
    first = cut(rt, *sets)
    verdict(rt, first["review"]["sha256"])
    publish = cut(rt, "--approved", first["review"]["sha256"], "--live")
    verdict(rt, publish["review"]["sha256"])
    fakes.resumes = [result]
    done = cut(rt, "--approved", publish["review"]["sha256"], "--live")
    assert done["status"] == "refused" and "no ACCEPTED verdict" in done["phase"] and "verify" not in fakes.calls
    assert next(s for s in done["steps"] if s["name"] == "resume")["status"] == "open"


# ----------------------------------------------------------------------------- campaign g5: C1
def approved_publish(tmp_path: Path):
    rt, fakes = runtime(tmp_path), CutFakes(tmp_path)
    rt.providers.vcs.remote["main"] = MAIN
    _, sets = release_packet(tmp_path)
    first = cut(rt, *sets)
    verdict(rt, first["review"]["sha256"])
    publish = cut(rt, "--approved", first["review"]["sha256"], "--live")  # the admission is applied: the run is past an external step
    assert publish["review"]["step"] == "publish review"
    verdict(rt, publish["review"]["sha256"])
    return rt, fakes, publish["review"]


def test_c1_inputs_changed_after_the_publish_approval_refuse_and_resume_does_not_run(tmp_path):
    rt, fakes, review = approved_publish(tmp_path)
    plan = Path(review["file"]).read_text()
    fakes.resumes = [ReleaseRun(action="resume", version="0.9.40", exit=0, finished=True, accepted=True)]
    done = cut(rt, "--approved", review["sha256"], "--set", "candidate_gate=yes", "--set", "intro=Something nobody reviewed.", "--live")
    assert done["status"] == "refused" and "approved as" in done["phase"] and "candidate_gate" in done["phase"] and "intro" in done["phase"]
    assert not [c for c in fakes.calls if isinstance(c, tuple) and c[0] == "resume"]
    assert Path(review["file"]).read_text() == plan  # the reviewed plan file is not rewritten
    again = cut(rt, "--approved", review["sha256"], "--set", "candidate_gate=no", "--set", "intro=Plugins update in one click.", "--live")  # the approved values again
    assert again["status"] == "finished"
    assert [c for c in fakes.calls if isinstance(c, tuple) and c[0] == "resume"] == [("resume", "0.9.40", "Plugins update in one click.", True)]


def test_c1_resume_runs_with_the_approved_plan_and_the_plan_may_change_before_its_verdict(tmp_path):
    rt, fakes = runtime(tmp_path), CutFakes(tmp_path)
    rt.providers.vcs.remote["main"] = MAIN
    _, sets = release_packet(tmp_path)
    first = cut(rt, *sets)
    verdict(rt, first["review"]["sha256"])
    publish = cut(rt, "--approved", first["review"]["sha256"], "--live")
    fixed = cut(rt, "--set", "intro=Plugins update in one click, safely.")  # no verdict yet: a new plan, a new digest
    assert fixed["review"]["step"] == "publish review" and fixed["review"]["sha256"] != publish["review"]["sha256"]
    verdict(rt, fixed["review"]["sha256"])
    fakes.resumes = [ReleaseRun(action="resume", version="0.9.40", exit=0, finished=True, accepted=True)]
    assert cut(rt, "--approved", fixed["review"]["sha256"], "--live")["status"] == "finished"
    assert ("resume", "0.9.40", "Plugins update in one click, safely.", True) in fakes.calls


# ----------------------------------------------------------------------------- campaign g3: head-admit CHANGES








