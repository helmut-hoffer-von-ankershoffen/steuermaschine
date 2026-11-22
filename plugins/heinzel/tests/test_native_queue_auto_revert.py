"""The auto-revert of a red Core main (queue_lib/auto_revert.py), against a fake queue module, `gh` and `git`."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel.native.queue_lib import auto_revert, edge, hzlib

CORE = hzlib.CORE
RED, GREEN = "a" * 40, "b" * 40
EMAIL = "1826050+owner@users.noreply.github.com"


def run(sha, conclusion, rid, attempt=1):
    return {"status": "completed", "conclusion": conclusion, "databaseId": rid, "headSha": sha, "attempt": attempt, "displayTitle": "x"}


class Queue(SimpleNamespace):
    def __init__(self, jobs_known=False):
        state = {"items": []}
        super().__init__(state=state, logs=[], known=jobs_known)

    def ci_workflow(self):
        return "ci.yml"

    def valid_main_run(self, row):
        return True

    def attempt_jobs(self, rid, attempt):
        return [{"name": "Test", "status": "completed", "conclusion": "failure"}]

    def main_attempt_failures(self, rid, sha, jobs):
        return ["Test"], {"Test": "bead-x" if self.known else None}, ""

    def load(self):
        return json.loads(json.dumps(self.state))

    def save(self, data):
        self.state.clear()
        self.state.update(data)

    def update(self, **fields):
        self.state.update(fields)

    def log(self, text):
        self.logs.append(text)

    def state_lock(self):
        import contextlib

        return contextlib.nullcontext()


@pytest.fixture
def world(monkeypatch):
    calls, gits, alarms = [], [], []
    answers = {
        "run list": [run(RED, "failure", 2), run(GREEN, "success", 1)],
        f"repos/{CORE}/commits/{RED}": {"commit": {"message": "feat(web): a change (#801)\n\nbody", "author": {"name": "owner", "email": EMAIL}}},
        "pr view 801": {"headRefName": "feat/x", "mergeCommit": {"oid": RED}},
        "pr list": [],
        "pr create": "https://github.com/x/y/pull/950\n",
        "pr view 950": {"headRefOid": "c" * 40},
    }

    def gh(*args):
        line = " ".join(args)
        calls.append(line)
        hit = next((answers[k] for k in sorted(answers, key=len, reverse=True) if k in line), None)
        if hit is None:
            return 1, ""
        return 0, hit if isinstance(hit, str) else json.dumps(hit)

    def fake_run(argv, *a, **kw):
        gits.append((argv, kw.get("env") or {}))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(hzlib, "gh", gh)
    monkeypatch.setattr(hzlib, "note", lambda text: None)
    monkeypatch.setattr(edge, "run", fake_run)
    monkeypatch.setattr(edge, "raise_alarm", lambda key, level, text, source="queue": alarms.append((key, level, text)))
    monkeypatch.setattr(auto_revert.main_lanes, "split_runs", lambda rows: (rows, []))
    return SimpleNamespace(calls=calls, gits=gits, alarms=alarms, answers=answers)


def test_main_turned_red_on_a_merge_with_an_unknown_failure_opens_one_revert_pr_as_the_merge_author(world):
    q = Queue()
    text = auto_revert.check(q)
    assert "revert PR #950 opened" in text
    creates = [c for c in world.calls if c.startswith("pr create")]
    assert creates and "--head revert/pr-801" in creates[0] and "--title revert: feat(web): a change (#801)" in creates[0]
    argvs = [" ".join(argv) for argv, _ in world.gits]
    assert any(f"revert --no-commit {RED}" in a for a in argvs)
    push = next(a for a in argvs if " push " in a)
    assert push.endswith("push origin HEAD:refs/heads/revert/pr-801") and "--force" not in push and "-f " not in push
    assert all("--no-verify" not in a for a in argvs)
    commit_env = next(env for argv, env in world.gits if "commit" in argv)
    assert commit_env["GIT_AUTHOR_EMAIL"] == commit_env["GIT_COMMITTER_EMAIL"] == EMAIL
    row = q.state["items"][-1]
    assert (row["key"], row["status"], row["guarded_head"]) == ("950", "queued", "c" * 40)  # the queue arms auto-merge on it
    assert world.alarms and world.alarms[0][:2] == ("main-red:2", "ALERT")
    assert auto_revert.check(q) == "" and len([c for c in world.calls if c.startswith("pr create")]) == 1  # never twice per run


def test_a_known_flake_is_not_reverted(world):
    assert auto_revert.check(Queue(jobs_known=True)) == "" and not world.gits and not world.alarms


def test_main_red_before_the_commit_is_not_reverted(world):
    world.answers["run list"] = [run(RED, "failure", 2), run(GREEN, "failure", 1)]
    assert auto_revert.check(Queue()) == "" and not world.gits


@pytest.mark.parametrize(("change", "why"), [
    ({"message": "release: 0.9.56 (#801)"}, "release or revert commit"),
    ({"message": "revert: feat: x (#801)"}, "release or revert commit"),
    ({"message": "Merge branch main"}, "not the squash merge of a PR"),
    ({"email": "someone@example.com"}, "noreply"),
    ({"head": "release/0.9.56"}, "release PR"),
])
def test_what_the_tool_never_reverts_raises_only_the_alarm(world, change, why):
    commit = world.answers[f"repos/{CORE}/commits/{RED}"]["commit"]
    commit["message"] = change.get("message", commit["message"])
    commit["author"]["email"] = change.get("email", EMAIL)
    if "head" in change:
        world.answers["pr view 801"]["headRefName"] = change["head"]
    text = auto_revert.check(Queue())
    assert "no auto-revert" in text and why in text and not world.gits and world.alarms


def test_an_existing_revert_branch_pr_is_not_opened_again(world):
    world.answers["pr list"] = [{"number": 950}]
    assert "has a PR already" in auto_revert.check(Queue()) and not world.gits


def test_the_subject_keeps_the_72_character_rule():
    subject = auto_revert.subject_of("feat(web): " + "x" * 90, "801")
    assert len(subject) == 72 and subject.startswith("revert: feat(web): ") and subject.endswith("... (#801)")
