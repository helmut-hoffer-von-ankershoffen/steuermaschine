"""Test isolation for the whole repository, structural: no test can touch this machine's real files.

1. Every test runs with HOME, the XDG directories and the steuermaschine state home pointing into a temporary
   sandbox, and without the settings variables of the developer's shell.
2. Opening a file for writing under the real home directory, outside this checkout and outside the temporary
   directories, fails the test at the line that does it.
3. The real files a test once damaged are fingerprinted before and after the session; a change fails the run.
4. The one process runner refuses a real `git push` or GitHub write under pytest (steuermaschine.defaults.Subprocess).
"""

from __future__ import annotations

import builtins
import hashlib
import io
import os
import pwd
import tempfile
from pathlib import Path

import pytest

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
ROOT = Path(__file__).resolve().parent
TEMP = tuple(str(Path(p).resolve()) for p in {tempfile.gettempdir(), "/tmp", "/private/tmp", "/var/folders", "/private/var/folders"} if Path(p).exists())
# Fingerprinted before and after the session. The live tracking document is NOT here: people and agents edit it while a
# gate runs, which failed gates that no test caused. Writes to it are still refused by the guards below (open, and the
# rename/replace an atomic writer uses), and no setting defaults to it.
WATCHED = [REAL_HOME / ".config" / "steuermaschine" / "config.toml"]


def _allowed(path: str) -> bool:
    try:
        full = str(Path(path).resolve())
    except OSError:
        return True
    if not full.startswith(str(REAL_HOME)):
        return True
    return full.startswith(str(ROOT)) or full.startswith(TEMP) or "/.cache/uv/" in full or "/site-packages/" in full or "/__pycache__/" in full


def _guarded(real):
    def opener(file, mode="r", *args, **kwargs):
        if isinstance(file, (str, os.PathLike)) and any(c in str(mode) for c in "wax+") and not _allowed(os.fspath(file)):
            raise PermissionError(f"test isolation: a test opened a real file of this machine for writing: {file}. "
                                  "Use tmp_path; settings must not default to a real path.")
        return real(file, mode, *args, **kwargs)

    return opener



def _guarded_move(real):
    """os.replace / os.rename onto a real file of this machine: how an atomic writer would bypass the open guard."""

    def move(src, dst, *args, **kwargs):
        if isinstance(dst, (str, os.PathLike)) and not _allowed(os.fspath(dst)):
            raise PermissionError(f"test isolation: a test replaced a real file of this machine: {dst}. Use tmp_path.")
        return real(src, dst, *args, **kwargs)

    return move

def _fingerprint() -> dict:
    """The content digest of each watched file (None: absent). Content, not mtime: a person or a tool that rewrites the
    file with the same bytes while a gate runs is no change a test made (bead t84w)."""
    out = {}
    for path in WATCHED:
        try:
            out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            out[str(path)] = None
    return out


def pytest_configure(config):
    """A sandbox home already while modules are collected: a module that resolves a state path when it is imported
    (the moved libraries do) never sees the real home. Each test then gets its own sandbox (_sandbox_home)."""
    session = Path(tempfile.mkdtemp(prefix="sm-test-session-home-"))
    for name, value in (("HOME", session), ("XDG_CONFIG_HOME", session / ".config"), ("XDG_STATE_HOME", session / ".local" / "state"),
                        ("XDG_CACHE_HOME", session / ".cache"), ("STEUERMASCHINE_HOME", session / ".local" / "state" / "steuermaschine")):
        os.environ[name] = str(value)


@pytest.fixture(scope="session", autouse=True)
def _real_files_untouched():
    before = _fingerprint()
    real_open, real_io_open = builtins.open, io.open
    real_replace, real_rename = os.replace, os.rename
    builtins.open = io.open = _guarded(real_open)
    os.replace, os.rename = _guarded_move(real_replace), _guarded_move(real_rename)
    yield
    builtins.open, io.open = real_open, real_io_open
    os.replace, os.rename = real_replace, real_rename
    changed = [path for path, mark in _fingerprint().items() if mark != before[path]]
    assert not changed, f"test isolation: the test session changed real files of this machine: {changed}"


@pytest.fixture(autouse=True)
def _sandbox_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("STEUERMASCHINE_HOME", str(home / ".local" / "state" / "steuermaschine"))
    for name in list(os.environ):
        if name.startswith("SM_") or name in ("STEUERMASCHINE_PLUGIN", "STEUERMASCHINE_PROJECT", "STEUERMASCHINE_USER_CONFIG", "STEUERMASCHINE_RELEASE", "STEUERMASCHINE_PINNED") or name.startswith("HZ_"):
            monkeypatch.delenv(name, raising=False)
    # the worker caps an outer `sm gate run` exports: a caller's lower value wins, so they would change what tests see
    for name in ("VITEST_MAX_WORKERS", "VITEST_MAX_THREADS", "VITEST_MAX_FORKS", "PYTEST_XDIST_AUTO_NUM_WORKERS"):
        monkeypatch.delenv(name, raising=False)
    yield home
