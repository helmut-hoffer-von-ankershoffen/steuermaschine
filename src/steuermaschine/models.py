"""The result models. One model backs the text renderer, the JSON output and the published JSON Schema
(`sm self schema NAME`). In json format a command prints exactly one of these (or a list of them) on stdout."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel


class Model(BaseModel):
    """Base class of every result. `render()` is the text form; `--format json` prints `model_dump()`."""

    model_config = ConfigDict(extra="forbid")

    def render(self) -> list[str]:
        return [f"{name}: {value}" for name, value in self.model_dump().items()]

    # A model whose command's exit code carries meaning defines a property `exit_code` (default: 0).


class Error(Model):
    """What a failing command prints in json format, with a non-zero exit code."""

    error: str
    code: int
    hint: str = ""


class Alarm(Model):
    key: str
    level: str  # alert | warn
    text: str
    next: str = ""
    acked: bool = False


class GoItem(Model):
    phase: str
    next: str = ""
    waiting_on: str = ""


class GoState(Model):
    runs: dict[str, int] = {}
    focus: list[str] = []


class GoReport(Model):
    """The `go` contract."""

    schema_version: int = 1
    project: str = ""
    plugin: str = ""
    phase: str
    next: str = ""  # one command that can be run now; empty when nothing is runnable
    waiting_on: str = ""
    trap: str = ""
    also: list[GoItem] = []
    alarms: list[Alarm] = []
    state: GoState = GoState()
    read_only: bool = True


class StepRow(Model):
    name: str
    status: str  # open | waiting | done
    detail: str = ""


class RunReport(Model):
    """What one pipeline command prints: the state of one head or release after this run."""

    project: str
    pipeline: str
    key: str
    label: str
    status: str  # finished | review | waiting | dry_run | refused | plan
    phase: str
    next: str = ""
    waiting_on: str = ""
    trap: str = ""
    exit_code: int = 0
    steps: list[StepRow] = []
    log: list[str] = []
    review: dict[str, Any] = Field({}, description="At a review stop: step, file, sha256 (canonical), brief, record (the reviewer's command), resume.")


class RunState(Model):
    """One persisted run, as `head status` and `release status` list it."""

    model_config = ConfigDict(extra="ignore")
    pipeline: str
    key: str
    label: str = ""
    status: str = ""
    phase: str = ""
    next: str = ""
    waiting_on: str = ""
    trap: str = ""
    lane: str = ""
    author: str = ""
    updated: str = ""
    review: dict[str, Any] = {}
    pid: int | None = None


class ToolMapRow(Model):
    """One old tool and what became of it. A plugin returns its rows from the hook `sm_tool_map`."""

    tool: str
    kind: str = "command"  # command | internal | retired
    path: str = ""  # for kind command: the command path without the program name
    note: str = ""
    also: list[str] = []  # further command paths that took over part of the tool
    owner: str = ""  # who ports it, while it is missing
    core: bool = False  # the generic core command at `path` fully replaces the tool (no plugin code needed)
    requires: list[str] = []  # for a `path` that is a group: every command path the old tool's subcommands became
    status: str = ""  # computed: native | missing | internal | retired


class ToolGaps(Model):
    """What is not ported yet. `wrapped` is the number of plugin commands outside the `@native` contract (mounted as
    plain Typer apps through `sm_commands`, unreviewed): where a wrapper of an old script could hide; 0 is the goal."""

    missing: list[ToolMapRow] = []
    counts: dict[str, int] = {}
    wrapped: int = 0

    def render(self) -> list[str]:
        lines = [f"MISSING {r.tool}: becomes `sm {r.path}`" + (f"   owner: {r.owner}" if r.owner else "") for r in self.missing]
        c = self.counts
        return [*lines, f"{len(self.missing)} missing, {c.get('native', 0)} native, {c.get('internal', 0)} internal, {c.get('retired', 0)} retired; {self.wrapped} wrapped"]

    @property
    def exit_code(self) -> int:
        return 1 if self.missing else 0


class SettingValue(Model):
    value: object = None
    source: str = "default"


class EffectiveConfig(Model):
    project: str | None = None
    plugin: str | None = None
    plugin_loaded: bool = False
    problem: str = ""
    root: str | None = None
    home: str | None = None
    api: int = 1
    installed_plugins: dict[str, str] = {}
    core: dict[str, SettingValue] = {}  # [project], [lanes], [alarms], format, and the core tables [github], [sonar]
    settings: dict[str, SettingValue] = {}  # every plugin's table
    environment: dict[str, SettingValue] = {}  # the process variables the core reads besides SM_*


class PluginRow(Model):
    order: int
    name: str
    layer: str  # base | middle | outermost
    source: str = ""
    hooks: list[str] = []
    wrappers: list[str] = []


class ReachedPath(Model):
    """One write path a change reaches, and the changed files its code covers."""

    path: str
    files: list[str] = []


class ReviewReach(Model):
    """What the diff since the last promoted commit reaches (`self reviews`): the write paths whose code it changes,
    so the one independent reviewer of the promote knows what to read first."""

    base: str  # the commit the diff starts at: the last promoted one, or --base
    head: str  # the candidate commit
    changed: list[str] = []  # every file the diff changes, as git names it
    gate: list[str] = []  # changed files of the promote gate itself (bin/promote*, bin/rollback, bin/check): read them first
    reached: list[ReachedPath] = []
    unreached: list[str] = []  # changed files no write path's code covers: tests, docs, read-only code (CI covers them)


class HookRow(Model):
    hook: str
    kind: str
    arguments: list[str] = []
    doc: str = ""


class Nothing(Model):
    """A command that had nothing to report this time (a daemon tick with no work): `{}` in json format."""


class Verdict(Model):
    """One recorded review verdict (`review verdict`)."""

    digest: str
    verdict: str  # approve | reject
    reviewer: str
    model: str = ""
    notes: str = ""
    at: float = 0.0  # epoch seconds of the clock provider, as stored
    scope: str = ""  # exact hosted operation, when the verdict uses its verified evidence
    kind: str = ""  # "" (prepared bytes) or "ci_change" (a commit whose CI paths changed since the last release)


class ReviewWait(Model):
    """One run step with a review digest and the verdict recorded for it (`review list`)."""

    label: str | None = None
    step: str
    digest: str
    brief: str = ""
    status: str | None = None
    verdict: str = "none"
    reviewer: str = ""


class FocusList(RootModel[dict[str, dict[str, str]]]):
    """The declared critical path: entry -> {"for": version, "note": text} (`focus add|done|list`)."""


class AlarmAcks(RootModel[dict[str, float]]):
    """Acknowledged alarms: key -> when, in epoch seconds of the clock provider (`alarm ack`)."""


class StateMoveRow(Model):
    """One state path of an earlier release and its name in this one."""

    old: str
    new: str
    state: str = Field(description="old: the old path still holds the state; moved: the old path links to the new one; none: neither exists yet")


class StateMigration(Model):
    """What `self migrate-state` found or did."""

    action: str = Field(description="check | list | forward | back")
    moves: list[StateMoveRow] = []
    done: list[str] = Field(default_factory=list, description="One line per path that moved, in order.")

    def render(self) -> list[str]:
        if self.action in ("forward", "back"):
            return self.done or ["nothing to move"]
        pending = [m for m in self.moves if m.state == "old"]
        head = [f"{len(pending)} of {len(self.moves)} state path(s) still at the old name"] if self.action in ("check", "forward") else []
        return head + [f"  {m.state:5} {m.old} -> {m.new}" for m in self.moves if self.action == "list" or m.state == "old"]


class ModelNames(RootModel[list[str]]):
    """The names `self schema NAME` accepts."""


class JsonSchema(RootModel[dict[str, Any]]):
    """The JSON Schema of one result model (`self schema NAME`)."""


class GateRow(Model):
    name: str
    describe: str = ""


class TreeRow(Model):
    """One command or group of the built command tree (`self tree`)."""

    path: str
    group: bool
    default: str = ""  # a group that runs this subcommand for a word that is none of its own (`sm release 1.2.3`)
    hidden: bool = False
    purpose: str = ""
    help: str = ""
    read_only: bool = False
    options: list[str] = []
    arguments: list[str] = []
    missing: str = ""  # letters of the help contract it lacks (see `self lint`)
    undocumented: list[str] = []  # arguments and options without their own help
    declared: bool = False  # the command declares whether it writes (`writes=`, a pipeline, a core writer)


class LintFindings(RootModel[list[str]]):
    """One line per help contract finding; empty when every command passes (`self lint`)."""


class Markdown(RootModel[str]):
    """A Markdown document (`self doc`)."""


class Tokens(Model):
    """The tokens of one session transcript (`self tokens`): summed over every usage block, per kind."""

    answers: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def render(self) -> list[str]:
        return [f"{self.answers} answers: {self.total} tokens (input {self.input}, output {self.output}, cache read {self.cache_read}, cache write {self.cache_write})"]

    @classmethod
    def count(cls, lines: list[str]) -> Tokens:
        found = cls()
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            message = row.get("message") if isinstance(row, dict) else None
            usage = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(usage, dict):
                continue
            found.answers += 1
            for field, key in (("input", "input_tokens"), ("output", "output_tokens"), ("cache_read", "cache_read_input_tokens"),
                               ("cache_write", "cache_creation_input_tokens")):
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    setattr(found, field, getattr(found, field) + value)
        return found


MODELS: dict[str, type[BaseModel]] = {m.__name__: m for m in (
    Error, Alarm, GoReport, RunReport, RunState, StepRow, ToolMapRow, ToolGaps, EffectiveConfig, PluginRow, HookRow, ReviewReach, Nothing, Verdict,
    ReviewWait, FocusList, AlarmAcks, StateMigration, ModelNames, JsonSchema, GateRow, TreeRow, LintFindings, Markdown, Tokens)}
