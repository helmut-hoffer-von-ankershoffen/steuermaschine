"""Pure decisions for `sm ci one` (unit-tested, no I/O): which run and jobs to re-run, which focused selection to
dispatch, and whether runner capacity allows it now.

Rules: never main or release/* without --force; never more than 5 focused runs (re-runs plus focused-job
dispatches) in flight; respect the account's job ceiling and its concurrent Apple-silicon (macOS ARM) runners.

Beside a release candidate (an active release/* run), since 2026-11-14: a run that needs no macOS runner may start,
as long as all jobs together stay within the job ceiling; a run that needs macOS waits. Hosted machines are isolated
from each other, so a Linux run takes nothing from the candidate but a place under the ceiling; the macOS runners are
the scarce part, and the candidate gets them. The two numbers are the account's, not a repository's: `Caps`, filled
from the settings `ci_job_cap` and `ci_macos_cap` (`dispatch.DispatchSettings`).
"""
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from steuermaschine import window

from ..queue_lib import core_facts

MAX_FOCUSED = 5
MAX_REPEAT = 60  # rounds in one focused job (core focused_job.py MAX_REPEAT)
MAX_RERUN_REPEAT = 10  # sequential re-runs of one CI job
DEFAULT_REPEAT = 10
JOB_CEILING = 20  # the defaults of `Caps`; the account's real numbers come from the settings
ARM_LIMIT = 4


@dataclass(frozen=True)
class Caps:
    """What the GitHub account runs at once: all jobs together, and the macOS/ARM jobs among them."""

    jobs: int = JOB_CEILING
    macos: int = ARM_LIMIT
ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")
WAITING = frozenset(ACTIVE) - {"in_progress"}
LANES = ("cli", "supervised", "web-1", "web-2")
OSES = ("macos-latest", "ubuntu-24.04")
WORKFLOW = "focused-job.yml"
# Checks that are not a re-runnable CI job (code scanning results, Sonar, gates): fix those from their findings.
OTHER_CHECKS = ("CodeQL", "SonarCloud", "review", "attempt")


def not_jobs() -> tuple[str, ...]:
    """Checks that are not jobs of the CI run: these and the merge-gate workflow's (Core's release policy)."""
    return OTHER_CHECKS + tuple(core_facts.API_CHECKS)
# Mirrors scripts/focused_job.py in core, so a bad selector is refused here instead of after a runner was taken.
TOKEN = re.compile(r"\w[\w./]{0,199}(::\w{1,120}(\[[\w.,=+-]{1,80}\])?){0,2}", re.ASCII)
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


def protected(branch):
    return branch == "main" or (branch or "").startswith("release/")


def branch_refusal(branch, force=False):
    if protected(branch) and not force:
        return f"refusing: {branch} is main or a release candidate (pass --force only when you mean it)"
    return ""


# Branch prefixes that never become a PR head: a focused run may stay attached to such a tip. rehearsal/* are
# the rehearsal's lanes: one throwaway commit on main's tip shared by all of its branches, dispatched through this tool.
THROWAWAY_PREFIXES = ("validation/", "bisect/", "rehearsal/")
SHA = re.compile(r"[0-9a-f]{40}")


def sha_refusal(sha, branch, open_prs, plan_keys, tip_branches):
    """Why a focused run must not start on `sha`: reasons, empty when the SHA is used by nothing else.

    GitHub attaches `merge-gate` and `coverage-floor` by SHA to the OLDEST github-actions check suite of that commit.
    A dispatch on a SHA that is, or becomes, a PR head makes that suite the dispatch's, and the PR stays BLOCKED with
    every check green (core #790, three hours on 2026-11-12). open_prs: (number, head sha); plan_keys: keys of the
    focused plan registry; tip_branches: remote branches whose tip is `sha` (the dispatched branch always counts).
    The rule itself is the core's (`steuermaschine.window.sha_uses`)."""
    return window.sha_uses(sha, branch, open_prs, plan_keys, tip_branches, THROWAWAY_PREFIXES,
                           reserved_text="the focused plan registry holds {key} (an admitted or pre-open PR head)")


def sha_refusal_text(sha, branch, reasons):
    return (f"refusing: {branch} @ {sha[:8]} is or will be a PR head: " + "; ".join(reasons) + ".\n"
            "merge-gate and coverage-floor attach by SHA to the oldest github-actions check suite; a dispatch here "
            "would take that place and leave the PR BLOCKED with green checks.\n"
            f"Run it on a commit of its own: sm ci one dispatch {branch} --throwaway ... [--wait --cleanup]")


def throwaway_branch(branch, sha):
    """validation/<name>-<sha8>: the source branch flattened, bound to the source tip it validates."""
    return window.throwaway_branch(branch, sha)


def throwaway_refusal(throwaway, cleanup, wait, reviewed_head):
    if cleanup and not (throwaway and wait):
        return "--cleanup deletes the validation branch after the result: it needs --throwaway and --wait"
    if throwaway and reviewed_head is not None:
        return "--throwaway creates a new commit; --reviewed-head binds an existing one: use one of them"
    return ""


def pick_run(runs, head_sha=None, workflow="CI"):
    """Newest run of `workflow` for the head (gh run list json: databaseId, headSha, workflowName, createdAt)."""
    cands = [r for r in runs if r.get("workflowName") == workflow and (head_sha is None or r.get("headSha") == head_sha)]
    return max(cands, key=lambda r: r.get("createdAt", ""), default=None)


def failed_jobs(jobs):
    return [j for j in jobs if j.get("conclusion") in ("failure", "timed_out", "cancelled") and j.get("name") != core_facts.aggregate()]


def match_job(jobs, name):
    """Exact name first, else a unique case-insensitive substring. -> (job, error)."""
    exact = [j for j in jobs if j.get("name") == name]
    if exact:
        return exact[0], ""
    sub = [j for j in jobs if name.lower() in (j.get("name") or "").lower()]
    if len(sub) == 1:
        return sub[0], ""
    if not sub:
        return None, f"no job named like {name!r}; failing: " + "; ".join(j["name"] for j in failed_jobs(jobs))
    return None, f"{name!r} matches {len(sub)} jobs: " + "; ".join(j["name"] for j in sub[:6])


def is_arm(labels: Iterable[str] | None) -> bool:
    return any("macos" in label or label.endswith("-arm") for label in labels or [])


def capacity_refusal(active_runs: Collection[Mapping[str, Any]], active_jobs: Collection[Mapping[str, Any]], focused_inflight: int, wants_arm: bool, *,
                     new_jobs: int = 1, new_arm_jobs: int | None = None, caps: Caps | None = None) -> str:
    """active_runs: run identity, status and headBranch; active_jobs: run_id, status and labels;
    focused_inflight: focused runs still active; caps: the account's `Caps` (none: the defaults).

    Beside an active release/* run a run that needs macOS is refused, and --force does not lift it: the candidate
    gets the macOS runners. A run that needs none may start while all jobs together, the requested ones included,
    stay within the job ceiling; the ceiling is strict then, whether or not main waits.

    Otherwise the job ceiling and the macOS/ARM runners are respected by priority, not by never starting: PR CI keeps
    the ceiling saturated most of the day, and one focused job queues behind it like any PR job. So a saturated ceiling
    (or ARM pool) refuses only while a main CI run or one of its jobs is waiting; otherwise the job is queued and
    capacity_note() says how many are ahead."""
    caps = caps or Caps()
    arm_wanted = int(wants_arm) if new_arm_jobs is None else new_arm_jobs
    candidate = any(protected_release(r) for r in active_runs if r.get("status") in ACTIVE)
    if candidate and arm_wanted:
        return "refusing: a release/* run is active; a run that needs macOS would compete with the candidate for the macOS runners"
    if candidate and len(active_jobs) + new_jobs > caps.jobs:
        return (f"refusing: a release/* run is active and {len(active_jobs)} active + {new_jobs} requested jobs exceed the {caps.jobs}-job ceiling; "
                "the candidate first")
    if focused_inflight >= MAX_FOCUSED:
        return f"refusing: {focused_inflight} focused runs already in flight (max {MAX_FOCUSED}); `sm ci one status`"
    main_runs = [run for run in active_runs if run.get("headBranch") == "main" and run.get("status") in ACTIVE]
    main_ids = {run["id"] for run in main_runs if "id" in run}
    main_queued = any(run["status"] in WAITING for run in main_runs) or any(
        job.get("run_id") in main_ids and job.get("status") in WAITING for job in active_jobs
    )
    if main_queued and len(active_jobs) + new_jobs > caps.jobs:
        return f"refusing: {len(active_jobs)} active + {new_jobs} requested exceed the {caps.jobs}-job ceiling; main first"
    arm = sum(1 for j in active_jobs if is_arm(j.get("labels")))
    if main_queued and arm_wanted and arm + arm_wanted > caps.macos:
        return f"refusing: {arm} active + {arm_wanted} requested macOS/ARM jobs exceed {caps.macos}; main first"
    return ""


def capacity_note(active_jobs: Collection[Mapping[str, Any]], wants_arm: bool, caps: Caps | None = None) -> str:
    """One line on how full the runners are, for a job that will queue."""
    caps = caps or Caps()
    arm = sum(1 for j in active_jobs if is_arm(j.get("labels")))
    if len(active_jobs) >= caps.jobs or (wants_arm and arm >= caps.macos):
        return f"queued behind {len(active_jobs)} jobs ({arm} macOS/ARM; ceiling {caps.jobs}, ARM {caps.macos}); no main CI waiting"
    return ""


def protected_release(run: Mapping[str, Any]) -> bool:
    return (run.get("headBranch") or "").startswith("release/")


def selection(suite=None, tests=(), kind=None):
    """-> (kind, selector, error) for focused-job.yml."""
    tests = [t for t in tests if t]
    if suite and tests:
        return None, None, "give --suite or --test, not both"
    if suite:
        if suite not in LANES:
            return None, None, f"--suite must be one of {', '.join(LANES)}"
        if kind not in (None, "e2e"):
            return None, None, "--suite is an e2e lane"
        return "e2e", suite, ""
    if not tests:
        return None, None, "give --suite LANE or --test ID"
    if len(tests) > 10:
        return None, None, "at most 10 --test ids"
    for t in tests:
        if not TOKEN.fullmatch(t) or ".." in t:
            return None, None, f"{t!r} is not a test id (path[::name[param]])"
    if kind is None:
        path = tests[0].split("::", 1)[0]
        kind = "e2e" if path.startswith("tests/e2e/") else "pytest" if path.startswith("tests/") else "vitest"
    if kind == "e2e" and not all(t.startswith("tests/e2e/") for t in tests):
        return None, None, "e2e ids must be under tests/e2e/"
    if kind == "pytest" and not all(t.startswith("tests/") for t in tests):
        return None, None, "pytest ids must be under tests/"
    return kind, " ".join(tests), ""


def verify_hint(target, job_names):
    """The one line the tick digest and the fail log print for a failing PR or run."""
    names = [n for n in job_names if n and n not in (core_facts.aggregate(), "run") and not n.startswith(not_jobs())]
    if not names:
        return f"verify with: sm ci one rerun {target} --wait   (hosted-only failure; do not push-and-wait for full CI)"
    return f'verify with: sm ci one rerun {target} --job "{names[0]}" --wait   (hosted-only failure; do not push-and-wait for full CI)'


def summarize(jobs, names=None):
    """Per-job result lines for the jobs that were re-run (or all non-skipped ones)."""
    rows = [j for j in jobs if (names is None or j.get("name") in names) and j.get("conclusion") != "skipped"]
    if names is None and len(rows) > 6:  # a whole run: the non-green jobs, and a count of the rest
        ok = [j for j in rows if j.get("conclusion") == "success"]
        rows = [j for j in rows if j.get("conclusion") != "success"]
        return [f"{(j.get('conclusion') or j.get('status') or '?').upper():9} {j.get('name')}" for j in rows] + [f"SUCCESS   {len(ok)} other jobs"]
    return [f"{(j.get('conclusion') or j.get('status') or '?').upper():9} {j.get('name')}" for j in rows]


def parse_rate(text):
    """'1/3', '0.33', '33%' -> 0.33; None or '' -> None."""
    if not text:
        return None
    text = str(text).strip()
    if text.endswith("%"):
        return float(text[:-1]) / 100
    if "/" in text:
        a, b = text.split("/", 1)
        return float(a) / float(b)
    return float(text)


def rounds_for(fail_rate, cap=MAX_REPEAT):
    """Rule of three: with N green rounds, a fail rate p is excluded at ~95 % when N >= 3/p. Capped per job."""
    import math
    if not fail_rate or fail_rate <= 0:
        return DEFAULT_REPEAT
    return max(1, min(cap, math.ceil(3 / min(fail_rate, 1))))


def repeat_refusal(repeat, suite, selector, *, reviewed_head=None, branch=None, kind=None):
    if not 1 <= repeat <= MAX_REPEAT:
        return f"--repeat must be 1-{MAX_REPEAT} (one job; use `sm ci one repeats --fail-rate P`)"
    if repeat > 1 and suite:
        return "--repeat takes --test ids, not a lane"
    exact_repeat = repeat > 1 and "[" in (selector or "")
    if not exact_repeat:
        return "--reviewed-head is only for a parametrized --test with --repeat" if reviewed_head is not None else ""
    if not reviewed_head or not re.fullmatch(r"[0-9a-f]{40}", reviewed_head):
        return "a parametrized repeat requires --reviewed-head with the full 40-character commit SHA"
    if not branch or not re.fullmatch(r"validation/[A-Za-z0-9][A-Za-z0-9._-]*", branch):
        return "a parametrized repeat requires a unique validation/* branch"
    if (kind not in ("pytest", "e2e") or not TOKEN.fullmatch(selector)
            or selector.count("[") != 1 or not selector.endswith("]")
            or not selector.split("::", 1)[0].endswith(".py")):
        return "a parametrized repeat takes one complete pytest or e2e test id ending in [param]"
    return ""


def parse_tally(log):
    m = re.search(r"FOCUSED TALLY (\d+)/(\d+) green[^\n]*", log or "")
    return m.group(0).removeprefix("FOCUSED TALLY ").strip() if m else ""


def tally_line(results):
    green = sum(1 for r in results if r == "success")
    return f"{green}/{len(results)} green"


def status_line(row):
    state = row.get("tally") or row.get("result") or row.get("status") or "in flight"
    return f"{row['run']} {row['kind']} {row['what'][:90]} [{state}] (since {row['at'][11:16]})"
