"""The stable launcher that bin/promote writes: `current` is resolved once, and a pin counts only for a release of the
same install. Runs the launcher text of bin/promote_lib.sh against a stub layout (no promote, no network)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def launcher_text() -> str:
    text = (REPO / "bin" / "promote_lib.sh").read_text()  # install_launcher writes it
    return text.split("<<'SH'\n", 1)[1].split("\nSH\n", 1)[0] + "\n"


def release(root: Path, name: str) -> Path:
    rel = root / "releases" / name
    (rel / "venv" / "bin").mkdir(parents=True)
    entry = rel / "venv" / "bin" / "steuermaschine"
    entry.write_text(f'#!/bin/sh\necho "{name} $STEUERMASCHINE_PINNED"\n')
    entry.chmod(0o755)
    return rel


def layout(tmp_path: Path) -> Path:
    root = tmp_path / "stable"
    release(root, "a")
    release(root, "b")
    (root / "current").symlink_to("releases/a")
    (root / "bin").mkdir()
    (root / "bin" / "sm").write_text(launcher_text())
    (root / "bin" / "sm").chmod(0o755)
    return root


def run(root: Path, pin: str | None = None) -> list[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STEUERMASCHINE_")}
    if pin is not None:
        env["STEUERMASCHINE_PINNED"] = pin
    out = subprocess.run([str(root / "bin" / "sm")], capture_output=True, text=True, env=env, check=True).stdout
    return out.split()


def test_without_a_pin_current_is_resolved_and_passed_on(tmp_path):
    root = layout(tmp_path)
    name, pinned = run(root)
    assert name == "a" and pinned == str((root / "releases" / "a").resolve())


def test_a_pin_to_a_release_of_this_install_wins_over_current(tmp_path):
    root = layout(tmp_path)
    assert run(root, str(root / "releases" / "b"))[0] == "b"


def test_a_pin_outside_this_install_is_ignored(tmp_path):
    root = layout(tmp_path)
    foreign = release(tmp_path / "elsewhere", "evil")
    assert run(root, str(foreign))[0] == "a"
    assert run(root, str(root / "releases" / "b" / "venv"))[0] == "a"  # deeper than one release
    assert run(root, str(tmp_path / "missing"))[0] == "a"



def run_env(root: Path, script: str = "sm", **env: str) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STEUERMASCHINE_")}
    return subprocess.run([str(root / "bin" / script)], capture_output=True, text=True, env={**base, **env}, check=False)


def with_canary_launcher(root: Path) -> Path:
    import importlib.util

    spec = importlib.util.spec_from_file_location("release_ci_for_launcher", REPO / "bin" / "release_ci.py")
    assert spec and spec.loader
    rc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rc)
    (root / "bin" / "sm-canary").write_text(rc.CANARY_LAUNCHER)
    (root / "bin" / "sm-canary").chmod(0o755)
    return root


def test_bin_sm_never_runs_the_canary_and_only_sm_canary_does(tmp_path):
    root = with_canary_launcher(layout(tmp_path))
    assert run_env(root, "sm-canary").stdout.split()[0] == "a"  # no canary: sm-canary is bin/sm
    (root / "canary").symlink_to("releases/b")
    assert run_env(root).stdout.split()[0] == "a" and run_env(root, STEUERMASCHINE_CANARY="1").stdout.split()[0] == "a"  # builders
    name, pinned = run_env(root, "sm-canary").stdout.split()
    assert name == "b" and pinned == str((root / "releases" / "b").resolve())  # what it starts stays on b through the pin
    (root / "canary").unlink()
    (root / "canary").symlink_to(release(tmp_path / "elsewhere", "evil"))
    assert run_env(root, "sm-canary").stdout.split()[0] == "a"  # a canary outside this install is ignored


def test_a_timeout_kills_the_real_sm_not_only_a_wrapper(tmp_path):
    """Both launchers exec: the process a caller's timeout signals IS sm, so no orphan keeps the caller's pipes open."""
    import signal
    import time

    root = with_canary_launcher(layout(tmp_path))
    (root / "canary").symlink_to("releases/b")
    for name in ("a", "b"):
        entry = root / "releases" / name / "venv" / "bin" / "steuermaschine"
        entry.write_text(f"#!/bin/sh\necho $$ > {tmp_path}/pid-{name}\nexec sleep 30\n")
    for script, name in (("sm", "a"), ("sm-canary", "b")):
        env = {k: v for k, v in os.environ.items() if not k.startswith("STEUERMASCHINE_")}
        proc = subprocess.Popen([str(root / "bin" / script)], stdout=subprocess.PIPE, env=env)
        pid_file = tmp_path / f"pid-{name}"
        for _ in range(100):
            if pid_file.is_file() and pid_file.read_text().strip():
                break
            time.sleep(0.05)
        assert int(pid_file.read_text()) == proc.pid  # exec: sm runs in the launcher's own process
        proc.send_signal(signal.SIGTERM)
        assert proc.communicate(timeout=10)[0] == b""  # the pipe closes: nothing else holds it
        assert proc.returncode == -signal.SIGTERM


def test_a_stable_release_logs_its_own_exit_70(tmp_path):
    from steuermaschine import app

    rel = tmp_path / "stable" / "releases" / "v0.1.1"
    rel.mkdir(parents=True)
    app.record_tool_error(1, str(rel), 100.0)  # a refusal is no tool error
    app.record_tool_error(70, None, 100.0)  # not a stable install
    app.record_tool_error(70, str(tmp_path / "elsewhere"), 100.0)
    assert not (tmp_path / "stable" / "exits.log").exists()
    app.record_tool_error(70, str(rel), 100.0)
    assert (tmp_path / "stable" / "exits.log").read_text() == "100 v0.1.1 70\n"


def test_main_records_the_exit_70_it_raises(tmp_path, monkeypatch):
    from steuermaschine import app

    seen: list = []
    monkeypatch.setattr(app, "_main", lambda argv: (_ for _ in ()).throw(SystemExit(70)))
    monkeypatch.setattr(app, "record_tool_error", lambda code, pinned: seen.append(code))
    try:
        app.main([])
    except SystemExit as done:
        assert done.code == 70
    assert seen == [70]


def test_the_exit_code_survives_a_failing_error_record(monkeypatch):
    """Gate review of PR 63, finding 3: recording the tool error never replaces the original exit code."""
    import pytest

    from steuermaschine import app

    monkeypatch.setattr(app, "_main", lambda argv=None: (_ for _ in ()).throw(SystemExit(70)))
    monkeypatch.setattr(app.config_mod, "environment", lambda: (_ for _ in ()).throw(ValueError("bad environment")))
    with pytest.raises(SystemExit) as exit_info:
        app.main([])
    assert exit_info.value.code == 70
