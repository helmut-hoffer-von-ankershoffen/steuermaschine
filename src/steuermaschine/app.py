"""The command line. `main()` finds the project, loads its plugin through entry points, builds one command tree
(core commands, the plugin's pipelines as verbs, the plugin's own Typer app merged at the root) and runs it.

Bare `sm` runs `sm go`. Every command has help in contract form (helpdoc.py) and accepts `--format text|json`,
before or after the command (dispatch.py).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import typer
from pydantic import BaseModel, RootModel

from . import ALARM, API_VERSION, INTERNAL, OK, PROG, REFUSED, USAGE, WAITING, dispatch, helpdoc, listopts, models, plugins, reviewgraph, statemove, statsreport, subjects, surface, toolmap, usagelog, verdicts
from . import config as config_mod
from . import go as go_mod
from . import native as native_mod
from .defaults import Subprocess, core_providers
from .engine import Engine, Options, Pipeline
from .helpdoc import H
from .protocols import Busy, Providers

CTX = {"help_option_names": ["-h", "--help"], "max_content_width": 160}
RESERVED = {"self"}  # a plugin cannot replace these
JSON = typer.Option(False, "--json", hidden=True, help="Old spelling of the root option --format json; kept for one release.")
OUT = {"format": "text"}  # set by the root option --format (or SM_FORMAT, or the settings files)


def json_mode(as_json: bool = False) -> bool:
    return as_json or OUT["format"] == "json"


@dataclass
class Runtime:
    """What commands and plugin apps work with. Providers are built on first use, so --help touches nothing."""

    project: config_mod.Project | None
    plugin: plugins.Stack | None  # the loaded plugins of the project, base first
    settings: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, Any] = field(default_factory=dict)  # plugin setting -> where its value came from
    _providers: Providers | None = None
    problem: str = ""  # why there is no project or plugin, for `go` and `self config`
    config_error: bool = False  # a settings file exists and is wrong (as opposed to: there is none)
    plugin_paths: set[Any] = field(default_factory=set)  # top-level names the plugin's own app contributed

    @property
    def providers(self) -> Providers:
        if self._providers is None:
            if self.project is None:
                fail(self.problem or "no project", USAGE)
            base = core_providers(self.project.home, self.project.base)
            self._providers = self.plugin.providers(self.settings, base) if self.plugin else base
        return self._providers

    def engine(self) -> Engine:
        gates = {g.name: g for g in (self.plugin.gates(self.settings) if self.plugin else [])}
        traps = self.plugin.traps(self.settings) if self.plugin else {}
        assert self.project is not None, "engine() needs a project"
        return Engine(self.project.name, self.providers, self.settings, self.project.lanes, gates, traps, out=lambda line: typer.echo(line, err=True),
                      run_step=self.plugin.run_step if self.plugin else None)

    def pipelines(self) -> list[Pipeline]:
        return self.plugin.pipelines(self.settings) if self.plugin else []


@contextlib.contextmanager
def locked(store: Any, name: str) -> Iterator[None]:
    """A read-modify-write of the store under its lock `name`; held by another process: exit 75 (wait, then again)."""
    try:
        with store.lock(name):
            yield
    except Busy:
        fail(f"another process is changing {name}", WAITING, hint="run the same command again")


def fail(message: str, code: int = REFUSED, hint: str = "") -> NoReturn:
    """Text: one line on stderr. json: one Error object on stdout. Always a non-zero exit."""
    usagelog.reason(message)
    if json_mode():
        typer.echo(models.Error(error=message, code=code, hint=hint).model_dump_json())
    else:
        typer.echo(f"{PROG}: {message}" + (f"\n  {hint}" if hint else ""), err=True)
    raise typer.Exit(code)


def emit(as_json: bool, data: Any, lines: list[str], model: type[BaseModel]) -> None:
    """Text: the lines. json: exactly one JSON document on stdout, validated by `model` (a list: each row; a RootModel:
    the whole value). Every command prints through this or through `emit_result`, so json output is always typed."""
    if not json_mode(as_json):
        typer.echo("\n".join(lines))
        return
    rows = isinstance(data, list) and not issubclass(model, RootModel)
    data = [model.model_validate(d).model_dump() for d in data] if rows else model.model_validate(data).model_dump()
    typer.echo(json.dumps(data, sort_keys=True, default=str))


def group(parent: typer.Typer, name: str, purpose: str, when: str, changes: str, example: str, next_: str, hidden: bool = False) -> typer.Typer:
    sub = typer.Typer(help=H(purpose, when, changes, example, next_), short_help=purpose, no_args_is_help=True, context_settings=CTX,
                      rich_markup_mode=None, add_completion=False)
    parent.add_typer(sub, name=name, hidden=hidden)
    return sub


# ----------------------------------------------------------------------------- building the tree
def build(rt: Runtime) -> typer.Typer:
    app = typer.Typer(help=H(
        f"{PROG} (steuermaschine): steer work from a change to a live release; the artificial helmsman for agents and their owner.",
        f"Use at every tick, cold start and context reset: bare `{PROG}` runs `{PROG} go` and prints the ONE next command.",
        "nothing by itself; each command's help has its own Changes: line. Writes outside this machine need --live.",
        f"{PROG} go",
        "the command on the NEXT line, after reading the TRAP line.",
        f"\b\nProject: {rt.project.name + ' (plugin ' + rt.project.plugin + ')' if rt.project else 'none: ' + rt.problem}\n"
        "Exit codes: 0 done or a planned stop, 1 refused, 2 usage, 3 alarm, 70 tool error, 75 waiting (same command again)."),
        context_settings=CTX, rich_markup_mode=None, add_completion=False, no_args_is_help=False)

    @app.callback()
    def root(format_: str | None = typer.Option(None, "--format", metavar="text|json", envvar="SM_FORMAT",
                                                  help="Output of every command: text (default), or json = exactly one JSON document on stdout, progress on stderr.")) -> None:
        if format_ is not None:
            if format_ not in ("text", "json"):
                typer.echo(f"{PROG}: --format is text or json, not '{format_}'", err=True)
                raise typer.Exit(USAGE)
            OUT["format"] = format_

    # ------------------------------------------------------------------ go
    @app.command("go", context_settings=CTX, short_help="Where is the project, and what is the ONE next command?", help=H(
        "Where is the project, and what is the ONE next command? The front door.",
        "Use at every tick, cold start and context reset, and whenever you are unsure what to type next.",
        "nothing (read-only; it reads run states and asks the plugin's read-only probes).",
        f"{PROG} go            (machines: {PROG} go --json)",
        "the command on the NEXT line, after reading the TRAP line."))
    def go(as_json: bool = JSON) -> None:
        if rt.project is None:
            report = models.GoReport(phase=f"no project: {rt.problem}", next=f"{PROG} self config").model_dump()
            emit(as_json, report, go_mod.render(report), models.GoReport)
            raise typer.Exit(USAGE)
        p = rt.providers
        report = go_mod.compute(rt.project, p.store, rt.plugin, p, rt.settings)
        emit(as_json, report, go_mod.render(report), models.GoReport)
        raise typer.Exit(ALARM if any(a["level"] == "alert" and not a["acked"] for a in report["alarms"]) else OK)

    # ------------------------------------------------------------------ head, release: pipelines as verbs
    words = {"head": ("head: take one head (a commit on a work branch) through the project's pipeline.", "Use when a builder reports a head ready.", "."),
             "release": ("release: take one version through the project's release pipeline.", "Use when a release is due, running or stuck.", "1.2.3")}
    by_subject: dict[str, list[Pipeline]] = {}
    for pl in (rt.pipelines() if rt.project else []):
        by_subject.setdefault(pl.subject, []).append(pl)
    for subject, (purpose, when, _example_arg) in words.items():
        sub = group(app, subject, purpose, when, "nothing by itself; see each command.", f"{PROG} {subject} status", f"{PROG} go")
        _status_command(rt, sub, subject)
        for pl in by_subject.pop(subject, []):
            _pipeline_command(rt, sub, pl)
    for subject, pls in by_subject.items():  # a plugin may name its own subject
        sub = group(app, subject, f"{subject}: pipelines of plugin {rt.plugin.name if rt.plugin else 'none'}.", "Use when the plugin's help says so.", "see each command.",
                    f"{PROG} {subject} status", f"{PROG} go")
        _status_command(rt, sub, subject)
        for pl in pls:
            _pipeline_command(rt, sub, pl)

    # Review stops and focus belong to engine runs. A project whose plugin has no pipeline yet (everything mounted)
    # keeps them out of the listing, so one word never has two meanings on the screen; they still work.
    quiet = rt.project is not None and not rt.pipelines()
    _tools(rt, app)
    _review(rt, app, quiet)
    _focus(rt, app, quiet)
    _alarm(rt, app)
    from .github import cli as github_cli  # generic `ci` and `pr` on the forge / CI providers
    github_cli.register(rt, app)
    _self(rt, app)
    return app


def _status_command(rt: Runtime, sub: typer.Typer, subject: str) -> None:
    @sub.command("status", context_settings=CTX, short_help=f"Every {subject} with a run: where it stands and its next command.", help=H(
        f"Every {subject} with a run: where it stands and its next command.",
        f"Use when `{PROG} go` names more than one {subject}, or to find a run again.",
        "nothing (read-only; it reads the state files only).",
        f"{PROG} {subject} status --json",
        "the NEXT of the row you care about."))
    def status(as_json: bool = JSON, all_: bool = typer.Option(False, "--all", help="Include finished runs.")) -> None:
        rows = [s for s in go_mod.run_states(rt.providers.store) if s["pipeline"].startswith(subject + ".") and (all_ or s.get("status") != "finished")]
        lines = [f"{s.get('status', '?'):9} {s.get('label')}  {s.get('phase')}\n          NEXT {s.get('next') or 'nothing runnable now'}" for s in rows]
        emit(as_json, rows, lines or [f"no {subject} in flight"], models.RunState)


def _pipeline_command(rt: Runtime, sub: typer.Typer, pl: Pipeline) -> None:
    steps = " -> ".join(s.name + (" (STOP)" if type(s).__name__ == "ReviewStop" else "*" if s.external else "") for s in pl.steps)
    writes = "; ".join(f"{s.name}: {s.changes}" for s in pl.steps if s.external) or "nothing outside this machine"
    more = (f"\b\nSteps: {steps}\n(* writes outside this machine: runs only with --live. STOP: a different agent must approve a digest.)\n"
            "Resumable: state is kept per subject, arguments are remembered, a step done by hand is adopted.\n"
            + ("Inputs (--set NAME=VALUE, remembered):\n" + "\n".join(f"  {k}: {v}" for k, v in pl.inputs.items()) if pl.inputs else ""))
    cmd = f"{PROG} {pl.subject} {pl.verb}"

    @sub.command(pl.verb, context_settings=CTX, short_help=pl.purpose, help=H(
        pl.purpose, pl.when or f"Use when a {pl.subject} is ready for this pipeline.",
        f"state on this machine; without --live nothing else. With --live: {writes}",
        f"{cmd} {pl.example_arg}", f"the NEXT line it prints; {PROG} go shows it again", more))
    def run(subject: str = typer.Argument(..., metavar=pl.subject.upper(), help="A worktree for a head; a version for a release."),
            live: bool = typer.Option(False, "--live", help="Also run the steps that write outside this machine."),
            dry_run: bool = typer.Option(False, "--dry-run", help="The default, stated: stop before the first step that writes outside this machine."),
            plan: bool = typer.Option(False, "--plan", help="Print every step and its Changes; run and write nothing."),
            approved: str = typer.Option("", "--approved", metavar="SHA256", help="The digest a different agent approved at the review stop."),
            author: str = typer.Option(config_mod.environment().steuermaschine_agent or "coordinator", "--as", metavar="NAME", help="Who drives this run; the reviewer must be someone else."),
            lane: str = typer.Option("", "--lane", help="Lane of a new run (default: default)."),
            set_: list[str] | None = typer.Option(None, "--set", metavar="NAME=VALUE", help="An input of the pipeline; remembered for later runs."),
            as_json: bool = JSON) -> None:
        if live and dry_run:
            fail("--live and --dry-run exclude each other", USAGE)
        inputs = {}
        for pair in set_ or []:
            name, eq, value = pair.partition("=")
            if not eq or name not in pl.inputs:
                fail(f"--set {pair}: expected NAME=VALUE with NAME one of: {', '.join(pl.inputs) or 'none'}", USAGE)
            inputs[name] = value
        writer = None if plan else pl.unit or native_mod.unit(f"{pl.subject} {pl.verb}", _pipeline_command)
        with native_mod.writing(rt, writer) as pending:  # a run holds the writers' lock and waits out a switch, as a typed write does
            if pending is not None:
                fail(f"a promote (pid {pending}) is switching the live sm: `sm {pl.subject} {pl.verb}` writes and waits until it is done",
                     WAITING, hint="nothing was run; run the same command again in a minute")
            with native_mod.using(native_mod.NativeRuntime(rt.providers, rt.settings, rt.project,
                                                              commands=lambda: native_mod.registered(rt))):  # steps call native commands
                report = rt.engine().advance(pl, subject, Options(live=live, plan=plan, approved=approved, author=author, lane=lane, inputs=inputs))
        emit(as_json, report.as_json(), report.lines(), models.RunReport)
        raise typer.Exit(report.exit_code)


def _tools(rt: Runtime, app: typer.Typer) -> None:
    tools = group(app, "tools", "tools: what became of each old tool; what is native, what is not ported yet.",
                  "Use when you are about to type an old tool name, or to see what is still missing.",
                  "nothing (read-only).", f"{PROG} tools gaps", "the command it names, with --help", hidden=rt.project is not None and rt.plugin is None)

    def rows() -> list[Any]:
        raw = [r if isinstance(r, models.ToolMapRow) else models.ToolMapRow.model_validate(r) for r in (rt.plugin.tool_map() if rt.plugin else [])]
        # Only what plugins brought counts as ported: a core command with the same words (`go`, `alarm list`) is the
        # generic one and does not know what the old tool knew. A row served by the core says so with `core = true`.
        tree = command_tree(rt)
        every = {r["path"][len(PROG) + 1:] for r in helpdoc.tree(tree, PROG)}
        ported = {c.path for c in (rt.plugin.native(rt.settings) if rt.plugin else []) if hasattr(c, "fn")}
        ported |= {f"{pl.subject} {pl.verb}" for pl in rt.pipelines()}  # engine pipelines are plugin commands too
        for extra in (rt.plugin.clis(rt) if rt.plugin else []):
            ported |= {r["path"][len(PROG) + 1:] for r in helpdoc.tree(typer.main.get_command(extra), PROG)}
        paths = (ported & every) | {r.path for r in raw if r.core and r.path in every}
        # A row naming a group (`ci one`) is ported when every path of its `requires` is (toolmap.status).
        return toolmap.resolve(raw, paths)

    @tools.command("map", context_settings=CTX, short_help="The whole table: every old tool, its status and its command.", help=H(
        "The whole table: every old tool, its status (native, missing, internal, retired) and its command.",
        "Use when you want the full picture of the port.", "nothing (read-only).", f"{PROG} tools map --status missing", f"{PROG} tools where NAME"))
    def map_(status: str | None = typer.Option(None, "--status", help="Only native, missing, internal or retired."), as_json: bool = JSON) -> None:
        found = [r for r in rows() if status in (None, r.status)]
        order = ("native", "missing", "internal", "retired")
        found.sort(key=lambda r: (order.index(r.status), r.path, r.tool))
        c = toolmap.counts(found)
        lines = [f"{r.tool:<24} {r.status:<9} {(PROG + ' ' + r.path) if r.path else '-':<28} {r.owner if r.status == 'missing' else r.note}" for r in found]
        emit(as_json, [r.model_dump() for r in found], [*lines, f"{len(found)} tools: " + ", ".join(f"{c[k]} {k}" for k in order)], models.ToolMapRow)

    @tools.command("where", context_settings=CTX, short_help="What became of this old tool?", help=H(
        "What became of this old tool: its command, or that it is not ported yet, internal or retired.",
        "Use when you are about to type an old tool name.", "nothing (read-only).", f"{PROG} tools where OLD-NAME", "the command it prints, with --help"))
    def where(name: str = typer.Argument(..., metavar="NAME", help="The old tool name, with or without its prefix."), as_json: bool = JSON) -> None:
        row = toolmap.where(rows(), name)
        if row is None:
            fail(f"{name}: not in the tool map", REFUSED, hint=f"{PROG} tools map lists all")
        emit(as_json, row.model_dump(), [toolmap.describe(row, PROG)], models.ToolMapRow)
        raise typer.Exit(1 if row.status == "missing" else 0)

    @tools.command("gaps", context_settings=CTX, short_help="Old tools that are not ported yet, with the owner of each; exit 1 while there is one.", help=H(
        "Old tools that are not ported yet, with the owner of each; exit 1 while there is one.",
        "Use when you want to see how far the port is; the last line is the burn-down.", "nothing (read-only).", f"{PROG} tools gaps --wrapped", f"{PROG} tools map --status missing"))
    def gaps(wrapped: bool = typer.Option(False, "--wrapped", help="Print only the number of plugin commands outside the @native contract "
                                                                     "(mounted through sm_commands, where a wrapper of an old script could hide); 0 is the goal."),
             as_json: bool = JSON) -> None:
        found = rows()
        result = models.ToolGaps(missing=[r for r in found if r.status == "missing"], counts=toolmap.counts(found), wrapped=len(mounted_commands(rt)))
        if wrapped and not json_mode(as_json):
            typer.echo(str(result.wrapped))
        else:
            emit(as_json, result.model_dump(), result.render(), models.ToolGaps)
        raise typer.Exit(result.exit_code)


def mounted_commands(rt: Runtime) -> list[str]:
    """The commands plugins mount as plain Typer apps (`sm_commands`), by name: outside the `@native` contract, unreviewed,
    so `sm tools gaps --wrapped` counts them."""
    import typer.main

    def leaves(group: Any, prefix: str) -> list[str]:
        out = [f"{prefix}{name}".strip() for name in getattr(group, "commands", {}) if not hasattr(group.commands[name], "commands")]
        return out + [leaf for name, sub in getattr(group, "commands", {}).items() if hasattr(sub, "commands") for leaf in leaves(sub, f"{prefix}{name} ")]

    found: list[str] = []
    for extra in (rt.plugin.clis(rt) if rt.plugin else []):
        found += leaves(typer.main.get_command(extra), "")
    return found


def emit_result(result: Any) -> None:
    """A native command's return value: text = render(), json = one document."""
    many = result if isinstance(result, list) else None
    result = models.Nothing() if result is None else result
    if json_mode():
        typer.echo(json.dumps([r.model_dump(mode="json") for r in many] if many is not None else result.model_dump(mode="json"), sort_keys=True))
    else:  # a model with no lines prints nothing, not an empty line (`sm guard uv` must leave uv's output byte-exact)
        lines = [line for r in many for line in r.render()] if many is not None else ([] if isinstance(result, models.Nothing) else result.render())
        if lines:
            typer.echo("\n".join(lines))


def _review(rt: Runtime, app: typer.Typer, hidden: bool = False) -> None:
    review = group(app, "review", "review: verdicts of independent reviewers on exact digests.", "Use when a run stopped at a review.",
                   "verdict writes one receipt on this machine; list is read-only.", f"{PROG} review list", "the run's command with --approved SHA256", hidden)

    @review.command("verdict", context_settings=CTX, short_help="Record your verdict on one digest. For the reviewer, never the author.", help=H(
        "Record your verdict on one digest. For the reviewer, never the author.",
        "Use after you reviewed the exact bytes named in a reviewer brief.",
        "writes one verdict receipt on this machine; it cannot be changed afterwards.",
        f"{PROG} review verdict 3f2a...64 --approve --reviewer reviewer-1 --notes \"digest recomputed; scope matches\"",
        "tell the coordinator; they run the pipeline command with --approved SHA256"))
    def verdict(digest: str = typer.Argument(..., metavar="SHA256", help="The digest named in the reviewer brief."),
                approve: bool = typer.Option(False, "--approve", help="The bytes are right: the run may go on."),
                reject: bool = typer.Option(False, "--reject", help="The bytes are wrong: the run stays stopped."),
                reviewer: str = typer.Option(..., "--reviewer", metavar="NAME", help="Who reviewed; must differ from the run's author."),
                model: str = typer.Option("", "--model", help="The reviewer's model, recorded as evidence."),
                notes: str = typer.Option("", "--notes", help="What was checked and what was not."),
                kind: str = typer.Option("", "--kind", metavar="KIND", help="ci_change: a strong review of a commit whose CI paths changed since the last "
                                         "release (the release gate asks for it; needs --model and --notes naming the paths)."),
                as_json: bool = JSON) -> None:
        if approve == reject:
            fail("give exactly one of --approve and --reject", USAGE)
        if kind not in verdicts.KINDS:
            fail(f"--kind {kind}: expected one of {', '.join(k for k in verdicts.KINDS if k)}", USAGE)
        if kind == verdicts.CI_CHANGE and not (model.strip() and notes.strip()):
            fail("a ci_change verdict is a strong review: give --model and --notes naming the CI paths you read", USAGE)
        store = rt.providers.store
        authors = verdicts.authors_of(store, digest, go_mod.run_states(store))
        if not authors:
            fail(f"no run is waiting for a review of {digest[:12]} and no command registered it (`prepare` does): check the digest in the brief", USAGE)
        if reviewer in authors:
            fail(f"{reviewer} drives the run that produced these bytes: a different agent must review")
        if approve and (unchecked := verdicts.missing_check(store, digest)):
            fail(unchecked)
        data = {"digest": digest, "verdict": "approve" if approve else "reject", "reviewer": reviewer, "model": model, "notes": notes, "at": rt.providers.clock.now()}
        if kind:
            data["kind"] = kind
        try:
            store.put_receipt("verdicts", digest, data)
        except FileExistsError:
            fail(f"a verdict for {digest[:12]} is already recorded and cannot be changed: {store.get_receipt('verdicts', digest)}")
        emit(as_json, data, [f"recorded: {data['verdict']} {digest} by {reviewer}"], models.Verdict)

    @review.command("list", context_settings=CTX, short_help="Runs stopped at a review, and the verdict recorded for each digest.", help=H(
        "Runs stopped at a review, and the verdict recorded for each digest.",
        "Use when you wait for a reviewer, or to find the brief again.",
        "nothing (read-only).", f"{PROG} review list --json", "the run's command with --approved SHA256 once a verdict is there"))
    def list_(as_json: bool = JSON) -> None:
        store = rt.providers.store
        rows = []
        for s in go_mod.run_states(store):
            for name, st in s.get("steps", {}).items():
                if st.get("digest"):
                    v = store.get_receipt("verdicts", st["digest"]) or {}
                    rows.append({"label": s.get("label"), "step": name, "digest": st["digest"], "brief": st.get("brief", ""), "status": st.get("status"),
                                 "verdict": v.get("verdict", "none"), "reviewer": v.get("reviewer", "")})
        emit(as_json, rows, [f"{r['label']} {r['step']}: {r['digest']} verdict {r['verdict']} {r['reviewer']}  brief {r['brief']}" for r in rows] or ["no review in flight"], models.ReviewWait)


def _focus(rt: Runtime, app: typer.Typer, hidden: bool = False) -> None:
    focus = group(app, "focus", "focus: the heads or versions the owner declared the critical path.", "Use when the owner names what ships next.",
                  "add and done edit the focus list on this machine; list is read-only.", f"{PROG} focus list", f"{PROG} go", hidden)

    @focus.command("add", context_settings=CTX, short_help="Put one head or version on the critical path.", help=H(
        "Put one head or version on the critical path: `go` ranks it first.", "Use when the owner sets the destination.",
        "the focus list on this machine.", f"{PROG} focus add ~/work/fix-login --for 1.4.0 --note \"blocks the cut\"", f"{PROG} go"))
    def add(key: str = typer.Argument(..., metavar="KEY", help="A worktree path (stored resolved) or a version."),
            for_: str = typer.Option("", "--for", metavar="VERSION", help="The version this entry ships in."),
            note: str = typer.Option("", "--note", help="Why it is on the critical path."),
            as_json: bool = JSON) -> None:
        store = rt.providers.store
        with locked(store, "focus.list"):
            data = store.load("focus", "list") or {}
            data[os.path.realpath(os.path.expanduser(key)) if os.path.exists(os.path.expanduser(key)) else key] = {"for": for_, "note": note}
            store.save("focus", "list", data)
        emit(as_json, data, [f"focus: {', '.join(data)}"], models.FocusList)

    @focus.command("done", context_settings=CTX, short_help="Take one entry off the critical path.", help=H(
        "Take one entry off the critical path.", "Use when it shipped or the owner changed course.", "the focus list on this machine.",
        f"{PROG} focus done ~/work/fix-login", f"{PROG} go"))
    def done(key: str = typer.Argument(..., metavar="KEY", help="The entry as `focus list` shows it, or its worktree path."), as_json: bool = JSON) -> None:
        store = rt.providers.store
        with locked(store, "focus.list"):
            data = store.load("focus", "list") or {}
            full = os.path.realpath(os.path.expanduser(key))
            if key not in data and full not in data:
                fail(f"{key} is not in focus: {', '.join(data) or 'the list is empty'}", USAGE)
            data.pop(key, None)
            data.pop(full, None)
            store.save("focus", "list", data)
        emit(as_json, data, [f"focus: {', '.join(data) or 'none'}"], models.FocusList)

    @focus.command("list", context_settings=CTX, short_help="The critical path as declared.", help=H(
        "The critical path as declared.", "Use when you are unsure what the owner asked to ship first.", "nothing (read-only).",
        f"{PROG} focus list --json", f"{PROG} go"))
    def list_(as_json: bool = JSON) -> None:
        data = rt.providers.store.load("focus", "list") or {}
        emit(as_json, data, [f"{k}  for {v.get('for') or '-'}  {v.get('note', '')}" for k, v in data.items()] or ["no focus declared"], models.FocusList)


def _alarm(rt: Runtime, app: typer.Typer) -> None:
    alarm = group(app, "alarm", "alarm: stalls and failures that need someone now.", f"Use when `{PROG} go` shows an ALERT, and at every tick.",
                  "ack edits the acknowledged list on this machine; list is read-only.", f"{PROG} alarm list", "the command the alarm names")

    def rows() -> list[dict[str, Any]]:
        p = rt.providers
        return go_mod.alarms(p.store, rt.plugin, p, rt.settings, p.clock.now(), rt.project.stall_minutes if rt.project else 15.0)

    @alarm.command("list", context_settings=CTX, short_help="Every alarm that holds now. Exit 3 when an unacknowledged ALERT holds.", help=H(
        "Every alarm that holds now. Exit 3 when an unacknowledged ALERT holds.", "Use at every tick.",
        "nothing (read-only).", f"{PROG} alarm list --json", f"act on it, then {PROG} alarm ack KEY"))
    def list_(as_json: bool = JSON) -> None:
        found = rows()
        emit(as_json, found, [f"{'ACKED' if a['acked'] else a['level'].upper()} {a['key']}: {a['text']}" + (f" -> {a['next']}" if a["next"] else "") for a in found] or ["OK"], models.Alarm)
        raise typer.Exit(ALARM if any(a["level"] == "alert" and not a["acked"] for a in found) else OK)

    @alarm.command("ack", context_settings=CTX, short_help="Acknowledge one alarm: it stays listed, without exit 3.", help=H(
        "Acknowledge one alarm: it stays listed, without exit 3.", "Use after you started acting on it.",
        "the acknowledged list on this machine.", f"{PROG} alarm ack stall:head.ship:3f2a9c1d0b7e", f"{PROG} go"))
    def ack(key: str = typer.Argument(..., metavar="KEY", help="The alarm key as `alarm list` prints it."), as_json: bool = JSON) -> None:
        if key not in {a["key"] for a in rows()}:
            fail(f"no alarm {key} holds now", USAGE)
        store = rt.providers.store
        with locked(store, "alarms.acks"):
            data = store.load("alarms", "acks") or {}
            data[key] = rt.providers.clock.now()
            store.save("alarms", "acks", data)
        emit(as_json, data, [f"acknowledged: {key}"], models.AlarmAcks)


def _self(rt: Runtime, app: typer.Typer) -> None:
    self_ = group(app, "self", "self: the tool about itself; configuration, plugins, help lint, the command tree.",
                  "Use when a command is missing, the wrong project is selected, or you change the tool or a plugin.",
                  "nothing, except migrate-state, which moves state files to the names of this release.", f"{PROG} self config", f"{PROG} go")

    @self_.command("migrate-state", context_settings=CTX, short_help="Move state files to the names of this release, or back.", help=H(
        "Move the state files an earlier release used to the names of this release, in one pass, or back with --back.",
        "Use when a command refuses because state is not migrated; bin/promote runs it before the switch and bin/rollback runs --back.",
        "state files: each old path moves to its new name and becomes a link to it, and pipeline run states move into their subject journals"
        " (--back: the reverse). --check and --list change nothing.",
        f"{PROG} self migrate-state --check", f"{PROG} go"))
    def migrate_state(back: bool = typer.Option(False, "--back", help="Move every new path back to its old name (for an older release)."),
                      check: bool = typer.Option(False, "--check", help="Only say which paths still hold state at the old name; exit 3 if any."),
                      listing: bool = typer.Option(False, "--list", help="Only list every move with its state."),
                      keep: Path | None = typer.Option(None, "--keep", metavar="FILE", help="With --back: a JSON list (from --list --format json of the target release) of moves that stay."),
                      wait: int = typer.Option(600, "--wait", metavar="SECONDS", help="How long to wait for running write commands to finish before refusing."),
                      as_json: bool = JSON) -> None:
        if rt.project is None:
            fail("no project: there is no state to migrate", USAGE)
        moves = rt.plugin.state_moves(rt.settings, rt.project.home) if rt.plugin is not None else []
        if keep is not None and back:
            kept = {row.get("old") for row in json.loads(keep.read_text()).get("moves", [])}
            moves = [m for m in moves if str(m.old) not in kept]

        def rows() -> list[models.StateMoveRow]:
            return [models.StateMoveRow(old=str(m.old), new=str(m.new), state=_move_state(m)) for m in moves]

        if check or listing:
            found = models.StateMigration(action="list" if listing else "check", moves=rows())
            emit(as_json, found.model_dump(), found.render(), models.StateMigration)
            if check and any(r.state == "old" for r in found.moves):
                raise typer.Exit(ALARM)
            return
        log = rt.project.home / "state-migrations.log"
        log.parent.mkdir(parents=True, exist_ok=True)

        def note(line: str) -> None:
            with log.open("a") as handle:
                handle.write(f"{rt.providers.clock.now():.0f} {line}\n")

        try:
            with _exclusive_writers(rt, wait):
                done = statemove.back(moves, note) if back else statemove.forward(moves, note)
                runs = subjects.unmigrate_runs(rt.project.home) if back else subjects.migrate_runs(rt.project.home)  # run state <-> subject journals
                for line in runs:
                    note(line)
                done = [*done, *runs]
        except statemove.MigrationError as exc:
            fail(f"state not migrated: {exc}", INTERNAL, hint="nothing moved; fix the named paths, then run it again")
        result = models.StateMigration(action="back" if back else "forward", moves=rows(), done=done)
        emit(as_json, result.model_dump(), result.render(), models.StateMigration)

    @self_.command("config", context_settings=CTX, short_help="The effective settings and where each value came from.", help=H(
        "The effective settings and where each value came from (command line, environment, project file, user file, default).",
        "Use when commands are missing, a setting seems ignored, or state seems to belong to another project.", "nothing (read-only).",
        f"{PROG} --format json self config", f"{PROG} go"))
    def config(as_json: bool = JSON) -> None:
        pr = rt.project
        core: dict[str, dict[str, Any]] = {} if pr is None else {
            "format": {"value": OUT["format"], "source": pr.source_of("format")},
            "project.name": {"value": pr.name, "source": pr.source_of("project", "name")},
            "project.plugin": {"value": pr.plugin, "source": next((src for src in (pr.source_of("project", "plugins"), pr.source_of("project", "plugin")) if src != "default"), "default")},
            "project.base": {"value": pr.base, "source": pr.source_of("project", "base")},
            "lanes": {"value": pr.lanes, "source": pr.source_of("lanes")},
            "alarms.stall_minutes": {"value": pr.stall_minutes, "source": pr.source_of("alarms", "stall_minutes")}}
        if pr is not None:
            from .github import cli as github_cli

            for table, model in github_cli.TABLES.items():
                values = github_cli.table(rt, table)
                env = config_mod.prefixed(f"SM_{table.upper()}__")
                for key in model.model_fields:
                    source = f"environment SM_{table.upper()}__{key.upper()}" if key in env else pr.source_of(table, key)
                    core[f"{table}.{key}"] = {"value": values[key], "source": source}
        def secret(v: Any) -> Any:
            return "**********" if type(v).__name__.startswith("Secret") else v

        data: dict[str, Any] = {"project": pr.name if pr else None, "plugin": pr.plugin if pr else None, "plugin_loaded": rt.plugin is not None, "problem": rt.problem,
                "root": str(pr.root) if pr else None, "home": str(pr.home) if pr else None, "api": API_VERSION, "installed_plugins": plugins.available(),
                "core": core, "settings": {f"{n}.{k}": {"value": secret(v), "source": rt.sources.get(f"{n}.{k}", "default")}
                                           for n, table in getattr(rt.settings, "tables", {}).items() for k, v in table.items()},
                "environment": config_mod.environment_sources()}
        lines = [f"project: {data['project']}   plugin: {data['plugin']} ({'loaded' if data['plugin_loaded'] else 'NOT loaded: ' + rt.problem})",
                 f"config root: {data['root']}   state: {data['home']}   plugin API: {API_VERSION}   installed plugins: {', '.join(data['installed_plugins']) or 'none'}"]
        lines += [f"  {k} = {v['value']!r}   [{v['source']}]" for k, v in core.items()]
        lines += [f"  {k} = {v['value']!r}   [{v['source']}]" for k, v in data["settings"].items()]
        lines += [f"  ${k} = {v['value']!r}   [{v['source']}]" for k, v in data["environment"].items() if v["value"] is not None]
        emit(as_json, data, lines, models.EffectiveConfig)

    @self_.command("schema", context_settings=CTX, short_help="The JSON Schema of one result model, or the list of models.", help=H(
        "The JSON Schema of one result model, or the list of models.",
        "Use when you write a program, a dashboard or an MCP tool that reads `--format json` output.", "nothing (read-only).",
        f"{PROG} self schema GoReport", f"{PROG} --format json go"))
    def schema(name: str | None = typer.Argument(None, metavar="[MODEL]", help="A model name; without it, the list of models."), as_json: bool = JSON) -> None:
        known = dict(models.MODELS)
        for cmd in natives(rt):  # the result models of native commands
            for model in getattr(cmd, "models", []):
                known[model.__name__] = model
        if name is None:
            emit(as_json, sorted(known), [f"{n}: {(m.__doc__ or '').strip().splitlines()[0] if m.__doc__ else ''}" for n, m in sorted(known.items())], models.ModelNames)
            return
        if name not in known:
            fail(f"no model {name}", USAGE, f"one of: {', '.join(sorted(known))}")
        found = known[name].model_json_schema()
        emit(as_json, found, [json.dumps(found, indent=1, sort_keys=True)], models.JsonSchema)

    @self_.command("stats", context_settings=CTX, short_help="How the tool itself is used: calls, durations, refusals, gate time, top friction.", help=H(
        "How `sm` itself is used: calls, p50/p95 durations, refusals, reruns, flaky commands, gate time, top friction.",
        "Use at the start of each day to find what to improve in the tooling; a repeated refusal of one command is a signal that someone routes by hand.",
        "nothing (read-only; it reads invocations.jsonl and gate-history.jsonl in the state directory; reports unchanged reruns and flaky candidates by tree SHA).",
        f"{PROG} self stats --since 7d", "file a bead for each Top friction line"))
    def stats(since: str = typer.Option("1d", "--since", metavar="1d|7d|30d", help="The window to report: the last 1, 7 or 30 days."),
              calls: bool = typer.Option(False, "--calls", help="Calls per command over 30 days (or --since), every command listed; says whether the counts are trusted."),
              as_json: bool = JSON) -> None:
        if since not in statsreport.WINDOWS:
            fail(f"--since is 1d, 7d or 30d, not '{since}'", USAGE)
        if rt.project is None:
            fail("no project: there is no state to read", USAGE)
        home = rt.project.home
        if calls:
            paths = {r["path"].removeprefix(PROG).strip(): r["hidden"] for r in helpdoc.tree(command_tree(rt), PROG) if not r["group"]}
            counted = statsreport.calls(statsreport.read_calls(home / usagelog.CALLS), paths, 30 if since == "1d" else statsreport.WINDOWS[since])
            emit(as_json, counted.model_dump(), counted.render(), statsreport.CallsReport)
            return
        report = statsreport.build(statsreport.read_lines(home / usagelog.FILE), statsreport.read_lines(home / "gate-history.jsonl"), since)
        emit(as_json, report.model_dump(), report.render(), statsreport.StatsReport)

    @self_.command("gates", context_settings=CTX, short_help="The gates of this project and what each one means.", help=H(
        "The gates of this project: the named conditions a pipeline step needs right before it acts.",
        "Use when a step refused with `gate ... does not hold`.", "nothing (read-only).",
        f"{PROG} self gates", "fix what the gate names, then the pipeline command again"))
    def gates(as_json: bool = JSON) -> None:
        found = rt.plugin.gates(rt.settings) if rt.plugin else []
        emit(as_json, [{"name": g.name, "describe": g.describe} for g in found], [f"{g.name}: {g.describe}" for g in found] or ["this project has no gates"], models.GateRow)

    @self_.command("reviews", context_settings=CTX, short_help="Which write commands the diff since the last promoted commit reaches.", help=H(
        "Which write commands the diff since the last promoted commit reaches: the paths whose code it changes, each with\n"
        "the changed files it covers, and the changed files no write path covers. A promote needs one independent verdict\n"
        "on its exact commit (bin/promote_verdict.py); this list tells the reviewer what to read first: changes to the promote\n"
        "gate itself (bin/promote*, bin/rollback, bin/check) come first, as GATE CODE. A diff that reaches\n"
        "no write path still needs that verdict; CI covers the rest. Run it with the candidate's own sm (bin/sm-dev) when\n"
        "the candidate adds write commands: the reach is computed from the code of the sm that runs.",
        "Use when you review a candidate for bin/promote, and before you ask for that review.", "nothing (read-only; it runs git diff in REPO).",
        f"{PROG} self reviews --repo . --base v1.2.3",
        "read the diff, then record the verdict: bin/promote_verdict.py record --reviewer NAME COMMIT"))
    def reviews(repo: str = typer.Option(".", "--repo", metavar="DIR", help="The checkout of the candidate; its HEAD is the candidate."),
                base: str = typer.Option("", "--base", metavar="REV", help="Where the diff starts (default: the commit the live stable release was built from)."),
                as_json: bool = JSON) -> None:
        start = base or promoted_commit()
        if not start:
            fail("no promoted release is known to this sm (it was not started by a stable launcher): give --base REV", USAGE)
        git = rt.providers.commands
        found = [git.run(["git", "-C", repo, "rev-parse", "--verify", f"{rev}^{{commit}}"], timeout=30) for rev in (start, "HEAD")]
        if any(r.rc != 0 for r in found):
            fail(f"{repo}: cannot resolve {start} and HEAD to commits: {(found[0].err or found[1].err).strip()[:200]}", USAGE)
        old, new = (r.out.strip() for r in found)
        diff = git.run(["git", "-C", repo, "diff", "--name-only", "--no-renames", old, new], timeout=60)
        if diff.rc != 0:
            fail(f"{repo}: git diff {old[:12]} {new[:12]} failed: {diff.err.strip()[:200]}", USAGE)
        changed = sorted({line.strip() for line in diff.out.splitlines() if line.strip()})
        with reviewgraph.parsed_once():  # every unit asks for its imports: one parse per file for all of them
            hits = reviewgraph.reached_by(reviewgraph.reach(write_units(rt)), changed)
        gate_code = [c for c in changed if c.startswith(GATE_CODE)]
        covered = {f for files in hits.values() for f in files} | set(gate_code)
        unreached = [c for c in changed if c not in covered]
        lines = [f"base {old[:12]}  head {new[:12]}: {len(changed)} changed file(s), {len(hits)} write path(s) reached"]
        if gate_code:
            lines.append(f"GATE CODE (read first: it decides what goes live, partly with the candidate's own copy): {', '.join(gate_code)}")
        lines += [f"reached  {PROG} {p}\n         {', '.join(f)}" for p, f in hits.items()]
        if unreached:
            lines.append(f"in no write path ({len(unreached)}): {', '.join(unreached)}")
        if not hits:
            lines.append("the diff reaches no write path: CI covers it; the promote still needs one verdict on the head")
        emit(as_json, {"base": old, "head": new, "changed": changed, "gate": gate_code, "reached": [{"path": p, "files": f} for p, f in hits.items()],
                       "unreached": unreached}, lines, models.ReviewReach)

    @self_.command("plugins", context_settings=CTX, short_help="The loaded plugins in order (base first, later ones are outer layers) and the hooks each implements.", help=H(
        "The loaded plugins in order (base first, later ones are outer layers) and the hooks each implements.",
        "Use when a command, gate or trap is missing or comes from an unexpected layer.", "nothing (read-only).",
        f"{PROG} self plugins", f"{PROG} self hooks"))
    def plugins_(as_json: bool = JSON) -> None:
        rows = rt.plugin.describe() if rt.plugin else []
        lines = [f"{r['order']}. {r['name']} ({r['layer']}; {r['source']})\n   hooks: {', '.join(r['hooks']) or 'none'}"
                 + (f"\n   wraps: {', '.join(r['wrappers'])}" if r["wrappers"] else "") for r in rows]
        installed = sorted(set(plugins.available()) - {r["name"] for r in rows})
        emit(as_json, rows, (lines or [f"no plugin loaded: {rt.problem}"]) + [f"installed, not loaded: {', '.join(installed) or 'none'}"], models.PluginRow)

    @self_.command("hooks", context_settings=CTX, short_help="The hook specifications a plugin can implement, with kind and arguments.", help=H(
        "The hook specifications a plugin can implement, with kind and arguments.",
        "Use when you write or review a plugin.", "nothing (read-only).", f"{PROG} self hooks", f"{PROG} self plugins"))
    def hooks(as_json: bool = JSON) -> None:
        rows = plugins.hook_table()
        emit(as_json, rows, [f"{r['hook']}({', '.join(r['arguments'])})  [{r['kind']}]\n   {r['doc'].splitlines()[0] if r['doc'] else ''}" for r in rows], models.HookRow)

    @self_.command("tree", context_settings=CTX, short_help="The whole command tree as data: path, purpose, read-only or not, options.", help=H(
        "The whole command tree as data: path, purpose, read-only or not, options.",
        "Use for generating agent skills or MCP tools, or to see every command at once.", "nothing (read-only; it runs no command).",
        f"{PROG} self tree --json", f"{PROG} self lint"))
    def tree(all_: bool = typer.Option(False, "--all", help="Also the hidden commands (old names and plumbing that jobs and hooks call)."), as_json: bool = JSON) -> None:
        rows = [r for r in helpdoc.tree(command_tree(rt), PROG, writes_of(rt)) if all_ or not r["hidden"]]
        emit(as_json, rows, [f"{'R' if r['read_only'] else 'W'}{'H' if r['hidden'] else ''} {r['path']}: {r['purpose']}" for r in rows], models.TreeRow)

    @self_.command("doc", context_settings=CTX, short_help="The command reference in Markdown, from the command tree.", help=H(
        "The command reference in Markdown, generated from the command tree: every visible command with its Use, Changes and Example lines.",
        "Use when a skill, a start document or a new agent needs the commands; paste it between the generated markers.",
        "nothing (read-only; it prints, it runs no command).", f"{PROG} self doc > reference.md", f"{PROG} self lint"))
    def doc(as_json: bool = JSON) -> None:
        rows = [r for r in helpdoc.tree(command_tree(rt), PROG, writes_of(rt)) if not r["hidden"]]
        text = helpdoc.markdown(rows, PROG)
        emit(as_json, text, [text], models.Markdown)

    @self_.command("tokens", context_settings=CTX, short_help="Tokens of one agent session, from its transcript file.", help=H(
        "Tokens of one agent session, from its transcript (JSON lines with a usage block per model answer).",
        "Use after a session, to measure what a task or a cold start cost.", "nothing (read-only; it reads the one file).",
        f"{PROG} self tokens ~/.claude/projects/X/SESSION.jsonl", "compare with the budget of the task"))
    def tokens(transcript: Path = typer.Argument(..., metavar="TRANSCRIPT", help="The session transcript, one JSON object per line."), as_json: bool = JSON) -> None:
        try:
            text = transcript.expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            fail(f"cannot read {transcript}: {exc.strerror}", USAGE)
        found = models.Tokens.count(text.splitlines())
        emit(as_json, found.model_dump(), found.render(), models.Tokens)

    @self_.command("lint", context_settings=CTX, short_help="Check every command's help against the help contract.", help=H(
        "Check every command's help against the help contract (purpose, Use when, Changes:, Example:, Next:).",
        "Use when you add or change a command, in core or in a plugin.", "nothing (read-only; it runs no command).",
        f"{PROG} self lint", "fix the first line it prints, then run it again"))
    def lint(as_json: bool = JSON) -> None:
        rows = helpdoc.tree(command_tree(rt), PROG, writes_of(rt))  # strict: hidden commands too
        problems = [line for row in rows if (line := helpdoc.finding(row))]
        problems += [f"{row['path']}: {found}" for row in rows if (found := helpdoc.disagrees(row))]
        for rule in (rt.plugin.lint_rules() if rt.plugin else []):  # hook sm_help_lint_rules
            problems += [f"{row['path']}: {found}" for row in rows if (found := rule(row))]
        root = command_tree(rt)
        problems += listopts.findings(root, PROG)  # one parser for list options
        if surface.of(natives(rt))[0] is not None:  # the `sm -h` contract of a declared surface replaces the root's five lines
            problems += surface.help_findings(root.help or "")
        emit(as_json, problems, problems or ["help contract: every command passes"], models.LintFindings)
        raise typer.Exit(1 if problems else 0)


# ----------------------------------------------------------------------------- the write paths and their units
def _move_state(move: Any) -> str:
    if statemove.pending([move]):
        return "old"
    return "moved" if move.old.is_symlink() else ("new" if move.new.exists() else "none")


@contextlib.contextmanager
def _exclusive_writers(rt: Runtime, wait: int) -> Iterator[None]:
    """The writers' lock, exclusively: no write command runs while state files move. Gives up after `wait` seconds."""
    import fcntl

    path = Path(rt.project.home) / native_mod.WRITERS_LOCK  # type: ignore[union-attr]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        waited = 0.0
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if waited >= wait:
                    raise statemove.MigrationError(f"a write command still runs after {wait}s (it holds {path})") from None
                rt.providers.clock.sleep(1.0)
                waited += 1.0
        yield


def _core_unit() -> None:
    """Anchor of the unit of the core's own local writers in this module (`review verdict`, `focus add|done`,
    `alarm ack`): this file and every core file it imports."""


CORE_WRITERS = ("review verdict", "focus add", "focus done", "alarm ack", "self migrate-state")


def core_units() -> list[Any]:
    from .github import cli as github_cli

    return [*(native_mod.unit(path, _core_unit) for path in CORE_WRITERS), native_mod.unit("ci cancel", github_cli.unit_anchor)]


def natives(rt: Runtime) -> list[Any]:
    """The native commands and group helps of this project: the core's own (`steuermaschine.commands`, the innermost
    layer), then the plugins', base first; a later entry with the same path replaces an earlier one (the order rule)."""
    from . import commands

    by_key = {}
    for item in [*commands.natives(), *(rt.plugin.native(rt.settings) if rt.plugin else [])]:
        by_key[(type(item).__name__, item.path)] = item
    return list(by_key.values())


def write_units(rt: Runtime) -> list[Any]:
    """Every write path of this project with its unit (its code): the core's writers, the plugin's write commands and
    its pipelines. `self reviews` names those a change reaches."""
    writers = [c for c in natives(rt) if getattr(c, "writes", False)]
    pipelines = [pl.unit for pl in (rt.pipelines() if rt.project else []) if pl.unit is not None]
    return [*core_units(), *writers, *pipelines]


GATE_CODE = ("bin/promote", "bin/rollback", "bin/check")  # the promote gate of this repository: promote, promote_lib.sh, promote_verdict.py


def promoted_commit() -> str:
    """The commit the live stable release was built from: bin/promote names each release directory after the first 12
    characters of its commit, and the launcher passes the pointer to it ($STEUERMASCHINE_RELEASE). "" when unknown."""
    pointer = native_mod._release()
    name = Path(pointer).name if pointer else ""
    return name if len(name) >= 7 and all(c in "0123456789abcdef" for c in name) else ""


def _declared_writes(rt: Runtime) -> dict[str, bool]:
    out = {f"{PROG} {c.path}": bool(c.writes) for c in natives(rt) if hasattr(c, "fn")}
    out.update({f"{PROG} {pl.subject} {pl.verb}": True for pl in (rt.pipelines() if rt.project else [])})
    out.update({f"{PROG} {path}": True for path in (*CORE_WRITERS, "ci cancel")})
    return out


def writes_of(rt: Runtime) -> dict[str, bool]:
    """Command path (with the program name) -> whether it writes, for every command that declares it: native
    commands by `writes=`, pipelines and the core's writers always. Other commands are read from their help.
    A renamed old name is a stub and writes nothing; a verb mounted at a new path writes what its target writes."""
    out = _declared_writes(rt)
    _, verbs, renames, _ = surface.of(natives(rt))
    out.update({f"{PROG} {v.path}": out[f"{PROG} {v.target}"] for v in verbs if f"{PROG} {v.target}" in out})
    out.update({f"{PROG} {r.old}": False for r in renames if r.stub})
    out[PROG] = False  # the root runs nothing by itself
    return out


# ----------------------------------------------------------------------------- merging the plugin's app, running
def command_tree(rt: Runtime) -> Any:
    """The Click group that runs: core, then the plugin's app merged at the root. A plugin name replaces a core name
    (the plugin owns that word in its project), except the reserved ones."""
    root = typer.main.get_command(build(rt))
    rt.plugin_paths = set()
    if rt.plugin is not None:
        keep = {tuple(p.split()) for p in rt.plugin.native_paths(rt.settings)}
        for extra in rt.plugin.clis(rt):  # base first: a later layer's name replaces an earlier one's
            built = typer.main.get_command(extra)
            _merge(root, built, (), keep)
            rt.plugin_paths |= set(getattr(built, "commands", {})) - RESERVED
    native_app = native_mod.build(natives(rt), rt, emit_result, fail, CTX)
    if native_app is not None:  # after the Typer apps: a native command replaces whatever has its path; groups merge
        built = typer.main.get_command(native_app)
        _merge_deep(root, built)
        native_mod.mount_aliases(root, natives(rt))
        rt.plugin_paths |= {c.path.split()[0] for c in (rt.plugin.native(rt.settings) if rt.plugin else []) if hasattr(c, "fn")} - RESERVED
    project_line = f"Project: {rt.project.name} (plugin {rt.project.plugin})" if rt.project else ""
    surface.apply(root, natives(rt), _declared_writes(rt), fail, typer.echo, PROG, project_line)  # the visible verbs, stubs, the resolver
    listopts.install(root)  # one rule for list options: repeated or A,B
    dispatch.add_format_option(root, choose_format)  # `--format` after the command, for every command of every layer
    return root


def choose_format(value: str) -> None:
    OUT["format"] = value


def execute(rt: Runtime, args: list[str]) -> int:
    """Run one command line on a runtime and return the exit code: what `main` does once the settings are resolved."""
    if (asked := dispatch.scan_format(args)) in dispatch.FORMATS:
        OUT["format"] = asked  # known before parsing, so a usage error is already one JSON document
    return run_logged(rt, command_tree(rt), list(args) or ["go"])


def run_logged(rt: Runtime, root: Any, args: list[str]) -> int:
    """Run the command tree once and append the invocation to the log (usagelog.py; it never fails or blocks)."""
    started, began = time.time(), time.perf_counter()
    code = INTERNAL  # an exception that leaves `dispatch.run` is logged as a tool error and still propagates
    try:
        code = dispatch.run(root, args, PROG, json_mode)
        return code
    except KeyboardInterrupt:
        code = 130
        raise
    finally:
        usagelog.record(rt.project.home if rt.project else None, None if config_mod.environment().pytest_current_test else Subprocess(), root, args, started, began, code)


def _merge_deep(into: Any, source: Any) -> None:
    for name, command in getattr(source, "commands", {}).items():
        mine = into.commands.get(name)
        if name in RESERVED and mine is not None and not hasattr(command, "commands"):
            continue
        if mine is not None and hasattr(mine, "commands") and hasattr(command, "commands"):
            _merge_deep(mine, command)
        elif name not in RESERVED:
            into.commands[name] = command


def _merge(into: Any, source: Any, path: tuple[Any, ...], keep: set[Any]) -> None:
    for name, command in getattr(source, "commands", {}).items():
        here = (*path, name)
        if (not path and name in RESERVED) or here in keep:
            continue
        mine = into.commands.get(name)
        if mine is not None and hasattr(mine, "commands") and hasattr(command, "commands") and any(k[:len(here)] == here for k in keep):
            _merge(mine, command, here, keep)  # both are groups and the core keeps something below: merge one level down
        else:
            into.commands[name] = command


def runtime(start: str | None = None, cli: dict[str, Any] | None = None) -> Runtime:
    """Resolve the settings and load the plugin. Never raises: a problem is kept and shown by `go` and `self config`."""
    try:
        project = config_mod.load(start, cli, set(plugins.available()))
    except config_mod.ConfigError as exc:
        return Runtime(None, None, problem=str(exc), config_error=True)
    if project is None:
        return Runtime(None, None, problem=f"no {config_mod.FILE} here or above, and no user config {config_mod.user_config()}")
    OUT["format"] = project.format
    if not project.plugins:
        return Runtime(project, None, problem="no plugin selected (project.plugin is 'none')")
    try:
        plugin = plugins.load(project.plugins)
        settings, sources = plugin.validate(project.raw_settings, project.root, project.layers)
    except plugins.PluginError as exc:
        return Runtime(project, None, problem=str(exc))
    return Runtime(project, plugin, settings, sources)


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    start, cli, i = None, {}, 0
    rest: list[str] = []
    while i < len(args):  # the root options are read here too: the settings must exist before the command tree is built
        a = args[i]
        if a == "--project" and i + 1 < len(args):
            start, i = args[i + 1], i + 2
            continue
        if a.startswith("--format") and not rest:
            cli["format"] = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")
        elif not a.startswith("-") and not (rest and rest[-1] == "--format"):
            rest += args[i:]
            break
        rest.append(a)
        i += 1
    args = rest
    OUT["format"] = "text"
    if (asked := dispatch.scan_format(args)) is not None:  # `--format` after the command counts like the root option
        cli["format"] = asked
    if cli.get("format") not in (None, "text", "json"):
        typer.echo(f"{PROG}: --format is text or json, not '{cli['format']}'", err=True)
        sys.exit(USAGE)
    rt = runtime(start, cli)
    words = [a for a in args if not a.startswith("-") and a not in ("text", "json")]
    broken = (rt.project is not None and rt.plugin is None and bool(rt.project.plugins)) or (rt.project is None and rt.config_error)
    if broken and words[:1] != ["self"]:
        # Never fall back to core commands silently: a coordinator would read a foreign `head` or `release` as its own,
        # and a builder would see "no such command". Only `sm self ...` runs, to find the cause.
        what = f"plugin '{rt.project.plugin}' is NOT loaded" if rt.project is not None else "the settings are NOT valid"
        hint = f"nothing was run. Find the cause with `{PROG} self config`; to run the core alone on purpose set STEUERMASCHINE_PLUGIN=none."
        if cli.get("format") == "json":
            typer.echo(models.Error(error=f"{what}: {rt.problem}", code=INTERNAL, hint=hint).model_dump_json())
        else:
            typer.echo(f"{PROG}: {what}: {rt.problem}\n{PROG}: {hint}", err=True)
        sys.exit(INTERNAL)
    if cli.get("format") in dispatch.FORMATS:
        OUT["format"] = cli["format"]
    try:
        root = command_tree(rt)
    except Exception as exc:  # a broken plugin must not take the core commands away
        typer.echo(f"{PROG}: plugin '{rt.plugin.name if rt.plugin else '?'}' failed to build its commands ({type(exc).__name__}: {exc}); core commands only", err=True)
        rt = Runtime(rt.project, None, problem=f"plugin failed: {exc}")
        root = command_tree(rt)
    if not [a for a in args if not a.startswith("--format") and a not in ("text", "json")]:
        args = [*args, "go"]
    try:
        sys.exit(run_logged(rt, root, args))
    except PermissionError as exc:
        typer.echo(f"{PROG}: {exc}", err=True)
        sys.exit(INTERNAL)


if __name__ == "__main__":
    main()
