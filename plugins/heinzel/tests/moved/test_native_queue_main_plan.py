"""Bead q9kh: the queue never merges a core PR whose squash onto CURRENT main lands a tree without a focused plan.

Replay of 2026-11-13: #803 (update-all) and #804 (web-wave-3) were both admitted at main A (v0.9.40). #804 merged
first (main B). #803's plan is for merge-tree(A, #803); its squash onto B is another tree, without a plan. The queue
merged it and main went red. Now: the tick refuses #803 before `gh pr merge`, `queue add` refuses it, `queue why`
names it with the re-prepare command. Git is real (a local repository of the same shape); GitHub and the registry
codec are scripted.
"""

import json
import os
import subprocess

import pytest
from test_native_queue_watch_queue import add, lib, queue, setup, view  # noqa: F401  (fixtures and helpers)

from steuermaschine_heinzel.native.queue_lib import main_plan as plan_module


def git(repo, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_AUTHOR_DATE": "2026-11-13T12:00:00Z",
           "GIT_COMMITTER_DATE": "2026-11-13T12:00:00Z", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, files, message):
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path, real_main_plan, monkeypatch):
    """origin: A, #803 and #804 heads; main moves A -> B (#804) as the test says. core: a bare clone (the setting)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    i18n = "".join(f"line {n}\n" for n in range(40))
    a = commit(origin, {"web/src/i18n.ts": i18n, "src/heinzel/modules/system/_update.py": "x = 1\n"}, "chore(release): v0.9.40 (#802)")
    git(origin, "checkout", "-q", "-b", "update-all")
    h803 = commit(origin, {"src/heinzel/modules/system/_update.py": "x = 2\n",
                           "web/src/i18n.ts": i18n.replace("line 3\n", "line 3 update\n")}, "feat(system): one update (#803)")
    git(origin, "checkout", "-q", "-b", "web-wave-3", a)
    h804 = commit(origin, {"web/src/i18n.ts": i18n.replace("line 30\n", "line 30 look\n")}, "feat(web): draft (#804)")
    git(origin, "checkout", "-q", "main")
    tree_804 = git(origin, "merge-tree", "--write-tree", a, h804).splitlines()[0]
    b = git(origin, "commit-tree", tree_804, "-p", a, "-m", "feat(web): draft (#804)")
    git(origin, "update-ref", "refs/pull/803/head", h803)
    git(origin, "update-ref", "refs/pull/804/head", h804)
    core = tmp_path / "core.git"
    git(tmp_path, "clone", "-q", "--bare", str(origin), str(core))
    real_merge_tree = plan_module.merge_tree
    monkeypatch.setattr(plan_module, "merge_tree", lambda core_, pr, main, head: real_merge_tree(core_, pr, main, head, origin=str(origin)))
    planned_a = git(origin, "merge-tree", "--write-tree", a, h803).splitlines()[0]
    # the admitted pairs at A: #803 and #804, each with its resulting-main plan on A
    registry = {f"push-tree:{planned_a}": {"base": a}, f"push-tree:{tree_804}": {"base": a}}
    monkeypatch.setattr(plan_module, "registry", lambda core_, main: dict(registry))
    plan_module.configure(core)
    state = {"main": a}

    def move_main(to):
        git(origin, "update-ref", "refs/heads/main", to)
        state["main"] = to

    yield type("World", (), dict(origin=origin, core=core, a=a, b=b, h803=h803, h804=h804, registry=registry,
                                 state=state, move_main=move_main, planned_a=planned_a))
    plan_module.configure(None)


def answer(world, fallback=None):
    def ghj(*args, default=None):
        if args[:2] == ("pr", "view") and "headRefOid" in args:
            return {"headRefOid": world.h803 if args[2] == "803" else world.h804}
        if args[:2] == ("api", f"repos/{plan_module.hzlib.CORE}/git/ref/heads/main"):
            return {"object": {"sha": world.state["main"]}}
        return fallback(*args, default=default) if fallback else default
    return ghj


def test_merge_tree_is_the_squash_tree_on_current_main(world):
    tree = plan_module.merge_tree(world.core, "803", world.a, world.h803)
    assert tree == world.planned_a
    world.move_main(world.b)
    assert plan_module.merge_tree(world.core, "803", world.b, world.h803) != world.planned_a
    with pytest.raises(ValueError, match="main moved"):
        plan_module.merge_tree(world.core, "803", world.a, world.h803)


def test_gap_is_empty_at_the_admitted_main_and_names_the_trap_after_another_merge(world, monkeypatch):
    monkeypatch.setattr(plan_module.hzlib, "ghj", answer(world))
    assert plan_module.gap("803") == ""
    world.move_main(world.b)
    reason = plan_module.gap("803")
    assert reason.startswith("stale main plan: no focused plan for tree ") and f"on main {world.b[:8]}" in reason
    assert "Next: re-prepare #803 at main" in reason and "sm head prepare PACKET --pr 803" in reason
    world.registry[f"push-tree:{plan_module.merge_tree(world.core, '803', world.b, world.h803)}"] = {"base": world.b}
    assert plan_module.gap("803") == ""  # re-prepared at B: planned again


@pytest.mark.parametrize("defect", ["no-setting", "registry", "head", "other-base"])
def test_gap_fails_closed(world, monkeypatch, defect):
    monkeypatch.setattr(plan_module.hzlib, "ghj", answer(world))
    if defect == "no-setting":
        plan_module.configure(None)
    elif defect == "registry":
        monkeypatch.setattr(plan_module, "registry", lambda core, main: (_ for _ in ()).throw(ValueError("reader refused")))
    elif defect == "head":
        monkeypatch.setattr(plan_module.hzlib, "ghj", lambda *a, default=None: default)
    elif defect == "other-base":
        world.registry[f"push-tree:{world.planned_a}"] = {"base": world.b}
    reason = plan_module.gap("803")
    assert reason.startswith("stale main plan")
    if defect == "no-setting":
        assert "queue_core_repo" in reason


def test_todays_sequence_the_tick_merges_804_and_refuses_803(queue, lib, monkeypatch, world):  # noqa: F811
    setup(monkeypatch, lib, queue, [view()] * 2)
    monkeypatch.setattr(queue, "site_only", lambda item: False)
    monkeypatch.setattr(plan_module.hzlib, "ghj", answer(world, plan_module.hzlib.ghj))
    add(queue, "804", "803")
    train = lib.load("hz-merge-train")
    merged = []

    def fake_train(repo, pr, ignore_freeze=False, queue_core=False, expected_head="", pre_merge_guard=None):
        if pre_merge_guard is not None and not pre_merge_guard():
            return False, "main evidence changed or unavailable after waits; reassessment required"
        merged.append(pr)
        if pr == "804":
            world.move_main(world.b)  # the squash of #804 is main B
        return True, "abc12345"

    monkeypatch.setattr(train, "train", fake_train)
    queue.tick(now=10_000)
    items = {i["key"]: i for i in queue.load()["items"]}
    assert merged == ["804"], items
    assert items["804"]["status"] == "merged"
    assert items["803"]["status"] == "needs work" and items["803"]["detail"].startswith("stale main plan: no focused plan")


def test_queue_add_refuses_a_core_pr_whose_merge_has_no_plan(queue, lib, monkeypatch, world, capsys):  # noqa: F811
    setup(monkeypatch, lib, queue, [view()])
    world.move_main(world.b)
    monkeypatch.setattr(plan_module.hzlib, "ghj", answer(world, plan_module.hzlib.ghj))
    assert queue.main(["add", "803"]) == 1
    err = capsys.readouterr().err
    assert "refused 803: stale main plan: no focused plan" in err and "sm head prepare PACKET --pr 803" in err
    assert not queue.load()["items"]


def test_queue_why_names_the_stale_plan_and_the_next_command(queue, lib, monkeypatch, world, capsys):  # noqa: F811
    setup(monkeypatch, lib, queue, [view()])
    monkeypatch.setattr(plan_module.hzlib, "ghj", answer(world, plan_module.hzlib.ghj))
    assert queue.main(["add", "803"]) == 0  # planned at A
    world.move_main(world.b)
    for name in ("later_main_passes", "rerun_restoration"):
        monkeypatch.setattr(queue, name, lambda: {} if name == "rerun_restoration" else False)
    monkeypatch.setattr(queue, "main_rerun_plan", lambda: ("none", "main is green", {}))
    capsys.readouterr()
    queue.why()
    out = capsys.readouterr().out
    assert "item 803: stale main plan: no focused plan" in out
    verdict = [line for line in out.splitlines() if line.startswith("verdict:")]
    assert verdict and "#803 stale main plan" in verdict[0] and "re-prepare #803" in verdict[0]


def test_registry_reads_core_codec_through_the_protected_reader(monkeypatch, real_main_plan, tmp_path):
    seen = {}
    monkeypatch.setattr(plan_module.main_checkpoint, "transports", lambda: {
        "FOCUSED_CI_PLANS": "{}", **{f"FOCUSED_CI_PLANS_V2_{i}": f"s{i}" for i in range(4)}})
    monkeypatch.setattr(plan_module.main_checkpoint, "isolated_reader", lambda request: seen.update(request) or {"k": 1})
    assert plan_module.registry(tmp_path, "a" * 40) == {"k": 1}
    assert seen == {"mode": "registry", "core_repo": str(tmp_path), "reader_head": "a" * 40, "legacy": "{}",
                    "slots": ["s0", "s1", "s2", "s3"]}
    assert json.dumps(seen)  # the request is plain JSON for the isolated reader
