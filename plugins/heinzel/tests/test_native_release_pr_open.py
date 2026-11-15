"""`sm release pr VERSION --head SHA`: push the reviewed prepared release branch through the run lock with the
release-only hook selectors and open its PR with the checked title and body; never merge, never delete a branch."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel.native import release as area
from steuermaschine_heinzel.native.queue_lib import edge, hzlib
from steuermaschine_heinzel.plugin import plugin

HEAD = "b" * 40
TAG = "v0.9.41"
BRANCH = f"refs/heads/release/{TAG}"


@pytest.fixture
def rel(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HZ_RELEASE_FLOW_DIR", str(tmp_path / "flow"))
    monkeypatch.setenv("HZ_RELEASE_DIR", str(tmp_path / "work"))
    (tmp_path / "work/scripts").mkdir(parents=True)
    (tmp_path / "work/scripts/release_notes.py").write_text("")
    monkeypatch.setattr(hzlib, "_gh_process", lambda *a, **_: subprocess.CompletedProcess(a, 1, "", ""))
    module = edge.fresh(edge.module_of("hz-release"))
    world = {"tag": False, "pr": None, "pushed": False, "notes": "## v0.9.41\n\nFaster start.\n", "view": None, "calls": []}
    monkeypatch.setattr(module, "has_tag", lambda repo, tag: world["tag"])
    monkeypatch.setattr(module, "release_pr", lambda tag: world["pr"])
    monkeypatch.setattr(module, "verify_release_pr_scope", lambda work, tag: HEAD)
    monkeypatch.setattr(module.release_notes_audit, "audit", lambda work, base: [])

    def run(command, **kwargs):
        world["calls"].append((command, kwargs))
        if command[0] == "git" and "ls-remote" in command:
            return SimpleNamespace(returncode=0, stdout=f"{HEAD}\t{BRANCH}\n" if world["pushed"] else "", stderr="")
        if command[0] == "git" and "--abbrev-ref" in command:
            return SimpleNamespace(returncode=0, stdout=f"release/{TAG}\n", stderr="")
        if command[0] == "git" and "rev-parse" in command:
            return SimpleNamespace(returncode=0, stdout=HEAD + "\n", stderr="")
        if command[0] == "git":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "scripts/release_notes.py" in command:
            return SimpleNamespace(returncode=0, stdout=world["notes"], stderr="")
        if "push" in command:
            world["pushed"] = True
        return SimpleNamespace(returncode=0, stdout="https://github.com/example/heinzel/pull/820\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    opened = {"number": 820, "state": "OPEN", "title": f"chore(release): {TAG}", "baseRefName": "main", "headRefOid": HEAD,
              "autoMergeRequest": None, "body": world["notes"]}
    world["view"] = opened
    monkeypatch.setattr(module.hzlib, "ghj", lambda *a, **k: world["view"] if a[:2] == ("pr", "view") else pytest.fail(str(a)))
    said = []
    monkeypatch.setattr(module.hzlib, "say", lambda *a, **k: said.append(a[0]))
    module.world, module.said = world, said
    return module


def commands(rel):
    return [command for command, _ in rel.world["calls"]]


def test_the_reviewed_branch_is_pushed_under_the_run_lock_with_release_selectors_and_the_pr_opened(rel, tmp_path):
    rel.remember(version="0.9.41")
    assert rel.main(["0.9.41", "--open-pr", "--reviewed-head", HEAD]) == 0
    gate = rel.edge.gate()
    push = next((command, kwargs) for command, kwargs in rel.world["calls"] if command[:len(gate) + 1] == [*gate, "git"])
    assert push[0] == [*gate, "git", "push", "--quiet", "origin", f"{HEAD}:{BRANCH}"]
    assert {name: push[1]["env"][name] for name in rel.RELEASE_PR_PUSH_ENV} == rel.RELEASE_PR_PUSH_ENV
    create = next(command for command in commands(rel) if command[:3] == ["gh", "pr", "create"])
    assert create[create.index("--title") + 1] == f"chore(release): {TAG}" and "--body-file" in create
    words = [part for command in commands(rel) for part in command if not part.startswith("/")]  # a checkout path may say merge
    assert not any("merge" in part for part in words)  # never merges, never enables auto-merge
    assert not any("--delete" in command or "-D" in command for command in commands(rel))  # never deletes a branch
    assert rel.state()["release_pr_opened"]["pr"] == 820 and rel.state()["release_pr_opened"]["head"] == HEAD
    assert any(line.startswith(f"OPENED release PR #820 \"chore(release): {TAG}\" at {HEAD}") for line in rel.said)


def test_a_dry_run_checks_everything_and_pushes_nothing(rel):
    assert rel.main(["0.9.41", "--open-pr", "--reviewed-head", HEAD, "--dry-run"]) == 0
    assert not any("push" in command or command[:2] == ["gh", "pr"] for command in commands(rel))
    assert any(line.startswith("DRY RUN: checks passed.") for line in rel.said)


@pytest.mark.parametrize("change, why", [
    ({"tag": True}, "already tagged"),
    ({"pr": {"number": 794, "state": "CLOSED"}}, "release PR #794 for v0.9.41 already exists (CLOSED)"),
    ({"notes": "  \n"}, "release notes are empty"),
])
def test_a_tag_an_existing_pr_or_empty_notes_stop_before_any_push(rel, capsys, change, why):
    rel.world.update(change)
    assert rel.main(["0.9.41", "--open-pr", "--reviewed-head", HEAD]) == 1
    assert why in capsys.readouterr().err
    assert not any("push" in command for command in commands(rel))


def test_another_head_than_the_reviewed_one_stops_before_any_push(rel, capsys):
    assert rel.main(["0.9.41", "--open-pr", "--reviewed-head", "c" * 40]) == 1
    assert f"release head {HEAD} is not the reviewed head" in capsys.readouterr().err
    assert not any("push" in command for command in commands(rel))


def test_another_unfinished_release_in_the_receipt_is_preserved(rel, capsys):
    rel.remember(version="0.9.40")
    assert rel.main(["0.9.41", "--open-pr", "--reviewed-head", HEAD]) == 1
    assert "belongs to the unfinished release 0.9.40" in capsys.readouterr().err and rel.state() == {"version": "0.9.40"}


@pytest.mark.parametrize("change", [{"autoMergeRequest": {"enabledAt": "now"}}, {"title": "release"}, {"headRefOid": "c" * 40}, {"body": ""}, {"state": "MERGED"}])
def test_an_opened_pr_that_is_not_the_checked_one_stops_and_is_preserved(rel, capsys, change):
    rel.world["view"] = rel.world["view"] | change
    assert rel.main(["0.9.41", "--open-pr", "--reviewed-head", HEAD]) == 1
    assert "is not the checked one" in capsys.readouterr().err
    assert not any(command[:3] in (["gh", "pr", "close"], ["gh", "pr", "merge"]) for command in commands(rel))


@pytest.mark.parametrize("argv", [["0.9.41", "--open-pr"], ["patch", "--open-pr", "--reviewed-head", HEAD], ["0.9.41", "--open-pr", "--reviewed-head", HEAD, "--intro", "x"],
                                  ["0.9.41", "--dry-run"]])
def test_the_runner_refuses_an_incomplete_or_mixed_open_pr(rel, argv):
    with pytest.raises(SystemExit) as stop:
        rel.main(argv)
    assert stop.value.code == 2


def make(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {"release_dir": str(tmp_path / "work")}}, Path("."))
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=True), stack, settings,
                   _providers=fake_providers(commands=FakeGh()))


def test_the_native_command_passes_the_reviewed_head_and_dry_run(tmp_path, monkeypatch):
    rt = make(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(area, "run", lambda module, argv: seen.append(argv) or (0, [], []))
    out = invoke(rt, ["--format", "json", "release", "pr", "0.9.41", "--head", HEAD, "--dry-run"])
    assert out.exit_code == 0, out
    assert seen == [["0.9.41", "--open-pr", "--reviewed-head", HEAD, "--dry-run"]] and json.loads(out.stdout)["action"] == "pr"
    bad = invoke(rt, ["release", "pr", "patch", "--head", HEAD])
    assert bad.exit_code == 2 and len(seen) == 1


def test_the_native_command_is_a_write_that_reaches_the_release_and_queue_libraries():
    from steuermaschine.reviewgraph import reach
    from steuermaschine_heinzel import native

    cmd = next(c for c in native.commands() if getattr(c, "path", "") == "release pr")
    names = {name.removeprefix("steuermaschine_heinzel/native/") for name in reach([cmd])[0].files}
    assert cmd.writes and {"release.py", "release_lib/runner.py", "queue_lib/hzlib.py"} <= names
    assert os.path.basename(area.__file__) == "release.py"


def test_a_rejected_release_is_refused_before_the_pr_is_opened(rel, capsys, monkeypatch):
    def rejected(version):
        raise rel.release_rejection.InvalidRejection("rejected release version cannot be resumed or dispatched")

    monkeypatch.setattr(rel.release_rejection, "guard", rejected)
    assert rel.main(["0.9.41", "--open-pr", "--reviewed-head", HEAD]) == 1
    assert "release rejection history blocks dispatch" in capsys.readouterr().err
    assert not rel.world["calls"]
