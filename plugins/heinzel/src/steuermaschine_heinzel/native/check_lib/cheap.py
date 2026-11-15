"""The checks of `sm check`: the cheap ones, for exactly what one Core head changes, before any review or push.

Nothing here knows which test belongs to which path. Three sources at the commit say it:
  the selector   Core's scripts/affected.py, as `sm head select` reads it: the tests of the changed paths (the docs
                 style test for a document or an ADR, the tests that import a changed module, the Vitest files of a
                 changed web file), or "the full set"
  the hook       Core's scripts/scoped_checks.py: which changed files are web files or documents (its own predicates),
                 the steps it runs for them (biome, the generated client, tsc, Vitest; the template, the generated
                 documents, typos, the Markdown tests), its early guards (the contract tests of a changed path) and its
                 type-check commands
  the standards  Core's scripts/preflight_standards.txt, else the setting `preflight_standards`

Checks, in the order they run (every one runs, so one pass names every finding):
  tree        clean tree, head ahead of the base: the receipt is bound to the commit, so the commit must be what ran
  ruff        format and lint of the changed Python files
  types       the hook's type checks, when the change is not web, desktop, plugins or documents only
  hook        Core's hook answered for the changed paths; if it did not, its steps and guards are unknown and the run fails
  web         the hook's web steps when web files changed: lint, generated client, types, the selected Vitest files
  docs        the hook's document steps when documents or the site's tooling changed
  standards   the repository-wide standards tests
  tests       the hook's early guards and the selector's tests of the changed paths
  coverage    when src/ changed: scoped coverage of the whole changed files by the selector's tests (they run once, here)
  bound       HEAD and the tree are still what the checks began with
"""
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from steuermaschine.preflight import FAIL, SKIP, Preflight, Step

from ..preflight_lib import head

STEPS = ("tree", "ruff", "types", "hook", "web", "docs", "standards", "tests", "coverage", "bound")
NO_SLOT = "wait for load to drop and run sm check again; do not bypass the run lock"
# Runs in the worktree's own interpreter: Core's hook answers for the changed paths; this code only asks.
PROBE = """
import importlib.util, json, sys
from pathlib import Path
wt, ask = Path(sys.argv[1]), json.loads(sys.argv[2])
try:
    spec = importlib.util.spec_from_file_location("core_scoped_checks", wt / "scripts" / "scoped_checks.py")
    hook = importlib.util.module_from_spec(spec)
    # Always inspect the current hook bytes, never a timestamp/size-matched pyc.
    exec(compile(Path(spec.origin).read_bytes(), spec.origin, "exec"), hook.__dict__)
    changed, picked, note = ask["changed"], ask["vitest"], ""
    docs = [p for p in changed if hook.is_document(p) or hook.is_docs_tooling(p)]
    web = [p for p in changed if p not in docs and hook.scoped_steps([p]) is not None]
    web_steps = []
    if web and picked is None:
        web_steps = hook.scoped_steps(web)
    elif web and not picked:
        web_steps = [s for s in hook.scoped_steps(web) if s != hook.web_test_step(None)]
    elif web:
        try:
            web_steps = hook.scoped_steps(web, web_test_files=" ".join(picked))
        except ValueError as why:
            web_steps, note = hook.scoped_steps(web), f"the hook does not take the selected Vitest files ({why}): every Vitest file ran"
    found = {"web": web_steps, "docs": hook.scoped_steps(docs) if docs else [], "guards": hook.early_guard_commands(changed),
             "web_files": web, "doc_files": docs, "note": note}
except Exception as why:
    found = {"error": f"{type(why).__name__}: {why}"}
print(json.dumps(found))
"""


@dataclass
class Plan:
    """What the head changes and what the three sources select for it."""

    changed: list[str] = field(default_factory=list)
    selector: str = ""  # core: the checkout's scripts/affected.py; port: the moved copy
    full: bool = False  # the selector cannot narrow this change
    pytest: list[str] | None = None  # the tests of the changed paths; None: the whole suite (nothing narrower is known)
    narrowed: str = ""  # how a full selection was narrowed, or why the whole unit suite runs
    error: str = ""  # the selection could not be made (Core's import graph failed): the run fails, nothing is guessed
    web: list[tuple[str, list[str]]] = field(default_factory=list)  # (step name, argv) of the hook's web steps
    docs: list[tuple[str, list[str]]] = field(default_factory=list)
    guards: list[list[str]] = field(default_factory=list)
    web_files: list[str] = field(default_factory=list)
    docs_files: list[str] = field(default_factory=list)
    hook: str = ""  # why the hook's steps could not be read; "": they were
    note: str = ""
    extra: list[str] = field(default_factory=list)  # tests the caller added (--sel)
    lanes: list[str] = field(default_factory=list)  # the e2e lanes the selector names: never run here


@dataclass
class Ctx(head.Ctx):
    plan: Plan = field(default_factory=Plan)
    dirty: str = ""  # `git status --porcelain` as the tree check saw it


def steps_of(lines: list[str]) -> list[tuple[str, list[str]]]:
    """The hook's step lines (`"&name=command"`: & marks a step that may run beside the one before) as (name, argv)."""
    out = []
    for line in lines:
        name, _, command = str(line).lstrip("&").partition("=")
        if command.strip():
            out.append((name, shlex.split(command)))
    return out


def hook_facts(c: head.Ctx, changed: list[str], vitest: list[str] | None) -> tuple[dict[str, Any], str]:
    """What Core's own hook says about the changed paths -> (facts, why they could not be read)."""
    if not (c.wt / head.HOOK_SCRIPT).is_file():
        return {}, f"Core has no {head.HOOK_SCRIPT}"
    ask = json.dumps({"changed": changed, "vitest": vitest})
    rc, out = c.sh(["sh", "-c", 'exec "$@" 2>/dev/null', "sh", "uv", "run", "--quiet", "python", "-c", PROBE, str(c.wt), ask], cwd=c.wt)
    lines = [line for line in out.splitlines() if line.strip()]
    try:
        found = json.loads(lines[-1]) if rc == 0 and lines else None
    except ValueError:
        found = None
    if not isinstance(found, dict):
        return {}, f"{head.HOOK_SCRIPT} could not be asked (exit {rc})"
    if found.get("error"):
        return {}, f"{head.HOOK_SCRIPT} does not answer: {found['error']}"
    return found, ""


def plan(c: Ctx, selection: dict[str, Any], extra: list[str]) -> Plan:
    """Bind the selector's answer and the hook's answer for the head's changed paths."""
    changed = c.changed()
    full = bool(selection.get("full")) or selection.get("pytest") == ["ALL"]
    p = Plan(changed=changed, selector=str(selection.get("selector", "")), full=full, extra=list(extra), lanes=list(selection.get("e2e_lanes") or []))
    if not full:
        p.pytest = list(selection.get("pytest") or [])
    else:
        narrow(c, p)
    vitest = selection.get("vitest") or []
    facts, p.hook = hook_facts(c, changed, None if vitest == ["ALL"] else list(vitest))
    p.web, p.docs = steps_of(facts.get("web") or []), steps_of(facts.get("docs") or [])
    p.guards = [[str(word) for word in command] for command in facts.get("guards") or []]
    p.web_files, p.docs_files, p.note = list(facts.get("web_files") or []), list(facts.get("doc_files") or []), str(facts.get("note") or "")
    return p


def narrow(c: Ctx, p: Plan) -> None:
    """The selector says "everything". Core's own import graph (the push hook's selection) may still name the unit
    tests of the change; when it says ALL too, the whole unit suite runs; when it fails, the run fails."""
    mappable = [f for f in p.changed if not f.startswith(head.WEB_INPUTS)]
    if not mappable:
        p.pytest, p.narrowed = [], "the selector selects the full set; only web inputs changed, so the web check runs every Vitest file and no unit test"
        return
    if not (c.wt / head.RELATED_SCRIPT).is_file():
        p.narrowed = f"the selector selects the full set and Core has no {head.RELATED_SCRIPT} to narrow it: the whole unit suite runs"
        return
    picked, why = head.hook_selection(c, mappable)
    if why:
        p.pytest, p.error = [], why
    elif picked is None:
        p.narrowed = f"the selector and {head.RELATED_SCRIPT} both select everything: the whole unit suite runs"
    else:
        p.pytest, p.narrowed = picked, f"the selector selects the full set; {head.RELATED_SCRIPT} narrows the unit tests to {len(picked)} file(s)"


def production(c: Ctx) -> list[str]:
    """The Python files under src/ the head changes and keeps: what scoped coverage measures."""
    return [f for f in c.changed("src", filter_="d") if f.endswith(".py")]


def selected_tests(c: Ctx) -> list[str]:
    """The selector's tests plus the caller's, without the ones a hook step already runs by name."""
    named = {word for _, argv in [*c.plan.web, *c.plan.docs] for word in argv} | {word for argv in c.plan.guards for word in argv}
    return [t for t in dict.fromkeys([*(c.plan.pytest or []), *c.plan.extra]) if t not in named and (c.wt / t).exists()]


# ---- steps ---------------------------------------------------------------------------------------------------------

def step_tree(c: Ctx) -> Step:
    s = Step("tree")
    dirty = c.dirty = c.git("status", "--porcelain")[1].strip()
    if dirty:
        s.fail(f"working tree is not clean ({len(dirty.splitlines())} paths, first: {dirty.splitlines()[0].strip()})",
               "commit the change (or remove the stray files): the receipt is bound to the commit, so the commit must be what was checked")
    ahead = c.git("rev-list", "--count", f"{head.BASE}..HEAD")[1].strip()
    if not ahead.isdigit() or ahead == "0":
        s.fail("the head has no commit of its own over the base", "commit the change on the branch; there is nothing to check")
    s.detail = f"{ahead or '?'} commit(s), {len(c.plan.changed)} changed path(s)"
    return s


def step_ruff(c: Ctx) -> Step:
    s = Step("ruff")
    files = [f for f in c.changed(filter_="ACMR") if f.endswith(".py") and (c.wt / f).is_file()]
    if not files:
        s.detail = "no Python file changed"
        return s
    for name, cmd in (("format", ["uv", "run", "ruff", "format", "--check", "--force-exclude", "--", *files]),
                      ("lint", ["uv", "run", "ruff", "check", "--no-fix", "--force-exclude", "--", *files])):
        rc, out = c.sh(cmd, cwd=c.wt)
        if rc != 0:
            s.fail(f"ruff {name} failed on the changed files (exit {rc})\n{head.tail(out)}", "`make format` fixes the format and what ruff can fix; fix the rest in the source")
    s.detail = f"{len(files)} changed Python file(s): format and lint clean" if s.status != FAIL else ""
    return s


def run_steps(c: Ctx, s: Step, steps: list[tuple[str, list[str]]], fix: str) -> None:
    for name, argv in steps:
        rc, out = c.gate(argv, cwd=c.wt)
        if rc == 75:
            s.fail(f"the run lock gave no slot for {name} (exit 75 = WAIT)", NO_SLOT)
            return
        if rc != 0:
            log = c.log(name, out)
            s.fail(f"{name} failed (exit {rc}): `{shlex.join(argv)}`{'; log ' + log if log else ''}\n{head.tail(out)}", fix)


def step_hook(c: Ctx) -> Step:
    """Core's hook must answer: it alone knows the web steps, the document steps and the early guards of the changed
    paths. A hook that is missing, fails or says something else leaves them unknown, and unknown is not green."""
    s = Step("hook")
    if c.plan.hook:
        return s.fail(f"{c.plan.hook}: the web steps, the document steps and the early guards of this head are unknown",
                      f"run `uv run python {head.HOOK_SCRIPT} --help` in the worktree and fix the cause; without the hook's answer no receipt is written")
    s.detail = f"{head.HOOK_SCRIPT} answered: {len(c.plan.web_files)} web file(s), {len(c.plan.docs_files)} document(s), {len(c.plan.guards)} early guard command(s)"
    return s


def hook_step(name: str, files: str, fix: str) -> Any:
    """A check that runs the hook's own steps for one kind of changed file (`web`, `docs`); the hook says which files those are."""

    def step(c: Ctx) -> Step:
        s = Step(name)
        steps, touched = getattr(c.plan, name), getattr(c.plan, f"{name}_files")
        if c.plan.hook:
            return Step(name, SKIP, "not run: the hook did not answer (see the hook check)")
        if not steps:
            s.detail = f"no {files} changed"
            return s
        run_steps(c, s, steps, fix)
        if name == "web" and c.plan.note:
            s.warn(c.plan.note)
        s.detail = f"{len(touched)} changed {files}: {', '.join(n for n, _ in steps)} green" if s.status != FAIL else ""
        return s

    return step


def step_tests(c: Ctx) -> Step:
    s = Step("tests")
    for argv in c.plan.guards:
        run_steps(c, s, [("the hook's early guard", argv)], "fix the code, never the test: the push hook runs these guards before anything else")
    if c.plan.error:
        s.fail(c.plan.error, "run it by hand in the worktree and fix the cause; the tests of a change are never guessed")
    whole = c.plan.pytest is None
    measured = bool(production(c)) and not whole
    files = [] if measured else selected_tests(c)  # with src/ changed they run once, under coverage
    if whole:  # nothing narrows the change: the unit suite runs, as the push hook runs it; it is never skipped
        rc, out = c.gate(head.UNIT_SUITE, cwd=c.wt)
        if rc != 0:
            outcome = "got no slot (exit 75 = WAIT)" if rc == 75 else f"was refused or failed (exit {rc})"
            s.fail(f"{c.plan.narrowed}; it {outcome}\n{head.tail(out)}",
                   "fix the failing tests; when the run lock refuses the whole suite on this machine, the coordinator runs `sm check WORKTREE --allow-full`")
    if c.plan.narrowed:
        s.warn(c.plan.narrowed + ("" if whole else "; integration tests of the change run on CI"))
    if files:
        head.run_tests(c, s, "selected", files)
    if c.plan.lanes:
        s.warn(f"e2e lanes the selector names are not run here: {', '.join(c.plan.lanes)}")
    s.data.update(tests=files, guards=len(c.plan.guards), selector=c.plan.selector, unit_suite=whole)
    where = "they run under coverage" if measured else f"{len(files)} selected test file(s)"
    s.detail = f"{len(c.plan.guards)} early guard command(s), {'the whole unit suite, ' if whole else ''}{where}" if s.status != FAIL else ""
    return s


def step_coverage(c: Ctx) -> Step:
    files = production(c)
    if not files:
        return head.step_coverage(c)
    tests = selected_tests(c) if c.plan.pytest is not None else [t for t in c.plan.extra if (c.wt / t).exists()]
    if not tests:
        return Step("coverage").fail(f"{len(files)} file(s) under src/ changed and no test is selected for them ({c.plan.error or c.plan.narrowed or 'the selector names none'})",
                                     'write the tests of the changed files, or name the covering ones: --sel FILE with {"python": [test files]}')
    c.sel = {"python": tests}
    c.sel_bytes, c.sel_path = json.dumps(c.sel).encode(), Path("selector")
    s = head.step_coverage(c)
    s.data["tests"] = tests
    return s


def step_bound(c: Ctx) -> Step:
    s = Step("bound")
    now, dirty = c.git("rev-parse", "HEAD")[1].strip(), c.git("status", "--porcelain")[1].strip()
    if now != c.sha:
        s.fail(f"HEAD moved while the checks ran ({c.sha[:9]} -> {now[:9]})", "run sm check again on the new head")
    elif dirty != c.dirty:
        s.fail("the tree changed while the checks ran", "commit or remove the change, then run sm check again")
    s.detail = f"HEAD is still {c.sha[:9]}, the tree is {'as it was' if dirty else 'clean'}" if s.status != FAIL else ""
    return s


def checks() -> Preflight:
    table = {"tree": step_tree, "ruff": step_ruff, "types": head.step_types, "hook": step_hook,
             "web": hook_step("web", "web file(s)", "fix the reported lint or type errors; a stale generated client: regenerate it as the step's message says"),
             "docs": hook_step("docs", "document(s)", "fix the document or regenerate the generated file the step names"),
             "standards": head.step_standards, "tests": step_tests, "coverage": step_coverage, "bound": step_bound}
    registry = Preflight()
    for name in STEPS:
        registry.add(name, table[name])
    return registry
