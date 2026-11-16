"""In-memory providers. Every external call is recorded in `.calls`, so a test can assert that a dry run made none."""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from ..protocols import PR, Busy, Check, CIStatus, Issue, Prepared, Providers, QueueEntry, ReleaseStatus, Result, Run, Verification


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t: float = start
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class MemoryStore:
    def __init__(self) -> None:
        self.state: dict[tuple[str, str], Any] = {}
        self.receipts: dict[tuple[str, str], Any] = {}
        self.held: set[str] = set()
        self.writes = 0

    def kinds(self) -> list[str]:
        return sorted({k for k, _ in self.state})

    def load(self, kind: str, key: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.state.get((kind, key)))

    def save(self, kind: str, key: str, data: dict[str, Any]) -> None:
        self.writes += 1
        self.state[(kind, key)] = copy.deepcopy(data)

    def keys(self, kind: str) -> list[str]:
        return sorted(k for kd, k in self.state if kd == kind)

    def delete(self, kind: str, key: str) -> None:
        self.state.pop((kind, key), None)

    @contextmanager
    def lock(self, name: str) -> Iterator[None]:
        if name in self.held:
            raise Busy(name)
        self.held.add(name)
        try:
            yield
        finally:
            self.held.discard(name)

    def put_receipt(self, kind: str, key: str, data: dict[str, Any]) -> None:
        if (kind, key) in self.receipts:
            raise FileExistsError(key)
        self.receipts[(kind, key)] = copy.deepcopy(data)

    def get_receipt(self, kind: str, key: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.receipts.get((kind, key)))


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def notify(self, level: str, key: str, text: str) -> None:
        self.sent.append((level, key, text))


class MemoryBriefs:
    def __init__(self) -> None:
        self.briefs: dict[str, str] = {}
        self.spawned: list[tuple[str, str, str]] = []

    def brief(self, role: str, title: str, text: str) -> str:
        ref = f"brief:{role}:{title}"
        self.briefs[ref] = text
        return ref

    def spawn(self, role: str, brief_ref: str, model: str = "") -> str:
        self.spawned.append((role, brief_ref, model))
        return f"agent-{len(self.spawned)}"


class FakeVcs:
    def __init__(self, head: str = "a" * 40, branch: str = "feat/x", base: str = "b" * 40) -> None:
        self.h, self.b, self.base_sha, self.dirt = head, branch, base, ""
        self.remote: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    def head(self, worktree: str) -> str:
        return self.h

    def branch(self, worktree: str) -> str:
        return self.b

    def base(self, worktree: str) -> str:
        return self.base_sha

    def dirty(self, worktree: str) -> str:
        return self.dirt

    def remote_head(self, worktree: str, branch: str) -> str | None:
        return self.remote.get(branch)

    def push(self, worktree: str, branch: str) -> None:
        self.calls.append(("push", branch))
        self.remote[branch] = self.h


class FakeForge:
    def __init__(self) -> None:
        self.prs: dict[int, PR] = {}
        self.check_rows: dict[str, list[Check]] = {}
        self.runs: list[Run] = []
        self.variables: dict[str, str] = {}
        self.tags: dict[str, str] = {}
        self.heads: dict[str, str] = {}  # branch -> sha, as pushed
        self.calls: list[tuple[str, Any]] = []

    def pr_for_branch(self, branch: str) -> PR | None:
        return next((p for p in self.prs.values() if p.branch == branch and p.state == "open"), None)

    def open_pr(self, branch: str, title: str, body: str) -> PR:
        self.calls.append(("open_pr", branch))
        pr = PR(len(self.prs) + 1, branch, self.heads.get(branch, ""))
        self.prs[pr.number] = pr
        return pr

    def checks(self, sha: str) -> list[Check]:
        return self.check_rows.get(sha, [])

    def active_runs(self) -> list[Run]:
        return [r for r in self.runs if r.status != "completed"]

    def merge(self, pr: int, head: str) -> None:
        self.calls.append(("merge", pr))
        old = self.prs[pr]
        if old.head != head:
            raise RuntimeError(f"PR {pr} is at {old.head}, not {head}")
        self.prs[pr] = PR(old.number, old.branch, old.head, "merged")

    def tag(self, name: str, sha: str) -> None:
        self.calls.append(("tag", name))
        self.tags[name] = sha

    def get_variable(self, name: str) -> str | None:
        return self.variables.get(name)

    def set_variable(self, name: str, value: str) -> None:
        self.calls.append(("set_variable", name))
        self.variables[name] = value


class FakeCI:
    def __init__(self) -> None:
        self.states: dict[str, CIStatus] = {}

    def status(self, sha: str) -> CIStatus:
        return self.states.get(sha, CIStatus("pending"))

    def failures(self, sha: str) -> list[str]:
        return list(self.states.get(sha, CIStatus("pending")).failing)


class FakeAdmission:
    def __init__(self, text: str = '{"plan": 1}', digest: str = "d" * 64) -> None:
        self.text, self.digest = text, digest
        self.blocking: list[str] = []
        self.receipts: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    def blockers(self, worktree: str) -> list[str]:
        return list(self.blocking)

    def prepare(self, worktree: str, inputs: dict[str, Any]) -> Prepared:
        return Prepared(self.digest, self.text, "Review the plan.")

    def apply(self, worktree: str, inputs: dict[str, Any], approved_digest: str) -> str:
        self.calls.append(("apply", approved_digest))
        self.receipts[worktree] = f"receipt-{approved_digest[:8]}"
        return self.receipts[worktree]

    def receipt(self, head: str) -> str | None:
        return self.receipts.get(head)


class FakeQueue:
    def __init__(self) -> None:
        self.entries: dict[int, QueueEntry] = {}
        self.calls: list[tuple[str, int]] = []

    def add(self, pr: int, head: str, reason: str) -> None:
        self.calls.append(("add", pr))
        self.entries[pr] = QueueEntry(pr, "queued")

    def entry(self, pr: int) -> QueueEntry | None:
        return self.entries.get(pr)

    def drop(self, pr: int) -> None:
        self.calls.append(("drop", pr))
        self.entries.pop(pr, None)


class FakeRecipe:
    def __init__(self) -> None:
        self.ready: tuple[bool, str] = (True, "rehearsal green")
        self.pin_map: dict[str, str] = {"consumer": "abc"}
        self.statuses: dict[str, ReleaseStatus] = {}
        self.calls: list[tuple[str, str]] = []

    def candidate_ready(self, base: str) -> tuple[bool, str]:
        return self.ready

    def pins(self) -> dict[str, str]:
        return dict(self.pin_map)

    def cut(self, version: str) -> None:
        self.calls.append(("cut", version))
        self.statuses[version] = ReleaseStatus(version, "candidate")

    def status(self, version: str) -> ReleaseStatus:
        return self.statuses.get(version, ReleaseStatus(version, "none"))

    def drop(self, version: str) -> None:
        self.calls.append(("drop", version))
        self.statuses[version] = ReleaseStatus(version, "dropped")


class FakeVerifier:
    def __init__(self) -> None:
        self.live = ""

    def verify(self, version: str) -> Verification:
        return Verification(self.live == version, self.live, f"live version is {self.live or 'unknown'}")


class FakeTracker:
    def __init__(self) -> None:
        self.issues: dict[str, Issue] = {}
        self.calls: list[tuple[str, str]] = []

    def get(self, issue: str) -> Issue | None:
        return self.issues.get(issue)

    def search(self, query: str) -> list[Issue]:
        return [i for i in self.issues.values() if query in i.title]

    def comment(self, issue: str, text: str) -> None:
        self.calls.append(("comment", issue))

    def close(self, issue: str, reason: str) -> None:
        self.calls.append(("close", issue))


class FakeCommands:
    """Scripted process runner: `on(prefix, result_or_function)`; every argv is recorded."""

    def __init__(self) -> None:
        self.rules: list[tuple[list[str], Result | Callable[[list[str]], Result]]] = []
        self.calls: list[list[str]] = []

    def on(self, prefix: list[str], result: Result | Callable[[list[str]], Result]) -> None:
        self.rules.append((prefix, result))

    def run_bytes(self, argv: list[str], cwd: str, timeout: float, env: dict[str, str]) -> tuple[int, bytes]:
        result = self.run(argv, cwd, timeout, env)
        return result.rc, result.out.encode()

    def passthrough(self, argv: list[str], cwd: str | None = None, env: dict[str, Any] | None = None, timeout: float | None = None) -> int:
        return self.run(argv, cwd=cwd, env=env, timeout=timeout).rc

    def run(self, argv: list[str], cwd: str | None = None, timeout: float | None = None, env: dict[str, Any] | None = None) -> Result:
        self.calls.append(list(argv))
        for prefix, result in reversed(self.rules):
            if len(argv) >= len(prefix) and str(argv[0]).endswith(prefix[0]) and list(argv[1:len(prefix)]) == list(prefix[1:]):
                return result(argv) if callable(result) else result
        return Result(127, "", f"no fake for {argv[:3]}")


def fake_providers(**over: Any) -> Providers:
    base: dict[str, Any] = dict(clock=FakeClock(), store=MemoryStore(), notifier=RecordingNotifier(), agents=MemoryBriefs(), vcs=FakeVcs(), commands=FakeCommands(),
                admission=FakeAdmission(), forge=FakeForge(), ci=FakeCI(), queue=FakeQueue(), recipe=FakeRecipe(), verifier=FakeVerifier(), tracker=FakeTracker())
    base.update(over)
    return Providers(**base)


def external_calls(p: Providers) -> list[Any]:
    return [c for obj in (p.vcs, p.forge, p.admission, p.queue, p.recipe, p.tracker) for c in getattr(obj, "calls", [])]


class Invoked:
    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code, self.stdout, self.stderr = exit_code, stdout, stderr

    def __repr__(self) -> str:
        return f"<exit {self.exit_code}; stdout {self.stdout[-300:]!r}; stderr {self.stderr[-300:]!r}>"


def invoke(runtime: Any, args: list[str]) -> Invoked:
    """Run the full command tree (core plus the plugin's merged app) in process, as `main` does after it resolved
    the settings. Returns exit code, stdout and stderr."""
    import contextlib
    import io

    from ..app import OUT, execute

    out, err, code = io.StringIO(), io.StringIO(), 0
    import os

    before, env = OUT["format"], os.environ.get("SM_FORMAT")
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = execute(runtime, list(args))
        except SystemExit as stop:
            code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
        finally:
            OUT["format"] = before
            os.environ.pop("SM_FORMAT", None)
            if env is not None:
                os.environ["SM_FORMAT"] = env
    return Invoked(code, out.getvalue(), err.getvalue())
