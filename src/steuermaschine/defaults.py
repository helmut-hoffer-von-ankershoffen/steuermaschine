"""Generic providers that ship in core: file store, system clock, stderr notifier, brief files, git, no admission."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .protocols import Busy, Prepared, Providers, Result

SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe(name: str) -> str:
    return SAFE.sub("_", name).strip("_") or "_"


class SystemClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FileStore:
    """JSON files under one directory: <root>/state/<kind>/<key>.json, <root>/receipts/..., <root>/locks/..."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, area: str, kind: str, key: str) -> Path:
        return self.root / area / kind / f"{safe(key)}.json"

    def load(self, kind: str, key: str) -> dict[str, Any] | None:
        try:
            return dict(json.loads(self._path("state", kind, key).read_text()))
        except FileNotFoundError:
            return None

    def save(self, kind: str, key: str, data: dict[str, Any]) -> None:
        path = self._path("state", kind, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        tmp.replace(path)  # atomic: a reader sees the old or the new file, never half

    def keys(self, kind: str) -> list[str]:
        return sorted(p.stem for p in (self.root / "state" / kind).glob("*.json"))

    def delete(self, kind: str, key: str) -> None:
        self._path("state", kind, key).unlink(missing_ok=True)

    @contextmanager
    def lock(self, name: str) -> Any:
        path = self.root / "locks" / f"{safe(name)}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Busy(name) from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def put_receipt(self, kind: str, key: str, data: dict[str, Any]) -> None:
        path = self._path("receipts", kind, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        draft = path.with_name(f".{path.name}.{os.getpid()}.draft")  # whole or not at all: a reader never sees half a receipt
        draft.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        try:
            os.link(draft, path)  # a receipt is written once: FileExistsError when it is there
        finally:
            draft.unlink()

    def get_receipt(self, kind: str, key: str) -> dict[str, Any] | None:
        try:
            return dict(json.loads(self._path("receipts", kind, key).read_text()))
        except FileNotFoundError:
            return None


class ReadOnlyStore:
    """What `go` gets: reads pass through, every write is a bug."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def load(self, kind: Any, key: Any) -> Any:
        return self._inner.load(kind, key)

    def keys(self, kind: Any) -> Any:
        return self._inner.keys(kind)

    def get_receipt(self, kind: Any, key: Any) -> Any:
        return self._inner.get_receipt(kind, key)

    def _refuse(self, *_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError("this command is read-only: it must not write state, receipts or locks")

    save = put_receipt = lock = delete = _refuse


class StderrNotifier:
    def notify(self, level: str, key: str, text: str) -> None:
        print(f"{level.upper()} {key}: {text}", file=sys.stderr)


class BriefFiles:
    """The default AgentRunner: writes the brief to a file and leaves starting an agent to the caller."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def brief(self, role: str, title: str, text: str) -> str:
        path = self.root / f"{safe(role)}-{safe(title)}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return str(path)

    def spawn(self, role: str, brief_ref: str, model: str = "") -> str:
        raise NotImplementedError("no agent runner is configured: start an agent yourself and give it " + brief_ref)


class Subprocess:
    """The one process runner. Everything that starts a process goes through a `Commands` provider."""

    @staticmethod
    def _refuse_in_tests(argv: list[str]) -> None:
        """Under pytest the real runner never pushes and never calls a GitHub write: a test passes a fake instead."""
        from .config import environment

        env = environment()
        if env.pytest_current_test is None or env.sm_test_allow_local_push == " ".join(argv[:2]):
            return
        words = [str(a) for a in argv]
        name = words[0].rsplit("/", 1)[-1] if words else ""
        writes = (name == "git" and "push" in words and not any(w.startswith(("/", "file:")) for w in words[words.index("push"):])) or (
            name == "gh" and (tuple(words[1:3]) in {("pr", "merge"), ("pr", "create"), ("pr", "close"), ("pr", "comment"), ("variable", "set"), ("workflow", "run"),
                                                     ("run", "cancel"), ("run", "rerun"), ("release", "create")}
                              or (words[1:2] == ["api"] and any(w in ("-X", "--method", "-f", "-F", "--field", "--input") for w in words) and "GET" not in words)))
        if writes:
            raise RuntimeError(f"a test tried to run a real write through the process runner: {' '.join(words[:6])}. Pass a fake `commands` provider.")

    def run(self, argv: list[str], cwd: str | None = None, timeout: float | None = None, env: dict[str, Any] | None = None) -> Result:
        self._refuse_in_tests(argv)
        try:
            p = subprocess.run(argv, cwd=cwd, timeout=timeout, env=env, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            return Result(127, "", str(exc))
        except subprocess.TimeoutExpired:
            return Result(124, "", f"timed out after {timeout}s: {argv[0]}")
        return Result(p.returncode, p.stdout, p.stderr)

    def run_bytes(self, argv: list[str], cwd: str, timeout: float, env: dict[str, str], *, input: bytes | None = None) -> tuple[int, bytes]:
        """Capture literal output for callers that must not decode or rewrite it."""
        self._refuse_in_tests(argv)
        try:
            p = subprocess.run(argv, cwd=cwd, timeout=timeout, env=env, input=input, capture_output=True, check=False)
        except FileNotFoundError:
            return 127, b""
        except subprocess.TimeoutExpired:
            return 124, b""
        return p.returncode, p.stdout

    def passthrough(self, argv: list[str], cwd: str | None = None, env: dict[str, Any] | None = None, timeout: float | None = None) -> int:
        self._refuse_in_tests(argv)
        try:
            return subprocess.call(argv, cwd=cwd, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"timed out after {timeout}s: {argv[0]}", file=sys.stderr)
            return 124
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 127


class GitVcs:
    def __init__(self, commands: Any, base_ref: str = "origin/main") -> None:
        self.c, self.base_ref = commands, base_ref

    def _git(self, worktree: str, *args: str) -> str:
        res = self.c.run(["git", "-C", worktree, *args], timeout=60)
        if res.rc:
            raise RuntimeError(f"git {' '.join(args)} failed in {worktree}: {res.err.strip()[:200]}")
        return str(res.out.strip())

    def head(self, worktree: str) -> str:
        return self._git(worktree, "rev-parse", "HEAD")

    def branch(self, worktree: str) -> str:
        return self._git(worktree, "rev-parse", "--abbrev-ref", "HEAD")

    def base(self, worktree: str) -> str:
        return self._git(worktree, "rev-parse", self.base_ref)

    def dirty(self, worktree: str) -> str:
        return self._git(worktree, "status", "--porcelain", "--untracked-files=no")

    def remote_head(self, worktree: str, branch: str) -> str | None:
        out = self._git(worktree, "ls-remote", "origin", f"refs/heads/{branch}")
        return out.split()[0] if out else None

    def push(self, worktree: str, branch: str) -> None:
        self._git(worktree, "push", "origin", f"HEAD:refs/heads/{branch}")


class NoAdmission:
    """The default policy: every head is admitted. `prepare` yields nothing to review."""

    def blockers(self, worktree: str) -> list[str]:
        return []

    def prepare(self, worktree: str, inputs: dict[str, Any]) -> Prepared:
        return Prepared(digest="", text="")

    def apply(self, worktree: str, inputs: dict[str, Any], approved_digest: str) -> str:
        return "no admission policy"

    def receipt(self, head: str) -> str | None:
        return "no admission policy"


def core_providers(home: Path, base_ref: str = "origin/main") -> Providers:
    commands = Subprocess()
    from .subjects import JournalStore  # pipeline runs live in their subject journals

    return Providers(clock=SystemClock(), store=JournalStore(home), notifier=StderrNotifier(), agents=BriefFiles(home / "briefs"),
                     vcs=GitVcs(commands, base_ref), commands=commands, admission=NoAdmission())
