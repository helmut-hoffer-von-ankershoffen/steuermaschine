"""Exercise check ordering and private promotion diagnostics with fake gates."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_long_gate_emits_heartbeat_and_keeps_full_output(tmp_path, capsys, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "bin"))
    import check_progress

    class Child:
        calls = 0

        def wait(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("fixture", timeout)
            return 0

    def start(command, stdout, stderr):
        stdout.write(b"complete detail\n")
        return Child()

    monkeypatch.setattr(check_progress.subprocess, "Popen", start)
    output = tmp_path / "gate.log"
    code = check_progress.run("fixture", output, ["fixture"], interval=0.02)
    assert code == 0 and "check: RUN fixture" in capsys.readouterr().out
    assert output.read_text().strip() == "complete detail"


def fake_check(tmp_path: Path, fail: str = "", fast: bool = False):
    project = tmp_path / "project"
    (project / "bin").mkdir(parents=True)
    (project / ".venv/bin").mkdir(parents=True)
    (project / "tests/projects/toy").mkdir(parents=True)
    for name in ("check", "check_progress.py"):
        shutil.copy(ROOT / "bin" / name, project / "bin" / name)
    (project / ".venv/bin/python").symlink_to(sys.executable)
    fake = tmp_path / "fake"
    fake.mkdir()
    uv = fake / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TRACE"\n'
                  'case "$*" in *"$FAIL_MATCH"*) [ -z "$FAIL_MATCH" ] || { echo "full diagnostic text"; exit 1; };; esac\n'
                  'echo ok\n')
    uv.chmod(0o755)
    exe = project / ".venv/bin/steuermaschine"
    exe.write_text("#!/bin/sh\necho ok\n")
    exe.chmod(0o755)
    trace, details = tmp_path / "trace", tmp_path / "details"
    details.mkdir()
    env = os.environ | {"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(tmp_path), "TRACE": str(trace),
                        "CHECK_DETAIL_DIR": str(details), "FAIL_MATCH": fail}
    result = subprocess.run(["bash", str(project / "bin/check"), *(["--fast"] if fast else [])],
                            env=env, capture_output=True, text=True, check=False, timeout=30)
    return result, trace.read_text().splitlines(), "\n".join(path.read_text() for path in details.iterdir())


def test_source_failure_stops_before_tests_and_retains_full_diagnostic(tmp_path):
    result, calls, details = fake_check(tmp_path, fail="ruff check")
    assert result.returncode == 1 and "check: FAIL ruff" in result.stdout
    assert not any("pytest" in call for call in calls)
    assert "full diagnostic text" in details and "check: START" in result.stdout


def test_success_runs_source_tests_and_both_coverage_gates(tmp_path):
    result, calls, _ = fake_check(tmp_path)
    assert result.returncode == 0 and "PROMOTE GATE GREEN" in result.stdout
    source = next(i for i, call in enumerate(calls) if "debt.py --gate --source-only" in call)
    tests = next(i for i, call in enumerate(calls) if "pytest" in call)
    coverage = next(i for i, call in enumerate(calls) if "debt.py --gate --coverage-only" in call)
    assert source < tests < coverage
    assert sum("coverage report" in call for call in calls) == 2


def test_coverage_failure_still_refuses_after_tests(tmp_path):
    result, calls, _ = fake_check(tmp_path, fail="--coverage-only")
    assert result.returncode == 1 and "coverage debt ratchet" in result.stdout
    assert any("pytest" in call for call in calls)


def test_fast_keeps_static_and_tests_but_omits_debt_and_coverage(tmp_path):
    result, calls, _ = fake_check(tmp_path, fast=True)
    assert result.returncode == 0 and any("pytest" in call for call in calls)
    assert not any("debt.py" in call or "coverage report" in call for call in calls)


def test_failed_promotion_keeps_private_head_bound_logs_without_receipt(tmp_path):
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    for name in ("promote", "promote_lib.sh"):
        shutil.copy(ROOT / "bin" / name, repo / "bin" / name)
    check = repo / "bin/check"
    check.write_text('#!/bin/sh\necho "complete private detail" > "$CHECK_DETAIL_DIR/gate.test.log"\n'
                     'echo "check: FAIL tests"\nexit 1\n')
    check.chmod(0o755)
    fake = tmp_path / "fake"
    fake.mkdir()
    git = fake / "git"
    git.write_text('#!/bin/sh\ncase "$*" in "status --porcelain") exit 0;; "rev-parse HEAD") printf "%040d\\n" 1;; esac\n')
    git.chmod(0o755)
    stable = tmp_path / "stable"
    env = os.environ | {"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(tmp_path), "SM_STABLE": str(stable)}
    result = subprocess.run(["bash", str(repo / "bin/promote"), "--check"], env=env, capture_output=True, text=True,
                            check=False, timeout=30)
    logs = list((stable / "gate-logs").glob("*"))
    assert result.returncode == 1 and len(logs) == 1
    assert logs[0].name.startswith("0" * 39 + "1.") and logs[0].stat().st_mode & 0o077 == 0
    assert "complete private detail" in (logs[0] / "gate.test.log").read_text()
    assert "check: FAIL tests" in (logs[0] / "summary.log").read_text()
    assert str(logs[0]) in result.stderr and not (stable / "gate-passed").exists()
