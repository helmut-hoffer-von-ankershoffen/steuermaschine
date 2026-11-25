"""Shared by the hz-* tools (imported, never run): gh calls, tool loading, PR keys, flakes, notes."""

import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path
from . import edge  # moved: records, processes and what used to be separate scripts
from . import ruleset
from ... import state as statedir  # the plugin's state paths

TOOLS = Path(__file__).resolve().parent
OWNER = "helmut-hoffer-von-ankershoffen"
CORE = f"{OWNER}/heinzel"
PLUGINS = f"{OWNER}/heinzel-plugins"
SHORT = {"": CORE, "core": CORE, "plugins": PLUGINS, "acme": f"{OWNER}/heinzel-acme", "sf": f"{OWNER}/heinzel-sf"}
RUN_URL = re.compile(r"/runs/(\d+)")


class _GateChecks:
    """The merge-gate workflow's checks (Core's release policy at main), read on use: hzlib is imported by core_facts."""

    def _names(self) -> tuple[str, ...]:
        from . import core_facts

        return core_facts.api_checks()

    def __iter__(self) -> Iterator[str]:
        return iter(self._names())

    def __contains__(self, name: object) -> bool:
        return name in self._names()


GATE_CHECKS = _GateChecks()


def _aggregate() -> str:
    from . import core_facts

    return core_facts.aggregate()


def _ci_file() -> str:
    from . import core_facts

    return core_facts.ci_file()


ADVISORY_REVIEW = set(ruleset.ADVISORY_REVIEW)  # advisory only where no ruleset requires checks (queue_lib/ruleset.py)


def release_holds() -> Path:
    """Each release owns a unique hold name; no release deletes a shared incident sentinel."""
    return statedir.path("release-holds")


def release_holds_present() -> bool:
    """Unknown, unreadable or malformed hold directories block merging just like an owned hold."""
    root = release_holds()
    try:
        mode = root.lstat().st_mode
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if not stat.S_ISDIR(mode):
        return True
    try:
        return any(root.iterdir())
    except OSError:
        return True


def incident_frozen(legacy: Path | None = None) -> bool:
    """Honor the shared sentinel, including dangling links and unreadable state."""
    try:
        (legacy or statedir.path("merge-freeze")).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def merge_frozen(legacy: Path | None = None) -> bool:
    """Honor the shared incident hold and every release-owned hold."""
    return incident_frozen(legacy) or release_holds_present()


def core_owned_release_hold() -> bool:
    """The sole hold belongs to the active Core receipt. Anything uncertain stays global."""
    from .release_candidate import unique_object

    root, receipt = release_holds(), statedir.path("release.json")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        with os.fdopen(os.open(receipt, flags), "rb") as stream:
            recorded = os.fstat(stream.fileno())
            if not stat.S_ISREG(recorded.st_mode) or recorded.st_size > 64 * 1024:
                return False
            data = json.loads(stream.read(64 * 1024 + 1), object_pairs_hook=unique_object)
        token, version = data["freeze_token"], data["version"]
        if (not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token)
                or not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
                or data.get("finished", False) is not False):
            return False
        directory = os.open(root, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
        try:
            if os.listdir(directory) != [token]:
                return False
            with os.fdopen(os.open(token, flags, dir_fd=directory), "rb") as stream:
                hold = os.fstat(stream.fileno())
                if not stat.S_ISREG(hold.st_mode) or stream.read(len(version) + 1) != version.encode("ascii"):
                    return False
            return (os.listdir(directory) == [token]
                    and os.path.samestat(hold, os.stat(token, dir_fd=directory, follow_symlinks=False))
                    and os.path.samestat(os.fstat(directory), root.lstat())
                    and os.path.samestat(recorded, receipt.lstat()))
        finally:
            os.close(directory)
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return False


RULES_TTL = 120.0  # seconds: one queue tick reads each repository's rules once
_RULES: dict[str, tuple[float, frozenset[str]]] = {}


def required_checks(repo: str) -> frozenset[str] | None:
    """The contexts the ruleset of main requires in REPO, from GitHub (cached for RULES_TTL); None when unreadable."""
    hit = _RULES.get(repo)
    if hit and time.monotonic() - hit[0] < RULES_TTL:
        return hit[1]
    found = ruleset.required_from(ghj("api", f"repos/{repo}/rules/branches/main"))
    if found is not None:
        _RULES[repo] = (time.monotonic(), found)
    return found


def advisory_checks(repo: str) -> ruleset.Advisory:
    """The checks that do not hold a PR of REPO: only `review` (and what a caller waives by name). A red check holds,
    required or not; the ruleset (queue_lib/ruleset.py) only adds that a required check must have reported."""
    return ruleset.Advisory(required_checks(repo))


def missing_required(advisory: object, rollup: list[dict]) -> list[str]:
    """Required contexts not reported at all in ROLLUP (GitHub holds such a PR); [] without a ruleset."""
    return advisory.missing(rollup) if isinstance(advisory, ruleset.Advisory) else []


def required_names(advisory: object) -> list[str]:
    return advisory.names() if isinstance(advisory, ruleset.Advisory) else []


def latest_checks(rollup: list[dict]) -> list[dict]:
    """The newest run of each check name. GitHub counts the latest run, so an older cancelled run (a gate cancelled by
    the concurrency group, then rerun green) must not hold a PR in these tools. Entries without a time keep their order."""
    newest: dict[str, tuple[str, int, dict]] = {}
    for index, check in enumerate(rollup):
        name = check.get("name") or check.get("context") or f"?{index}"
        stamp = check.get("completedAt") or check.get("startedAt") or ""
        if name not in newest or (stamp, index) >= newest[name][:2]:
            newest[name] = (stamp, index, check)
    return [entry[2] for entry in sorted(newest.values(), key=lambda e: e[1])]


def only_never_started_macos_jobs_failed(repo: str, run_id: int) -> bool:
    """True when a red CI run is red only because macOS cells were never given a runner (no runner, no steps, cancelled:
    GitHub's macOS capacity) and the aggregate, which counts them. Nothing ran, so nothing was hidden (policy 2026-11-02)."""
    jobs = ghj("api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100", "--jq", "[.jobs[]|{name, conclusion, runner: .runner_name, steps: (.steps|length)}]")
    if not isinstance(jobs, list) or not jobs:
        return False
    bad = [j for j in jobs if j.get("conclusion") not in ("success", "skipped")]
    others = [j for j in bad if j.get("name") != _aggregate()]
    return bool(others) and all(
        "macos" in (j.get("name") or "").lower() and j.get("conclusion") == "cancelled" and not j.get("runner") and not j.get("steps") for j in others
    )


def non_blocking(name: str | None, advisory: "set[str] | ruleset.Advisory") -> bool:
    """A check that doesn't hold a merge: advisory, or macOS until the full suite has 3 green runs (owner, 2026-11-01); still shown."""
    lowered = (name or "").lower()
    if lowered.startswith("focused "):  # hz-ci-one dispatches (focused-job.yml) attach to the head; a diagnostic, never a gate
        return True
    return name in advisory or ("macos" in lowered and "macos-specific" not in lowered and "notifier" not in lowered)


def tool_required_checks(repo: str) -> set[str]:
    """Checks only these tools can demand: a private repo gets no GitHub rules without Pro. Demanded once main has the job,
    so PRs opened before it don't stall. Doesn't stop a hand `gh pr merge`; hz-merge-gate says so."""
    if repo != f"{OWNER}/heinzel-sf":
        return set()
    from . import core_facts

    code, out = gh("api", f"repos/{repo}/contents/{core_facts.ci_path()}", "-H", "Accept: application/vnd.github.raw+json")
    return {name for name in core_facts.api_checks() if f"{name}:" in out} if code == 0 else set()


def _gh_process(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    return edge.run(["gh", *args], capture_output=True, text=True, check=False, timeout=timeout)


GH_READ_TIMEOUT = 120  # seconds; a stalled gh must not hold the queue or release lock while launchd sees a live process
GH_WRITE_TIMEOUT = 300  # a merge or another write may take longer on a busy forge, never unbounded
GH_TIMED_OUT = 124  # the exit code a timed-out gh call reports, as `timeout(1)` does


def gh_timeout(args: tuple[str, ...]) -> int:
    """The bound for one gh call: reads 120 s, writes (merge, create, edit, rerun, cancel, POST/PUT/PATCH/DELETE) 300 s."""
    writes = {("pr", "merge"), ("pr", "create"), ("pr", "edit"), ("pr", "update-branch"), ("run", "rerun"), ("run", "cancel"),
              ("workflow", "run"), ("release", "create"), ("release", "edit")}
    return GH_WRITE_TIMEOUT if tuple(args[:2]) in writes or gh_api_method(args).upper() != "GET" else GH_READ_TIMEOUT


GH_BODY_FLAGS = ("-f", "-F", "--field", "--raw-field", "--input")


def gh_api_method(args: tuple[str, ...]) -> str:
    """The HTTP method gh sends: -X/--method when given; else `gh api` with a field or an input body sends POST."""
    for i, a in enumerate(args):
        if a in ("-X", "--method") and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--method="):
            return a.split("=", 1)[1]
        if a.startswith("-X") and len(a) > 2:
            return a[2:]
    body = any(a in GH_BODY_FLAGS or a.startswith(("--field=", "--raw-field=", "--input=")) for a in args)
    return "POST" if args[:1] == ("api",) and body else "GET"


def gh(*args: str) -> tuple[int, str]:
    """One bounded gh call: (exit code, stdout). A call past its bound is refused as exit 124 with a line on stderr."""
    limit = gh_timeout(args)
    try:
        done = _gh_process(*args, timeout=limit)
    except subprocess.TimeoutExpired:
        print(f"refused: gh {' '.join(args[:3])} timed out after {limit}s; nothing is assumed from it", file=sys.stderr, flush=True)
        return GH_TIMED_OUT, ""
    return done.returncode, done.stdout


def ghj(*args: str, default=None):
    code, out = gh(*args)
    try:
        return json.loads(out) if code == 0 else default
    except ValueError:
        return default


MERGE_TIER_MODE = "auto"  # the setting merge_tier_mode, bound by the queue's edge (sm queue ..., sm daemon queue)


def merge_tier() -> bool:
    """Core's merge tier (ADR 0149) is at main: reruns of red PR and main runs repair, a replaced main run is not red.
    Read from main's scripts/focused_plan.py unless the setting forces it; an unreadable main keeps the older rules."""
    from .. import merge_tier as tier

    def plan():
        data = ghj("api", f"repos/{CORE}/contents/{tier.PLAN_PATH}?ref=main", default=None)
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            return None
        import base64
        return base64.b64decode(str(data.get("content") or "")).decode("utf-8", errors="replace")

    return tier.active(str(MERGE_TIER_MODE or "auto"), plan)


def load(name: str):
    """Load a sibling tool script (no extension) as a module."""
    return edge.moved(name)  # moved: a fresh copy of the module the tool moved to; never a script


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def split_key(tokens: list[str] | str) -> tuple[str, str, str]:
    """`393`, `#393`, `plugins 38`, `plugins #38` -> (repo, number, key as pr-actions.toml writes it)."""
    words = tokens.split() if isinstance(tokens, str) else list(tokens)
    prefix = words[0] if len(words) > 1 else ""
    number = words[-1].lstrip("#")
    if prefix not in SHORT or not number.isdigit():
        raise ValueError(f"not a PR: {' '.join(words)}")
    return SHORT[prefix], number, f"{prefix} #{number}" if prefix else number


def failing_tests(repo: str, run_id: int) -> list[str]:
    """Failing test names of a finished run's latest attempt, taken from its log (cached by hz-flakes)."""
    info = ghj("run", "view", str(run_id), "-R", repo, "--json", "attempt", default={}) or {}
    data = load("flakes_read").read_attempt(repo, run_id, info.get("attempt", 1))
    return list(dict.fromkeys(data.get("tests", [])))


def failed_runs(rollup: list) -> list[int]:
    """Run ids behind the failed checks of a pull request."""
    ids = [int(m.group(1)) for c in rollup if c.get("conclusion") in ("FAILURE", "CANCELLED", "TIMED_OUT") and c.get("name") != "review" for m in [RUN_URL.search(c.get("detailsUrl") or "")] if m]
    return list(dict.fromkeys(ids))


def note(text: str) -> None:
    edge.note(text)  # moved: the same line in the same document, written here


def say(text: str) -> None:
    print(text, flush=True)


def fail(text: str) -> None:
    sys.exit(text)



def _failed_main_source(row):
    return row.get("status") == "completed" and isinstance(row.get("conclusion"), str) and row["conclusion"] in {
        "failure", "timed_out", "startup_failure", "cancelled", "action_required", "stale",
    }


def _admission_jobs(payload):
    if not isinstance(payload, dict) or type(payload.get("total_count")) is not int:
        raise ValueError("unknown admission job count")
    jobs = payload.get("jobs")
    conclusions = {"success", "skipped", "neutral", "failure", "cancelled", "timed_out", "startup_failure", "action_required", "stale"}
    if not isinstance(jobs, list) or not jobs or payload["total_count"] != len(jobs):
        raise ValueError("incomplete admission jobs")
    if any(not isinstance(j, dict) or not isinstance(j.get("name"), str) or not j["name"]
           or j.get("status") != "completed" or j.get("conclusion") not in conclusions for j in jobs):
        raise ValueError("malformed admission job")
    return jobs


_ADMISSION_READ_TIMEOUT = 15
_ADMISSION_HTTP_ERROR = re.compile(r"gh: (?:HTTP ([1-5][0-9]{2})(?:: [^\r\n]*)?|[^\r\n]+ \(HTTP ([1-5][0-9]{2})\))")
_ADMISSION_TRANSIENT = {"HTTP 502", "HTTP 503", "HTTP 504", "command timeout", "TLS handshake timeout",
                        "response headers timeout", "connection reset"}


def _admission_read_error(result, path):
    """Classify only known CLI errors. The raw response may contain credentials and is never emitted."""
    if result.returncode == 1:
        error = result.stderr.strip()
        if match := _ADMISSION_HTTP_ERROR.fullmatch(error):
            return f"HTTP {match[1] or match[2]}"
        prefix = f'Get "https://api.github.com/{path}": '
        if error.startswith(prefix):
            transport = error.removeprefix(prefix)
            if transport == "net/http: TLS handshake timeout":
                return "TLS handshake timeout"
            if transport == "net/http: timeout awaiting response headers":
                return "response headers timeout"
            if re.fullmatch(r"read tcp [^\r\n]+: read: connection reset by peer", transport):
                return "connection reset"
    return "unclassified CLI error"


def _restoration_admission_jobs(endpoint, run, attempt):
    if type(run) is not int or run < 1:
        raise ValueError("invalid restoration admission run ID")
    path = f"repos/{CORE}/{endpoint}"
    event = {"run": run, "attempt": attempt}
    print(json.dumps({"event": "queue.restoration_admission.start", **event}), file=sys.stderr, flush=True)
    first_failure = None
    jobs = None
    for request in (1, 2):
        try:
            result = _gh_process("api", "--hostname", "github.com", "--method", "GET", path,
                                 timeout=_ADMISSION_READ_TIMEOUT)
        except subprocess.TimeoutExpired:
            reason = "command timeout"
        except OSError:
            reason = "could not execute GitHub CLI"
        except UnicodeError:
            reason = "invalid response encoding"
        else:
            if result.returncode:
                reason = _admission_read_error(result, path)
            else:
                try:
                    payload = json.loads(result.stdout)
                except (ValueError, RecursionError):
                    reason = "invalid JSON"
                else:
                    try:
                        jobs = _admission_jobs(payload)
                        reason = None
                    except ValueError as error:
                        reason = str(error)  # fixed messages from the local validator
                    except TypeError:
                        reason = "malformed admission job"
        if request == 1 and reason in _ADMISSION_TRANSIENT:
            first_failure = reason
            continue
        break
    print(json.dumps({"event": "queue.restoration_admission.outcome", **event, "requests": request,
                      "outcome": "ok" if jobs is not None else "unavailable", "first_failure": first_failure,
                      "reason": reason}), file=sys.stderr, flush=True)
    if jobs is None:
        raise ValueError("restoration admission jobs unavailable")
    return jobs


def resolve_main_runs(rows, *, restoration_read=False):
    """Follow successful admissions only; opt in to diagnostic reads for queue descendant restoration."""
    from . import main_batch

    if not isinstance(rows, list):
        return rows
    if not all(isinstance(row, dict) for row in rows):
        return None
    resolved = []
    tier: list[bool] = []  # merge_tier(), read at most once and only for a delegated batch

    def reruns() -> bool:
        if not tier:
            tier.append(merge_tier())
        return tier[0]

    for row in rows:
        effective = row
        # A known failed/cancelled source is independent evidence, including a failed rerun.
        # Preserve it before consulting optional proof or potentially malformed metadata.
        if not _failed_main_source(row):
            try:
                title, attempt = row.get("displayTitle"), row.get("attempt")
                if not isinstance(title, str) or not title.strip() or type(attempt) is not int or attempt < 1:
                    raise ValueError("missing requested main-run metadata")
                if title.startswith(main_batch.PREFIX) and row.get("status") == "completed":
                    if row.get("conclusion") != "success":
                        raise ValueError("source admission did not succeed")

                    def get(endpoint):
                        return ghj("api", f"repos/{CORE}/{endpoint}", default={}) or {}

                    run = row["databaseId"]
                    endpoint = f"actions/runs/{run}/attempts/{attempt}/jobs?per_page=100"
                    jobs = (_restoration_admission_jobs(endpoint, run, attempt) if restoration_read
                            else _admission_jobs(get(endpoint)))
                    delegated = [j for j in jobs if j["name"].startswith(main_batch.DELEGATED)]
                    if len(delegated) > 1:
                        raise ValueError("ambiguous delegation jobs")
                    # Full CI legitimately skips the alternative delegation job. Its presence is not a delegation.
                    delegated = [j for j in delegated if j["conclusion"] != "skipped"]
                    if delegated:
                        # merge tier: a rerun may complete the final run; an older Core keeps the attempt-1 call unchanged
                        proof = (main_batch.proof_for(row["headSha"], get, reruns=True) if reruns()
                                 else main_batch.proof_for(row["headSha"], get))
                        if (attempt != 1 or len(delegated) != 1 or delegated[0]["conclusion"] != "success"
                                or any(j["conclusion"] not in {"success", "skipped", "neutral"} for j in jobs)
                                or not proof or delegated[0]["name"] != f"{main_batch.DELEGATED}{proof['run']}"
                                or proof["prs"][-1][1] == row["headSha"]):
                            raise ValueError("unproven delegation")
                        final = get(f"actions/runs/{proof['run']}")
                        if not (main_batch.valid_run(final, proof, reruns=True) if reruns() else main_batch.valid_run(final, proof)):
                            raise ValueError("final run changed after proof validation")
                        effective = {**row, "databaseId": final["id"], "headSha": final["head_sha"],
                                     "status": final["status"], "conclusion": final.get("conclusion"),
                                     "attempt": final["run_attempt"], "createdAt": final["created_at"],
                                     "displayTitle": final["display_title"], "event": final["event"],
                                     "headBranch": final["head_branch"], "batch": proof}
                    elif not any(j["name"] == _aggregate() and j["conclusion"] == "success" for j in jobs):
                        raise ValueError("admission is not full CI")
            except (ValueError, TypeError, KeyError, AttributeError):
                effective = {**row, "status": "waiting", "conclusion": None, "batch_unknown": True}
        existing = next((r for r in resolved if r.get("databaseId") == effective.get("databaseId")), None)
        if existing is None:
            resolved.append(effective)
        elif _failed_main_source(effective):
            existing.update(effective)
        elif effective.get("batch_unknown") and not _failed_main_source(existing):
            existing.update(effective)
        elif effective.get("batch") and not _failed_main_source(existing) and not existing.get("batch_unknown"):
            existing.update(effective)
    return resolved


# --- release train main gate (coordinator decision 2026-11-07 19:4x: the release candidate is the gate) -------------
HARD_JOB = ("failure", "timed_out", "startup_failure")
TRAIN_GATE_RUNS = 20


def _attempt_jobs(run_id: int, attempt: int) -> list[dict] | None:
    payload = ghj("api", f"repos/{CORE}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100", default=None)
    if not isinstance(payload, dict) or type(payload.get("total_count")) is not int:
        return None
    jobs = payload.get("jobs")
    if (not isinstance(jobs, list) or not jobs or payload["total_count"] != len(jobs)
            or any(not isinstance(j, dict) or not isinstance(j.get("name"), str) or j.get("status") != "completed" for j in jobs)):
        return None
    return jobs


FLAKY_NODE_ID = re.compile(r"[\w./\-]+\.py::[\w.]+(?:::[\w.]+)*(?:\[[^\]\s]*\])?")
FLAKY_DAYS = 7  # like known-failures.toml: a flaky bead counts for 7 days after its last update, unless it carries `until`


def _flaky_live(bead: dict, today) -> bool:
    from datetime import date, datetime, timedelta
    meta = bead.get("metadata") if isinstance(bead.get("metadata"), dict) else {}
    until = bead.get("until") or meta.get("until")
    if until:
        try:
            return today <= date.fromisoformat(str(until)[:10])
        except ValueError:
            return False
    try:
        updated = datetime.fromisoformat(str(bead.get("updated_at")).replace("Z", "+00:00")).date()
    except ValueError:
        return False  # no date at all: overdue, fail closed
    return today <= updated + timedelta(days=FLAKY_DAYS)


def flaky_node_ids() -> dict[str, str]:
    """Full test node ids (path.py::name[::name][param]) written in open, live beads labelled `flaky` -> bead id.
    Deterministic: `bd list -l flaky --json`. No title, bare function name or job name ever matches. Any read problem -> {}."""
    from datetime import datetime, timezone
    try:
        out = subprocess.run(["bd", "list", "-l", "flaky", "--json"], capture_output=True, text=True, timeout=30,
                             cwd=str(edge.records_dir()), check=False)
        beads = json.loads(out.stdout) if out.returncode == 0 else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    today = datetime.now(timezone.utc).date()
    found: dict[str, str] = {}
    for b in beads if isinstance(beads, list) else []:
        if (not isinstance(b, dict) or b.get("status") not in (None, "open", "in_progress") or not isinstance(b.get("id"), str)
                or not _flaky_live(b, today)):
            continue
        for text in (b.get("title"), b.get("description"), b.get("notes")):
            for node in FLAKY_NODE_ID.findall(str(text or "")):
                found.setdefault(node, b["id"].removeprefix("heinzel-management-"))
    return found


def flaky_bead(test: str, nodes: dict[str, str]) -> str | None:
    """Bead whose node id equals `test` exactly; a bead id without [param] also covers `test` with any [param]."""
    if test in nodes:
        return nodes[test]
    return nodes.get(test.split("[", 1)[0]) if test.endswith("]") and "[" in test else None


def _known_registry() -> list:
    from . import known_failures as hz_digest
    return hz_digest.load_known(str(edge.setting("queue_known_failures_file") or edge.records_dir() / "research" / "known-failures.toml"))


def classify_attempt(run_id: int, attempt: int) -> tuple[str, str]:
    """('none', why) no job hard-failed (cancelled or never started only: not evidence), ('known', tags) every hard-failed
    job is KNOWN in date, ('unknown', why) otherwise (a regression candidate, or evidence that cannot be read).

    A job is KNOWN by its name (research/known-failures.toml `match`, in date), or, when it is the ONLY hard-failed job,
    by its failing tests: the attempt's log names at least one failing test and every one of them is KNOWN. Two or more
    jobs not matched by name are UNKNOWN, since the log's tests cannot be attributed to one job (fail closed)."""
    from . import known_failures as hz_digest
    jobs = _attempt_jobs(run_id, attempt)
    if jobs is None:
        return "unknown", f"run {run_id} attempt {attempt}: job list unreadable or incomplete"
    hard = [j["name"] for j in jobs if j["name"] != _aggregate() and j.get("conclusion") in HARD_JOB]
    if not hard:
        return "none", f"run {run_id} attempt {attempt}: no job failed (cancelled or never started only)"
    known = _known_registry()
    by_name = {name: hz_digest.known_bead([name], known) for name in hard}
    if all(by_name.values()):
        return "known", ", ".join(f"{n}=KNOWN({b})" for n, b in by_name.items())
    if len(hard) == 1:
        try:
            tests = list(dict.fromkeys(load("flakes_read").read_attempt(CORE, run_id, attempt).get("tests", [])))
        except Exception:  # noqa: BLE001 - unreadable log is UNKNOWN, never KNOWN
            tests = []
        nodes = flaky_node_ids() if tests else {}
        beads = [hz_digest.known_bead([t], known) or flaky_bead(t, nodes) for t in tests]
        if tests and all(beads):
            return "known", f"{hard[0]}=KNOWN({', '.join(dict.fromkeys(beads))}) via {len(tests)} failing test(s)"
    unknown = [n for n, b in by_name.items() if not b]
    return "unknown", f"run {run_id} attempt {attempt}: UNKNOWN failing job(s) {', '.join(unknown)}"


def main_release_gate(tip: str = "") -> dict:
    """The release train's main precondition (TRAIN PATH ONLY; hz-release's default main_tip_green is unchanged).

    The release candidate (release/vX.Y.Z: full suite + acceptance on both OSes, first attempt) is the real gate, so the
    train does not wait for main's newest run and does not refuse a red main whose failures are all KNOWN. It reads
    main's newest COMPLETED run that is evidence (a run where no job failed, only cancelled or never-started jobs, is
    skipped), newest first within the last TRAIN_GATE_RUNS runs:
      first-attempt green                                   -> ok  (kind green)
      green on attempt N>1 with every attempt-1 failure KNOWN -> ok  (kind restored: restored-by-rerun)
      red with every hard-failed job KNOWN in date          -> ok  (kind known: red-by-KNOWN)
      red with an UNKNOWN job, a green rerun of an UNKNOWN failure, unreadable evidence, no evidence -> refuse
    Returns {verdict, kind, why, run, sha, tip_finished}."""
    from . import main_lanes

    rows = resolve_main_runs(ghj("run", "list", "-R", CORE, "--workflow", _ci_file(), "--branch", "main", "-L", str(TRAIN_GATE_RUNS),
                                 "--json", "status,conclusion,databaseId,headSha,displayTitle,attempt", default=None))
    result = {"verdict": "refuse", "kind": "unknown", "run": None, "sha": "", "tip_finished": False}
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) and type(r.get("databaseId")) is int for r in rows):
        return result | {"why": "main CI evidence is unreadable"}
    result["tip_finished"] = bool(tip) and any(r.get("headSha") == tip and r.get("status") == "completed" for r in rows)
    skipped = []
    for row in rows:
        if row.get("status") != "completed" or row.get("batch_unknown"):
            continue
        rid, sha, attempt, conclusion = row["databaseId"], row.get("headSha", ""), row.get("attempt"), row.get("conclusion")
        if main_lanes.is_macos_lane(row) and conclusion in ("success", "cancelled"):
            continue  # the dispatched macOS lane is no proof that main's push run is green; a cancelled one was replaced
        if type(attempt) is not int or attempt < 1:
            return result | {"why": f"run {rid}: attempt unreadable", "run": rid, "sha": sha}
        where = f"run {rid} at {sha[:9]}"
        base = result | {"run": rid, "sha": sha}
        tail = f" (skipped {len(skipped)} run(s) where nothing failed)" if skipped else ""
        if conclusion == "success" and attempt == 1:
            return base | {"verdict": "ok", "kind": "green", "why": f"{where} first-attempt green{tail}"}
        if conclusion == "success":
            kind, text = classify_attempt(rid, 1)
            if kind == "known":
                return base | {"verdict": "ok", "kind": "restored", "why": f"{where} restored-by-rerun (attempt {attempt} green; attempt 1: {text}){tail}"}
            if kind == "none":
                return base | {"verdict": "ok", "kind": "restored", "why": f"{where} green on attempt {attempt}; attempt 1 had no failed job{tail}"}
            return base | {"why": f"{where} green only on rerun of an UNKNOWN failure ({text}): a regression candidate{tail}"}
        kind, text = classify_attempt(rid, attempt)
        if kind == "none":
            skipped.append(rid)
            continue
        if kind == "known":
            return base | {"verdict": "ok", "kind": "known", "why": f"{where} red-by-KNOWN: {text}{tail}"}
        return base | {"why": f"{where} {conclusion}: {text}: a regression candidate{tail}"}
    return result | {"why": f"no completed main run with evidence in the last {len(rows)} runs"}
