"""The provider Protocols: the only way the core touches the world.

Each has the smallest method set the pipelines need. A plugin supplies implementations through
`Plugin.providers()`; core has a generic default for some (see `defaults.py` and DESIGN.md section 4).
Rule: a method that changes something outside this machine is named in the step's `Changes:` text and is only
ever called from a step marked `external=True`.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# ----------------------------------------------------------------------------- data
@dataclass(frozen=True)
class PR:
    number: int
    branch: str
    head: str
    state: str = "open"  # open | merged | closed
    url: str = ""


@dataclass(frozen=True)
class Check:
    name: str
    state: str  # green | red | pending
    url: str = ""


@dataclass(frozen=True)
class Run:
    id: str
    status: str  # queued | in_progress | completed
    branch: str = ""
    event: str = ""
    workflow: str = ""


@dataclass(frozen=True)
class CIStatus:
    state: str  # green | red | pending | unknown
    failing: tuple[str, ...] = ()
    url: str = ""


@dataclass(frozen=True)
class Prepared:
    """What an admission policy wants reviewed: exact bytes and their digest."""

    digest: str
    text: str
    brief: str = ""


@dataclass(frozen=True)
class QueueEntry:
    pr: int
    state: str  # queued | merging | merged | needs_work
    why: str = ""


@dataclass(frozen=True)
class ReleaseStatus:
    version: str
    state: str  # none | candidate | published | failed | dropped
    detail: str = ""


@dataclass(frozen=True)
class Verification:
    ok: bool
    version: str = ""
    evidence: str = ""


@dataclass(frozen=True)
class Issue:
    id: str
    title: str
    state: str = "open"


@dataclass(frozen=True)
class Result:
    rc: int
    out: str = ""
    err: str = ""


class Busy(Exception):
    """A lock is held by another process."""


# ----------------------------------------------------------------------------- protocols
@runtime_checkable
class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


@runtime_checkable
class Store(Protocol):
    """State, locks and receipts. State is mutable; a receipt is written once and never changed."""

    def load(self, kind: str, key: str) -> dict[str, Any] | None: ...
    def save(self, kind: str, key: str, data: dict[str, Any]) -> None: ...
    def keys(self, kind: str) -> list[str]: ...
    def delete(self, kind: str, key: str) -> None: ...  # state only (never a receipt); absent is fine
    def lock(self, name: str) -> AbstractContextManager[None]: ...  # raises Busy
    def put_receipt(self, kind: str, key: str, data: dict[str, Any]) -> None: ...  # raises FileExistsError
    def get_receipt(self, kind: str, key: str) -> dict[str, Any] | None: ...


@runtime_checkable
class Vcs(Protocol):
    """The local checkout of one head."""

    def head(self, worktree: str) -> str: ...
    def branch(self, worktree: str) -> str: ...
    def base(self, worktree: str) -> str: ...  # tip of the branch the head will merge into
    def dirty(self, worktree: str) -> str: ...  # "" when clean
    def remote_head(self, worktree: str, branch: str) -> str | None: ...
    def push(self, worktree: str, branch: str) -> None: ...  # external; never a force push


@runtime_checkable
class Forge(Protocol):
    """PRs, checks, runs and variables on the code host."""

    def pr_for_branch(self, branch: str) -> PR | None: ...
    def open_pr(self, branch: str, title: str, body: str) -> PR: ...  # external
    def checks(self, sha: str) -> list[Check]: ...
    def active_runs(self) -> list[Run]: ...
    def merge(self, pr: int, head: str) -> None: ...  # external; refuses when the PR is not at `head`
    def tag(self, name: str, sha: str) -> None: ...  # external
    def get_variable(self, name: str) -> str | None: ...
    def set_variable(self, name: str, value: str) -> None: ...  # external


@runtime_checkable
class CIProvider(Protocol):
    def status(self, sha: str) -> CIStatus: ...
    def failures(self, sha: str) -> list[str]: ...  # one line per failing job or test, for a builder brief


@runtime_checkable
class AdmissionPolicy(Protocol):
    """Decides whether a head may enter CI and the queue, and records that it did."""

    def blockers(self, worktree: str) -> list[str]: ...  # what must finish before prepare or apply
    def prepare(self, worktree: str, inputs: dict[str, Any]) -> Prepared: ...
    def apply(self, worktree: str, inputs: dict[str, Any], approved_digest: str) -> str: ...  # external; returns a receipt id
    def receipt(self, head: str) -> str | None: ...


@runtime_checkable
class Queue(Protocol):
    def add(self, pr: int, head: str, reason: str) -> None: ...  # external
    def entry(self, pr: int) -> QueueEntry | None: ...
    def drop(self, pr: int) -> None: ...  # external


@runtime_checkable
class ReleaseRecipe(Protocol):
    def candidate_ready(self, base: str) -> tuple[bool, str]: ...  # the candidate rule: may this base be cut
    def pins(self) -> dict[str, str]: ...  # everything a reviewer must approve before a cut
    def cut(self, version: str) -> None: ...  # external
    def status(self, version: str) -> ReleaseStatus: ...
    def drop(self, version: str) -> None: ...  # external


@runtime_checkable
class Verifier(Protocol):
    def verify(self, version: str) -> Verification: ...  # is this version live and healthy


@runtime_checkable
class Tracker(Protocol):
    def get(self, issue: str) -> Issue | None: ...
    def search(self, query: str) -> list[Issue]: ...
    def comment(self, issue: str, text: str) -> None: ...  # external
    def close(self, issue: str, reason: str) -> None: ...  # external


@runtime_checkable
class Notifier(Protocol):
    def notify(self, level: str, key: str, text: str) -> None: ...  # level: info | warn | alert


@runtime_checkable
class AgentRunner(Protocol):
    """Hands work to another agent. `brief` only prepares; `spawn` starts one."""

    def brief(self, role: str, title: str, text: str) -> str: ...  # returns where the brief is (a path or an id)
    def spawn(self, role: str, brief_ref: str, model: str = "") -> str: ...  # external; returns an agent id


@runtime_checkable
class Commands(Protocol):
    """Runs a local process. Everything that starts one takes this, so tests can replace it."""

    def run(self, argv: list[str], cwd: str | None = None, timeout: float | None = None, env: dict[str, Any] | None = None) -> Result: ...
    def passthrough(self, argv: list[str], cwd: str | None = None, env: dict[str, Any] | None = None, timeout: float | None = None) -> int: ...  # the child owns the terminal; 124 on timeout


@runtime_checkable
class BinaryCommands(Protocol):
    """Optional literal process output for byte-sensitive readers."""

    def run_bytes(self, argv: list[str], cwd: str, timeout: float, env: dict[str, str]) -> tuple[int, bytes]: ...


@dataclass
class Providers:
    """One of each. None means the project has none; a step that needs it refuses with a clear message."""

    clock: Clock
    store: Store
    notifier: Notifier
    agents: AgentRunner
    vcs: Vcs
    commands: Commands
    admission: AdmissionPolicy
    forge: Forge | None = None
    ci: CIProvider | None = None
    queue: Queue | None = None
    recipe: ReleaseRecipe | None = None
    verifier: Verifier | None = None
    tracker: Tracker | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # plugin-private objects


PROTOCOLS = {"clock": Clock, "store": Store, "notifier": Notifier, "agents": AgentRunner, "vcs": Vcs, "commands": Commands,
             "admission": AdmissionPolicy, "forge": Forge, "ci": CIProvider, "queue": Queue, "recipe": ReleaseRecipe,
             "verifier": Verifier, "tracker": Tracker}
