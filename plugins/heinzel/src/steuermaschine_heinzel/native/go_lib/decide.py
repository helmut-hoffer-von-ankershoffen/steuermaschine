"""The decision of `go`: collected state in, ranked items out; the top one names the phase. Pure: no process, no
file, no clock (the caller passes `now`). Every probe value may be {"unknown": why}; the decision is then made on
what is left.
"""

from __future__ import annotations

import re

from ..updater_hold import HELD
from .words import Words, q

ACTIVE = {"queued", "in_progress", "waiting", "requested", "pending"}
BAD = {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
MAX_VAR = 48 * 1024  # the size limit of one focused-plan variable
SONAR_CHECK = "SonarCloud Code Analysis"
RANK = ["candidate-failed", "candidate-running", "main-red", "published-held", "published-not-live", "pipeline-failed", "apply-running", "pipeline-running",
        "pr-queued-stalled", "pr-queued", "pr-green", "admitted-not-pushed", "apply-refused", "pipeline-interrupted", "pipeline-stale",
        "pipeline-refused", "pipeline-ready", "pipeline-review", "pipeline-waiting", "prepare-running", "packet-approved", "packet-prepared", "packet-new",
        "focus-failed", "focus-local", "focus-validating", "pr-blocked-green", "pr-red", "pr-pending", "main-pending", "packet-stale",
        "focus-blocked", "rehearsal-red", "rehearsal-running", "cut-ready", "idle"]
BLOCKING = {"candidate-failed", "candidate-running", "main-red", "apply-running"}
QUIET = ("idle", "cut-ready", "rehearsal-red", "rehearsal-running", "packet-stale", "focus-blocked")


def unknown(why: object) -> dict:
    return {"unknown": str(why).strip().splitlines()[-1][:160] if str(why).strip() else "no output"}


def known(value: object) -> bool:
    return not (isinstance(value, dict) and "unknown" in value)


def next_patch(version: str) -> str:
    parts = version.split(".")
    return ".".join([*parts[:-1], str(int(parts[-1]) + 1)]) if len(parts) == 3 and all(p.isdigit() for p in parts) else ""


def version_key(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text)[:3])


def classify_pr(pr: dict) -> dict:
    """Reduce one PR row to number, head, merge state and green / pending / red."""
    failing, pending = [], []
    for check in pr.get("statusCheckRollup") or []:
        name = check.get("name") or check.get("context") or "?"
        state = (check.get("conclusion") or check.get("state") or "").upper()
        status = (check.get("status") or "").upper()
        if state in BAD:
            failing.append(name)
        elif (status and status != "COMPLETED") or state in ("PENDING", "EXPECTED"):
            pending.append(name)
    checks = "red" if failing else "pending" if pending else "green" if pr.get("statusCheckRollup") else "none"
    return {"number": pr.get("number"), "branch": pr.get("headRefName", ""), "head": pr.get("headRefOid", ""),
            "title": (pr.get("title") or "")[:70], "draft": bool(pr.get("isDraft")), "merge": pr.get("mergeStateStatus", ""),
            "checks": checks, "failing": failing[:4], "pending": len(pending)}


def main_ci(runs: list[dict], sha: str, *, exact: bool = False) -> dict:
    """The CI workflow's push or schedule run for exactly this main SHA."""
    mine = [r for r in runs if r.get("workflowName", "" if exact else "CI") == "CI"
            and r.get("event", "" if exact else "push") in ("push", "schedule")
            and r.get("headBranch", "" if exact else "main") == "main" and sha
            and (r.get("headSha") == sha if exact else str(r.get("headSha", "")).startswith(sha[:8]))]
    if not mine:
        return {"state": "none"}
    best = max(mine, key=lambda r: r.get("databaseId", 0))
    status = best.get("status")
    if status not in ACTIVE and (status != "completed" or not best.get("conclusion")):
        return {"state": "unknown"}
    state = "pending" if status in ACTIVE else "green" if best.get("conclusion") == "success" else "red"
    return {"state": state, "run": best.get("databaseId"), "conclusion": best.get("conclusion") or status}


FOCUSED_JOBS = ("Focused tests", "Main batch focused scope")
WORKLOAD_LABEL = {"focused": "focused CI, not the full candidate", "full": "full candidate workload", "unknown": "workload unknown"}


def workload(jobs: object, full: set[str] | frozenset[str]) -> str:
    """What a main CI run actually ran, from its authenticated job list: `full` when every job of the full candidate
    workload (`full`) ran and succeeded, `focused` when a focused job ran, else `unknown` (never guessed green)."""
    if not isinstance(jobs, list) or not jobs or not all(isinstance(j, dict) for j in jobs):
        return "unknown"
    ran = {str(j.get("name")): j.get("conclusion") for j in jobs if j.get("conclusion") not in (None, "skipped")}
    if full and all(ran.get(name) == "success" for name in full):
        return "full"
    if any(name.startswith(FOCUSED_JOBS) for name in ran):
        return "focused"
    return "unknown"


def candidate(runs: list[dict], prs: list[dict], active_receipt: dict | None) -> dict | None:
    """A candidate is in flight when a release PR is open or the release runner holds an unfinished receipt."""
    release_prs = [p for p in prs if str(p.get("branch", "")).startswith("release/v")]
    version = None
    if release_prs:
        version = release_prs[0]["branch"].removeprefix("release/v")
    elif active_receipt and active_receipt.get("version") and not active_receipt.get("finished"):
        version = str(active_receipt["version"])
    if not version:
        return None
    mine = [r for r in runs if r.get("headBranch") == f"release/v{version}" and r.get("workflowName") == "CI"]
    latest = max(mine, key=lambda r: r.get("databaseId", 0)) if mine else None
    state = "starting" if not latest else "running" if latest.get("status") in ACTIVE else (
        "green" if latest.get("conclusion") == "success" else "failed")
    return {"version": version, "pr": release_prs[0]["number"] if release_prs else None, "state": state,
            "run": latest.get("databaseId") if latest else None, "conclusion": latest.get("conclusion") if latest else None}


def packet_state(p: dict, main_sha: str, remote_heads: dict | None, pr_heads: set[str], receipts: set[str]) -> str:
    """Where one admission packet stands. Order matters: what already happened wins over what the files say."""
    head = p.get("head", "")
    pushed = head in pr_heads or (remote_heads is not None and remote_heads.get(p.get("branch", "")) == head)
    if pushed:
        return "pushed"
    if main_sha and p.get("main") and p["main"] != main_sha:  # a plan binds one main: at any other main it is history
        return "admitted-old-main" if head in receipts else "packet-stale"
    if head in receipts:
        return "admitted-not-pushed"
    if p.get("apply_refused"):
        return "apply-refused"
    if not p.get("prepared_sha256"):
        return "packet-new"
    if p.get("approved_sha256") == p["prepared_sha256"]:
        return "packet-approved"
    return "packet-prepared"


def apply_command(p: dict, w: Words) -> str:
    """The whole apply command for one prepared packet. An argument that could not be established is printed as
    `UNKNOWN`; `apply_unknown` names each one, and the command is then never offered as NEXT."""
    missing = {u.split(":", 1)[0] for u in p.get("unknown_args") or []}
    cmd = [*w.apply_head(), p["packet"], "--core-repo", p.get("core_repo") or "UNKNOWN", "--review-root", p.get("review_root") or "UNKNOWN"]
    if p.get("pr"):
        cmd += ["--pr", str(p["pr"])]
    if p.get("expected_remote_head"):
        cmd += ["--expected-remote-head", p["expected_remote_head"]]
    if p.get("resulting_main_only"):
        cmd.append("--resulting-main-only")
    if p.get("protected_producer"):
        cmd.append("--reviewed-protected-producer")
    if p.get("nonconsumers") or "--nonconsumers" in missing:
        cmd += ["--nonconsumers", p.get("nonconsumers") or "UNKNOWN"]
    cmd += ["--prepared", p["prepared"], "--approved-sha256", p["prepared_sha256"]]
    return " ".join(q(c) for c in cmd)


def apply_unknown(p: dict) -> str | None:
    """None when the apply command is complete, else one line that names every argument that is not known."""
    rows = list(p.get("unknown_args") or [])
    rows += [f"{flag}: not in wave.json or prepare.args" for flag, key in (("--core-repo", "core_repo"), ("--review-root", "review_root"))
             if not p.get(key) and not any(r.startswith(flag) for r in rows)]
    return ("the apply command is not complete, unknown " + "; ".join(rows)) if rows else None


def open_alerts(alarms: object, w: Words) -> list[str]:
    """The unacknowledged ALERT lines of the alarm state, oldest first."""
    return [f"{w.alarm_text(e.get('text') or key)}  (silence: {w.alarm_ack(key)})" for key, e in open_alert_rows(alarms)]


def open_alert_rows(alarms: object) -> list[tuple[str, dict]]:
    if not isinstance(alarms, dict):
        return []
    rows = [(e.get("first") or 0, k, e) for k, e in alarms.items() if isinstance(e, dict) and e.get("cls") == "ALERT" and not e.get("acked")]
    return [(key, e) for _, key, e in sorted(rows, key=lambda r: (r[0], r[1]))]


def focus_merged(f: dict) -> bool:
    return any(isinstance(pr, dict) and pr.get("state") == "MERGED" and pr.get("headRefOid") == f.get("head") for pr in f.get("prs") or [])


def same_packet(packet: str | None, named: str | None) -> bool:
    """Does a process argument name this packet file? A relative argument matches by its trailing path."""
    if not packet or not named:
        return False
    return packet == named or (not named.startswith("/") and packet.endswith("/" + named.removeprefix("./")))


def working_on(p: dict, procs: list[dict], main_sha: str) -> dict | None:
    """The running prepare or admission of this packet, if any (the fix for 'no packet' while one is in the making)."""
    for proc in procs:
        if proc.get("kind") == "prepare" and same_packet(p.get("packet"), proc.get("packet")):
            return proc
        if proc.get("kind") == "admit" and proc.get("worktree") and proc.get("worktree") == p.get("worktree") and p.get("main") == main_sha:
            return proc
    return None


def decide(s: dict, now: float, w: Words | None = None, stall_minutes: int = 15) -> dict:
    """Turn the collected state into ranked items; the top one names the phase."""
    w = w or Words()
    items: list[dict] = []
    unknowns = [f"{name}: {val['unknown']}" for name, val in s.items() if not known(val)]

    def item(kind: str, what: str, next_cmd: str | None = None, trap: str | None = None, waiting: str | None = None) -> dict:
        return {"kind": kind, "phase": what, "next": next_cmd, "trap": w.trap(trap), "waiting": waiting}

    def get(key, default):
        return s[key] if known(s.get(key)) and s.get(key) is not None else default

    main_sha = get("main", {}).get("sha", "")
    runs, raw_prs = get("runs", []), get("prs", [])
    prs = [classify_pr(p) for p in raw_prs]
    queue, rel, holds = get("queue", {}), get("release", {}), get("holds", {})
    active_runs = [r for r in runs if r.get("status") in ACTIVE]
    runs_known = known(s.get("runs"))
    procs = [p for p in get("procs", []) if isinstance(p, dict)]

    # 1. candidate
    cand = candidate(runs, prs, rel.get("active"))
    if cand:
        v = cand["version"]
        if cand["state"] == "failed":
            items.append(item("candidate-failed", f"candidate v{v} FAILED (CI run {cand['run']}: {cand['conclusion']})", w.ci_why(cand["run"]), "first_attempt"))
            items[-1]["then"] = f"after the cause is fixed forward on main: {w.drop(v, bool(get('tools', {}).get('drop_candidate')))}"
        elif cand["state"] == "green":
            items.append(item("candidate-running", f"candidate v{v} CI green, release tail (tag, PyPI, compose, acceptance) in flight",
                              w.release_status(), "live", w.release_finishing(v)))
        else:
            items.append(item("candidate-running", f"candidate v{v} {cand['state']} (PR #{cand['pr']}, CI run {cand['run']})",
                              None, "first_attempt", f"candidate CI run {cand['run']} (about 85 min); watch with {w.release_status()}"))

    # 2. main
    main_result = s.get("main_ci")
    if "main_ci" not in s:  # historical snapshots predate the authoritative probe
        ci = main_ci(runs or queue.get("main_runs", []), main_sha) if main_sha else {"state": "unknown"}
    else:
        ci = main_result if known(main_result) and isinstance(main_result, dict) and main_result.get("sha") == main_sha else {"state": "unknown"}
    if ci["state"] == "red":
        items.append(item("main-red", f"main {main_sha[:8]} is RED (CI run {ci['run']}: {ci['conclusion']})", w.main_evidence(), "red"))
    elif ci["state"] == "pending":
        items.append(item("main-pending", f"main {main_sha[:8]} CI running (run {ci['run']})", None, "red", f"main CI run {ci['run']}"))

    # 3. published, not live
    latest = get("releases", {}).get("latest", "")
    mini = get("mini", {})
    held = mini.get("held") or ""
    if latest and mini.get("cli") and version_key(mini["cli"]) < version_key(latest) and held and version_key(held) >= version_key(latest):
        items.append(item("published-held", f"v{latest} published, {HELD} (Mini on {mini['cli']}; its updater holds v{held} until acceptance passes)",
                          w.release_status(), "held"))
    elif latest and mini.get("cli") and version_key(mini["cli"]) < version_key(latest):
        items.append(item("published-not-live", f"v{latest} published, Mini still on {mini['cli']}: not live-verified", w.verify_mini(latest), "live"))
    elif latest and mini.get("live_ok") is False:
        items.append(item("published-not-live", f"v{latest} published, live proof missing: {mini.get('live', '')[:80]}", w.verify_mini(latest), "live"))

    # 4. admission processes
    for proc in procs:
        if proc["kind"] == "apply":
            items.append(item("apply-running", f"admission apply running (pid {proc['pid']}, {proc['etime']}) for {proc.get('packet', '?')}",
                              None, "idle", f"apply pid {proc['pid']} to print its receipt path; then run {w.go()} again"))

    # 4b. engine pipeline runs (sm head admit, sm release cut): their own state says where they stand
    for run in get("pipelines", []):
        items.extend(pipeline_items(run, main_sha, w, item))

    # 5. pull requests
    queued = set(queue.get("queued", []))
    added = queue.get("added", {})
    held = holds.get("prs", {})
    packets = get("packets", [])
    path_heads = {p.get("head") for p in packets if p.get("main") == main_sha}  # heads prepared for this main
    focus = [f for f in get("focus", []) if isinstance(f, dict)]
    focus_open = [f for f in focus if f.get("head") and not focus_merged(f) and (not latest or version_key(str(f.get("for"))) > version_key(latest))]
    path_heads |= {f["head"] for f in focus_open}  # a declared focus head is the ship path, never backlog
    for pr in prs:
        before = len(items)
        n, key = pr["number"], str(pr["number"])
        if pr["branch"].startswith("release/v") or pr["draft"]:
            continue
        label = f"PR #{n} ({pr['branch']} {pr['head'][:9]})"
        if key in held:
            items.append(item("held", f"{label} held: {held[key][:70]}"))
        elif pr["merge"] == "DIRTY":
            items.append(item("conflict", f"{label} conflicts with main"))
        elif key in queued and pr["checks"] == "green":
            age = int((now - added[key]) / 60) if key in added else None
            if age is not None and age >= stall_minutes and pr["merge"] in ("CLEAN", "HAS_HOOKS", "UNSTABLE"):
                items.append(item("pr-queued-stalled", f"{label} green and queued for {age} min: STALLED", w.queue_why_pr(n), "stall"))
            else:
                items.append(item("pr-queued", f"{label} green, queued" + (f" {age} min" if age is not None else ""), None, "batch",
                                  f"{w.queue_tick()} to merge #{n}"))
        elif pr["checks"] == "red" and pr["failing"] == [SONAR_CHECK]:
            items.append(item("pr-red", f"{label} red only on {SONAR_CHECK}", w.sonar(n), "sonar"))
        elif pr["checks"] == "red":
            items.append(item("pr-red", f"{label} red: {', '.join(pr['failing'])}", w.faillog(n), "cycle"))
        elif pr["checks"] == "pending":
            items.append(item("pr-pending", f"{label} CI running ({pr['pending']} checks pending)", None, "sha", f"PR #{n} checks (about 25 min)"))
        elif pr["checks"] == "green" and pr["merge"] == "BLOCKED":
            items.append(item("pr-blocked-green", f"{label} every check green but BLOCKED", w.queue_why_pr(n), "sha"))
        elif pr["checks"] == "green":
            items.append(item("pr-green", f"{label} green, not queued", w.ship(n), "batch"))
        for new in items[before:]:  # a PR that is neither queued nor from a packet for this main is backlog, not the ship path
            new["backlog"] = key not in queued and pr["head"] not in path_heads

    # 6. admission packets
    pr_heads = {p["head"] for p in prs}
    remote = get("remote_heads", None)
    receipts = set(get("receipts", []))
    applying = {proc.get("packet") for proc in procs if proc["kind"] == "apply"}
    admitted, old, stale, live_branches = 0, [], [], set()
    for p in packets:
        if p.get("packet") in applying:
            continue
        state = packet_state(p, main_sha, remote, pr_heads, receipts)
        label = f"{p.get('branch')} {p.get('head', '')[:9]}"
        if state not in ("packet-stale", "admitted-old-main"):
            live_branches.add(p.get("branch"))
        proc = working_on(p, procs, main_sha) if state in ("packet-new", "packet-prepared", "packet-approved", "apply-refused") else None
        if proc:
            what = "admission" if proc["kind"] == "admit" else "prepare"
            waits = " (it waits for the idle window)" if proc.get("waits") else ""
            items.append(item("prepare-running", f"{what} running (pid {proc['pid']}, {proc['etime']}){waits} for {label}", None, "truncated",
                              f"pid {proc['pid']} to write {p.get('dir')}/prepare.out; then run {w.go()} again"))
        elif state == "admitted-not-pushed":
            admitted += 1
            unsure = "" if remote is not None else " (remote heads unknown: confirm it is not pushed)"
            items.append(item("admitted-not-pushed", f"fix admitted, not pushed: {label}{unsure}",
                              w.pr_open(p["worktree"], p.get("bead") or "-", p.get("subject") or "TITLE", p.get("body")),
                              "batch" if admitted > 1 else "sha"))
        elif state == "admitted-old-main":
            old.append(p.get("branch", "?"))
        elif state == "packet-stale":
            stale.append(p.get("branch", "?"))
        elif state == "apply-refused":
            retry = "truncated" in p["apply_refused"]
            gap = apply_unknown(p) if retry else None
            items.append(item("apply-refused", f"apply refused for {label}: {p['apply_refused']}",
                              apply_command(p, w) if retry and not gap else None, "truncated" if retry else "cycle",
                              gap if retry else "a new packet: the refusal is not a retry case"))
            if gap:
                items[-1]["then"] = apply_command(p, w)
        elif state == "packet-approved":
            wait = apply_unknown(p)
            if wait:
                pass
            elif not runs_known:
                wait = "the Actions run inventory (unknown): confirm zero active runs before apply"
            elif active_runs:
                wait = "idle window: active runs " + ", ".join(f"{r['databaseId']} {r.get('headBranch', '')}" for r in active_runs[:4])
            items.append(item("packet-approved", f"packet approved, not applied: {label}", None if wait else apply_command(p, w), "idle", wait))
            if wait:
                items[-1]["then"] = apply_command(p, w)
        elif state == "packet-prepared":
            items.append(item("packet-prepared", f"packet prepared, awaiting independent review: {label}", None, "review",
                              f"an independent reviewer (different model) to end {p['dir']}/APPLY-REVIEW.md with "
                              f"`VERDICT: APPROVE sha256={p['prepared_sha256']}`"))
            items[-1]["then"] = (f"after the verdict, in an idle window: {apply_command(p, w)}" + (f"  ({gap})" if (gap := apply_unknown(p)) else ""))
        elif state == "packet-new":
            items.append(item("packet-new", f"packet built, not prepared: {label}",
                              w.prepare(p["packet"], p["core_repo"], p["review_root"], p["dir"] + "/prepare.out"), "truncated"))

    stale = [b for b in dict.fromkeys(stale) if b not in live_branches and b not in old and not re.fullmatch(r"[0-9a-f]{40}", b)]
    if stale:
        items.append(item("packet-stale", f"{len(stale)} local heads with a packet for an older main, not admitted (rebuild at main {main_sha[:8]}): "
                          + ", ".join(stale[:6]) + (" ..." if len(stale) > 6 else ""),
                          f"{w.preopen_help()}  # one per branch, with --main {main_sha}", "batch"))
    old = [b for b in dict.fromkeys(old) if b not in live_branches]
    if old:
        items.append(item("history", f"{len(old)} packets admitted at an older main and not on the remote (merged and deleted, or abandoned): "
                          + ", ".join(old[:6]) + (" ..." if len(old) > 6 else "")))

    # 6b. declared focus heads that neither a PR nor a packet for this main covers yet
    for f in focus:
        wt, head, name = f.get("worktree", "?"), f.get("head") or "", f"focus v{f.get('for')}: {f.get('branch') or '?'} {(f.get('head') or '')[:9]}"
        if not head:
            items.append(item("focus-blocked", f"{name} unreadable ({f.get('error') or 'no head'}): {wt}", w.focus("list"), "focus"))
        elif focus_merged(f):
            items.append(item("focus-done", f"{name} is merged: {w.focus('done', wt, f)}"))
        elif f not in focus_open:
            items.append(item("focus-done", f"{name} was declared for v{f.get('for')}, which is out (v{latest}): {w.focus('done', wt, f)} or add it again --for the next one"))
        elif head in pr_heads or any(p.get("head") == head and p.get("main") == main_sha for p in packets):
            continue  # the PR or packet item above carries this head through its steps
        else:
            dirty = " (worktree has uncommitted changes)" if f.get("dirty") else ""
            older = next((f" (PR #{pr['number']} holds an older head {pr['head'][:9]})" for pr in prs if pr["branch"] == f.get("branch")), "")
            mine = [r for r in runs if str(r.get("headBranch", "")).startswith("validation/") and head[:7] in str(r.get("headBranch"))]
            going = [r for r in mine if r.get("status") in ACTIVE]
            failed = [r for r in mine if r.get("status") not in ACTIVE and r.get("conclusion") not in ("success", "cancelled", "skipped")]
            packet_cmd = f"{w.preopen_help()}  # build the packet for {f.get('branch')} in {wt} with --main {main_sha}"
            proc = next((x for x in procs if x.get("kind") in ("prepare", "admit") and wt in (x.get("worktree"), x.get("core_repo"))), None)
            if head in receipts:
                items.append(item("admitted-not-pushed", f"{name} admitted, not pushed{dirty}", w.pr_open(wt, "-", f.get("subject") or "TITLE"), "sha"))
            elif proc:
                what = "admission" if proc["kind"] == "admit" else "prepare"
                items.append(item("prepare-running", f"{name} local, unpushed, {what} running (pid {proc['pid']}, {proc['etime']}){dirty}{older}", None,
                                  "truncated", f"pid {proc['pid']} to finish; then run {w.go()} again"))
            elif going:
                shown = ", ".join(f"{r['databaseId']} {r['headBranch']}" for r in going[:6])
                items.append(item("focus-validating", f"{name} local, unpushed, validating{dirty}{older}", None, "focus",
                                  f"validation runs {shown}; then the packet: {packet_cmd}"))
            elif failed:
                items.append(item("focus-failed", f"{name} validation run {failed[0]['databaseId']} on {failed[0]['headBranch']}: {failed[0].get('conclusion')}{dirty}",
                                  w.faillog(failed[0]["databaseId"]), "cycle"))
            else:
                seen = f"validated by run {mine[0]['databaseId']}" if mine else "no validation run for it among the latest runs"
                stale_packet = any(p.get("head") == head for p in packets)
                items.append(item("focus-local", f"{name} local, unpushed, " + ("its packet is for an older main" if stale_packet else "no packet")
                                  + f" ({seen}){dirty}{older}", packet_cmd, "focus" if not mine else "batch"))

    # 7. nothing in flight
    busy = [i for i in items if i["kind"] in RANK and not i.get("backlog")]
    since = get("since_tag", {}).get("count")
    rehearse = get("rehearse", {})
    last = rehearse.get("last") if isinstance(rehearse.get("last"), dict) else {}
    current = rehearse.get("current") if isinstance(rehearse.get("current"), dict) else None
    start_rehearsal = w.rehearse("start --consumers")
    if focus_open:  # never a rehearsal or a cut while a declared focus head for the next release is unmerged
        if not [i for i in items if i["kind"] in RANK]:
            f = focus_open[0]
            items.append(item("focus-blocked", f"focus v{f.get('for')}: {f.get('branch')} {f['head'][:9]} is not merged and nothing can move it now "
                              "(see ASIDE)", None, "focus", f"the focus head to merge; no rehearsal and no cut before ({w.focus('done', f.get('worktree'), f)} lifts it)"))
    elif not busy and ci["state"] == "green" and since:
        ready = f"main {main_sha[:8]} green, {since} commits since v{latest}, no candidate"
        if last.get("verdict") == "red":  # blocks the cut until a later rehearsal replaces the verdict
            failed = "; ".join(str(f) for f in last.get("failures") or []) or f"failures unreadable: {w.rehearse('status --json', typed=False)}"
            same = str(last.get("main")) == main_sha
            items.append(item("rehearsal-red", f"{ready}, but the last rehearsal (main {str(last.get('main'))[:8]}) is RED: {failed}",
                              None if current else w.rehearse("status --json") if same else start_rehearsal, "rehearsal",
                              f"the open rehearsal: {w.rehearse('step', typed=False)}" if current else None))
            if same and not current:
                items[-1]["then"] = f"after the fix is on main: {start_rehearsal}"
        elif current:
            items.append(item("rehearsal-running", f"{ready}, rehearsal of main {str(current.get('main'))[:8]} running", None, "rehearsal",
                              f"the open rehearsal ({w.rehearse('step')} reads it; launchd ticks it)"))
        elif get("tools", {}).get("rehearse") and not (last.get("verdict") == "green" and str(last.get("main")) == main_sha):
            items.append(item("cut-ready", ready, start_rehearsal, "consumer"))
        else:
            items.append(item("cut-ready", ready + (", rehearsal green" if last.get("verdict") == "green" else ""), w.cut(next_patch(latest)), "consumer"))
    elif not [i for i in items if i["kind"] in RANK]:
        why = "main CI state unknown" if ci["state"] in ("unknown", "none") else "nothing to ship"
        items.append(item("idle", f"idle: {why}", None, None, "new work: nothing is in the ship pipeline"))

    order = {k: i for i, k in enumerate(RANK)}
    ranked = sorted((i for i in items if i["kind"] in order), key=lambda i: (bool(i.get("backlog")), order[i["kind"]]))
    if any(not i.get("backlog") and i["kind"] not in QUIET for i in ranked):
        for i in ranked:  # one critical path: while it is in flight, backlog is listed, never offered as the next command
            if i.get("backlog"):
                i["phase"], i["next"], i["waiting"] = f"backlog: {i['phase']}" + (f" -> {i['next']}" if i["next"] else ""), None, None
    side = [i for i in items if i["kind"] not in order]
    top = ranked[0]
    runnable = top if top["kind"] in BLOCKING else next((i for i in ranked if i["next"]), None)
    waits = [i["waiting"] for i in ranked if i["waiting"]]
    lead = runnable if runnable and runnable["next"] else top
    return {"phase": top["phase"], "kind": top["kind"], "next": lead["next"], "next_for": lead["phase"] if lead is not top else None,
            "then": lead.get("then"), "trap": lead["trap"] or top["trap"], "waiting": None if lead["next"] else (waits[0] if waits else None),
            "also": [i["phase"] for i in ranked[1:]], "side": [i["phase"] for i in side], "unknown": unknowns,
            "alerts": open_alerts(get("alarms", {}), w),
            "facts": facts(s, ci, cand, active_runs),
            "also_items": [{"phase": i["phase"], "next": i["next"] or "", "waiting_on": i["waiting"] or ""} for i in ranked[1:]]}


def pipeline_items(run: dict, main_sha: str, w: Words, item) -> list[dict]:
    """One pipeline run as go items: running, interrupted, stale, failed, at a review, ready for --live, waiting."""
    subject, _, verb = str(run.get("pipeline") or "").partition(".")
    label, status = run.get("label") or str(run.get("key", ""))[:12], run.get("status")
    again = w.pipeline(subject, verb, str(run.get("arg") or run.get("key") or ""))
    trap = run.get("trap") or None
    bound = (run.get("fingerprint") or {}).get("main", "")
    if status == "running" and run.get("alive") is not False:
        out = item("pipeline-running", f"{label}: {run.get('phase')}", None, None, f"pid {run.get('pid')} running '{run.get('running')}'; then {w.go()} again")
        out["trap"] = f"Start nothing else on {label} while it runs; a second {again} only waits on the lock. " + (trap or "")
        return [out]
    if status == "running":
        return [item("pipeline-interrupted", f"{label}: '{run.get('running')}' was interrupted (pid {run.get('pid')} is gone)", again, "interrupted")]
    head = run.get("worktree_head")
    if run.get("pipeline") == "head.admit" and head and head != run.get("key") and not run.get("external_done"):
        return [item("pipeline-stale", f"{label}: the worktree moved on to {head[:9]}; this run is for an older head", again, "new_head")]
    if main_sha and bound and bound != main_sha and not run.get("external_done"):
        return [item("pipeline-stale", f"{label}: plan stale, main moved {bound[:8]} -> {main_sha[:8]} before the apply", again, "plan_stale")]
    if status == "refused" and run.get("pipeline") == "release.cut" and "compose" in str(run.get("phase", "")).lower():
        out = item("pipeline-failed", f"{label}: {run.get('phase')}", w.release_status(), "compose")
        out["then"] = f"after the fix is published: {run.get('next') or again}"
        return [out]
    if status == "refused":
        out = item("pipeline-refused", f"{label}: {run.get('phase')}", run.get("next") or again)
        out["trap"] = trap or w.trap("cycle")
        return [out]
    if status == "review":
        review = run.get("review") or {}
        out = item("pipeline-review", f"{label}: {run.get('phase')}", None, None,
                   f"an independent reviewer (not the author): brief {review.get('brief', '?')}, file {review.get('file') or '?'}, "
                   f"sha256 {review.get('sha256', '?')}; they record: {review.get('record', 'sm review verdict SHA256 --approve --reviewer NAME')}")
        out["trap"], out["then"] = trap or w.trap("review"), run.get("next") or again
        return [out]
    if status == "dry_run":
        out = item("pipeline-ready", f"{label}: {run.get('phase')}", run.get("next") or again)
        out["trap"] = trap
        return [out]
    out = item("pipeline-waiting", f"{label}: {run.get('phase')}", None, None, run.get("waiting_on") or "the step it names")
    out["trap"], out["then"] = trap, run.get("next") or again
    return [out]


def facts(s: dict, ci: dict, cand: dict | None, active_runs: list[dict]) -> list[str]:
    out = []
    focus = s.get("focus") if known(s.get("focus")) else []
    if focus:
        out.append("focus: " + "; ".join(f"{f.get('branch') or '?'} {(f.get('head') or '')[:9]} for v{f.get('for')}"
                                          + (f" ({f['note']})" if f.get("note") else "") for f in focus if isinstance(f, dict)))
    main = s.get("main")
    label = ""
    if "main_workload" in s:  # a state recorded before the workload probe existed carries no label
        kind = s["main_workload"] if known(s["main_workload"]) and isinstance(s["main_workload"], dict) else {}
        ran = kind.get("workload") if ci.get("run") and kind.get("run") == ci["run"] else "unknown"
        label = ", " + WORKLOAD_LABEL.get(ran, WORKLOAD_LABEL["unknown"])
    out.append(f"main: unknown: {main['unknown']}" if not known(main) else f"main {main.get('sha', '')[:8]} CI {ci['state']}"
               + (f" (run {ci['run']}{label})" if ci.get("run") else ""))
    rel = s.get("releases")
    mini = s.get("mini") if known(s.get("mini")) else {}
    if known(rel):
        out.append(f"latest release v{rel.get('latest')}; Mini CLI {mini.get('cli', 'unknown')}"
                   + (f"; live: {mini['live'][:70]}" if mini.get("live") else " (live not probed; --live)"))
    out.append(f"candidate: {'v' + cand['version'] + ' ' + cand['state'] if cand else 'none'}")
    release = s.get("release") if known(s.get("release")) else {}
    if release.get("dropped"):
        out.append("dropped candidates: " + ", ".join(release["dropped"]))
    queue = s.get("queue")
    if known(queue):
        out.append(f"queue: {', '.join(queue.get('queued', [])) or 'empty'}; freeze {'ON' if queue.get('freeze') else 'off'}"
                   + (f"; {queue['verdict'][:70]}" if queue.get("verdict") else "") + (f" [{queue['source']}]" if queue.get("source") else ""))
    holds = s.get("holds") if known(s.get("holds")) else {}
    if holds.get("files"):
        out.append("holds: " + ", ".join(holds["files"]))
    train = s.get("train")
    if known(train) and (train.get("trains") or train.get("tails")):
        out.append("train: " + "; ".join((train.get("trains") or train.get("tails"))[:2])[:150])
    if known(s.get("runs")):
        out.append("active runs: " + (", ".join(f"{r['databaseId']} {r.get('workflowName', '')} {r.get('headBranch', '')}"
                                               for r in active_runs[:4]) or "none (idle window open)"))
        window = [r for r in active_runs if str(r.get("headBranch", "")).startswith("validation/")]
        if window:
            out.append(f"hosted window in flight ({len(window)} validation runs): "
                       + ", ".join(f"{r['databaseId']} {r['headBranch']}" for r in window[:8]) + (" ..." if len(window) > 8 else ""))
    rehearse = s.get("rehearse") if known(s.get("rehearse")) else {}
    if isinstance(rehearse, dict) and (rehearse.get("last") or rehearse.get("current")):
        last, current = rehearse.get("last") or {}, rehearse.get("current")
        out.append("rehearsal: " + (f"running main {str(current.get('main'))[:8]}; " if current else "")
                   + (f"last {last.get('verdict')} main {str(last.get('main'))[:8]}" if last else "no verdict yet"))
    slots = s.get("slots")
    if known(slots) and slots:
        out.append("focused slots: " + ", ".join(f"{k.replace('FOCUSED_CI_PLANS', 'PLANS')} {v * 100 // MAX_VAR}%" for k, v in slots.items()))
    return out


def render(d: dict, limit: int = 8) -> list[str]:
    lines = [f"ALERT    {a}" for a in d.get("alerts") or []]  # unacknowledged alarms come before everything else
    lines.append(f"PHASE    {d['phase']}")
    if d["next"]:
        lines.append(f"NEXT     {d['next']}")
        if d.get("next_for"):
            lines.append(f"         (for: {d['next_for']})")
    else:
        lines.append("NEXT     nothing runnable now")
    if d.get("waiting"):
        lines.append(f"WAITING  {d['waiting']}")
    if d.get("then"):
        lines.append(f"THEN     {d['then']}")
    if d.get("trap"):
        lines.append(f"TRAP     {d['trap']}")
    for title, key in (("ALSO", "also"), ("ASIDE", "side")):
        rows = d.get(key) or []
        for row in rows[:limit]:
            lines.append(f"{title:<8} {row}")
            title = ""
        if len(rows) > limit:
            lines.append(f"         ... {len(rows) - limit} more (--all)")
    lines += [f"STATE    {f}" if n == 0 else f"         {f}" for n, f in enumerate(d.get("facts", []))]
    lines += [f"UNKNOWN  {u}" if n == 0 else f"         {u}" for n, u in enumerate(d.get("unknown", []))]
    return lines
