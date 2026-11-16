"""bin/promote_verdict.py: a promote needs one independent verdict on the exact candidate commit, kept outside the tree
in the stable directory. Every way it can be wrong refuses: no verdict, a garbled one, one for another commit, one that
reviewed the diff from another base, one by an author of the commits. Run against a real throwaway git repository."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "promote_verdict.py"
PROMOTE = SCRIPT.parent / "promote"


def git(repo: Path, *args: str, who: str = "Ada Author <ada@example.org>") -> str:
    name, email = who[:-1].split(" <")
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True, text=True, env=env, timeout=30).stdout.strip()


def commit(repo: Path, name: str, who: str = "Ada Author <ada@example.org>", trailer: str = "") -> str:
    (repo / name).write_text(name)
    git(repo, "add", name, who=who)
    git(repo, "commit", "-q", "-m", f"feat: {name}" + (f"\n\n{trailer}" if trailer else ""), who=who)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def setup(tmp_path):
    """A repository with a live commit (promoted: stable/current -> releases/<12>) and a candidate on top of it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    live = commit(repo, "a.txt")
    cand = commit(repo, "b.txt", who="Bob Builder <bob@example.org>", trailer="Co-Authored-By: Model Five <noreply@example.org>")
    stable = tmp_path / "stable"
    (stable / "releases" / live[:12]).mkdir(parents=True)
    (stable / "current").symlink_to(f"releases/{live[:12]}")
    return stable, repo, live, cand


def run(*args: str, agent: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "STEUERMASCHINE_AGENT"} | ({"STEUERMASCHINE_AGENT": agent} if agent is not None else {})
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *map(str, args)], capture_output=True, check=False, text=True, timeout=60, env=env)


def receipt(stable: Path, cand: str, **over: str) -> Path:
    fields = {"commit": cand, "base": "", "reviewer": "rita-reviewer", "at": "2026-11-14T12:00:00Z", **over}
    path = stable / "verdicts" / cand
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{k}: {v}\n" for k, v in fields.items()) + "VERDICT: APPROVE\n")
    return path


def test_a_recorded_verdict_by_a_non_author_passes(setup):
    stable, repo, live, cand = setup
    done = run("record", stable, repo, cand, "--reviewer", "rita-reviewer", "--notes", "read the whole diff")
    assert done.returncode == 0, done.stderr
    text = (stable / "verdicts" / cand).read_text()
    assert text.splitlines() == [f"commit: {cand}", f"base: {live}", "reviewer: rita-reviewer", "recorded-by: unset", text.splitlines()[4],
                                 "notes: read the whole diff", "VERDICT: APPROVE"]
    done = run("check", stable, repo, cand)
    assert done.returncode == 0 and f"APPROVE of {cand[:12]} by rita-reviewer" in done.stderr


def test_no_verdict_refuses(setup):
    stable, repo, _, cand = setup
    done = run("check", stable, repo, cand)
    assert done.returncode == 1 and "no verdict is recorded" in done.stderr


def test_a_verdict_for_another_commit_refuses(setup):
    stable, repo, live, cand = setup
    other = commit(repo, "c.txt")  # the candidate moved on after the review
    receipt(stable, cand, base=live)
    assert run("check", stable, repo, other).returncode == 1
    (stable / "verdicts" / cand).rename(stable / "verdicts" / other)  # a receipt copied to the new name still names the old commit
    done = run("check", stable, repo, other)
    assert done.returncode == 1 and f"names commit {cand[:12]}, not the candidate {other[:12]}" in done.stderr


@pytest.mark.parametrize("who", ["Bob Builder", "bob@example.org", "BOB BUILDER", "Model Five", "Bob Builder <bob@example.org>", "Ada Author"])
def test_an_author_of_the_range_as_reviewer_refuses(setup, who):
    stable, repo, live, cand = setup
    if who == "Ada Author":  # an author of any commit of the range counts, not only of the last one
        commit(repo, "d.txt", who="Ada Author <ada@example.org>")
        cand = git(repo, "rev-parse", "HEAD")
    receipt(stable, cand, base=live, reviewer=who)
    done = run("check", stable, repo, cand)
    assert done.returncode == 1 and "is an author of the commits" in done.stderr, done.stderr
    assert run("record", stable, repo, cand, "--reviewer", who).returncode == 1


def test_a_verdict_on_another_base_refuses(setup):
    stable, repo, live, cand = setup
    receipt(stable, cand, base=cand)
    done = run("check", stable, repo, cand)
    assert done.returncode == 1 and f"the live release is {live[:12]}" in done.stderr
    assert run("record", stable, repo, cand, "--reviewer", "rita", "--base", cand).returncode == 1


@pytest.mark.parametrize("text", [
    "",
    "VERDICT: APPROVE\n",
    "commit: {cand}\nbase: {live}\nreviewer: rita\nat: now\nVERDICT: REJECT\n",
    "commit: {cand}\nbase: {live}\nreviewer: rita\nat: now\nVERDICT: APPROVE\nnotes: after the verdict\n",
    "commit: {cand}\nbase: {live}\nreviewer: \nat: now\nVERDICT: APPROVE\n",
    "commit: {cand}\nbase: {live}\nreviewer: rita\nreviewer: other\nat: now\nVERDICT: APPROVE\n",
    "commit: {cand}\nbase: {live}\nreviewer: rita\nat: now\nskip: yes\nVERDICT: APPROVE\n",
    "commit: {short}\nbase: {live}\nreviewer: rita\nat: now\nVERDICT: APPROVE\n",
    "commit: {cand}\nbase: HEAD~1\nreviewer: rita\nat: now\nVERDICT: APPROVE\n",
    "commit: {cand}\nbase: {live}\nreviewer: rita\nat: now\nVERDICT: APPROVE sha256=x\n",
])
def test_a_garbled_verdict_refuses(setup, text):
    stable, repo, live, cand = setup
    path = stable / "verdicts" / cand
    path.parent.mkdir(parents=True)
    path.write_text(text.format(cand=cand, live=live, short=cand[:12]))
    done = run("check", stable, repo, cand)
    assert done.returncode == 1 and "REFUSED" in done.stderr, done.stderr


def test_an_unreadable_verdict_refuses(setup):
    stable, repo, _, cand = setup
    path = stable / "verdicts" / cand
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00garbage")
    assert run("check", stable, repo, cand).returncode == 1


def test_a_live_release_that_is_not_a_commit_of_the_repo_refuses(setup):
    stable, repo, live, cand = setup
    receipt(stable, cand, base=live)
    (stable / "current").unlink()
    (stable / "releases" / "0123456789ab").mkdir()
    (stable / "current").symlink_to("releases/0123456789ab")
    done = run("check", stable, repo, cand)
    assert done.returncode == 1 and "is not a commit of" in done.stderr


def test_before_the_first_promote_the_base_is_none(setup):
    stable, repo, live, cand = setup
    (stable / "current").unlink()
    receipt(stable, cand, base=live)
    assert run("check", stable, repo, cand).returncode == 1  # nothing is live: the diff read must be the whole tree
    assert run("record", stable, repo, cand, "--reviewer", "rita").returncode == 0
    assert "base: none" in (stable / "verdicts" / cand).read_text() and run("check", stable, repo, cand).returncode == 0
    receipt(stable, cand, base="none", reviewer="Ada Author")  # with base none every commit of the candidate counts
    assert run("check", stable, repo, cand).returncode == 1


def test_bad_arguments_exit_2(setup):
    stable, repo, _, cand = setup
    for args in ([], ["approve", stable, repo, cand], ["check", stable, repo, cand, "--reviewer", "x"], ["record", stable, repo, cand],
                 ["record", stable, repo, cand, "--reviewer"], ["record", stable, repo, cand, "--reviewer", "a", "--reviewer", "b"]):
        assert run(*args).returncode == 2, args


def test_a_short_or_non_sha_candidate_refuses(setup):
    stable, repo, live, cand = setup
    receipt(stable, cand, base=live)
    assert run("check", stable, repo, cand[:12]).returncode == 1


def test_a_multiline_reviewer_cannot_forge_lines(setup):
    stable, repo, _, cand = setup
    assert run("record", stable, repo, cand, "--reviewer", "rita\nbase: none").returncode == 1
    assert not (stable / "verdicts" / cand).exists()


def test_promote_checks_the_verdict_before_the_compare_and_again_before_the_switch():
    text = PROMOTE.read_text()
    check = 'verdict_holds "$rel/venv/bin/python" "$stable" "$here" "$full" "$rel/src/bin/promote_verdict.py"'
    first, second = text.index(check), text.rindex(check)
    assert first < text.index('say "4/5 compare') < text.index('[ "$check" = 0 ] ||') < second < text.index('say "5/5 switch"')
    assert "review_gate.py" not in text and "self reviews) >" not in text  # the per-command gate is gone from promote
    assert "--allow-unverified" not in text and "verdict" not in text.split("for a in")[1].split("done")[0]  # no option skips it


def test_the_recording_agent_is_the_reviewer(setup):
    stable, repo, live, cand = setup
    done = run("record", stable, repo, cand, "--reviewer", "rita", agent="bob-builder")  # the builder records a verdict in another name
    assert done.returncode == 1 and "STEUERMASCHINE_AGENT is bob-builder, not --reviewer rita" in done.stderr
    assert not (stable / "verdicts" / cand).exists()
    assert run("record", stable, repo, cand, "--reviewer", "rita", agent="rita").returncode == 0
    assert "recorded-by: rita" in (stable / "verdicts" / cand).read_text() and run("check", stable, repo, cand).returncode == 0
    receipt(stable, cand, base=live, reviewer="rita", **{"recorded-by": "bob-builder"})  # a hand-written receipt says who recorded it
    done = run("check", stable, repo, cand)
    assert done.returncode == 1 and "recorded by bob-builder, not by its reviewer rita" in done.stderr


def test_every_record_is_kept_in_the_log(setup):
    stable, repo, _, cand = setup
    assert run("record", stable, repo, cand, "--reviewer", "r1").returncode == 0
    assert run("record", stable, repo, cand, "--reviewer", "r2").returncode == 0
    log = (stable / "verdicts" / "records.log").read_text().splitlines()
    assert [line.split(" | ")[2] for line in log] == ["reviewer: r1", "reviewer: r2"] and "reviewer: r2" in (stable / "verdicts" / cand).read_text()


def verdict_holds(stable: Path, repo: Path, cand: str, checker: Path) -> subprocess.CompletedProcess[str]:
    lib = PROMOTE.parent / "promote_lib.sh"
    return subprocess.run(["bash", "-c", f'. "{lib}"; verdict_holds "{sys.executable}" "{stable}" "{repo}" "{cand}" "{checker}"'],
                          capture_output=True, check=False, text=True, timeout=60)


def test_a_candidate_checker_that_always_passes_is_still_judged_by_the_live_one(setup, tmp_path):
    stable, repo, live, cand = setup
    weak = tmp_path / "weak.py"
    weak.write_text("import sys\nsys.exit(0)\n")
    (stable / "releases" / live[:12] / "src" / "bin").mkdir(parents=True)
    (stable / "releases" / live[:12] / "src" / "bin" / "promote_verdict.py").write_bytes(SCRIPT.read_bytes())  # the reviewed checker is live
    done = verdict_holds(stable, repo, cand, weak)
    assert done.returncode == 1 and "no verdict is recorded" in done.stderr and "the live release's checker refuses it" in done.stderr
    receipt(stable, cand, base=live)
    assert verdict_holds(stable, repo, cand, weak).returncode == 0
    assert verdict_holds(stable, repo, cand, SCRIPT).returncode == 0
    (stable / "verdicts" / cand).unlink()
    assert verdict_holds(stable, repo, cand, SCRIPT).returncode == 1  # the candidate's own checker refuses too


def test_without_a_live_checker_the_candidate_checker_decides_and_says_so(setup):
    stable, repo, live, cand = setup
    done = verdict_holds(stable, repo, cand, SCRIPT)
    assert done.returncode == 1 and "(bootstrap)" in done.stderr
    receipt(stable, cand, base=live)
    assert verdict_holds(stable, repo, cand, SCRIPT).returncode == 0
