"""`sm ship` finds everything it needs BEFORE the first check: the repository, the bead and the title, and a branch behind main is
merged by sm itself. One scenario per stall of 0.9.55: the bead asked after the check, "behind" found after the check, the
public repository shipped by hand, a branch holding a merge commit. A scripted machine answers git; nothing real runs."""

from __future__ import annotations

from typing import Any

import pytest
from test_native_ship_driver import FakeEngine, clean_overrides, worktree  # noqa: F401  (clean_overrides is an autouse fixture)
from test_pipeline_head_admit import native, runtime

from steuermaschine.engine import Refuse
from steuermaschine.protocols import Result
from steuermaschine_heinzel.native import ship_driver
from steuermaschine_heinzel.native.ship_lib import prepare

OWNER = "helmut-hoffer-von-ankershoffen"
ORIGINS = {"core": "o/core", "plugins": "heinzel-plugins", "steuermaschine": "steuermaschine", "management": "heinzel-management"}
SEP, END = "\x1f", "\x1e"


class Git:
    """A scripted git: a rule is a needle in the command line and its answer; every call is recorded."""

    def __init__(self, repo="o/core", branch="fix/thing", behind=False, dirty="", log=None, conflict=False, fetch_fails=False):
        self.calls: list[list[str]] = []
        self.merged = False
        self.repo, self.branch, self.behind, self.dirty, self.conflict, self.fetch_fails = repo, branch, behind, dirty, conflict, fetch_fails
        self.log = log if log is not None else [("fix(web): keep the draft", "")]

    def run(self, argv, cwd=None, timeout=None, env=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        line = " ".join(argv)
        if "remote get-url origin" in line:
            slug = self.repo if "/" in self.repo else f"{OWNER}/{self.repo}"
            return Result(0, f"git@github.com:{slug}.git\n", "")
        if "rev-parse --abbrev-ref HEAD" in line:
            return Result(0, self.branch + "\n", "")
        if " fetch " in line:
            return Result(1, "", "offline") if self.fetch_fails else Result(0, "", "")
        if "--is-ancestor" in line:
            return Result(1 if self.behind and not self.merged else 0, "", "")
        if "status --porcelain" in line:
            return Result(0, self.dirty, "")
        if " merge --abort" in line:
            return Result(0, "", "")
        if " merge " in line:
            if self.conflict:
                return Result(1, "CONFLICT (content): Merge conflict in src/a.py\n", "")
            self.merged = True
            return Result(0, "Merge made\n", "")
        if "--diff-filter=U" in line:
            return Result(0, "src/a.py\nsrc/b.py\n", "")
        if " log " in line:
            return Result(0, "".join(f"{s}{SEP}{b}{END}" for s, b in self.log), "")
        return Result(1, "", f"no scripted answer: {line}")

    def ran(self, needle):
        return [c for c in self.calls if needle in " ".join(c)]


def drive(tmp_path, monkeypatch, git, set_=None, live=True, engine=None):
    rt = runtime(tmp_path, gh=git)
    fake = engine or FakeEngine()
    monkeypatch.setattr(ship_driver, "engine", lambda: fake)
    with native(rt):
        return fake, ship_driver.ship(worktree(tmp_path), live=live, approved="", author="", set_=set_, wait=None)


# ----------------------------------------------------------------------------- the bead and the title are found before any check
def test_a_missing_title_is_one_refusal_naming_the_exact_set_and_nothing_was_checked(tmp_path, monkeypatch):
    git = Git(log=[("wip stuff", ""), ("more", "")])
    fake = FakeEngine()
    with pytest.raises(Refuse) as stop:
        drive(tmp_path, monkeypatch, git, engine=fake)
    assert not fake.calls  # the pipeline (and so its check) never started
    assert 'sm ship ' in str(stop.value) and '--set title="type(scope): subject"' in str(stop.value) and "no title" in str(stop.value)
    assert stop.value.code == 2 and "before any check" in str(stop.value)


def test_every_missing_input_is_listed_in_one_refusal(tmp_path, monkeypatch):
    git = Git(repo="somebody-elses", branch="main", log=[("wip", "")])
    with pytest.raises(Refuse) as stop:
        drive(tmp_path, monkeypatch, git)
    text = str(stop.value)
    assert "1. " in text and "2. " in text and "3. " in text  # the origin, the branch and the title, together
    assert "none of the repositories sm ships" in text and "not a work branch" in text and "no title" in text


def test_the_title_is_the_newest_conventional_subject_and_the_bead_comes_from_a_trailer(tmp_path, monkeypatch):
    git = Git(log=[("fix(ship): resolve inputs first", "Why.\n\nBead: heinzel-management-ab12c\n"), ("older", "")])
    fake, result = drive(tmp_path, monkeypatch, git)
    (_, _, opts), = fake.calls
    assert opts.inputs == {"bead": "heinzel-management-ab12c", "title": "fix(ship): resolve inputs first"}
    assert any("bead=heinzel-management-ab12c (from a trailer of the commits)" in d for d in result.derived) and any("title=" in d for d in result.derived)
    assert "DERIVED" in "\n".join(result.render())


@pytest.mark.parametrize("branch,bead", [("fix/bead-q7x2k-stall", "q7x2k"), ("feat/heinzel-management-zz9aa", "heinzel-management-zz9aa"), ("fix/plain", "none")])
def test_the_bead_comes_from_the_branch_name_else_the_plain_default(tmp_path, monkeypatch, branch, bead):
    fake, _ = drive(tmp_path, monkeypatch, Git(branch=branch))
    assert fake.calls[0][2].inputs["bead"] == bead


def test_a_set_input_beats_what_is_derived(tmp_path, monkeypatch):
    fake, result = drive(tmp_path, monkeypatch, Git(), set_=["bead=abc", "title=fix: mine"])
    assert fake.calls[0][2].inputs == {"bead": "abc", "title": "fix: mine"} and not any(d.startswith("title=") for d in result.derived)


def test_a_title_over_72_characters_is_refused_up_front(tmp_path, monkeypatch):
    with pytest.raises(Refuse, match="the title is 80 characters, the limit is 72"):
        drive(tmp_path, monkeypatch, Git(), set_=["title=fix: " + "x" * 75])


# ----------------------------------------------------------------------------- behind main: sm merges it, before the check
def test_a_branch_behind_main_gets_main_merged_by_sm_with_a_conventional_subject_before_the_pipeline(tmp_path, monkeypatch):
    git = Git(behind=True)
    fake, result = drive(tmp_path, monkeypatch, git)
    merge = git.ran(" merge --no-edit")[0]
    assert merge[-3:] == ["-m", "chore: merge origin/main", "origin/main"] and prepare.CONVENTIONAL.fullmatch("chore: merge origin/main")
    assert fake.calls and "merged origin/main into the branch (it was behind)" in result.derived  # the pipeline ran after the merge
    assert not git.ran("--force") and not git.ran("rebase") and not git.ran(" push")


def test_a_branch_not_behind_is_left_alone(tmp_path, monkeypatch):
    git = Git(behind=False)
    drive(tmp_path, monkeypatch, git)
    assert not git.ran(" merge ")


def test_a_merge_conflict_is_aborted_named_and_stops_before_any_check(tmp_path, monkeypatch):
    git = Git(behind=True, conflict=True)
    fake = FakeEngine()
    with pytest.raises(Refuse) as stop:
        drive(tmp_path, monkeypatch, git, engine=fake)
    assert "conflicts in: src/a.py, src/b.py" in str(stop.value) and "aborted" in str(stop.value) and git.ran(" merge --abort") and not fake.calls


def test_a_dirty_tree_behind_main_is_refused_without_touching_it(tmp_path, monkeypatch):
    git = Git(behind=True, dirty=" M src/a.py\n")
    with pytest.raises(Refuse, match="behind origin/main and the tree has uncommitted changes"):
        drive(tmp_path, monkeypatch, git)
    assert not git.ran(" merge ")


def test_an_offline_fetch_leaves_the_merge_to_the_later_steps_and_never_refuses_by_itself(tmp_path, monkeypatch):
    git = Git(behind=True, fetch_fails=True)
    fake, _ = drive(tmp_path, monkeypatch, git)
    assert fake.calls and not git.ran(" merge ")


# ----------------------------------------------------------------------------- a branch with a merge commit
def test_a_merge_commit_is_never_the_title_and_the_log_is_read_without_merges(tmp_path, monkeypatch):
    git = Git(log=[("fix(ship): the real change", "")])  # `git log --no-merges` does not list the merge
    fake, _ = drive(tmp_path, monkeypatch, git)
    assert fake.calls[0][2].inputs["title"] == "fix(ship): the real change"
    assert git.ran("--no-merges") and not any(c == "chore: merge origin/main" for c in fake.calls[0][2].inputs.values())


# ----------------------------------------------------------------------------- one call ends in an open PR, in each repository
@pytest.mark.parametrize("name", sorted(ORIGINS))
def test_one_ship_call_from_a_behind_branch_ends_in_a_pr_call_in_each_repository(tmp_path, monkeypatch, name):
    from steuermaschine_heinzel.native import ship as head

    git = Git(repo=ORIGINS[name], behind=True, branch="fix/bead-q7x2k-x")
    opened: list[dict[str, Any]] = []
    fake = FakeEngine()
    monkeypatch.setattr(head, "head_pr", lambda **kw: opened.append(kw) or head.HeadResult(action="pr", target=kw["bead"], verdict="done", lines=["head pr: PR #801 opened"], exit=0))
    _, result = drive(tmp_path, monkeypatch, git, engine=fake)
    assert git.ran(" merge --no-edit") and result.exit_code == 0
    if name == "core":
        assert fake.calls and fake.calls[0][2].inputs["bead"] == "q7x2k"  # the pipeline: check, pr, queue
    else:  # another repository: the PR route directly, with everything derived
        assert not fake.calls and opened[0]["bead"] == "q7x2k" and opened[0]["title"] == "fix(web): keep the draft" and result.pr == 801


def test_the_driver_still_refuses_an_unknown_input_name_before_it_touches_git(tmp_path, monkeypatch):
    git = Git(behind=True)
    with pytest.raises(Refuse, match="NAME one of"):
        drive(tmp_path, monkeypatch, git, set_=["nope=1"])
    assert not git.ran(" merge ")
