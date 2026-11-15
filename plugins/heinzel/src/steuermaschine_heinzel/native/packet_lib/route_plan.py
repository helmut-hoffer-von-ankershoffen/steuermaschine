"""`sm head plan`: the route one head will take through admission, read before anything is built.

Read-only and local: Git objects of the Core repository and the base's own scripts (its selector runs isolated, as
for a packet). Every fact is the base's at that commit: the tier from its selector, the protected sources from its
`protected_blobs` or PROTECTED_FILES, the limits from the integer constants its focused_plan.py and focused_job.py
define. This module holds no copy of a Core value; a limit Core does not define is not a limit here.

A route is OPEN, TAKEN (the one `sm head admit` follows) or CLOSED; a closed route says why and names the sanctioned
alternative. A head with no route to take is refused (exit 1) at the start, not at the last step.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from typing import Any

from steuermaschine.models import Model

from ..release_admit_lib import release_scope
from . import focused_manifest as fm

POLICY_FILES = ("scripts/focused_plan.py", "scripts/focused_job.py")
IDENTITY = (("scripts/focused_plan.py", "PROTECTED_FILES"), ("scripts/focused_plan.py", "PLAN_VARIABLE"),
            ("scripts/release_candidate.py", "REPOSITORY"))  # what a reviewed producer change must leave as it is (focused_admit.validate_canonical)
TAKEN, OPEN, CLOSED = "TAKEN", "OPEN", "CLOSED"


class Limit(Model):
    """One limit that applies to this head, with the measured value when the plan can measure it."""

    name: str
    source: str  # where the value is read from, at the base commit
    value: int
    measured: int | None = None
    what: str = ""
    binds: str = "unknown"  # yes | no | unknown (not measurable before the packet exists)


class Route(Model):
    """One way a head can reach main."""

    name: str
    state: str  # TAKEN | OPEN | CLOSED
    why: str
    alternative: str = ""  # a closed route: the sanctioned way instead
    command: str = ""


class RoutePlan(Model):
    """The route of one head at one base."""

    worktree: str = ""
    branch: str = ""
    head: str
    base: str
    tier: str  # focused | merge | full | none (nothing committed yet)
    tier_why: str = ""
    changed: int = 0
    production: int = 0
    selection: dict[str, int] = {}  # test ids per lane
    hosted_lanes: list[str] = []
    protected: list[str] = []
    limits: list[Limit] = []
    routes: list[Route] = []
    review_stops: list[str] = []
    lines: list[str] = []

    @property
    def route(self) -> str:
        return next((r.name for r in self.routes if r.state == TAKEN), "")

    @property
    def exit_code(self) -> int:
        return 0 if self.route or not self.routes else 1  # no routes at all: nothing is committed yet

    def render(self) -> list[str]:
        return list(self.lines)


def _number(node: ast.expr) -> int | None:
    """An integer literal, or sums and products of them (48 * 1024); nothing else is evaluated."""
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Add, ast.Sub)):
        left, right = _number(node.left), _number(node.right)
        if left is None or right is None:
            return None
        return left * right if isinstance(node.op, ast.Mult) else left + right if isinstance(node.op, ast.Add) else left - right
    return None


def core_limits(read: Callable[[str], bytes | None]) -> list[tuple[str, str, int]]:
    """(name, file, value) of every module-level integer constant with MAX in its name in the base's policy files."""
    found = []
    for path in POLICY_FILES:
        source = read(path)
        if source is None:
            continue
        try:
            assigned = fm._assignments(source)
        except (SyntaxError, ValueError):
            continue
        for name, node in assigned.items():
            value = _number(node)
            if "MAX" in name.split("_") and name.isupper() and value is not None:
                found.append((name, path, value))
    return found


def measure(name: str, facts: dict[str, Any]) -> tuple[int | None, str]:
    """What a Core limit of this name counts, measured on this head; (None, why) when the plan cannot know it yet.

    The words are Core's naming convention; the values never come from here."""
    words = set(name.split("_"))
    ids: dict[str, int] = facts["ids"]
    if "IDS" in words:
        lanes = [lane for lane in ids if lane.upper() in words] or list(ids)
        lane = max(lanes, key=lambda item: ids[item], default="")
        return (ids[lane], f"test ids in lane {lane}") if lane else (0, "test ids (none selected)")
    if "DIFF" in words:
        return facts["diff_bytes"], "bytes of the raw diff"
    if words & {"FILES", "PRODUCTION", "COVERED"}:
        return facts["production"], "changed production files"
    if words & {"PLAN", "MANIFEST", "SLOT", "REGISTRY"}:
        return facts["plan_bytes"], "bytes of the plan, at least (case digests come with the packet)"
    return None, "not measurable before the packet exists"


def limits_of(read: Callable[[str], bytes | None], facts: dict[str, Any], planned: bool = True) -> list[Limit]:
    """Core's limits on a focused plan, and the tool's own bound on the diff. PLANNED False: the route builds no plan
    (merge tier, nothing protected), so Core's plan limits are listed and do not apply."""
    out = []
    for name, path, value in core_limits(read):
        measured, what = measure(name, facts)
        lower_bound = "at least" in what
        binds = "unknown" if measured is None else "yes" if measured > value else "unknown" if lower_bound and measured * 2 > value else "no"
        if not planned:
            binds, what = "no", f"{what}; this route builds no plan, the limit is not applied"
        out.append(Limit(name=name, source=f"{path} at the base", value=value, measured=measured, what=what, binds=binds))
    # the tool's own bound on what it reads (not a Core fact): a diff past it is refused when the packet is built
    out.append(Limit(name="raw diff the packet builder reads", source="sm (focused_manifest.MAX_DIFF)", value=fm.MAX_DIFF,
                     measured=facts["diff_bytes"], what="bytes of the raw diff", binds="yes" if facts["diff_bytes"] > fm.MAX_DIFF else "no"))
    return out


def _literal(source: bytes | None, name: str) -> str | None:
    """The source text of the one module-level assignment to NAME (compared, never evaluated); None when absent."""
    if source is None:
        return None
    try:
        node = fm._assignments(source).get(name)
    except (SyntaxError, ValueError):
        return None
    return None if node is None else ast.dump(node)


def identity_changed(read_base: Callable[[str], bytes | None], read_tree: Callable[[str], bytes | None]) -> list[str]:
    """The identity constants the resulting tree changes: prepare refuses a producer change that touches one."""
    return [name for path, name in IDENTITY if _literal(read_base(path), name) != _literal(read_tree(path), name)]


def pins_changed(repo: Any, base: str, tree: str, policy: Any, pins: Callable[..., dict[str, str] | None]) -> list[str]:
    """The protected sources the resulting tree changes or deletes, as the packet builder computes them."""
    pinned = pins(repo, base, tree)
    if pinned is not None:
        execution, earlier = pinned, pins(repo, base, base) or {}
    else:
        execution = {p: repo.blob(tree, p) for p in policy.protected_paths(repo, tree)}
        earlier = {p: repo.blob(base, p) for p in policy.protected_paths(repo, base)}
    return sorted({p for p in execution if execution[p] != earlier.get(p)} | (set(earlier) - set(execution)))


def _reader(repo: Any, treeish: str) -> Callable[[str], bytes | None]:
    def read(path: str) -> bytes | None:
        done = repo.git("cat-file", "blob", f"{treeish}:{path}", check=False)
        return done.stdout if done.returncode == 0 else None
    return read


def release_route(changed: dict[str, str], worktree: str) -> Route:
    """The release path carries the generated release commit only (release_scope.SCOPE, the tool's reviewed validator)."""
    outside = sorted(set(changed) - release_scope.SCOPE)
    if outside:
        return Route(name="release path", state=CLOSED,
                     why=f"a release PR carries the generated release commit only; {len(outside)} changed path(s) are outside it "
                         f"({', '.join(outside[:3])}{' ...' if len(outside) > 3 else ''})",
                     alternative=f"ship the change as a head before the cut: sm ship {worktree}")
    return Route(name="release path", state=OPEN, why="every changed path is one the generated release commit touches; a release is cut, not admitted",
                 command="sm release cut VERSION")


def plan(repo: Any, head_rev: str, base_rev: str, *, worktree: str = "", branch: str = "", merge_tier_mode: str = "auto",
         select: Callable[..., dict[str, Any]] = fm.repo_selection, pins: Callable[..., dict[str, str] | None] = fm.base_protected_blobs) -> RoutePlan:
    """The route plan of HEAD merged onto BASE. Raises fm.ManifestError when the base's policy cannot be read."""
    from .. import merge_tier

    base, head = repo.commit(base_rev), repo.commit(head_rev)
    where = worktree or "WORKTREE"
    got = RoutePlan(worktree=worktree, branch=branch, head=head, base=base, tier="none")
    policy = fm.Policy(repo, base)
    merged = repo.git("merge-tree", "--write-tree", "--name-only", "--no-messages", base, head, check=False)
    names = merged.stdout.decode().splitlines()
    if merged.returncode or not names:
        files = ", ".join(sorted(set(names[1:]))[:5]) or "the base"
        got.tier, got.tier_why = "none", "the head does not merge onto the base"
        got.routes = [Route(name="focused admit", state=CLOSED, why=f"the head conflicts with the base in {files}: no plan binds an unmerged tree",
                            alternative=f"git -C {where} merge origin/main (never a force-push), resolve, commit, then plan again")]
        return _rendered(got)
    tree = names[0].strip()
    raw = repo.raw_diff(base, tree)
    if not raw:
        got.tier_why = "nothing is committed on this head yet: plan again after the first commit"
        return _rendered(got)
    changed = fm.changed_paths(raw, policy) if len(raw) <= fm.MAX_DIFF else {}
    read_base = _reader(repo, base)
    if not changed:  # a diff the packet builder refuses to read: only the tool's own bound is known
        got.tier, got.tier_why = "full", f"the raw diff is {len(raw)} bytes, more than the packet builder reads"
        got.limits = limits_of(read_base, {"ids": {}, "diff_bytes": len(raw), "production": 0, "plan_bytes": 0})
        got.routes = [Route(name="focused admit", state=CLOSED, why=got.tier_why, alternative="split the head into parts that each merge on their own")]
        return _rendered(got)
    advisory = select(repo, base, tree, changed)
    derived, refusals = fm.derive_selection(advisory, vitest_all=policy.vitest_all)
    plan_source = read_base(merge_tier.PLAN_PATH)
    tiered = merge_tier.active(merge_tier_mode, lambda: (plan_source or b"").decode("utf-8", "replace"))
    got.protected = pins_changed(repo, base, tree, policy, pins)
    got.changed = len(changed)
    production = sorted(p for p, status in changed.items() if fm.production_file(p) and status != "D")
    got.production = len(production)
    selection = derived or {}
    got.selection = {lane: len(ids) for lane, ids in selection.items()}
    got.hosted_lanes = sorted(str(lane) for lane in advisory.get("e2e_lanes") or [])
    got.tier = "full" if derived is None else "merge" if tiered else "focused"
    got.tier_why = ("; ".join(refusals[:3]) if derived is None else
                    "the base's Core has the merge tier: CI selects the lanes itself" if tiered else
                    "the base's Core has no merge tier: a reviewed plan names the tests")
    estimate = len(json.dumps({"changed": changed, "production": production, "selection": selection, "execution": got.protected},
                              sort_keys=True, separators=(",", ":")))
    got.limits = limits_of(read_base, {"ids": got.selection, "diff_bytes": len(raw), "production": got.production, "plan_bytes": estimate},
                           planned=not tiered or bool(got.protected))
    got.routes = _routes(got, changed, tiered, refusals, identity_changed(read_base, _reader(repo, tree)), where)
    got.review_stops = _stops(got, tiered)
    return _rendered(got)


def _routes(got: RoutePlan, changed: dict[str, str], tiered: bool, refusals: list[str], identity: list[str], where: str) -> list[Route]:
    admit = f"sm ship {where}   (or: sm head admit {where})"
    binding = [limit for limit in got.limits if limit.binds == "yes"]
    routes = []
    if got.protected:
        routes.append(Route(name="focused admit", state=CLOSED,
                            why=f"the head changes protected focused-CI sources ({', '.join(got.protected[:4])}{' ...' if len(got.protected) > 4 else ''})",
                            alternative="the protected-producer route below; `sm head admit` takes it by itself"))
        if identity:
            routes.append(Route(name="protected-producer route", state=CLOSED,
                                why=f"the head also changes {', '.join(identity)}: prepare refuses a producer change that alters the source identity "
                                    "or the protected file inventory",
                                alternative="none in sm: take the change of these constants out of this head and ask the coordinator how it lands"))
        elif binding:
            routes.append(_bound("protected-producer route", binding))
        else:
            routes.append(Route(name="protected-producer route", state=TAKEN, command=admit,
                                why="a reviewed plan is required again; the packet review names the changed sources, prepare checks the PR plan and "
                                    "the paired main plan with the base's resolver, and prepare.out gets its own verdict"))
    elif binding:
        routes.append(_bound("focused admit", binding))
    elif tiered:
        routes.append(Route(name="focused admit", state=TAKEN, command=admit,
                            why="merge tier: no plan, no packet, no manifest review" + ("; CI runs the FULL set" if got.tier == "full" else "")))
        routes.append(Route(name="protected-producer route", state=OPEN, why="not needed: no protected focused-CI source changes"))
    elif got.tier == "full":
        routes.append(Route(name="focused admit", state=CLOSED,
                            why="the base's selector asks for the full set (" + "; ".join(refusals[:2]) + "), and a focused plan names its tests",
                            alternative=f"a declared, narrower selection the packet reviewer approves: sm ship {where} --set sel=SEL.json "
                                        "(it must cover what the head changes; coverage of the changed files is measured by sm head preflight)"))
    else:
        routes.append(Route(name="focused admit", state=TAKEN, command=admit, why="a reviewed plan names the selected tests"))
        routes.append(Route(name="protected-producer route", state=OPEN, why="not needed: no protected focused-CI source changes"))
    routes.append(release_route(changed, where))
    return routes


def _bound(name: str, binding: list[Limit]) -> Route:
    text = "; ".join(f"{limit.name} = {limit.value} ({limit.source}), this head has {limit.measured} {limit.what}" for limit in binding)
    return Route(name=name, state=CLOSED, why=f"a limit binds: {text}",
                 alternative="split the head so each part is under the limit, or land the Core change that lifts the limit first")


def _stops(got: RoutePlan, tiered: bool) -> list[str]:
    stops = []
    if got.route == "protected-producer route":
        stops += ["packet review: an independent verdict on protected-producer.json (the packet and the changed sources)",
                  "apply review: a separate independent verdict on prepare.out (sm head hash)"]
    elif got.route == "focused admit" and not tiered:
        stops += ["packet review: an independent verdict on packet.json", "apply review: an independent verdict on prepare.out (sm head hash)"]
    if got.route:
        if got.hosted_lanes:
            stops.append(f"before admission: hosted validation of {', '.join(got.hosted_lanes)} on a throwaway commit (sm head validate), when the coordinator queues it")
        stops.append("at the pr step: the diff guard may ask for one more independent review (sm head guard PR)")
    return stops


def _rendered(got: RoutePlan) -> RoutePlan:
    lines = [f"PLAN     {got.branch or 'head'} {got.head[:12]} onto {got.base[:12]}",
             f"TIER     {got.tier}" + (f": {got.tier_why}" if got.tier_why else "")]
    if got.changed:
        ids = ", ".join(f"{lane} {count}" for lane, count in sorted(got.selection.items())) or "none"
        lines.append(f"SIZE     {got.changed} changed file(s), {got.production} production; test ids: {ids}"
                     + (f"; hosted lanes: {', '.join(got.hosted_lanes)}" if got.hosted_lanes else ""))
        lines.append("PROTECTED " + (", ".join(got.protected) if got.protected else "none changed"))
    for limit in got.limits:
        measured = "not measured" if limit.measured is None else f"{limit.measured} {limit.what}"
        lines.append(f"LIMIT    {limit.name} = {limit.value} ({limit.source}): {measured}; binds: {limit.binds}")
    if got.changed:
        lines.append("LIMIT    not read here (they need the packet or the network): coverage of the changed files (sm head preflight), "
                     "the fill of the plan slots (bare sm)")
    for route in got.routes:
        lines.append(f"ROUTE    {route.name}: {route.state}. {route.why}" + (f" -> {route.command}" if route.command else ""))
        if route.alternative:
            lines.append(f"         instead: {route.alternative}")
    for stop in got.review_stops:
        lines.append(f"STOP     {stop}")
    if got.routes and not got.route:
        lines.append("REFUSED  no route is open for this head as it is: see `instead` above")
    got.lines = lines
    return got
