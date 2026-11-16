"""The hosted proof must retain the same coverage and identity boundaries."""
import importlib.util
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
spec = importlib.util.spec_from_file_location("hosted_promotion", BIN / "hosted_promotion.py")
hosted = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hosted)

FILES = {"src/steuermaschine/a.py", "plugins/heinzel/src/steuermaschine_heinzel/a.py",
         "plugins/heinzel/src/steuermaschine_heinzel/native/x_lib/a.py"}
BASELINE = {"coverage core %": 95, "coverage plugin new code %": 98,
            "coverage plugin moved libraries %": 95}


def xml(files=FILES, hits=1):
    classes = "".join(f'<class filename="{name}"><lines><line number="1" hits="{hits}"/></lines></class>' for name in sorted(files))
    return f'<coverage lines-valid="{len(files)}" lines-covered="{len(files) * hits}" branches-valid="0" branches-covered="0"><packages>{classes}</packages></coverage>'.encode()


def test_complete_coverage_passes():
    assert hosted.coverage(xml(), FILES, BASELINE) == {"core": 100, "plugin": 100, "new": 100, "moved": 100}


def test_missing_or_duplicate_file_refused():
    with pytest.raises(hosted.Refused):
        hosted.coverage(xml(FILES - {next(iter(FILES))}), FILES, BASELINE)
    data = xml().replace(b"</packages>", b'<class filename="src/steuermaschine/a.py"><lines/></class></packages>')
    with pytest.raises(hosted.Refused):
        hosted.coverage(data, FILES, BASELINE)


def test_debt_threshold_and_totals_fail_closed():
    with pytest.raises(hosted.Refused):
        hosted.coverage(xml(hits=0), FILES, BASELINE)
    with pytest.raises(hosted.Refused):
        hosted.coverage(xml().replace(b'lines-covered="3"', b'lines-covered="2"'), FILES, BASELINE)


def test_inventory_must_be_complete():
    with pytest.raises(hosted.Refused):
        hosted.inventory({"total_count": 2, "jobs": [{}]}, "jobs")


def test_required_step_and_hosted_runner_cannot_be_omitted():
    job = {"status": "completed", "conclusion": "success", "runner_id": 42,
           "runner_name": "GitHub Actions 42", "runner_group_id": 0,
           "runner_group_name": "GitHub Actions", "steps": []}
    with pytest.raises(hosted.Refused):
        hosted.good_job(job, ("Tests",))
    job["steps"] = [{"name": "Tests", "status": "completed", "conclusion": "success"}]
    hosted.good_job(job, ("Tests",))
    job["labels"] = ["self-hosted"]
    with pytest.raises(hosted.Refused):
        hosted.good_job(job, ("Tests",))


def test_hosted_evidence_rejects_identity_and_artifact_changes(monkeypatch):
    import copy
    import hashlib
    import io
    import zipfile

    sha = "a" * 40
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("coverage.xml", xml())
    data = archive.getvalue()
    repo = "owner/repo"
    check_steps = ("Public history guard (every pushed or proposed commit descends from the public root)",
                   "Private-term deny-list (tree and commit messages)",
                   "Conventional Commits (the commits this push or PR adds)", "Run make install", "Run make check",
                   "Run make audit", "Coverage summary",
                   "Run actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9")

    def job(name, steps):
        return {"name": name, "run_id": 1, "run_attempt": 1, "head_sha": sha, "status": "completed",
                "conclusion": "success", "runner_id": 10, "runner_name": "GitHub Actions 10",
                "runner_group_id": 0, "runner_group_name": "GitHub Actions", "labels": ["ubuntu-latest"],
                "steps": [{"name": step, "status": "completed", "conclusion": "success"} for step in steps]}

    run = {"id": 1, "run_attempt": 1, "status": "completed", "conclusion": "success", "event": "push",
           "path": ".github/workflows/ci.yml", "head_sha": sha, "head_branch": "feature",
           "repository": {"id": 3}, "head_repository": {"id": 3}}
    jobs = [job("check", check_steps), job("Test (ubuntu-24.04, Python 3.15)", ("Tests",))]
    artifact = {"id": 5, "name": "coverage", "expired": False, "size_in_bytes": len(data),
                "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                "workflow_run": {"id": 1, "head_sha": sha, "repository_id": 3, "head_repository_id": 3}}
    state = {"run": run, "jobs": jobs, "artifact": artifact}

    def api(path):
        if path == f"repos/{repo}":
            return {"id": 3}
        if path.endswith("/runs/1"):
            return state["run"]
        if "/jobs?" in path:
            return {"total_count": len(state["jobs"]), "jobs": state["jobs"]}
        if "/artifacts?" in path:
            return {"total_count": 1, "artifacts": [state["artifact"]]}
        raise AssertionError(path)

    def command(*args):
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return sha.encode()
        if args[:2] == ("git", "status"):
            return b""
        if args[:3] == ("git", "remote", "get-url"):
            return f"https://github.com/{repo}.git".encode()
        if args[:3] == ("git", "branch", "--show-current"):
            return b"feature"
        if args[:2] == ("git", "ls-files"):
            return ("\0".join(FILES) + "\0").encode()
        if args[:2] == ("gh", "api"):
            return data
        raise AssertionError(args)

    monkeypatch.setattr(hosted, "api", api)
    monkeypatch.setattr(hosted, "command", command)
    monkeypatch.setattr(hosted, "trusted_sources", lambda _: None)
    assert hosted.verify(repo, 1, sha, Path("."))["artifact"] == 5
    for section, key, bad in (("run", "head_sha", "b" * 40), ("run", "event", "pull_request"),
                              ("run", "run_attempt", 2), ("run", "path", "other.yml"),
                              ("artifact", "digest", "sha256:" + "0" * 64), ("artifact", "expired", True)):
        before = copy.deepcopy(state[section])
        state[section][key] = bad
        with pytest.raises(hosted.Refused):
            hosted.verify(repo, 1, sha, Path("."))
        state[section] = before
    saved = state["jobs"].pop()
    with pytest.raises(hosted.Refused):
        hosted.verify(repo, 1, sha, Path("."))
    state["jobs"].append(saved)
    state["jobs"][0]["steps"][0]["conclusion"] = "skipped"
    with pytest.raises(hosted.Refused):
        hosted.verify(repo, 1, sha, Path("."))


def test_failed_hosted_proof_cannot_use_cached_gate_or_switch(tmp_path):
    import os
    import shutil
    import subprocess

    root = tmp_path / "checkout"
    (root / "bin").mkdir(parents=True)
    for name in ("promote", "promote_lib.sh"):
        shutil.copy(BIN / name, root / "bin" / name)
    def git(*args):
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    git("add", "bin")
    git("commit", "-qm", "test: candidate")
    sha = git("rev-parse", "HEAD")
    stable = tmp_path / "stable"
    (stable / "gate-passed").mkdir(parents=True)
    (stable / "gate-passed" / sha).write_text("cached\n")
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "uv").write_text("#!/bin/sh\nexit 1\n")
    (fake / "uv").chmod(0o755)
    env = dict(os.environ, SM_STABLE=str(stable), HOME=str(tmp_path), PATH=f"{fake}:/usr/bin:/bin")
    result = subprocess.run(["bash", str(root / "bin/promote"), "--hosted-run", "1", "--hosted-repo", "owner/repo"],
                            env=env, text=True, capture_output=True, timeout=10, check=False)
    assert result.returncode != 0 and "hosted evidence failed" in result.stderr
    assert not (stable / "current").exists() and not (stable / "releases").exists()
    assert (stable / "gate-passed" / sha).read_text() == "cached\n"


def test_trusted_gate_sources_must_match(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    installed = tmp_path / "installed"
    for source in (candidate, installed):
        for name in hosted.TRUSTED:
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("trusted")
    monkeypatch.setattr(hosted, "ROOT", candidate)
    hosted.trusted_sources(installed)
    (candidate / hosted.TRUSTED[0]).write_text("changed")
    with pytest.raises(hosted.Refused):
        hosted.trusted_sources(installed)


def test_branch_counts_and_coverage_rounding_respect_debt():
    classes = []
    for name in sorted(FILES):
        hit = 190 if "/native/" in name else 195 if name.startswith(hosted.PLUGIN) else 190
        classes.append(f'<class filename="{name}"><lines><line number="1" hits="1" branch="true" '
                       f'condition-coverage="0% ({hit - 1}/199)"/></lines></class>')
    xml_data = ('<coverage lines-valid="3" lines-covered="3" branches-valid="597" '
                'branches-covered="572"><packages>' + ''.join(classes) + '</packages></coverage>').encode()
    result = hosted.coverage(xml_data, FILES, BASELINE)
    assert result['core'] == 95 and result['new'] == 98 and result['moved'] == 95
    below = xml_data.replace(b'(194/199)', b'(193/199)').replace(b'branches-covered="572"', b'branches-covered="571"')
    with pytest.raises(hosted.Refused):
        hosted.coverage(below, FILES, BASELINE)
