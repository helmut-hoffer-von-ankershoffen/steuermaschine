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


def run_env(root: Path, **env: str) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STEUERMASCHINE_")}
    return subprocess.run([str(root / "bin" / "sm")], capture_output=True, text=True, env={**base, **env}, check=False)


def test_only_an_sm_started_with_the_canary_flag_runs_the_canary(tmp_path):
    root = layout(tmp_path)
    (root / "canary").symlink_to("releases/b")
    assert run_env(root).stdout.split()[0] == "a"  # a builder stays on current
    assert run_env(root, STEUERMASCHINE_CANARY="1").stdout.split()[0] == "b"
    (root / "canary").unlink()
    foreign = release(tmp_path / "elsewhere", "evil")
    (root / "canary").symlink_to(foreign)
    assert run_env(root, STEUERMASCHINE_CANARY="1").stdout.split()[0] == "a"  # a canary outside this install is ignored


def test_the_launcher_passes_the_exit_code_on_and_logs_each_tool_error(tmp_path):
    root = layout(tmp_path)
    entry = root / "releases" / "a" / "venv" / "bin" / "steuermaschine"
    entry.write_text("#!/bin/sh\nexit ${CODE}\n")
    assert run_env(root, CODE="1").returncode == 1 and not (root / "exits.log").exists()  # a refusal is no tool error
    assert run_env(root, CODE="70").returncode == 70
    stamp, name, code = (root / "exits.log").read_text().split()
    assert stamp.isdigit() and (name, code) == ("a", "70")
