"""`sm doc handoff` and `sm doc hour`, and the core's digest. git and gh are the fake process runner's: no test
commits or pushes anything."""

import json
import os

import typer
from coord_kit import NOW, app_runtime, ok

from steuermaschine import digest
from steuermaschine.engine import Refuse, Wait
from steuermaschine.models import Model
from steuermaschine.native import NativeCommand
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, invoke
from steuermaschine_heinzel.native.handoff_lib import hour, snapshot

PRS = [{"number": 796, "title": "fix: details | retry", "headRefName": "fix/details", "isDraft": False, "updatedAt": "2026-11-12T21:52:00Z"},
       {"number": 630, "title": "audio loopback", "headRefName": "feat/loop", "isDraft": True, "updatedAt": "2026-11-10T08:00:00Z"}]


def world(tmp_path, staged=True, fail="", ahead=0, branch="main", behind=False):
    repo = tmp_path / "tracker"
    (repo / "research").mkdir(parents=True, exist_ok=True)
    (repo / "AGENTS.md").write_text("## 8. Log\n")
    work, old, main = tmp_path / "heinzel-logo", tmp_path / "heinzel-old", tmp_path / "heinzel"
    for d in (work, old, main):
        (d / ".git").mkdir(parents=True, exist_ok=True)
    os.utime(old, (NOW - 9 * 86400,) * 2)
    os.utime(old / ".git", (NOW - 9 * 86400,) * 2)
    for d in (work, main):
        os.utime(d, (NOW - 60,) * 2)
    c = FakeCommands()
    c.on(["gh", "pr", "list"], ok(PRS))
    c.on(["gh", "run", "list"], ok([{"conclusion": "success", "status": "completed", "headSha": "7dc85604ba06"}, {"conclusion": None, "status": "in_progress", "headSha": "e9c943810ea7"}]))
    c.on(["gh", "release", "list"], ok([{"tagName": "v0.9.39", "isLatest": True, "isPrerelease": False}]))

    def git(argv):
        where, verb = argv[2], next(a for a in argv[3:] if not a.startswith("-") and a not in ("commit.gpgsign=false",))
        if verb == fail:
            return Result(1, "", f"fatal: {verb} refused")
        if verb == "rev-parse":
            return ok("abc1234\n" if "--short" in argv else (f"{branch}\n" if where == str(repo) else "main\n" if where == str(main) else "feat/logo\n"))
        if verb == "rev-list":
            return ok(f"{ahead}\n" if where == str(repo) else "2\n")
        if verb == "merge-base":
            return Result(1 if behind else 0, "", "")
        if verb == "status":
            return ok(" M a.py\n?? b.py\n")
        if verb == "diff":
            return Result(1 if staged else 0, "", "")
        return ok()

    c.on(["git"], git)
    return c, repo


def rt_of(tmp_path, c, repo, **over):
    return app_runtime(tmp_path, c, coord_document=str(repo / "AGENTS.md"), coord_tracker_dir=str(repo), **over)


def verbs(c):
    return [next(a for a in call[3:] if not a.startswith("-") and a != "commit.gpgsign=false") for call in c.calls if call[0] == "git" and call[2].endswith("tracker")]


def test_dry_run_prints_the_snapshot_and_writes_nothing(tmp_path):
    c, repo = world(tmp_path)
    done = invoke(rt_of(tmp_path, c, repo), ["doc", "handoff", "--dry-run"])
    out = done.stdout
    assert out.startswith("# Open work\n") and "**Open pull requests of helmut-hoffer-von-ankershoffen/heinzel** (2)" in out
    assert "| #630 | audio loopback | `feat/loop` | yes | 2026-11-10T08:00 |" in out and "| #796 | fix: details / retry |" in out
    assert "- CI success `7dc85604`" in out and "- CI in_progress `e9c94381`" in out and "- heinzel release `v0.9.39` latest=true pre=false" in out
    assert f"| {tmp_path}/heinzel-logo | `feat/logo` | 2 | 2 |" in out and "heinzel-old" not in out and f"| {tmp_path}/heinzel |" not in out
    assert not (repo / "research/open-work.md").exists() and verbs(c) == [] and "research/open-work.md" in out.splitlines()[-1]


def test_handoff_checks_first_then_writes_commits_only_its_file_and_pushes_fast_forward(tmp_path):
    c, repo = world(tmp_path)
    (repo / "HLD.md").write_text("unrelated uncommitted work\n")
    done = invoke(rt_of(tmp_path, c, repo), ["doc", "handoff"])
    assert done.stdout == "handoff: committed and pushed 1 commit(s), on the remote at abc1234\n"
    assert (repo / "research/open-work.md").read_text().startswith("# Open work\n")
    assert verbs(c) == ["rev-parse", "fetch", "merge-base", "rev-list", "add", "diff", "commit", "push", "rev-parse"]
    calls = [call for call in c.calls if call[:3] == ["git", "-C", str(repo)]]
    assert ["git", "-C", str(repo), "add", "--", "research/open-work.md"] in calls  # only the file it wrote
    assert ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-q", "--only", "-m", "docs: refresh the tracking document", "--", "research/open-work.md"] in calls
    assert ["git", "-C", str(repo), "push", "-q", "origin", "main:main"] in calls
    words = {word for call in calls for word in call}
    assert not words & {"pull", "rebase", "stash", "--autostash", "merge", "--force", "-f", "--force-with-lease", "-A", "--all", "."}
    assert (repo / "HLD.md").read_text() == "unrelated uncommitted work\n"


def test_paths_named_in_the_setting_are_committed_too_when_they_exist(tmp_path):
    c, repo = world(tmp_path)
    doc = snapshot.Handoff.model_validate(json.loads(invoke(rt_of(tmp_path, c, repo, handoff_files=["AGENTS.md", "missing.md"]), ["--format", "json", "doc", "handoff"]).stdout))
    assert doc.staged == ["research/open-work.md", "AGENTS.md"] and doc.published_commits == 1


def test_nothing_staged_and_nothing_ahead_means_no_commit_and_no_push(tmp_path):
    c, repo = world(tmp_path, staged=False)
    doc = snapshot.Handoff.model_validate(json.loads(invoke(rt_of(tmp_path, c, repo), ["--format", "json", "doc", "handoff"]).stdout))
    assert not doc.committed and not doc.pushed and "push" not in verbs(c) and "commit" not in verbs(c)
    assert doc.render() == ["handoff: nothing new to commit and nothing to push; the remote is at abc1234"]


def test_every_refusal_comes_before_anything_is_written(tmp_path):
    for over, want in ((dict(branch="feat/x"), "is on branch feat/x, not main"), (dict(behind=True), "has commits this checkout lacks"),
                       (dict(ahead=190), "190 local commit(s) the remote lacks"), (dict(fail="fetch"), "git fetch origin main failed")):
        c, repo = world(tmp_path, **over)
        done = invoke(rt_of(tmp_path, c, repo), ["doc", "handoff"])
        assert done.exit_code == 1 and want in done.stderr, want
        assert not (repo / "research/open-work.md").exists() and not {"add", "commit", "push"} & set(verbs(c)), want


def test_local_commits_are_published_only_when_the_caller_says_so(tmp_path):
    c, repo = world(tmp_path, ahead=3)
    done = invoke(rt_of(tmp_path, c, repo), ["doc", "handoff", "--include-local-commits"])
    assert done.stdout == "handoff: committed and pushed 4 commit(s), on the remote at abc1234\n"
    c, repo = world(tmp_path, ahead=2, staged=False)
    assert "nothing new to commit; pushed 2 commit(s)" in invoke(rt_of(tmp_path, c, repo), ["doc", "handoff", "--include-local-commits"]).stdout


def test_a_failed_step_after_the_checks_stops_the_handoff_and_names_the_step(tmp_path):
    for step, later in (("push", "never"), ("add", "diff"), ("commit", "push")):
        c, repo = world(tmp_path, fail=step)
        done = invoke(rt_of(tmp_path, c, repo), ["doc", "handoff"])
        assert done.exit_code == 1 and f"fatal: {step} refused" in done.stderr and later not in verbs(c)


def test_hour_without_handoff_gathers_facts_drafts_and_writes_nothing(tmp_path):
    c, repo = world(tmp_path)
    done = invoke(rt_of(tmp_path, c, repo, hour_facts=["doc asks", "ci nothing-here", "note", "doc velocity-record"]), ["doc", "hour", "--no-handoff"])
    out = done.stdout
    assert done.exit_code == 0 and out.startswith("FACTS ")
    assert "sm doc asks:   [refused]\n  no session transcript found" in out  # the source is down: shown, the update goes on
    assert "sm ci nothing-here:   [missing]\n  not available in this version" in out
    assert "sm note:   [skipped]" in out and "sm doc velocity-record:   [skipped]" in out  # never a write command, never one that needs an argument
    assert "Main CI (newest first):\n  7dc8560 success, e9c9438 in_progress" in out and "  heinzel: v0.9.39 (latest)" in out and "Handoff: skipped" in out
    assert "1. Live now (only what is verified; write the URL next to each)" in out and "| #796 | fix: details / retry |" in out
    assert out.rstrip().endswith('4. Decisions for you, ranked, with recommendation, or "none"\n  <write>')
    assert verbs(c) == [] and not (repo / "research/open-work.md").exists() and "hz-" not in out


def test_hour_runs_the_handoff_by_default_and_reports_its_failure(tmp_path):
    c, repo = world(tmp_path)
    doc = hour.Hour.model_validate(json.loads(invoke(rt_of(tmp_path, c, repo, hour_facts=[]), ["--format", "json", "doc", "hour"]).stdout))
    assert doc.handoff == "handoff: committed and pushed 1 commit(s), on the remote at abc1234" and "push" in verbs(c)
    c, repo = world(tmp_path, fail="push")
    done = invoke(rt_of(tmp_path, c, repo, hour_facts=[]), ["doc", "hour"])
    assert done.exit_code == 1 and "git push failed" in done.stderr
    c, repo = world(tmp_path, ahead=5)
    assert "5 local commit(s)" in invoke(rt_of(tmp_path, c, repo, hour_facts=[]), ["doc", "hour"]).stderr
    assert invoke(rt_of(tmp_path, c, repo, hour_facts=[]), ["doc", "hour", "--include-local-commits"]).exit_code == 0


def test_hour_cuts_a_long_fact_and_shows_unknown_sources(tmp_path):
    c, repo = world(tmp_path)
    c.on(["gh", "run", "list"], ok("not json"))
    c.on(["gh", "release", "list"], ok("not json"))
    rt = rt_of(tmp_path, c, repo, hour_facts=[])

    class Long(Model):
        def render(self):
            return [f"line {i}" for i in range(45)]

    cmd = NativeCommand("x long", lambda: Long(), "help")
    from steuermaschine.native import NativeRuntime

    got = hour.build(NativeRuntime(rt.providers, {**rt.settings, "hour_facts": ["x long"]}, rt.project), [cmd], True, load=lambda: (1.0, 2.0, 3.0))
    assert got.facts[0].lines[-1] == "... 5 more: sm x long" and got.main_ci == "unknown" and got.releases == ["heinzel: unknown"] and got.load == "1.00 2.00 3.00"
    assert hour.indent("") == "  -" and hour.indent("a\nb") == "  a\n  b"


# ----------------------------------------------------------------------------- the core's digest
class Text(Model):
    lines: list[str]
    code: int = 0

    @property
    def exit_code(self):
        return self.code

    def render(self):
        return self.lines


def test_digest_calls_read_only_commands_with_their_defaults_and_survives_each_failure():
    def plain(flag: bool = typer.Option(False, "--flag"), name: str = typer.Argument("x")) -> Text:
        return Text(lines=[f"{flag} {name}"], code=3)

    def needs(pr: int = typer.Argument(...)) -> Text:
        return Text(lines=["never"])

    def refuses() -> Text:
        raise Refuse("no", "sm x", code=2)

    def waits() -> Text:
        raise Wait("not yet")

    def breaks() -> Text:
        raise RuntimeError("boom")

    cmds = [NativeCommand("a", plain, "h"), NativeCommand("b", needs, "h"), NativeCommand("c", refuses, "h"), NativeCommand("d", waits, "h"), NativeCommand("e", breaks, "h"),
            NativeCommand("w", plain, "h", writes=True), NativeCommand("l", lambda: [Text(lines=["1"]), None, Text(lines=["2"], code=1)], "h"), object()]
    got = {f.path: f for f in digest.collect(cmds, ["a", "b", "c", "d", "e", "w", "l", "nope", "  a  "])}
    assert (got["a"].status, got["a"].lines, got["a"].exit_code) == ("ok", ["False x"], 3)
    assert got["b"].status == "skipped" and got["w"].status == "skipped" and got["nope"].status == "missing"
    assert (got["c"].status, got["c"].lines, got["c"].exit_code) == ("refused", ["no"], 2) and got["d"].lines == ["waiting: not yet"]
    assert got["e"].status == "failed" and got["e"].lines == ["RuntimeError: boom"] and (got["l"].lines, got["l"].exit_code) == (["1", "2"], 1)
    assert got["  a  "].status == "ok"


def test_hour_and_handoff_units_do_not_pull_in_the_plugins_whole_registry():
    from steuermaschine_heinzel.native import handoff

    for fn in (handoff.doc_hour, handoff.doc_handoff):
        labels = fn.__sm_native__.labels()
        assert "__init__.py" not in labels and "queue.py" not in labels and "tick.py" not in labels, labels  # a plugin change elsewhere keeps the verdict
        assert "handoff.py" in labels and "handoff_lib/hour.py" in labels
