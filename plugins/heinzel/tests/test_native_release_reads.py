"""`sm release status | candidate | verify | pins | after | notes-audit | assets` through the command tree.

fixtures/release/train/: train.json, v0.9.35.json (done) and v0.9.36.json (cleared) of the release train's state
directory, copied 2026-11-12 23:05 CET with `cp` (read only). GitHub is `steuermaschine.github.testing.FakeGh`."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel.native import release_read as area
from steuermaschine_heinzel.native.release_lib import approved
from steuermaschine_heinzel.plugin import plugin

REAL = Path(__file__).parent / "fixtures" / "release"
REPO = "helmut-hoffer-von-ankershoffen/heinzel"


class Commands(FakeGh):
    """The scripted `gh`; `git` runs for real, on the throwaway checkouts the tests create."""

    def run(self, argv, cwd=None, timeout=None, env=None):
        if argv[0] != "git":
            return super().run(argv, cwd=cwd, timeout=timeout, env=env)
        done = subprocess.run([str(a) for a in argv], cwd=cwd, capture_output=True, text=True, check=False)
        return Result(done.returncode, done.stdout, done.stderr)


@pytest.fixture
def gh():
    return Commands()


@pytest.fixture
def rt(tmp_path, monkeypatch, gh):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".cache").mkdir()
    shutil.copytree(REAL / "train", tmp_path / "train")
    conf = {"release_train_state_dir": str(tmp_path / "train"), "release_dir": str(tmp_path / "work"), "release_core_checkout": str(tmp_path / "core"),
            "release_assets_cache": str(tmp_path / "assets.json"), "lock_directory": str(tmp_path / ".cache"), "queue_records_dir": str(tmp_path / "records")}
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": conf}, Path("."))
    runtime = Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=True), stack, settings, _providers=fake_providers(commands=gh))
    runtime.home = tmp_path
    return runtime


def doc(rt, model, *args):
    out = invoke(rt, ["--format", "json", *args])
    return out, model.model_validate(json.loads(out.stdout))


# ----------------------------------------------------------------------------- status
def test_status_without_a_release_in_flight_reads_the_real_train_files(rt, gh):
    out = invoke(rt, ["release", "status"])
    assert out.exit_code == 0 and out.stdout.strip() == "no release in flight"  # 0.9.35 is done, 0.9.36 is cleared
    assert gh.calls == []
    out, status = doc(rt, area.ReleaseStatus, "release", "status", "--line")
    assert status.active is False and status.lines == []


def in_flight(rt, gh, ci="failure"):
    train = json.loads((REAL / "train" / "v0.9.36.json").read_text()) | {"phase": "release", "start": rt.providers.clock.now() - 1800}
    (rt.home / "train" / "v0.9.36.json").write_text(json.dumps(train))
    gh.on("pr list", [{"number": 705, "mergeable": "MERGEABLE", "statusCheckRollup": [
        {"status": "COMPLETED", "conclusion": "SUCCESS"}, {"status": "COMPLETED", "conclusion": "FAILURE"}, {"status": "IN_PROGRESS", "conclusion": ""}]}])
    gh.on("run list", [{"databaseId": 901, "name": "CI", "status": "completed", "conclusion": ci, "attempt": 1},
                       {"databaseId": 902, "name": "Acceptance", "status": "completed", "conclusion": "success", "attempt": 1}])
    gh.on("run view 901", "Test (macos-latest, Python 3.14, integration)\nCoverage\n")


def test_status_of_a_release_in_flight_names_blockers_and_the_next_step_in_sm_words(rt, gh):
    in_flight(rt, gh)
    (rt.home / ".cache" / "gate.hold").touch()
    out, status = doc(rt, area.ReleaseStatus, "release", "status")
    assert out.exit_code == 0 and status.active and status.version == "0.9.36" and status.pull_request == 705 and status.phase == "release"
    assert status.age_minutes == 30 and status.candidate_ci == "failure" and status.acceptance == "success" and status.frozen
    assert status.checks.model_dump() == {"ok": 1, "fail": 1, "run": 1, "skip": 0}
    assert status.blockers == ["candidate CI failed: Test (macos-latest, Python 3.14, integration), Coverage"]
    assert "sm ci one" in status.next and "hz-" not in out.stdout
    text = invoke(rt, ["release", "status"]).stdout
    assert text.splitlines()[0] == "RELEASE v0.9.36  PR #705  train phase release  age 30 min" and "freeze: ON (gate hold)" in text
    assert [c[1:3] for c in gh.calls[:3]] == [["pr", "list"], ["run", "list"], ["run", "view"]] and all(c[0] == "gh" for c in gh.calls)
    assert gh.writes() == []


def test_status_line_is_one_line(rt, gh):
    in_flight(rt, gh, ci="success")
    out = invoke(rt, ["release", "status", "--line"])
    assert out.stdout.splitlines() == ["RELEASE v0.9.36 PR #705 30m: CI success, acceptance success #1, freeze off; "
                                       "train merges when checks are green; verify tag, publish, sm release verify --mini"]


# ----------------------------------------------------------------------------- candidate
def run_row(run_id=77, attempt=1, status="completed", conclusion="failure"):
    return {"id": run_id, "name": "CI", "status": status, "conclusion": conclusion, "run_attempt": attempt, "head_branch": "release/v0.9.40",
            "head_sha": "a" * 40, "event": "pull_request", "created_at": "2026-11-12T18:00:00Z", "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}"}


def candidate(gh, **over):
    row = run_row(**over)
    gh.on("actions/runs?", {"workflow_runs": [row, run_row(run_id=70) | {"name": "Acceptance"}, run_row(run_id=60) | {"created_at": "2026-11-12T10:00:00Z"}]})
    gh.on(f"actions/runs/{row['id']}", row)
    gh.on(f"actions/runs/{row['id']}/jobs", {"jobs": [
        {"id": 1, "name": "Lint", "status": "completed", "conclusion": "success"},
        {"id": 2, "name": "Test (macos-latest, Python 3.14, integration)", "status": "completed", "conclusion": "failure",
         "steps": [{"name": "Run tests", "conclusion": "failure"}]},
        {"id": 3, "name": "Coverage", "status": "completed", "conclusion": "cancelled"},
        {"id": 4, "name": "CI OK", "status": "completed", "conclusion": "failure"}]})
    gh.on("actions/jobs/2/logs", "2026-11-12T18:20:00.0000000Z one\n2026-11-12T18:20:01.0000000Z FAILED tests/x.py::test_y\n")
    gh.on("actions/jobs/3/logs", 404)
    gh.on("actions/jobs/4/logs", "gate failed\n")


def test_candidate_by_version_shows_failed_and_cancelled_jobs_with_log_tails_and_the_rule(rt, gh):
    candidate(gh)
    out, found = doc(rt, area.ReleaseCandidate, "release", "candidate", "0.9.40", "--lines", "1")
    assert out.exit_code == 1 and found.run == "77" and found.version == "0.9.40" and found.first_attempt and not found.counts
    assert [j.name for j in found.failed] == ["Test (macos-latest, Python 3.14, integration)", "CI OK"] and [j.name for j in found.cancelled] == ["Coverage"]
    assert found.failed[0].log_tail == ["FAILED tests/x.py::test_y"] and found.failed[0].failed_steps == ["Run tests"]
    assert found.cancelled[0].log_state.startswith("unknown:") and found.rule.startswith("first-attempt rule:")
    text = invoke(rt, ["release", "candidate", "v0.9.40"]).stdout.splitlines()
    assert text[0] == "candidate run 77 v0.9.40 attempt 1: completed failure"
    assert text[1] == "verdict: RED (failure): 2 failed, 1 cancelled; do not rerun to make it green" and text[2] == area.FIRST_ATTEMPT_RULE
    assert "FAILED Test (macos-latest, Python 3.14, integration) (step: Run tests)" in text and "    FAILED tests/x.py::test_y" in text
    assert gh.writes() == [] and not any("rerun" in line for line in gh.lines())


def test_a_green_rerun_does_not_count_and_a_green_first_attempt_does(rt, gh):
    candidate(gh, attempt=2, conclusion="success")
    out, found = doc(rt, area.ReleaseCandidate, "release", "candidate", "77", "--jobs", "1")
    assert out.exit_code == 1 and not found.first_attempt and not found.counts and "does NOT count" in found.verdict
    assert len(found.failed) == 1 and found.omitted == ["CI OK", "Coverage"]
    candidate(gh, attempt=1, conclusion="success")
    out, found = doc(rt, area.ReleaseCandidate, "release", "candidate", "77")
    assert out.exit_code == 0 and found.counts and found.verdict == "GREEN on its first attempt: it counts"


def test_a_running_candidate_waits_and_an_unknown_target_refuses(rt, gh):
    candidate(gh, status="in_progress", conclusion=None, attempt=2)
    out, found = doc(rt, area.ReleaseCandidate, "release", "candidate", "77")
    assert out.exit_code == 75 and found.verdict == "running (attempt 2): a rerun, it will not count"
    assert invoke(rt, ["release", "candidate", "main"]).exit_code == 2
    gh.on("actions/runs?", {"workflow_runs": []})
    out = invoke(rt, ["release", "candidate", "0.9.41"])
    assert out.exit_code == 1 and "no candidate run for v0.9.41" in out.stderr
    gh.on("actions/runs/5", 404)
    assert invoke(rt, ["release", "candidate", "5"]).exit_code == 1


# ----------------------------------------------------------------------------- verify
def test_verify_checks_pypi_core_release_and_app_release(rt, gh, monkeypatch):
    import urllib.request

    seen = []

    class Reply:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"info": {"home_page": "", "project_urls": {"Homepage": "https://heinzel.team"}}}).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=0: seen.append(request.full_url) or Reply())
    gh.on(f"release list -R {REPO} ", [{"tagName": "v0.9.40", "isLatest": True}])
    gh.on(f"release list -R {REPO}-plugins", [{"tagName": "app-v0.9.40", "isLatest": False}])
    out, found = doc(rt, area.ReleaseVerify, "release", "verify", "0.9.40", "--homepage", "heinzel.team", "--latest")
    assert out.exit_code == 1 and seen == ["https://pypi.org/pypi/heinzel/0.9.40/json"]
    assert found.lines == ["PASS pypi heinzel 0.9.40", "PASS pypi heinzel links to heinzel.team", "PASS release v0.9.40 in heinzel",
                           "PASS release app-v0.9.40 in heinzel-plugins", "FAIL release app-v0.9.40 is latest"]
    assert [c.passed for c in found.checks] == [True, True, True, True, False] and found.mini_live is None
    out = invoke(rt, ["release", "verify", "0.9.40", "--surface", "release"])
    assert out.exit_code == 0 and out.stdout.splitlines() == ["PASS release v0.9.40 in heinzel"]
    out = invoke(rt, ["release", "verify", "--site", "/imprint/", "--contains", "nope"])
    assert out.exit_code == 1 and out.stdout.splitlines() == ["PASS site https://heinzel.team/imprint/", "FAIL site https://heinzel.team/imprint/ contains 'nope'"]


def test_verify_refuses_what_it_cannot_check(rt):
    for args in (["release", "verify"], ["release", "verify", "x"], ["release", "verify", "0.9.40", "--surface", "web"], ["release", "verify", "0.9.40", "--minimum", "0.9.40"]):
        assert invoke(rt, args).exit_code == 2, args


def test_verify_mini_calls_the_read_only_check_of_the_live_service(rt, monkeypatch):
    import sys
    import types

    from steuermaschine_heinzel.native import go

    class Proof:
        live, version, reason = False, "0.9.39", "below the minimum 0.9.40"

        def render(self):
            return ["NOT-LIVE: below the minimum 0.9.40"]

    asked = []
    mini = types.ModuleType("steuermaschine_heinzel.native.mini")
    mini.verify = lambda world, minimum=None: asked.append((world, minimum)) or Proof()
    monkeypatch.setitem(sys.modules, "steuermaschine_heinzel.native.mini", mini)
    monkeypatch.setattr(go, "world", lambda: "the world", raising=False)
    out, found = doc(rt, area.ReleaseVerify, "release", "verify", "--mini", "--minimum", "0.9.40")
    assert out.exit_code == 1 and asked == [("the world", "0.9.40")]
    assert found.mini_live is False and found.mini_version == "0.9.39" and found.lines == ["NOT-LIVE: below the minimum 0.9.40"]


# ----------------------------------------------------------------------------- pins
def core_checkout(path: Path, workflow: bytes) -> None:
    def git(*args):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)

    (path / ".github/workflows").mkdir(parents=True)
    (path / "scripts").mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, capture_output=True)
    for step, content in enumerate((approved.blob(".github/workflows/ci.yml"), workflow)):
        (path / ".github/workflows/ci.yml").write_bytes(content)
        (path / "scripts/release_candidate.py").write_bytes(approved.blob("scripts/release_candidate.py"))
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", f"step {step}")


def test_pins_match_the_reviewed_files_and_a_changed_workflow_prints_the_diff_and_a_review_request(rt):
    reviewed = approved.blob(".github/workflows/ci.yml")
    core_checkout(rt.home / "core", reviewed + b"# one more line\n")
    out, found = doc(rt, area.ReleasePins, "release", "pins", "HEAD~1")
    assert out.exit_code == 0 and [p.matches for p in found.pins] == [True, True] and found.lines == ["ok   .github/workflows/ci.yml", "ok   scripts/release_candidate.py"]
    out, found = doc(rt, area.ReleasePins, "release", "pins", "HEAD")
    assert out.exit_code == 1 and [p.matches for p in found.pins] == [False, True]
    assert found.pins[0].approved == approved.pins()[".github/workflows/ci.yml"] and len(found.pins[0].executed) == 12
    text = "\n".join(found.lines)
    assert "+# one more line" in text and "Review request: pin update for .github/workflows/ci.yml" in text
    assert "release_lib/approved_sources/" in text and "hz-" not in text and "tools/release_candidate_policy.py" not in text


def test_the_pins_are_the_hashes_of_the_reviewed_files_and_the_policy_uses_them():
    import hashlib

    from steuermaschine_heinzel.native.queue_lib import release_candidate
    from steuermaschine_heinzel.native.release_lib import candidate_policy

    assert candidate_policy.APPROVED_SOURCES == approved.pins() == {path: hashlib.sha256(approved.file(path).read_bytes()).hexdigest() for path in approved.FILES}
    assert set(approved.FILES) == {release_candidate.WORKFLOW, release_candidate.RECORDER}


# ----------------------------------------------------------------------------- after, notes-audit, assets
def test_after_prints_the_checklist_in_sm_commands(rt):
    out, found = doc(rt, area.ReleaseAfter, "release", "after", "0.9.40")
    assert out.exit_code == 0 and len(found.steps) == 5 and found.lines[0] == "Post-publish checklist for v0.9.40 (run in order):"
    assert found.steps[0].startswith("1. sm release train resume-ci --dry-run") and "sm release verify --mini" in found.steps[2]
    assert "sm beads stamp-released --version" in found.steps[3] and "sm beads sweep" in found.steps[3]
    assert "hz-" not in out.stdout and invoke(rt, ["release", "after", "next"]).exit_code == 2


def test_notes_audit_names_dropped_combine_members(rt):
    work = rt.home / "work"
    work.mkdir()
    out, found = doc(rt, area.NotesAudit, "release", "notes-audit")
    assert out.exit_code == 1 and found.problem == "CHANGELOG.md unreadable" and found.missing == []

    def git(*args):
        subprocess.run(["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], check=True, capture_output=True)

    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True, capture_output=True)
    (work / "CHANGELOG.md").write_text("# Changelog\n\n## 0.9.40\n\n- combined work (#770, #732)\n")
    git("add", "-A")
    git("commit", "-q", "-m", "feat: combine (#770)\n\nCombines #732 feat: one\nCombines #737 fix: two\n")
    out, found = doc(rt, area.NotesAudit, "release", "notes-audit", "--base", "main")
    assert out.exit_code == 1 and found.missing == ["737"]
    assert invoke(rt, ["release", "notes-audit", "--base", "main"]).stdout.splitlines()[0] == "notes audit: the release notes drop combine members #737"
    (work / "CHANGELOG.md").write_text("# Changelog\n\n## 0.9.40\n\n- combined work (#770, #732, #737)\n")
    out, found = doc(rt, area.NotesAudit, "release", "notes-audit", "--base", "main", "--checkout", str(work))
    assert out.exit_code == 0 and found.missing == [] and found.render()[0].startswith("notes audit: ok")


def test_assets_probes_the_urls_of_the_core_checkout_and_writes_only_its_cache(rt, monkeypatch):
    import urllib.error
    import urllib.request

    core = rt.home / "core" / "scripts"
    core.mkdir(parents=True)
    (core / "tools.toml").write_text('[gitleaks]\nversion = "8.1"\nurl = "https://example.invalid/{version}/{file}"\n[gitleaks.files.darwin-arm64]\nfile = "gitleaks_{version}.tgz"\n')

    class Reply:
        status = 206

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size):
            return b""

    def urlopen(request, timeout=0):
        if "gitleaks" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 503, "down", {}, None)
        return Reply()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    out, found = doc(rt, area.ReleaseAssets, "release", "assets")
    assert out.exit_code == 1 and next((r.name, r.ok, r.status) for r in found.results) == ("gitleaks:darwin-arm64", False, "503")
    assert sum(r.ok for r in found.results) == 2 and found.digest == "ASSETS: BAD 1/3 (gitleaks:darwin-arm64)"
    assert json.loads((rt.home / "assets.json").read_text())["results"][0]["url"] == "https://example.invalid/8.1/gitleaks_8.1.tgz"
    out = invoke(rt, ["release", "assets", "--digest"])
    assert out.exit_code == 0 and out.stdout.strip() == "ASSETS: BAD 1/3 (gitleaks:darwin-arm64)"  # the digest only reports, as before
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=0: Reply())
    out, found = doc(rt, area.ReleaseAssets, "release", "assets", "--stable", "2", "--interval", "7")
    assert out.exit_code == 0 and found.stable is True and rt.providers.clock.slept == [7] and "stable streak 2/2" in out.stderr


def test_the_assets_cache_is_replaced_atomically(tmp_path, monkeypatch):
    from steuermaschine_heinzel.native.release_read_lib import assets

    cache = tmp_path / "cache" / "assets.json"
    assets.save([("a", "u", True, 206)], str(cache))
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(assets.os, "replace", lambda src, dst: replaced.append((src, dst)) or real_replace(src, dst))
    assets.save([("b", "u", False, 504)], str(cache))
    assert len(replaced) == 1 and replaced[0][1] == str(cache) and os.path.dirname(replaced[0][0]) == str(cache.parent)
    assert json.loads(cache.read_text())["results"][0]["name"] == "b" and os.listdir(cache.parent) == ["assets.json"]

    def broken(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(assets.os, "replace", broken)
    with pytest.raises(OSError):
        assets.save([("c", "u", True, 200)], str(cache))
    assert json.loads(cache.read_text())["results"][0]["name"] == "b" and os.listdir(cache.parent) == ["assets.json"]


@pytest.mark.parametrize("argv", [["release", "assets"], ["release", "pins", "origin/main"]])
def test_without_the_core_checkout_setting_assets_and_pins_refuse_and_name_the_key(tmp_path, monkeypatch, gh, argv):
    """No machine path is a default: unset, the command refuses (usage error) and probes or reads nothing."""
    monkeypatch.setenv("HOME", str(tmp_path))
    settings, _ = stack_of(plugin).validate({"heinzel": {"release_assets_cache": str(tmp_path / "assets.json")}}, Path("."))
    assert settings.of("heinzel")["release_core_checkout"] is None
    runtime = Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=True), stack_of(plugin), settings,
                      _providers=fake_providers(commands=gh))
    out = invoke(runtime, argv)
    assert out.exit_code == 2 and "release_core_checkout" in out.stderr and out.stdout == "", out
    assert gh.calls == [] and not (tmp_path / "assets.json").exists()
