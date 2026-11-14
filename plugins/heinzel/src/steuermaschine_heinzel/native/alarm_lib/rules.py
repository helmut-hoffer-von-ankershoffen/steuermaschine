"""Stall and failure alarms as pure rules: facts in, alarms out. One line per alarm, class first:
ALERT (exit code 3 until acknowledged), WARN (printed only), INFO (expected, printed last)."""

from __future__ import annotations

import datetime

REQUIRED = ("CI OK", "merge-gate", "coverage-floor")  # contexts main requires; the caller drops what the rules do not demand
GREEN = ("SUCCESS", "SKIPPED", "NEUTRAL")
STALL_MIN = 15
ADMISSION_MIN = 20
RELEASE_MIN = 30
SLOT_FREE_MIN = 4000
SLOT_NAMES = ("FOCUSED_CI_PLANS", *(f"FOCUSED_CI_PLANS_V2_{i}" for i in range(4)))
SLOT_LIMITS = {"FOCUSED_CI_PLANS": 48 * 1024, **{f"FOCUSED_CI_PLANS_V2_{i}": 40000 for i in range(4)}}
ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")
BAD_JOB = ("failure", "cancelled", "timed_out", "startup_failure")
CANDIDATE_HOURS = 12
QUEUE_GATES = ("merge freeze", "main's latest run is not green", "main CI evidence is unknown", "batch cadence", "release")
TRAP = "see RCA trap: checks attached to another suite"
REHEARSAL_OPEN_MIN = 240
SUSPECT_CAP = 8


def alarm(cls: str, key: str, text: str) -> dict:
    return {"cls": cls, "key": key, "text": " ".join(str(text).split())}


def stamp(value) -> float | None:
    """Epoch seconds of a GitHub ISO time; None when absent or unreadable."""
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def minutes(since: float | None, now: float) -> int:
    return max(int((now - since) / 60), 0) if since else 0


def check_name(check: dict) -> str:
    return check.get("name") or check.get("context") or "?"


def check_result(check: dict) -> str:
    """One word per rollup entry: check runs carry status/conclusion, commit statuses carry state."""
    if "state" in check and "status" not in check:
        return {"SUCCESS": "SUCCESS", "PENDING": "PENDING", "EXPECTED": "PENDING"}.get(check["state"], check["state"] or "PENDING")
    if check.get("status") != "COMPLETED":
        return "PENDING"
    return check.get("conclusion") or "PENDING"


def green(rollup: list[dict]) -> bool:
    """Every check complete and green; an empty rollup is not green."""
    return bool(rollup) and all(check_result(c) in GREEN for c in rollup)


def green_since(rollup: list[dict]) -> float | None:
    times = [t for t in (stamp(c.get("completedAt")) for c in rollup) if t]
    return max(times) if times else None


def required_missing(required, rollup: list[dict]) -> list[str]:
    """Required contexts with no entry at all in the PR rollup (posted to another check suite of the same SHA)."""
    present = {check_name(c) for c in rollup}
    return [name for name in required if name not in present]


def required_not_green(required, rollup: list[dict]) -> list[str]:
    results = {check_name(c): check_result(c) for c in rollup}
    return [f"{name}={results[name]}" for name in required if name in results and results[name] != "SUCCESS"]


def wait_reason(state: str | None, rollup: list[dict], required, since: float | None, now: float, everything: list[dict] | None = None) -> str:
    """Why GitHub will not merge a PR whose checks say nothing is wrong. '' when it is not that case."""
    if state != "BLOCKED" or not green(rollup):
        return ""
    everything = rollup if everything is None else everything
    head = f"BLOCKED with green checks for {minutes(since, now)} min: "
    if missing := required_missing(required, everything):
        return head + f"required check contexts missing from the PR rollup: {', '.join(missing)}; {TRAP}"
    if weak := required_not_green(required, everything):
        return head + f"required checks present but not SUCCESS: {', '.join(weak)}"
    return head + "every required context is green in the rollup; look at reviews, conversations and branch rules"


def stall_alarm(key: str, state: str | None, rollup: list[dict], required, since: float | None, now: float,
                queue_block: str = "", limit: int = STALL_MIN, everything: list[dict] | None = None) -> dict | None:
    """A queued PR whose checks are all complete and green but which is unmerged for more than `limit` minutes.
    ALERT unless the queue itself recorded a gate (freeze, red main, cadence): then WARN, the gate has its own alarm."""
    if not green(rollup):
        return None
    age = minutes(since, now)
    if age <= limit:
        return None
    shown = key if not key.isdigit() else f"#{key}"
    reason = wait_reason(state, rollup, required, since, now, everything)
    if reason:
        return alarm("ALERT", f"stall:{key}", f"STALL {shown} {reason}; sm queue why")
    if queue_block and any(gate in queue_block for gate in QUEUE_GATES):
        return alarm("WARN", f"stall:{key}", f"STALL {shown} green and unmerged for {age} min, queue waits: {queue_block}")
    return alarm("ALERT", f"stall:{key}", f"STALL {shown} checks complete and green, {state or 'state unknown'}, unmerged for {age} min"
                 f" and the queue records {'`' + queue_block + '`' if queue_block else 'no block'}; sm queue why")


ASK_LABELS = ("owner-ask", "heinzel-issues")  # bead labels that must never sit unseen: the owner's asks and the in-app reports


def ask_alarms(beads: list[dict]) -> list[dict]:
    """One ALERT per open bead that carries an ask label (`ASK_LABELS`); `sm alarm ack KEY` silences it once it was seen."""
    found = []
    for bead in beads:
        labels = [x for x in ASK_LABELS if x in (bead.get("labels") or [])]
        if bead.get("status", "open") != "open" or not labels or not bead.get("id"):
            continue
        found.append(alarm("ALERT", f"bead-ask:{bead['id']}", f"NEW {labels[0].upper()} bead {bead['id']}: {str(bead.get('title') or '')[:90]}; sm beads tasks show {bead['id']}"))
    return found


def bad_jobs(jobs: list[dict]) -> list[str]:
    names = [j.get("name") or "?" for j in jobs if (j.get("conclusion") or "").lower() in BAD_JOB]
    return [n for n in dict.fromkeys(names) if n != "CI OK"] or list(dict.fromkeys(names))


def named(names: list[str], cap: int = 6) -> str:
    return ", ".join(names[:cap]) + (f" (+{len(names) - cap})" if len(names) > cap else "")


def main_red_alarm(run: dict | None, jobs: list[dict]) -> dict | None:
    """Main's latest completed CI run failed: name the jobs."""
    if not run or (run.get("conclusion") or "").lower() not in ("failure", "timed_out", "startup_failure"):
        return None
    failed = bad_jobs(jobs)
    rid = run.get("databaseId")
    return alarm("ALERT", f"main-red:{rid}", f"MAIN RED run {rid} at {str(run.get('headSha'))[:8]}: "
                 f"{named(failed) if failed else 'failed jobs unreadable'}; sm ci faillog {rid}")


def candidate_alarms(runs: list[dict], jobs_of, now: float, hours: int = CANDIDATE_HOURS, dropped=lambda branch: False) -> list[dict]:
    """Release candidate runs (release/v* branch, newest attempt of each workflow, last `hours`) with a failed or
    cancelled job, also while the run is still going. A candidate whose release PR is closed and whose branch is
    deleted was dropped (`dropped(branch)` is true): no alarm."""
    newest: dict[tuple, dict] = {}
    for run in runs:
        branch = run.get("headBranch") or ""
        created = stamp(run.get("createdAt"))
        if not branch.startswith("release/v") or not created or now - created > hours * 3600:
            continue
        slot = (branch, run.get("workflowName"))
        if slot not in newest or created > stamp(newest[slot].get("createdAt")):
            newest[slot] = run
    out = []
    for (branch, workflow), run in sorted(newest.items(), key=lambda kv: str(kv[0])):
        if run.get("status") == "completed" and (run.get("conclusion") or "").lower() in ("success", "skipped", "neutral"):
            continue
        failed = bad_jobs(jobs_of(run.get("databaseId")) or [])
        if not failed or (run.get("status") == "completed" and dropped(branch)):
            continue
        rid = run.get("databaseId")
        going = "" if run.get("status") == "completed" else " (run still in progress)"
        out.append(alarm("ALERT", f"candidate:{rid}:{len(failed)}", f"CANDIDATE {branch} {workflow} run {rid}{going}: "
                         f"{len(failed)} failed or cancelled job(s): {named(failed)}; sm ci faillog {rid}"))
    return out


def admission_alarms(runs: list[dict], covered_heads: set[str], now: float, limit: int = ADMISSION_MIN, waiting: bool = False) -> list[dict]:
    """Focused admission needs a window with no active run it cannot account for: an active run whose head has no
    admission receipt and that is older than `limit` minutes. WARN only while an admission is `waiting` (a prepare
    or apply is running); otherwise an INFO line, because an ordinary main push CI run blocks nobody."""
    rows = []
    for run in runs:
        created = stamp(run.get("createdAt"))
        if run.get("status") not in ACTIVE or not created or (run.get("headSha") or "") in covered_heads:
            continue
        age = minutes(created, now)
        if age > limit:
            rows.append((age, f"{run.get('databaseId')} {run.get('workflowName')} on {run.get('headBranch')} {run.get('status')} {age} min"))
    if not rows:
        return []
    rows.sort(reverse=True)
    head = "ADMISSION blocked by" if waiting else "ADMISSION idle; a prepare/apply now would wait for"
    return [alarm("WARN" if waiting else "INFO", "admission-blocked", f"{head} {len(rows)} active uncovered run(s) > {limit} min: "
                  + "; ".join(text for _, text in rows[:4]) + (f"; +{len(rows) - 4} more" if len(rows) > 4 else "")
                  + ("; sm release rehearse cancel frees the rehearsal/* runs" if waiting and any(" on rehearsal/" in text for _, text in rows) else ""))]


def slot_free(sizes: dict) -> int:
    return sum(max(SLOT_LIMITS[n] - int(sizes.get(n, 0)), 0) for n in SLOT_NAMES)


def slot_alarm(sizes: dict | None, floor: int = SLOT_FREE_MIN) -> dict | None:
    """Total free bytes over the five focused registry variables below `floor`: the next admission may not fit."""
    if not isinstance(sizes, dict):
        return None
    free = slot_free(sizes)
    if free >= floor:
        return None
    return alarm("WARN", "registry-slots", f"REGISTRY focused slots have {free} B free (< {floor}): retire heads before the next admission; sm status slots")


SLOT_FILL_PERCENT = 85


def slot_fill_alarm(sizes: dict[str, int] | None, percent: int = SLOT_FILL_PERCENT) -> dict[str, str] | None:
    """One focused registry variable above `percent` of its limit: collect it before the next admission lands in it."""
    if not isinstance(sizes, dict):
        return None
    full = sorted(((int(sizes.get(n, 0)) * 100 // SLOT_LIMITS[n], n) for n in SLOT_NAMES), reverse=True)
    over = [f"{n.replace('FOCUSED_CI_PLANS', 'PLANS')} {used}%" for used, n in full if used > percent]
    if not over:
        return None
    return alarm("WARN", "registry-slot-fill", f"REGISTRY focused slot over {percent}%: {', '.join(over)}; sm head slots collect (a dry run) lists what can go")


def release_alarm(receipt: dict | None, changed_at: float | None, active_release_runs: int, now: float, limit: int = RELEASE_MIN) -> dict | None:
    """The pre-tag release receipt: paused for more than `limit` min, or unfinished and untouched for more than
    `limit` min while no release/v* run is active (nothing is waiting on CI, so nothing moves it)."""
    if not isinstance(receipt, dict) or not receipt.get("version") or receipt.get("finished"):
        return None
    version = receipt["version"]
    events = receipt.get("hold_events") or []
    last = events[-1] if events and isinstance(events[-1], dict) else {}
    if last.get("action") in ("pause", "pause-request"):
        age = minutes(stamp(last.get("at")) or changed_at, now)
        if age > limit:
            return alarm("ALERT", f"release:{version}:paused", f"RELEASE {version} paused for {age} min: resume with sm release resume {version} or drop the candidate")
        return None
    age = minutes(changed_at, now)
    if age > limit and not active_release_runs:
        return alarm("ALERT", f"release:{version}:stuck", f"RELEASE {version} unfinished, receipt unchanged for {age} min and no release/v* run active: sm release status")
    return None


def rehearsal_alarms(state: dict | None, now: float, open_limit: int = REHEARSAL_OPEN_MIN) -> list[dict]:
    """The rehearsal state: ALERT while the last verdict is red (failed runs and the merges since the last green
    rehearsal), WARN while it is incomplete or a rehearsal has been open for too long. An incomplete verdict marked
    `preempted` (the rehearsal cancelled its own lanes for a queued PR or an admission) is expected: INFO."""
    if not isinstance(state, dict):
        return []
    out = []
    current = state.get("current") if isinstance(state.get("current"), dict) else None
    if current and minutes(current.get("started"), now) > open_limit:
        out.append(alarm("WARN", "rehearsal:open", f"REHEARSAL of main {str(current.get('main'))[:10]} open for "
                         f"{minutes(current.get('started'), now)} min: sm release rehearse step, or sm release rehearse cancel"))
    last = state.get("last") if isinstance(state.get("last"), dict) else None
    if not last:
        return out
    main = str(last.get("main"))[:10]
    if last.get("verdict") == "red":
        failures = [str(f) for f in last.get("failures") or []]
        suspects = [str(s) for s in last.get("suspects") or []]
        merges = (f" | merges since last green ({len(suspects)}): " + "; ".join(suspects[-SUSPECT_CAP:])) if suspects else " | no earlier green rehearsal to compare with"
        out.append(alarm("ALERT", f"rehearsal:{main}", f"REHEARSAL RED main {main}: {named(failures, 8) if failures else 'failures unreadable'}"
                         f"{merges}; sm release rehearse status --json"))
    elif last.get("verdict") == "incomplete":
        detail = last.get("yielded") or (last.get("consumers") or {}).get("detail") or "not every lane ran"
        out.append(alarm("INFO" if last.get("preempted") is True else "WARN", f"rehearsal:{main}",
                         f"REHEARSAL INCOMPLETE main {main}: {detail}; proves nothing, the next tick retries when main moves"))
    return out


def required_contexts(rules) -> list[str]:
    """REQUIRED narrowed to what main's rules demand; all of REQUIRED when the rules cannot be read."""
    if not isinstance(rules, list):
        return list(REQUIRED)
    try:
        demanded = {c["context"] for r in rules if r.get("type") == "required_status_checks"
                    for c in r["parameters"]["required_status_checks"]}
    except (KeyError, TypeError):
        return list(REQUIRED)
    return [name for name in REQUIRED if name in demanded] or list(REQUIRED)


def order(items: list[dict]) -> list[dict]:
    """ALERT first, then WARN, then acknowledged ones, then INFO."""
    rank = {"ALERT": 0, "WARN": 1, "INFO": 3}
    return sorted(items, key=lambda i: (rank.get(i["cls"], 1) if i["cls"] == "INFO" or not i.get("acked") else 2, i["key"]))


def exit_code(items: list[dict]) -> int:
    return 3 if any(i["cls"] == "ALERT" and not i.get("acked") for i in items) else 0
