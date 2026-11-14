"""The state machine: one resumable run per subject (a head or a release) through a pipeline of steps.

Contract of every run:
- Each step is checked against the world before it acts (`probe`), so a rerun does nothing twice and a step done by
  hand is adopted.
- State is saved after every step. The same command continues where the last one stopped.
- Inputs are given once and remembered.
- A step that writes outside this machine (`external=True`) runs only with `live=True`. Without it the run stops in
  front of that step and prints what it would do.
- A `ReviewStop` ends the run until a different agent has recorded a verdict for the exact digest.
- A step done under one fingerprint (for example head and base) is void when the fingerprint changes, until the first
  external step is done. After that nothing is voided: the write happened.
- Every run ends with the `go` contract: PHASE, NEXT, WAITING ON, TRAP.
"""

from __future__ import annotations

import shlex
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from . import INTERNAL, OK, PROG, REFUSED, STATE_SCHEMA, USAGE, WAITING
from .protocols import Busy, Providers


class Refuse(Exception):
    """A step or gate says no. `next_cmd` is the one command that helps."""

    def __init__(self, message: str, next_cmd: str = "", code: int = REFUSED):
        super().__init__(message)
        self.message, self.next_cmd, self.code = message, next_cmd, code


class Wait(Exception):
    """Nothing is wrong, something else must finish first. The same command continues."""

    def __init__(self, message: str, waiting_on: str = ""):
        super().__init__(message)
        self.message, self.waiting_on = message, waiting_on or message


@dataclass
class Subject:
    key: str  # names the state file: a head SHA, a version
    label: str  # for people: branch and short SHA
    fingerprint: dict[str, str] = field(default_factory=dict)  # what done steps are bound to
    facts: dict[str, Any] = field(default_factory=dict)  # anything steps need (worktree, branch)


@dataclass
class Ctx:
    providers: Providers
    settings: dict[str, Any]  # the plugin's table of steuermaschine.toml
    subject: Subject
    inputs: dict[str, Any]  # remembered arguments
    data: dict[str, Any]  # what earlier steps recorded (PR number, digest)
    live: bool
    author: str
    say: Callable[[str], None]


@dataclass
class Step:
    name: str
    run: Callable[[Ctx], str]  # returns one line of detail; raises Refuse or Wait
    changes: str = "nothing outside this machine"
    external: bool = False
    plan: Callable[[Ctx], str] | None = None  # what `run` would do, for the dry-run stop
    probe: Callable[[Ctx], str | None] | None = None  # is it already true in the world
    binds: tuple[str, ...] = ()  # fingerprint keys this step's result depends on
    trap: str = ""
    gates: tuple[str, ...] = ()  # names of gates that must hold before `run`


@dataclass
class ReviewStop:
    name: str
    subject: Callable[[Ctx], tuple[Any, ...]]  # (digest of the exact bytes, brief for the reviewer[, the file under review])
    changes: str = "writes a reviewer brief on this machine; stops until a different agent approves the digest"
    binds: tuple[str, ...] = ()
    trap: str = "The digest alone is not a review: a different agent must record the verdict."
    external: bool = False
    gates: tuple[str, ...] = ()


@dataclass
class Insert:
    """A step a plugin adds to another plugin's pipeline (hook sm_steps): give `after` or `before` a step name."""

    step: object
    after: str = ""
    before: str = ""


@dataclass
class Gate:
    name: str
    describe: str
    check: Callable[[Ctx], tuple[bool, str]]  # read-only


@dataclass
class Pipeline:
    subject: str  # the command group: "head" or "release"
    verb: str  # the command: `sm head admit`
    purpose: str
    steps: list[Any]
    identify: Callable[[Providers, dict[str, Any], str], Subject]
    inputs: dict[str, str] = field(default_factory=dict)  # name -> help; given as --set name=value
    when: str = ""
    example_arg: str = "."
    done_next: str = f"{PROG} go"
    unit: object = None  # the code of the pipeline itself (`native.unit(...)`): what `sm self reviews` follows for this write path

    @property
    def id(self) -> str:
        return f"{self.subject}.{self.verb}"


@dataclass
class Report:
    """What one run prints, and what `go` reads back from the state file."""

    project: str
    pipeline: str
    key: str
    label: str
    status: str  # finished | review | waiting | dry_run | refused | plan
    phase: str
    next: str = ""
    waiting_on: str = ""
    trap: str = ""
    exit_code: int = OK
    steps: list[Any] = field(default_factory=list)
    log: list[Any] = field(default_factory=list)
    review: dict[str, Any] = field(default_factory=dict)  # at a review stop: step, file, sha256, brief, the command that resumes

    def lines(self) -> list[str]:
        out = [f"PHASE    {self.phase}", f"NEXT     {self.next or 'nothing runnable now'}"]
        if self.review:
            out += [f"REVIEW   file   {self.review.get('file') or '(the brief names the bytes)'}", f"         sha256 {self.review['sha256']}",
                    f"         brief  {self.review['brief']}"]
        if self.waiting_on:
            out.append(f"WAITING  {self.waiting_on}")
        if self.trap:
            out.append(f"TRAP     {self.trap}")
        return out

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Options:
    live: bool = False
    plan: bool = False  # print every step, write nothing
    approved: str = ""
    author: str = "coordinator"
    lane: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)


def stamp(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now))


def identify_head(providers: Providers, settings: dict[str, Any], worktree: str) -> Subject:
    """The usual subject of a head pipeline: a clean worktree on a work branch."""
    import os

    wt = os.path.realpath(os.path.expanduser(worktree))
    if not os.path.isdir(wt):
        raise Refuse(f"{wt} is not a directory", code=USAGE)
    vcs = providers.vcs
    try:
        head, branch, base = vcs.head(wt), vcs.branch(wt), vcs.base(wt)
    except RuntimeError as exc:
        raise Refuse(str(exc), code=USAGE) from None
    if branch in ("main", "master", "HEAD", ""):
        raise Refuse(f"branch is '{branch}': a head pipeline works on a work branch", code=USAGE)
    if dirty := vcs.dirty(wt):
        raise Refuse("dirty tree: commit or discard first (a run binds the exact head):\n" + dirty[:300])
    return Subject(key=head, label=f"{branch} {head[:9]}", fingerprint={"head": head, "base": base},
                   facts={"worktree": wt, "branch": branch, "head": head, "base": base})


class Engine:
    def __init__(self, project: str, providers: Providers, settings: dict[str, Any] | None = None, lanes: dict[str, Any] | None = None,
                 gates: dict[str, Any] | None = None, traps: dict[str, Any] | None = None, out: Callable[[str], None] = print,
                 run_step: Callable[..., Any] | None = None):
        self.project, self.p, self.settings = project, providers, settings or {}
        self.run_step = run_step or (lambda step, ctx: step.run(ctx))  # plugins wrap this (hook sm_step_run)
        self.lanes, self.gates, self.traps, self.out = lanes or {}, gates or {}, traps or {}, out

    # ------------------------------------------------------------------ helpers
    def kind(self, pipeline: Pipeline) -> str:
        return f"runs.{pipeline.id}"

    def command(self, pipeline: Pipeline, arg: str) -> str:
        return f"{PROG} {pipeline.subject} {pipeline.verb} {shlex.quote(arg)}"

    def trap(self, pipeline: Pipeline, step: Step) -> str:
        return str(self.traps.get(f"{pipeline.id}.{step.name}", step.trap))

    def _lane_check(self, pipeline: Pipeline, lane: str, key: str) -> None:
        cap = self.lanes.get(lane)
        if cap is None:
            return
        kind = self.kind(pipeline)
        busy = [s for k in self.p.store.keys(kind) if (s := self.p.store.load(kind, k)) and s.get("lane") == lane
                and s.get("status") != "finished" and s.get("key") != key]
        if len(busy) >= cap:
            names = ", ".join(s.get("label", s["key"][:9]) for s in busy)
            raise Refuse(f"lane '{lane}' is full ({len(busy)} of {cap}): {names}. Finish or abandon one first",
                         f"{PROG} {pipeline.subject} status")

    # ------------------------------------------------------------------ the run
    def advance(self, pipeline: Pipeline, arg: str, opts: Options) -> Report:
        cmd = self.command(pipeline, arg)
        try:
            subject = pipeline.identify(self.p, self.settings, arg)
        except Refuse as no:
            return self._end(Report(self.project, pipeline.id, "", arg, "refused", f"cannot start: {no.message}", no.next_cmd, exit_code=no.code))
        cmd = self.command(pipeline, subject.facts.get("worktree", arg))  # an absolute path: NEXT works from any directory
        try:
            with self.p.store.lock(f"{pipeline.id}.{subject.key}") if not opts.plan else _nolock():
                return self._advance(pipeline, subject, cmd, opts, subject.facts.get("worktree", arg))
        except Busy:
            return self._end(Report(self.project, pipeline.id, subject.key, subject.label, "waiting",
                                    "another process is advancing this subject or the run it supersedes", cmd, "that process to finish", exit_code=WAITING))

    def _advance(self, pipeline: Pipeline, subject: Subject, cmd: str, opts: Options, arg: str) -> Report:
        store, kind, now = self.p.store, self.kind(pipeline), self.p.clock.now()
        state = store.load(kind, subject.key)
        log: list[str] = []
        total = len(pipeline.steps)

        def say(text: str) -> None:
            log.append(text)
            self.out(text)

        if state is None:
            lane = opts.lane or "default"
            try:
                self._lane_check(pipeline, lane, subject.key)
            except Refuse as no:
                return self._end(Report(self.project, pipeline.id, subject.key, subject.label, "refused", no.message, no.next_cmd, exit_code=no.code))
            state = {"schema": STATE_SCHEMA, "project": self.project, "pipeline": pipeline.id, "key": subject.key, "label": subject.label,
                     "arg": arg, "lane": lane, "author": opts.author, "inputs": {}, "data": {}, "steps": {},
                     "fingerprint": subject.fingerprint, "created": stamp(now)}
            if not opts.plan:
                self._supersede(pipeline, subject, arg, say)
        state["inputs"].update(opts.inputs)  # arguments given now win; the rest is remembered
        steps = state["steps"]
        names = [s.name for s in pipeline.steps]

        def done(name: str) -> bool:
            return bool(steps.get(name, {}).get("status") == "done")

        def void_from(index: int, why: str) -> None:
            dropped = [n for n in names[index:] if n in steps]
            for n in dropped:
                del steps[n]
            if dropped:
                say(f"{why}: {', '.join(dropped)} start again")

        frozen = any(done(s.name) and s.external for s in pipeline.steps)
        if not frozen:
            for i, s in enumerate(pipeline.steps):
                bound = steps.get(s.name, {}).get("fingerprint", {})
                moved = [k for k in s.binds if k in bound and bound[k] != subject.fingerprint.get(k)]
                if moved:
                    void_from(i, f"{', '.join(moved)} moved since '{s.name}' was done")
                    break
        state["fingerprint"] = subject.fingerprint
        ctx = Ctx(self.p, self.settings, subject, state["inputs"], state["data"], opts.live, state.get("author", opts.author), say)

        def save() -> None:
            if not opts.plan:
                state["updated"] = stamp(self.p.clock.now())
                store.save(kind, subject.key, state)

        def end(status: str, phase: str, next_cmd: str = "", waiting_on: str = "", trap: str = "", code: int = OK, review: dict[str, str] | None = None) -> Report:
            if status == "waiting":
                state.setdefault("waiting_since", self.p.clock.now())
            else:
                state.pop("waiting_since", None)
            for gone in ("pid", "running", "running_since"):
                state.pop(gone, None)
            state.update(status=status, phase=phase, next=next_cmd, waiting_on=waiting_on, trap=trap, review=review or {})
            save()
            rows = [{"name": n, "status": steps.get(n, {}).get("status", "open"), "detail": steps.get(n, {}).get("detail", "")} for n in names]
            return self._end(Report(self.project, pipeline.id, subject.key, subject.label, status, phase, next_cmd, waiting_on, trap, code, rows, log, review or {}))

        def running(step: Step, trap: str) -> None:
            """Before a step acts: the state says which process runs which step, so `go` can tell a running step from
            one whose process is gone (then the same command continues)."""
            import os

            state.update(status="running", phase=f"{step.name}: running (pid {os.getpid()})", next="", running=step.name, pid=os.getpid(),
                         running_since=self.p.clock.now(), waiting_on=f"pid {os.getpid()} running '{step.name}'", trap=trap, review={})
            save()

        mode = "  (plan: nothing is run or written)" if opts.plan else "  (live)" if opts.live else "  (dry run: stops before the first step that writes outside this machine)"
        say(f"{pipeline.id}: {subject.label}{mode}")
        for i, step in enumerate(pipeline.steps):
            tag = f"[{i + 1}/{total} {step.name}]"
            trap = self.trap(pipeline, step)
            if opts.plan:
                what = "STOP for an independent review of a digest" if isinstance(step, ReviewStop) else (step.plan(ctx) if step.plan and not done(step.name) and done_all(steps, names[:i]) else "")
                say(f"{tag} {steps.get(step.name, {}).get('status', 'open')}. Changes: {step.changes}" + (f". Would: {what}" if what else ""))
                continue
            try:
                if isinstance(step, ReviewStop):
                    digest, brief, *rest = step.subject(ctx)
                    under_review = str(rest[0]) if rest and rest[0] else ""
                    rec = steps.get(step.name, {})
                    if rec.get("status") == "done" and (rec.get("digest") == digest or frozen):
                        say(f"{tag} already approved: {rec.get('digest')}")
                        continue
                    if rec.get("status") == "done":
                        void_from(i, f"the bytes under review changed ({rec.get('digest', '')[:12]} -> {digest[:12]}), the verdict is void")
                    if not digest:  # nothing to review (for example the default admission policy)
                        steps[step.name] = {"status": "done", "detail": "nothing to review", "digest": "", "at": stamp(self.p.clock.now())}
                        say(f"{tag} done: nothing to review")
                        save()
                        continue
                    record = f"{PROG} review verdict {digest} --approve --reviewer NAME"
                    spent = {r.get("digest") for n, r in steps.items() if n != step.name and r.get("status") == "done"}
                    approved = "" if opts.approved in spent else opts.approved  # a digest an earlier stop consumed is not this stop's
                    if not approved:
                        head = f"File under review: {under_review}\nsha256 (canonical): {digest}\n\n" if under_review else f"sha256: {digest}\n\n"
                        where = self.p.agents.brief("reviewer", f"{pipeline.id}-{step.name.replace(' ', '-')}-{subject.key[:12]}",
                                                    head + brief + _verdict_lines(digest, ctx.author))
                        steps[step.name] = {"status": "waiting", "detail": digest, "digest": digest, "brief": where, "file": under_review}
                        say(f"{tag} STOP. These bytes need an independent review (a different agent, not {ctx.author}).")
                        if under_review:
                            say(f"  file: {under_review}")
                        say(f"  sha256: {digest}")
                        say(f"  reviewer brief: {where}")
                        say(f"  the reviewer records: {record}")
                        resume = f"{cmd} --approved {digest}"
                        return end("review", f"{step.name}: waiting for an independent verdict on {digest[:12]}", resume,
                                   f"a reviewer who is not {ctx.author}; brief: {where}", trap,
                                   review={"step": step.name, "file": under_review, "sha256": digest, "brief": where, "record": record, "resume": resume})
                    if approved != digest:
                        raise Refuse(f"--approved {opts.approved[:12]}... is not the digest of the bytes under review ({digest}). "
                                     "Changed bytes need a new verdict", cmd, USAGE)
                    verdict = store.get_receipt("verdicts", digest)
                    if not verdict:
                        raise Refuse(f"no verdict is recorded for {digest}: the digest alone is not a review", record)
                    if verdict.get("verdict") != "approve":
                        raise Refuse(f"the recorded verdict for {digest[:12]} is '{verdict.get('verdict')}' by {verdict.get('reviewer')}: "
                                     f"{verdict.get('notes', '')}. Change the work; new bytes get a new review", cmd)
                    if verdict.get("reviewer") == ctx.author:
                        raise Refuse(f"the verdict was recorded by {ctx.author}, who also drives this run: a different agent must review")
                    steps[step.name] = {"status": "done", "detail": f"approved by {verdict['reviewer']}", "digest": digest,
                                        "at": stamp(self.p.clock.now()), "fingerprint": {k: subject.fingerprint.get(k) for k in step.binds}}
                    say(f"{tag} done: {digest[:12]} approved by {verdict['reviewer']}")
                    save()
                    continue
                if done(step.name):
                    say(f"{tag} already done: {steps[step.name].get('detail', '')}")
                    continue
                detail = step.probe(ctx) if step.probe else None
                if detail is None:
                    for name in step.gates:
                        ok, why = self.gates[name].check(ctx)
                        if not ok:
                            raise Refuse(f"gate '{name}' does not hold: {why}", f"{PROG} self gates")
                    if step.external and not opts.live:
                        would = step.plan(ctx) if step.plan else step.changes
                        say(f"{tag} DRY RUN STOP. Changes: {step.changes}")
                        say(f"  would: {would}")
                        return end("dry_run", f"{step.name}: ready, not run (it writes outside this machine)", f"{cmd} --live", trap=trap)
                    running(step, trap)
                    detail = self.run_step(step, ctx)
                else:
                    detail = f"{detail} (adopted: already true)"
                steps[step.name] = {"status": "done", "detail": detail, "at": stamp(self.p.clock.now()),
                                    "fingerprint": {k: subject.fingerprint.get(k) for k in step.binds}}
                frozen = frozen or step.external
                say(f"{tag} done: {detail}")
                save()
            except Wait as wait:
                say(f"{tag} WAITING: {wait.message}")
                return end("waiting", f"{step.name}: {wait.message}", cmd + (" --live" if opts.live else ""), wait.waiting_on, trap, WAITING)
            except Refuse as no:
                say(f"{tag} REFUSED: {no.message}")
                return end("refused", f"{step.name} refused: {no.message.splitlines()[0][:200]}", no.next_cmd, trap=trap, code=no.code)
            except Exception as exc:  # a provider bug must not lose the state of the steps before it
                say(f"{tag} ERROR: {type(exc).__name__}: {exc}")
                return end("refused", f"{step.name} failed inside the tool: {type(exc).__name__}: {exc}", cmd, trap=trap, code=INTERNAL)
        if opts.plan:
            return self._end(Report(self.project, pipeline.id, subject.key, subject.label, "plan", "plan printed, nothing run", cmd, log=log))
        return end("finished", f"{pipeline.id} finished for {subject.label}", pipeline.done_next)

    def _end(self, report: Report) -> Report:
        return report

    def _supersede(self, pipeline: Pipeline, subject: Subject, arg: str, say: Callable[[str], None]) -> None:
        """A new run for the same argument (the same worktree, the same version) replaces an unfinished older one:
        a moved head is a new subject. The older run is marked, never deleted; only steps not done outside are lost."""
        store, kind = self.p.store, self.kind(pipeline)
        for key in store.keys(kind):
            if key == subject.key:
                continue
            with store.lock(f"{pipeline.id}.{key}"):  # the older run's own lock: no advance of it writes in between (Busy: wait)
                old = store.load(kind, key)
                if not old or old.get("arg") != arg or old.get("status") in ("finished", "superseded"):
                    continue
                old.update(status="superseded", phase=f"superseded by {subject.label}", next="", waiting_on="", superseded_by=subject.key)
                store.save(kind, key, old)
            say(f"{old.get('label', key[:12])}: superseded by {subject.label} (the run of the earlier subject ends here)")


def done_all(steps: dict[str, Any], names: list[str]) -> bool:
    return all(steps.get(n, {}).get("status") == "done" for n in names)


def _verdict_lines(digest: str, author: str) -> str:
    return (f"\n\nYou are the reviewer. You are not {author} and you did not produce these bytes. You change nothing else.\n"
            f"Record your verdict with exactly one of:\n  {PROG} review verdict {digest} --approve --reviewer YOUR_NAME --notes \"what you checked\"\n"
            f"  {PROG} review verdict {digest} --reject --reviewer YOUR_NAME --notes \"why\"\n")


class _nolock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> Literal[False]:
        return False
