"""`sm head prepare --repair-main`: replay of the 2026-11-13 red main (batch plan trap after a queue re-queue).

PR #803 was admitted at main A (pair: pull_request:<head>, push-tree:<merge-tree(A, head)>). #804 merged first
(main B); the queue then squashed #803 onto B (main C, tree T). C's push CI failed only in "Changed paths / Resolve
the exact reviewed focused plan": no plan for T. The repair admits push-tree:T with base B and nothing else.

Git is real (a tiny local repository with the same shape); GitHub is a FakeGh behind the admission runtime; the
registry codec is a transparent fixture of Core's. No network, no launchd, no real variable.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from steuermaschine.github.testing import FakeGh
from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native.admission_lib import focused_admit as a
from steuermaschine_heinzel.native.admission_lib import focused_slots as s
from steuermaschine_heinzel.native.admission_lib import repair_main as r

REPO = s.hzlib.CORE
RUN, JOB_PATHS, JOB_OK, PR = 43452895000, 131979590277, 131980761147, 803
LOG = ("2026-11-13T15:30:33.9Z Traceback (most recent call last):\n"
       '2026-11-13T15:30:33.9Z   File "/home/runner/work/heinzel/heinzel/scripts/focused_plan.py", line 563, in <module>\n'
       "2026-11-13T15:30:33.9Z scripts.release_candidate.CandidateError: No focused plan for this exact source. "
       "Submit an independently reviewed scope manifest.\n2026-11-13T15:30:34.0Z ##[error]Process completed with exit code 1.\n")
CODEC = '''import json
SLOT_VARIABLES = tuple(f"FOCUSED_CI_PLANS_V2_{i}" for i in range(4))
def registry_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
def plan_registry(legacy, slots):
    return {key: value for text in (legacy, *slots) for key, value in json.loads(text or "{}").items()}
'''


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_AUTHOR_DATE": "2026-11-13T12:00:00Z",
           "GIT_COMMITTER_DATE": "2026-11-13T12:00:00Z", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def history(tmp_path: Path) -> SimpleNamespace:
    """A (#802 release), head (#803), B = #804 on A, C = squash of head onto B."""
    repo = tmp_path / "core"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    i18n = "".join(f"line {n}\n" for n in range(40))
    start = {"web/src/i18n.ts": i18n, "src/heinzel/modules/system/_update.py": "x = 1\n", "README.md": "readme\n",
             "web/src/chat/Look.tsx": "look\n", "src/heinzel/web/chat.py": "chat = 1\n"}
    base = commit(repo, start, "chore(release): v0.9.40 (#802)")
    git(repo, "checkout", "-q", "-b", "feat/update-all")
    head = commit(repo, {"src/heinzel/modules/system/_update.py": "x = 2\n", "README.md": "readme, update\n",
                         "web/src/i18n.ts": i18n.replace("line 3\n", "line 3 update\n")}, "feat(system): one update (#803)")
    git(repo, "checkout", "-q", "main")
    between = commit(repo, {"web/src/chat/Look.tsx": "look 2\n", "web/src/i18n.ts": i18n.replace("line 30\n", "line 30 look\n")},
                     "feat(web): create a Heinzel as a draft (#804)")
    tree = git(repo, "merge-tree", "--write-tree", between, head).splitlines()[0]
    main = git(repo, "commit-tree", tree, "-p", between, "-m", "feat(system): one update (#803)")
    git(repo, "update-ref", "refs/heads/main", main)
    old_tree = git(repo, "merge-tree", "--write-tree", base, head).splitlines()[0]
    return SimpleNamespace(repo=repo, base=base, head=head, between=between, main=main, tree=tree, old_tree=old_tree)


def manifests(h: SimpleNamespace) -> tuple[dict, dict, dict]:
    """The admitted pair at A and the repair packet's resulting-main plan at B (same reviewed scope)."""
    scope = dict(schema=1, mode="pr-scoped", selection={"python": ["tests/unit/modules/system_update_test.py"],
                                                         "web": ["web/src/updates/controller.test.ts"]},
                 case_sha256={"python": "1" * 64, "web": "2" * 64},
                 production={"src/heinzel/modules/system/_update.py": "3" * 40}, config_blob="4" * 40, execution={})
    changed = {"README.md": "M", "src/heinzel/modules/system/_update.py": "M", "web/src/i18n.ts": "M"}
    pr_plan = dict(scope, event="pull_request", ref="refs/pull/*/merge", head=h.head, base=h.base, tree=h.old_tree,
                   changed=changed, diff_sha256="5" * 64)
    old_main = dict(scope, event="push", ref="refs/heads/main", base=h.base, tree=h.old_tree, changed=changed, diff_sha256="5" * 64)
    repair_plan = dict(scope, event="push", ref="refs/heads/main", base=h.between, tree=h.tree, changed=dict(changed),
                       diff_sha256="6" * 64)
    return pr_plan, old_main, repair_plan


def write_packet(tmp_path: Path, h: SimpleNamespace, repair_plan: dict) -> tuple[Path, dict]:
    pr_plan = {**{k: v for k, v in repair_plan.items() if k not in ("event", "ref")},
               "event": "pull_request", "ref": "refs/pull/*/merge", "head": h.head}
    review = tmp_path / "review" / "REVIEW.md"
    review.parent.mkdir(exist_ok=True)
    review.write_text("builder review of the repair packet\n")
    packet = dict(schema=1, manifest=pr_plan, main_manifest=repair_plan, review=dict(
        path="REVIEW.md", sha256=r.digest(review.read_bytes()), head=h.head, base=h.between, tree=h.tree,
        manifest_sha256=r.digest(a.canonical(pr_plan)), main_manifest_sha256=r.digest(a.canonical(repair_plan))))
    path = tmp_path / "review" / "packet.json"
    path.write_text(json.dumps(packet, indent=1))
    return path, packet


class Runtime:
    """The admission IO boundary over a FakeGh: api, pages and run('gh', ...) are all scripted GitHub."""

    def __init__(self, home: Path, core: Path, gh: FakeGh):
        self.home, self.core, self.gh, self.repo = home, core, gh, REPO
        self.cache = home / ".cache"
        self.cache.mkdir(exist_ok=True)
        self.management = home / "management"
        self.held_version_locks = None
        self.values: dict[str, str] = {}
        self.paused = False
        self.events: list[str] = []
        self.writes: list[tuple[str, str]] = []

    def run(self, *args, data=None, check=True, timeout=30):
        result = self.gh.run(list(args))
        if check and result.rc:
            raise a.AdmissionError(f"{args[0]} operation failed; inspect outcome before retry")
        return subprocess.CompletedProcess(args, result.rc, result.out.encode(), result.err.encode())

    def api(self, path):
        return a._json_bytes(self.run("gh", "api", path).stdout, "GitHub response")

    def pages(self, path, key):
        payload = self.api(path)
        return payload[key]

    def variables(self):
        return dict(self.values), {}

    def queue_state(self):
        return None if self.paused else "state = running"

    def set_variable(self, name, value):
        self.events.append("write")
        self.writes.append((name, value))
        self.values[name] = value

    @contextmanager
    def exclusions(self):
        self.events.append("pause")
        self.paused, self.held_version_locks = True, []
        try:
            yield
        finally:
            self.paused, self.held_version_locks = False, None
            self.events.append("restore")


def script_github(gh: FakeGh, h: SimpleNamespace) -> SimpleNamespace:
    """Today's GitHub, as the API answered it for run 43452895000 at f6e3b02b (shapes kept, ids kept)."""
    root = f"repos/{REPO}"
    run = dict(id=RUN, head_sha=h.main, path=".github/workflows/ci.yml", event="push", head_branch="main", status="completed",
               conclusion="failure", run_attempt=1)
    green = [dict(id=131979500000 + n, name=name, status="completed", conclusion=conclusion, run_id=RUN, run_attempt=1,
                  head_sha=h.main, steps=[]) for n, (name, conclusion) in enumerate(
                      [("Secret scan", "success"), ("Focused tests (python)", "skipped"), ("Lint", "skipped")])]
    paths = dict(id=JOB_PATHS, name=r.JOB, status="completed", conclusion="failure", run_id=RUN, run_attempt=1, head_sha=h.main,
                 steps=[dict(name="Set up job", conclusion="success"), dict(name="Recognize the canonical open release candidate", conclusion="success"),
                        dict(name=r.STEP, conclusion="failure"), dict(name="Complete job", conclusion="success")])
    ok = dict(id=JOB_OK, name="CI OK", status="completed", conclusion="failure", run_id=RUN, run_attempt=1, head_sha=h.main,
              steps=[dict(name="Set up job", conclusion="success"), dict(name=r.AGGREGATE_STEP, conclusion="failure"),
                     dict(name="Require successful focused workload and all owning checks", conclusion="skipped")])
    jobs = [*green, paths, ok]
    pull = dict(number=PR, merged=True, merge_commit_sha=h.main, state="closed", merged_at="2026-11-13T15:28:03Z",
                base=dict(ref="main", repo=dict(full_name=REPO)), head=dict(sha=h.head, ref="feat/update-all-0941b"))
    f = SimpleNamespace(run=run, jobs=jobs, paths=paths, ok=ok, pull=pull, log=LOG,
                        commit=dict(sha=h.main, parents=[dict(sha=h.between)]), listed=[dict(number=PR, merge_commit_sha=h.main)],
                        main=h.main)
    gh.on(f"api {root}/git/ref/heads/main", lambda argv: {"object": {"sha": f.main}})
    gh.on(f"api {root}/actions/workflows/ci.yml/runs?branch=main&event=push&per_page=1", lambda argv: {"workflow_runs": [f.run]})
    gh.on(f"api {root}/actions/runs/{RUN}/attempts/1/jobs", lambda argv: {"total_count": len(f.jobs), "jobs": f.jobs})
    gh.on(f"api --allow-escape-sequences {root}/actions/jobs/{JOB_PATHS}/logs", lambda argv: f.log)
    gh.on(f"api {root}/commits/{h.main}/pulls", lambda argv: f.listed)
    gh.on(f"api {root}/commits/{h.main}", lambda argv: f.commit)
    gh.on(f"api {root}/pulls/{PR}", lambda argv: f.pull)
    return f


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("HZ_QUEUE_STATE", "HZ_TRAIN_STATE_DIR", "HZ_RELEASE_DIR"):
        monkeypatch.delenv(name, raising=False)
    h = history(tmp_path)
    git(h.repo, "checkout", "-q", "-f", h.head)
    gh = FakeGh()
    rt = Runtime(tmp_path, h.repo, gh)
    github = script_github(gh, h)
    pr_plan, old_main, repair_plan = manifests(h)
    rt.values = {s.NAMES[0]: a.canonical({f"pull_request:{h.head}": pr_plan, f"push-tree:{h.old_tree}": old_main}).decode(),
                 **{name: "" for name in s.NAMES[1:]}}
    queue = {"items": [dict(key=str(PR), repo=REPO, pr=str(PR), status="merged", merge_sha=h.main[:8], reviewed_head=h.head),
                       dict(key="804", repo=REPO, pr="804", status="merged", merge_sha=h.between[:8], reviewed_head="")]}
    (rt.cache / r.QUEUE_STATE).write_text(json.dumps(queue))
    receipts = rt.cache / r.RECEIPTS
    receipts.mkdir()
    (receipts / f"head-{h.head}-{'4' * 32}.json").write_text(json.dumps(dict(
        schema=1, admission_id="4" * 32, head=h.head, transaction_sha256="9bd6a951" + "0" * 56, packet_sha256="c" * 64,
        registry_changed=True, slot_variable="FOCUSED_CI_PLANS_V2_2")))
    source = tmp_path / "trusted-source"
    (source / "scripts").mkdir(parents=True)
    (source / "scripts/focused_plan.py").write_text(CODEC)
    packet_path, packet = write_packet(tmp_path, h, repair_plan)
    return SimpleNamespace(h=h, gh=gh, rt=rt, github=github, pr_plan=pr_plan, old_main=old_main, plan=repair_plan,
                           queue=queue, receipts=receipts, source=source, packet_path=packet_path, packet=packet)


def prove(c):
    registry = {name: c.rt.values.get(name, "") for name in s.NAMES}
    return r.prove(c.rt, c.packet, c.h.repo, c.source, c.h.main, registry)


def test_todays_case_proves_exactly_the_missing_plan(case):
    proof = prove(case)
    assert proof["mode"] == r.MODE and proof["main"] == case.h.main and proof["base"] == case.h.between
    assert proof["tree"] == case.h.tree and proof["pr"] == PR and proof["head"] == case.h.head
    assert proof["composition"]["admitted_base"] == case.h.base and proof["composition"]["admitted_tree"] == case.h.old_tree
    assert proof["composition"]["overlap"] == ["web/src/i18n.ts"]
    assert proof["live"]["run"] == dict(run=RUN, attempt=1, jobs={r.JOB: JOB_PATHS, "CI OK": JOB_OK},
                                        log_sha256=r.digest(LOG.encode()))
    assert proof["live"]["admissions"] == ["9bd6a951" + "0" * 56]
    assert not case.gh.writes()


@pytest.mark.parametrize("defect", [
    "rerun-attempt", "green-run", "other-sha", "other-failing-job", "extra-failed-step", "incomplete-jobs", "log-message",
    "two-parents", "two-pulls", "not-merged", "other-head", "queue-not-merged", "queue-other-sha", "queue-other-head",
    "no-receipt", "receipt-unwritten", "receipt-resulting-only",
])
def test_live_evidence_refuses_every_other_situation(case, defect):
    f, h = case.github, case.h
    if defect == "rerun-attempt":
        f.run["run_attempt"] = 2
    elif defect == "green-run":
        f.run["conclusion"] = "success"
    elif defect == "other-sha":
        f.run["head_sha"] = h.between
    elif defect == "other-failing-job":
        f.jobs[0]["conclusion"] = "failure"
    elif defect == "extra-failed-step":
        f.paths["steps"][0]["conclusion"] = "failure"
    elif defect == "incomplete-jobs":
        f.jobs[1]["status"] = "in_progress"
    elif defect == "log-message":
        f.log = "##[error]Process completed with exit code 1.\n"
    elif defect == "two-parents":
        f.commit["parents"].append(dict(sha=h.base))
    elif defect == "two-pulls":
        f.listed.append(dict(number=804, merge_commit_sha=h.main))
    elif defect == "not-merged":
        f.pull["merged"] = False
    elif defect == "other-head":
        f.pull["head"]["sha"] = h.between
    elif defect == "queue-not-merged":
        case.queue["items"][0]["status"] = "queued"
    elif defect == "queue-other-sha":
        case.queue["items"][0]["merge_sha"] = h.between[:8]
    elif defect == "queue-other-head":
        case.queue["items"][0]["reviewed_head"] = h.between
    elif defect == "no-receipt":
        for path in case.receipts.iterdir():
            path.unlink()
    elif defect in ("receipt-unwritten", "receipt-resulting-only"):
        path = next(case.receipts.iterdir())
        receipt = json.loads(path.read_text())
        receipt.update(registry_changed=False) if defect == "receipt-unwritten" else receipt.update(resulting_main={"mode": "x"})
        path.write_text(json.dumps(receipt))
    (case.rt.cache / r.QUEUE_STATE).write_text(json.dumps(case.queue))
    with pytest.raises(a.AdmissionError, match="repair-main"):
        prove(case)


@pytest.mark.parametrize("defect", ["tree", "base", "cases", "selection", "changed", "uncovered-overlap", "no-admitted-pr",
                                    "no-admitted-main", "admitted-at-parent"])
def test_composition_refuses_anything_but_the_reviewed_scope_on_mains_tree(case, defect):
    c, h = case, case.h
    registry = json.loads(c.rt.values[s.NAMES[0]])
    if defect == "tree":
        c.plan["tree"] = h.old_tree
    elif defect == "base":
        c.plan["base"] = h.base
    elif defect == "cases":
        c.plan["case_sha256"] = {"python": "7" * 64, "web": "2" * 64}
    elif defect == "selection":
        c.plan["selection"] = {"python": ["tests/unit/other_test.py"], "web": c.plan["selection"]["web"]}
    elif defect == "changed":
        c.plan["changed"] = {"README.md": "M"}
    elif defect == "uncovered-overlap":
        for plan in (c.plan, c.pr_plan, c.old_main):
            plan["selection"] = {"python": plan["selection"]["python"]}
            plan["case_sha256"] = {"python": "1" * 64}
        registry = {f"pull_request:{h.head}": c.pr_plan, f"push-tree:{h.old_tree}": c.old_main}
    elif defect == "no-admitted-pr":
        registry.pop(f"pull_request:{h.head}")
    elif defect == "no-admitted-main":
        registry.pop(f"push-tree:{h.old_tree}")
    elif defect == "admitted-at-parent":
        tree = git(h.repo, "merge-tree", "--write-tree", h.between, h.head).splitlines()[0]
        registry = {f"pull_request:{h.head}": dict(c.pr_plan, base=h.between, tree=tree),
                    f"push-tree:{tree}": dict(c.old_main, base=h.between, tree=tree)}
    c.rt.values[s.NAMES[0]] = a.canonical(registry).decode()
    with pytest.raises(a.AdmissionError, match="repair-main"):
        prove(c)


def test_coverage_rule_names_what_counts():
    plan = {"selection": {"python": ["tests/x_test.py"], "web": ["web/src/a.test.ts"]}, "production": {"src/heinzel/a.py": "1"}}
    assert r.covered("src/heinzel/a.py", plan) and r.covered("tests/x_test.py", plan) and r.covered("web/src/i18n.ts", plan)
    assert not r.covered("src/heinzel/b.py", plan) and not r.covered("README.md", plan)
    assert not r.covered("web/src/i18n.ts", {"selection": {"python": []}})


@pytest.mark.parametrize("defect", ["none", "extra-key", "altered", "already-present"])
def test_append_proof_allows_exactly_the_repair_plan(case, defect):
    before = {name: case.rt.values.get(name, "") for name in s.NAMES}
    additions = {f"push-tree:{case.h.tree}": case.plan}
    if defect == "extra-key":
        additions["push-tree:" + "e" * 40] = case.plan
    elif defect == "altered":
        additions[f"push-tree:{case.h.tree}"] = dict(case.plan, tree="e" * 40)
    elif defect == "already-present":
        before[s.NAMES[2]] = a.canonical({f"push-tree:{case.h.tree}": case.plan}).decode()
    after = dict(before, **{s.NAMES[1]: a.canonical(additions).decode()})
    if defect == "none":
        r.append_proof(case.source, case.packet, before, after)
    else:
        with pytest.raises(a.AdmissionError, match="one-entry append"):
            r.append_proof(case.source, case.packet, before, after)


# ----------------------------------------------------------------------------- prepare and apply, through ops
@pytest.fixture
def wired(case, monkeypatch):
    c = case
    workspace = c.rt.home / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(s, "Runtime", lambda core, home=None: c.rt)
    monkeypatch.setattr(a, "_read_main", lambda repo: c.github.main)
    monkeypatch.setattr(a, "_prepare_core_snapshot", lambda core, base: (workspace, c.h.repo))
    monkeypatch.setattr(a, "_canonical_source", lambda core, base: (c.source, "2" * 40, REPO, s.NAMES[0], ()))
    monkeypatch.setattr(s, "reader_contract", lambda *args: None)
    monkeypatch.setattr(s.shutil, "rmtree", lambda *args, **kwargs: None)
    composed = []

    def canonical(packet, repo, base, repository, variable, files, archive, **options):
        assert packet["manifest"] == c.plan and options == {"resulting_main": True, "protected_producer": False}
        composed.append(packet["manifest"]["tree"])

    monkeypatch.setattr(a, "validate_canonical", canonical)
    monkeypatch.setattr(a, "_isolated_slot_append", lambda source, legacy, slots, additions: {
        "result": [0, a.canonical(additions).decode()]})
    monkeypatch.setattr(s, "check_consumers", lambda *args, **kwargs: None)
    monkeypatch.setattr(s, "check_budget", lambda *args, **kwargs: None)
    return SimpleNamespace(**vars(c), composed=composed)










def test_apply_slots_refuses_mixed_singleton_proofs(tmp_path):
    with pytest.raises(a.AdmissionError, match="conflicting singleton"):
        a.apply_slots([{}], tmp_path, {}, dict, lambda *x: None, {}, tmp_path / "l", tmp_path, approved_candidate_sha256="0" * 64,
                      check_fresh=lambda: None, resulting_main={"mode": "x"}, repair_main={"mode": r.MODE})


def test_receipt_for_skips_foreign_and_unreadable_receipts(tmp_path):
    assert r.receipt_for(tmp_path, RUN, "a" * 40, {}) is None
    root = statedir.root() / r.RECEIPTS
    root.mkdir(parents=True)
    (root / "head-x-1.json").write_text("not json")
    (root / "head-y-1.json").write_text(json.dumps({"head": "y", "registry_changed": True}))
    assert r.receipt_for(tmp_path, RUN, "a" * 40, {}) is None
    proof = copy.deepcopy({"mode": r.MODE, "main": "a" * 40, "live": {"run": {"run": RUN, "jobs": {"x": 1}}}})
    (root / "head-z-1.json").write_text(json.dumps({"repair_main": proof, "registry_changed": True, "transaction_sha256": "t"}))
    assert r.receipt_for(tmp_path, RUN, "a" * 40, {"x": 1})["transaction_sha256"] == "t"
