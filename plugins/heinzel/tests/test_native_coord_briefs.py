"""`sm beads brief` and `sm head review-brief`. The assertions of the former brief tests moved here; git, ps and lsof
are the fake process runner's."""
# ruff: noqa: F811  (a fixture imported from coord_kit is also a test parameter)

import json

from coord_kit import app_runtime, home, native_runtime, ok, unreviewed  # noqa: F401  (fixtures)

from steuermaschine.native import using
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, invoke
from steuermaschine_heinzel.native.briefs_lib import builder as brief
from steuermaschine_heinzel.native.briefs_lib import reviewer


def world(tmp_path, agents=0, worktree_fails=""):
    c = FakeCommands()
    code = tmp_path / "Code"
    c.on(["ps"], ok("".join(f"{100 + i} /usr/local/bin/claude\n" for i in range(agents)) + "7 /bin/zsh\n"))
    c.on(["lsof"], ok("".join(f"p{100 + i}\nfcwd\nn{code}/heinzel-t{i}/src\n" for i in range(agents))))
    c.on(["git"], ok())

    made = {}

    def add(argv):
        if argv[4] == "list":
            return ok("".join(f"worktree {tree}\nHEAD aaaa\nbranch refs/heads/{branch}\n\n" for tree, branch in made.items()))
        if worktree_fails:
            return Result(128, "", worktree_fails)
        (code / argv[6].split("/")[-1]).mkdir(parents=True)
        made[argv[6]] = argv[8]
        return ok()

    c.on(["git", "-C", str(code / "heinzel"), "worktree"], add)
    c.on(["git", "-C", str(code / "heinzel-plugins"), "worktree"], add)
    return c


def rt_of(tmp_path, c, load=1.0, **over):
    return dict(commands=c, brief_code_dir=str(tmp_path / "Code"), brief_test_load=load, **over)


# ----------------------------------------------------------------------------- the gate (assertions moved)
def test_brief_counts_agent_processes_by_cwd(tmp_path):
    ps = "11 /Users/x/.local/bin/claude\n12 /Applications/Claude.app/Contents/MacOS/Claude\n13 /opt/bin/codex\n14 /bin/zsh\n15 /x/claude.app/Contents/MacOS/claude\n"
    assert brief.agent_pids(ps) == [11, 13, 15]
    code = tmp_path / "Code"
    cwds = {11: str(code / "heinzel-logo/src"), 13: str(code / "heinzel-plugins-sounds"), 15: str(code), 16: str(code / "heinzel"), 17: str(code / "heinzel-management"),
            18: "/elsewhere", 19: str(code / "other")}
    skip = {"heinzel", "heinzel-plugins", "heinzel-acme", "heinzel-sf", "heinzel-management"}
    assert brief.worktree_agents(cwds, code, "heinzel", skip) == [(11, "heinzel-logo"), (13, "heinzel-plugins-sounds")]


def test_brief_full_names_the_measure():
    agents = [(i, f"heinzel-t{i}") for i in range(5)]
    assert brief.full(12.0, agents[:4]) == ""
    assert "load 41.0 is over 40" in brief.full(41.0, [])
    assert "5 agents are working" in brief.full(3.0, agents) and "heinzel-t0" in brief.full(3.0, agents)
    assert brief.full(40.0, []) == ""


def test_brief_makes_the_worktree_and_prints_the_prompt(tmp_path):
    c = world(tmp_path)
    rt = app_runtime(tmp_path, **rt_of(tmp_path, c))
    done = invoke(rt, ["beads", "brief", "logo", "feat/logo", "--task", "Bigger logo."])
    assert done.exit_code == 0
    code = tmp_path / "Code"
    assert ["git", "-C", str(code / "heinzel"), "fetch", "-q", "origin", "main"] in c.calls
    assert ["git", "-C", str(code / "heinzel"), "worktree", "add", "-q", str(code / "heinzel-logo"), "-b", "feat/logo", "origin/main"] in c.calls
    assert f"Worktree: {code}/heinzel-logo" in done.stdout and "section 6" in done.stdout and "Bigger logo." in done.stdout
    assert f"Read section 6 of {tmp_path}/AGENTS.md first" in done.stdout  # the document setting
    n = len(c.calls)
    again = json.loads(invoke(rt, ["--format", "json", "beads", "brief", "logo", "feat/logo"]).stdout)  # reuse
    assert brief.BuilderBrief.model_validate(again).created is False
    assert [call[3:5] for call in c.calls[n:] if call[0] == "git"] == [["worktree", "list"]]  # a reuse only reads


def test_brief_names_the_bead_and_the_other_repository(tmp_path):
    c = world(tmp_path)
    done = invoke(app_runtime(tmp_path, **rt_of(tmp_path, c)), ["beads", "brief", "sounds", "feat/s", "--repo", "plugins", "--bead", "ab12"])
    assert f"Worktree: {tmp_path}/Code/heinzel-plugins-sounds" in done.stdout
    assert "Bead: ab12 (note the PR on it; after merge: sm beads stamp ab12 --merged <PR>)" in done.stdout
    done = invoke(app_runtime(tmp_path, **rt_of(tmp_path, c)), ["beads", "brief", "x", "feat/x", "--repo", "nope"])
    assert done.exit_code == 2 and "unknown repository 'nope'" in done.stderr


def test_brief_refuses_when_full_and_force_goes_ahead_and_logs(tmp_path):
    c = world(tmp_path, agents=5)
    rt = app_runtime(tmp_path, **rt_of(tmp_path, c, load=3.0))
    done = invoke(rt, ["beads", "brief", "logo", "feat/logo", "--task", "Bigger logo."])
    assert done.exit_code == 1 and "5 agents are working" in done.stderr and "--force" in done.stderr
    assert not (tmp_path / "Code/heinzel-logo").exists() and not [call for call in c.calls if call[0] == "git"]
    done = invoke(rt, ["beads", "brief", "logo", "feat/logo", "--task", "Bigger logo.", "--force", "owner asked"])
    assert done.exit_code == 0 and (tmp_path / "Code/heinzel-logo").exists() and "Bigger logo." in done.stdout
    logged = (tmp_path / "AGENTS.md").read_text()
    assert "owner asked" in logged and "forced" in logged and "hz-" not in logged


def test_brief_refuses_over_load_40_and_an_empty_force_reason(tmp_path):
    for extra in ([], ["--force", "  "]):
        c = world(tmp_path)
        done = invoke(app_runtime(tmp_path, **rt_of(tmp_path, c, load=55.0)), ["beads", "brief", "logo", "feat/logo", *extra])
        assert done.exit_code == 1 and "load 55.0 is over 40" in done.stderr


def test_brief_reports_a_git_refusal_and_is_off_until_reviewed(tmp_path, unreviewed):
    c = world(tmp_path, worktree_fails="fatal: a branch named 'feat/logo' already exists")
    done = invoke(app_runtime(tmp_path, **rt_of(tmp_path, c)), ["beads", "brief", "logo", "feat/logo"])
    assert done.exit_code == 1 and "worktree failed: fatal: a branch named" in done.stderr
    c = world(tmp_path)
    done = invoke(app_runtime(tmp_path, allow=False, **rt_of(tmp_path, c)), ["beads", "brief", "logo", "feat/logo"])
    assert done.exit_code == 70 and "reviews/beads-brief.md" in done.stderr and not c.calls


def test_the_load_can_be_given_by_the_caller(tmp_path):
    with using(native_runtime(tmp_path, **rt_of(tmp_path, world(tmp_path), load=None))):
        try:
            brief.brief("logo", "feat/logo", load=lambda: 99.0)
            raise AssertionError("briefed on a full machine")
        except brief.MachineFull as stop:
            assert "load 99.0" in str(stop)


# ----------------------------------------------------------------------------- the reviewer brief
def test_review_brief_lists_flags_members_and_verdict_files(tmp_path):
    (tmp_path / "research").mkdir()
    (tmp_path / "research/review-pr796.md").write_text("APPROVE\n")
    flags = tmp_path / "flags.txt"
    flags.write_text("tests/e2e/x.py:12 timeout raised 30 -> 90\n")
    c = FakeCommands()
    c.on(["gh", "pr", "view"], ok({"title": "combine: details", "body": "combines #790 and #791; member #796"}))
    done = invoke(app_runtime(tmp_path, c), ["head", "review-brief", "796", "--flags", str(flags)])
    out = done.stdout.splitlines()
    assert done.exit_code == 0 and out[0] == "Independent review of PR #796 (combine: details) in helmut-hoffer-von-ankershoffen/heinzel. You did not write it."
    assert out[2] == "  tests/e2e/x.py:12 timeout raised 30 -> 90"
    assert "Member PRs of this combine (check each flagged hunk against its origin): #790, #791" in out
    assert "Existing verdict files (do not repeat): review-pr796.md" in out and "research/review-pr796.md" in out[-1]
    assert "hz-" not in done.stdout
    doc = reviewer.ReviewBrief.model_validate(json.loads(invoke(app_runtime(tmp_path, c), ["--format", "json", "head", "review-brief", "796", "--flags", str(flags), "-R", "o/r"]).stdout))
    assert doc.flagged and doc.members == [790, 791] and doc.repository == "o/r"


def test_review_brief_for_a_passing_guard_an_unreadable_file_and_a_plain_pull_request(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("\n")
    c = FakeCommands()
    done = invoke(app_runtime(tmp_path, c), ["head", "review-brief", "5", "--flags", str(empty)])
    assert done.stdout == "PR #5: guard PASS, no independent review needed\n" and not c.calls
    done = invoke(app_runtime(tmp_path, c), ["head", "review-brief", "5", "--flags", str(tmp_path / "missing.txt")])
    assert done.exit_code == 2 and "cannot read the guard output" in done.stderr
    flags = tmp_path / "f.txt"
    flags.write_text("a.py:1 skip added")
    done = invoke(app_runtime(tmp_path, c), ["head", "review-brief", "5", "--flags", str(flags)])  # gh fails: no title, no members
    assert "Not a combine." in done.stdout and "No verdict file yet." in done.stdout
    assert reviewer.brief(5, "", {}, [], "o/r").splitlines()[2] == "  (guard printed nothing)"


def test_review_brief_reads_the_guard_output_from_standard_input(tmp_path, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("a.py:1 skip added\n"))
    with using(native_runtime(tmp_path)):
        from steuermaschine_heinzel.native import briefs

        assert briefs.head_review_brief(5, briefs.Path("-"), None).flagged


# ----------------------------------------------------------------------------- a real git repository in the sandbox
def test_brief_adds_a_real_worktree_on_a_new_branch_from_origin_main(tmp_path):
    """As the former test did: a real `git worktree add` in a scratch repository whose origin is itself."""
    import subprocess

    from steuermaschine.defaults import Subprocess

    clone = tmp_path / "Code/heinzel"
    clone.mkdir(parents=True)

    def git(*args):
        return subprocess.run(["git", "-C", str(clone), *args], check=True, capture_output=True, text=True).stdout

    git("init", "-q", "-b", "main")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    git("remote", "add", "origin", str(clone))
    git("fetch", "-q", "origin", "main")
    rt = app_runtime(tmp_path, Subprocess(), brief_code_dir=str(tmp_path / "Code"), brief_test_load=1.0, coord_tool_path=[])
    done = invoke(rt, ["beads", "brief", "logo", "feat/logo", "--task", "Bigger logo."])
    assert done.exit_code == 0, done
    assert (tmp_path / "Code/heinzel-logo/.git").exists() and "feat/logo" in git("branch", "--list", "feat/logo")
    assert f"Worktree: {tmp_path}/Code/heinzel-logo" in done.stdout and "section 6" in done.stdout and "Bigger logo." in done.stdout
    assert invoke(rt, ["beads", "brief", "logo", "feat/logo"]).exit_code == 0  # reuse
    again = invoke(rt, ["beads", "brief", "logo2", "feat/logo"])  # the branch exists: git refuses, nothing is half-made
    assert again.exit_code == 1 and "worktree failed" in again.stderr and not (tmp_path / "Code/heinzel-logo2").exists()


# ----------------------------------------------------------------------------- review finding B3: the topic
def test_a_topic_that_is_not_a_plain_name_is_refused_before_anything_runs(tmp_path):
    for topic in ("x/../../../outside/escaped", "..", "a/b", ".hidden", "a b", "ä"):
        c = world(tmp_path)
        done = invoke(app_runtime(tmp_path, **rt_of(tmp_path, c)), ["beads", "brief", topic, "feat/x"])
        assert done.exit_code == 2 and "topic" in done.stderr and not c.calls, topic
    assert not (tmp_path / "outside").exists()


def test_a_topic_that_names_a_clone_or_the_tracker_is_refused(tmp_path):
    code = tmp_path / "Code"
    (code / "heinzel-plugins").mkdir(parents=True)
    c = world(tmp_path)
    done = invoke(app_runtime(tmp_path, **rt_of(tmp_path, c)), ["beads", "brief", "plugins", "feat/x"])  # <core clone>-plugins is the plugins MAIN clone
    assert done.exit_code == 2 and "main clone" in done.stderr and not [call for call in c.calls if call[0] == "git"]
    (code / "heinzel-management").mkdir()
    c = world(tmp_path)
    done = invoke(app_runtime(tmp_path, coord_tracker_dir=str(code / "heinzel-management"), **rt_of(tmp_path, c)), ["beads", "brief", "management", "feat/x"])
    assert done.exit_code == 2 and "tracker" in done.stderr and not [call for call in c.calls if call[0] == "git"]


def test_an_existing_directory_is_reused_only_when_it_is_this_clones_worktree_on_the_branch(tmp_path):
    code = tmp_path / "Code"
    tree = code / "heinzel-logo"
    tree.mkdir(parents=True)
    listing = f"worktree {code / 'heinzel'}\nHEAD aaaa\nbranch refs/heads/main\n\nworktree {tree}\nHEAD bbbb\nbranch refs/heads/feat/logo\n"
    c = world(tmp_path)
    c.on(["git", "-C", str(code / "heinzel"), "worktree", "list"], ok(listing))
    rt = app_runtime(tmp_path, **rt_of(tmp_path, c))
    assert invoke(rt, ["beads", "brief", "logo", "feat/logo"]).exit_code == 0  # reuse
    done = invoke(rt, ["beads", "brief", "logo", "feat/other"])
    assert done.exit_code == 1 and "feat/logo" in done.stderr and "feat/other" in done.stderr
    (code / "heinzel-stray").mkdir()
    done = invoke(rt, ["beads", "brief", "stray", "feat/stray"])
    assert done.exit_code == 1 and "not a worktree" in done.stderr


def test_a_failed_fetch_is_a_refusal_and_no_worktree_is_added(tmp_path):
    c = world(tmp_path)
    c.on(["git", "-C", str(tmp_path / "Code/heinzel"), "fetch"], Result(1, "", "fatal: unable to access origin"))
    done = invoke(app_runtime(tmp_path, **rt_of(tmp_path, c)), ["beads", "brief", "logo", "feat/logo"])
    assert done.exit_code == 1 and "fetch" in done.stderr and "unable to access" in done.stderr
    assert not [call for call in c.calls if "worktree" in call and "add" in call]
