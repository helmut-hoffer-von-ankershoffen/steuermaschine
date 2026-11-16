#!/usr/bin/env python3
"""hz-wave-size [--core DIR] [--base REV] [--json] [PATH:]BRANCH...: size one focused-admission wave, read-only.

Each argument is a Core branch, optionally `WORKTREE:BRANCH` (default Core: $HZ_CORE or ~/Code/heinzel; base
default origin/main, never fetched). Per branch and for the union it prints the changed test ids by lane (python /
web / browser ...), production files, protected files (focused_plan PROTECTED_FILES), pairwise file overlaps and merge
conflicts against the base and against each other. Selection and limits come from the base's own affected.py and
focused_plan/focused_job via hz_focused_manifest.py (nothing from the candidate runs).

Exit 1 (REFUSED) when any merge conflicts (no lane has an id maximum and production files have no bound: owner
2026-11-14, PRs of any size); the output names the
branch to move out of the wave. The union selection is the union of the branches' selections. When the base's
focused_job.py has VITEST_ALL, a web lane with Vitest "ALL" is the whole suite
`web/ALL`, which counts as one id (web=1/10 (whole suite)); without it such a lane stays over the limit.
Exit 0 fits, 2 usage."""
import sys
from itertools import combinations
from pathlib import Path

# (moved: the package is imported, no path insert)
try:  # not yet tracked on every branch; tests substitute a fake
    from . import focused_manifest as fm
except ImportError:
    fm = None


def facts_for(repo, base, branch, policy):
    """Changed files, selection and counts of one branch merged into base (merge-tree; a conflict is reported)."""
    head = repo.commit(branch)
    done = repo.git("merge-tree", "--write-tree", "--name-only", "--no-messages", base, head, check=False)
    lines = done.stdout.decode().splitlines()
    if done.returncode or not lines:
        return {"branch": branch, "head": head, "conflict": ", ".join(sorted(set(lines[1:]))[:5]) or "base", "changed": {}, "selection": {}, "production": [], "protected": []}
    tree = lines[0].strip()
    changed = fm.changed_paths(repo.raw_diff(base, tree), policy)
    result = fm.repo_selection(repo, base, tree, changed)
    selection, reasons = fm.derive_selection(result, vitest_all=getattr(policy, "vitest_all", None))
    if selection is None:  # full/broad selection: still report counts, but it can never fit
        selection = {"python": sorted(result["pytest"]), "web": sorted(result["vitest"])}
        selection = {k: v for k, v in selection.items() if v}
    return {"branch": branch, "head": head, "conflict": "", "changed": changed, "selection": selection, "reasons": reasons,
            "production": sorted(p for p in changed if fm.production_file(p)),
            "protected": sorted(p for p in changed if p in policy.protected)}


def pair_conflicts(repo, facts):
    """Branch pairs whose merge-tree conflicts (read-only; merge-tree writes only objects)."""
    out = []
    for a, b in combinations(facts, 2):
        if a["conflict"] or b["conflict"]:
            continue
        if repo.git("merge-tree", "--write-tree", "--no-messages", a["head"], b["head"], check=False).returncode:
            out.append((a["branch"], b["branch"]))
    return out


def limits_of(policy):
    return {"python": None, "default": None, "production": None,
            "vitest_all": getattr(policy, "vitest_all", None)}


def lane_limit(lane, limits):
    """The id limit of a lane: None, no lane has a maximum (kept for the report shape)."""
    return limits["python"] if lane == "python" else limits["default"]


def over_limit(n, limit):
    return limit is not None and n > limit


def union_counts(facts, limits=None):
    """Union ids per lane; with the base's VITEST_ALL a web lane holding it is that one id."""
    lanes, prod = {}, set()
    for f in facts:
        for lane, ids in f["selection"].items():
            lanes.setdefault(lane, set()).update(ids)
        prod.update(f["production"])
    sentinel = (limits or {}).get("vitest_all")
    web = lanes.get("web")
    if sentinel and web and sentinel in web:
        lanes["web"] = {sentinel}
    return lanes, prod


def overlaps(facts):
    return {(a["branch"], b["branch"]): sorted(set(a["changed"]) & set(b["changed"])) for a, b in combinations(facts, 2)
            if set(a["changed"]) & set(b["changed"])}


def to_move(facts, limits):
    """Branch whose removal leaves the least excess over the limits (ties: the last in the wave)."""
    best, score = None, None
    for f in facts:
        lanes, prod = union_counts([g for g in facts if g is not f], limits)
        excess = sum(max(len(ids) - lane_limit(lane, limits), 0) for lane, ids in lanes.items() if lane_limit(lane, limits) is not None)
        if score is None or excess <= score:
            best, score = f["branch"], excess
    return best


def evaluate(facts, limits):
    """(problems, over, lanes, prod) for a composition; problems is a list of strings, empty means it fits."""
    lanes, prod = union_counts(facts, limits)
    over = {}
    problems = []
    for lane, ids in sorted(lanes.items()):
        if over_limit(len(ids), lane_limit(lane, limits)):
            over[lane] = (len(ids), lane_limit(lane, limits))
    for dim, (n, limit) in over.items():
        problems.append(f"union {dim}: {n} > {limit}")
    for f in facts:
        if f["conflict"]:
            problems.append(f"{f['branch']}: conflicts with the base ({f['conflict']})")
        for reason in f.get("reasons") or []:
            problems.append(f"{f['branch']}: {reason}")
    return problems, over, lanes, prod


def whole(ids, limits):
    return " (whole suite)" if limits.get("vitest_all") and list(ids) == [limits["vitest_all"]] else ""


def report(facts, pairs, limits):
    lines = []
    for f in facts:
        counts = " ".join(f"{lane}={len(ids)}{whole(ids, limits)}" for lane, ids in sorted(f["selection"].items())) or "none"
        lines.append(f"{f['branch']} {f['head'][:10]}: files={len(f['changed'])} test ids: {counts}; production={len(f['production'])}; "
                     f"protected={len(f['protected'])}{' ' + ','.join(f['protected']) if f['protected'] else ''}")
    lanes, prod = union_counts(facts, limits)
    lines.append("union: " + (" ".join(f"{lane}={len(ids)}{'' if lane_limit(lane, limits) is None else '/' + str(lane_limit(lane, limits))}{whole(ids, limits)}" for lane, ids in sorted(lanes.items())) or "no test ids")
                 + f"; production={len(prod)}")
    for (a, b), files in overlaps(facts).items():
        lines.append(f"overlap {a} x {b}: {len(files)} file(s): {', '.join(files[:5])}{' ...' if len(files) > 5 else ''}")
    for a, b in pairs:
        lines.append(f"CONFLICT {a} x {b}")
    return lines


def run(specs, core, base, emit=print, gather=facts_for, conflicts=pair_conflicts):
    if fm is None:
        raise RuntimeError("hz_focused_manifest.py is missing next to this tool")
    repo_path = Path(core).expanduser()
    repos = []
    for spec in specs:
        path, _, branch = spec.rpartition(":")
        repos.append((Path(path).expanduser() if path else repo_path, branch or spec))
    repo = fm.Repo(repos[0][0])
    base = repo.commit(base)
    policy = fm.Policy(repo, base)
    limits = limits_of(policy)
    facts = [gather(fm.Repo(p), base, b, policy) for p, b in repos]
    pairs = conflicts(repo, facts)
    problems, over, _, _ = evaluate(facts, limits)
    problems += [f"conflict {a} x {b}" for a, b in pairs]
    for line in report(facts, pairs, limits):
        emit(line)
    if not problems:
        emit("FITS")
        return 0
    emit("REFUSED: " + "; ".join(problems))
    move = to_move(facts, limits) if over else (pairs[0][1] if pairs else None)
    if move:
        emit(f"MOVE {move} to the next wave")
    return 1

