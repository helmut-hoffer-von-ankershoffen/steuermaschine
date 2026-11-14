"""Promote and rollback end to end in a throwaway install: HOME and SM_STABLE in a temporary directory (R7 of the
dod13 review: no test ran them against a real layout).

The two releases are stubs that log every call (no build, no network, and no review gate: the real `self
migrate-state` is a gated writer, tested in test_core_statemove.py). Everything else runs for real: the switch-pending
file, the order of the state moves of both releases, the launcher, the atomic pointers, the link in ~/.local/bin, and
bin/rollback itself, back and forward again. Build, smoke and the review gate of bin/promote are not run here;
tests/test_promote_script.py covers their decisions.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "bin" / "promote_lib.sh"
ROLLBACK = REPO / "bin" / "rollback"
STUB = """#!/bin/sh
echo "{name} $*" >> "$HOME/calls.log"
case "$*" in
  "self migrate-state --help") exit 0 ;;
  "--format json self migrate-state --list") echo '{{"moves": []}}' ;;
  "self config"|"--format json self config") echo "user file $STEUERMASCHINE_USER_CONFIG pinned $STEUERMASCHINE_PINNED" ;;
esac
"""


def env_for(tmp_path: Path) -> dict[str, str]:
    """A temporary HOME, the throwaway SM_STABLE, and a `ps` that shows no running step (other builders' real steps on
    this machine must not make the test wait)."""
    fake = tmp_path / "fakebin"
    fake.mkdir(exist_ok=True)
    (fake / "ps").write_text("#!/bin/sh\necho '1 /sbin/launchd'\n")
    (fake / "ps").chmod(0o755)
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
    keep = {k: v for k, v in os.environ.items() if not k.startswith(("STEUERMASCHINE_", "SM_", "HZ_", "XDG_"))}
    return {**keep, "HOME": str(home), "SM_STABLE": str(tmp_path / "stable"), "PATH": f"{fake}:/usr/bin:/bin",
            "XDG_CONFIG_HOME": str(home / ".config"), "XDG_STATE_HOME": str(home / ".local" / "state"), "XDG_CACHE_HOME": str(home / ".cache")}


def release(stable: Path, name: str) -> Path:
    """releases/NAME with its own user config and a stub `steuermaschine` that logs its calls to $HOME/calls.log."""
    rel = stable / "releases" / name
    (rel / "venv" / "bin").mkdir(parents=True)
    (rel / "user-config.toml").write_text(f'# release {name}\n[project]\nname = "p"\nplugin = "heinzel"\n')
    entry = rel / "venv" / "bin" / "steuermaschine"
    entry.write_text(STUB.format(name=name))
    entry.chmod(0o755)
    return rel


def promote(env: dict[str, str], name: str) -> subprocess.CompletedProcess[str]:
    """The switch half of bin/promote, step for step: pending file, wait for running steps, state, launcher, pointers."""
    script = f'''
set -u
. "{LIB}"
stable="$SM_STABLE"; rel="$stable/releases/{name}"; cand="$rel/venv/bin/steuermaschine"
mark_switch_pending; trap clear_switch_pending EXIT
wait_idle 0 0 >/dev/null || exit 3
live_rel=""; [ -x "$stable/current/venv/bin/steuermaschine" ] && live_rel="$(cd "$stable/current" && pwd -P)"
migrate_state_to "$cand" "$rel/user-config.toml" "${{live_rel:+$live_rel/venv/bin/steuermaschine}}" "${{live_rel:+$live_rel/user-config.toml}}" || exit 4
install_launcher "$stable" || exit 5
point_current "$stable" "{name}" || exit 6
ln -sfn "$stable/bin/sm" "$HOME/.local/bin/sm"
'''
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=120)


def live(env: dict[str, str]) -> str:
    """Which release's user config the live `sm` runs with, as `sm self config` reports it (the launcher sets it)."""
    out = subprocess.run([str(Path(env["HOME"]) / ".local" / "bin" / "sm"), "self", "config"],
                         capture_output=True, text=True, env=env, timeout=60, cwd=env["HOME"])
    assert out.returncode == 0, out.stderr
    words = out.stdout.split()
    config, pinned = Path(words[2]), Path(words[4])
    assert config == pinned / "user-config.toml", out.stdout  # code and config switch together
    return pinned.name


def calls(env: dict[str, str]) -> list[str]:
    log = Path(env["HOME"]) / "calls.log"
    lines = log.read_text().splitlines() if log.exists() else []
    log.unlink(missing_ok=True)
    return [line for line in lines if "--help" not in line and "self config" not in line]


def pointers(stable: Path) -> tuple[str, str | None]:
    previous = stable / "previous"
    return os.readlink(stable / "current"), os.readlink(previous) if previous.is_symlink() else None


def test_promote_twice_then_rollback_back_and_forward(tmp_path):
    env = env_for(tmp_path)
    stable = Path(env["SM_STABLE"])
    release(stable, "r1")
    release(stable, "r2")
    pending = Path(env["HOME"]) / ".cache" / "sm-promote-switch-pending"

    first = promote(env, "r1")
    assert first.returncode == 0, first.stderr
    assert pointers(stable) == ("releases/r1", None) and live(env) == "r1"
    calls(env)
    second = promote(env, "r2")
    assert second.returncode == 0, second.stderr
    # the live release moves back what only it knows, then the new one moves forward; both before the switch
    moves = calls(env)
    assert len(moves) == 3 and moves[0] == "r2 --format json self migrate-state --list" and moves[2] == "r2 self migrate-state"
    assert moves[1].startswith("r1 self migrate-state --back --keep ")
    assert pointers(stable) == ("releases/r2", "releases/r1") and live(env) == "r2"
    assert not pending.exists()  # each switch removed its own pending file

    back = subprocess.run(["bash", str(ROLLBACK)], capture_output=True, text=True, env=env, timeout=120)
    assert back.returncode == 0, back.stderr
    assert "rollback: live is now releases/r1 (was releases/r2)" in back.stderr
    moves = calls(env)  # the same order towards the previous release
    assert len(moves) == 3 and moves[0] == "r1 --format json self migrate-state --list" and moves[2] == "r1 self migrate-state"
    assert moves[1].startswith("r2 self migrate-state --back --keep ")
    assert pointers(stable) == ("releases/r1", "releases/r2") and live(env) == "r1" and not pending.exists()

    forward = subprocess.run(["bash", str(ROLLBACK)], capture_output=True, text=True, env=env, timeout=120)
    assert forward.returncode == 0, forward.stderr
    assert pointers(stable) == ("releases/r2", "releases/r1") and live(env) == "r2"


def test_rollback_without_a_previous_release_refuses_and_switches_nothing(tmp_path):
    env = env_for(tmp_path)
    stable = Path(env["SM_STABLE"])
    release(stable, "r1")
    assert promote(env, "r1").returncode == 0
    done = subprocess.run(["bash", str(ROLLBACK)], capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 1 and "no previous release" in done.stderr and pointers(stable) == ("releases/r1", None)
