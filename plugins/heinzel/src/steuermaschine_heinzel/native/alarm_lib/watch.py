"""Every watch alarm that holds now, read from GitHub, the process list and the adopted state files. A reader that
fails yields one WARN naming the blind check, never silence. Nothing is written and nobody is notified."""

from __future__ import annotations

from ..go_lib import legacy
from ..go_lib.probes import World, rehearsal_state
from ..queue_lib import core_facts
from . import rules


def collect(w: World, now: float) -> list[dict]:
    out: list[dict] = []
    repo = w.repo

    def gh(args: list[str]):
        data, why = w.gh.try_json(args, timeout=60)
        return None if why else data

    def guarded(name: str, check) -> None:
        try:
            out.extend(a for a in (check() or []) if a)
        except Exception as error:  # noqa: BLE001  one blind check must not hide the others
            out.append(rules.alarm("WARN", f"blind:{name}", f"ALARM CHECK {name} unavailable: {str(error)[:80]}"))

    def stalls():
        data = legacy.queue_state(w.state_dir)
        items = [i for i in data.get("items", []) if i.get("status") == "queued" and i.get("repo", repo) == repo]
        if not items:
            return []
        required = rules.required_contexts(gh(["api", f"repos/{repo}/rules/branches/main"]))
        found = []
        for item in items:
            info = gh(["pr", "view", str(item.get("pr")), "-R", repo, "--json", "state,mergeStateStatus,statusCheckRollup"])
            if not isinstance(info, dict):
                raise RuntimeError(f"PR {item.get('pr')} unreadable")
            if info.get("state") != "OPEN":
                continue
            rollup = [c for c in info.get("statusCheckRollup") or [] if not rules.check_name(c).lower().startswith("focused ")]
            since = max(filter(None, [rules.green_since(rollup), item.get("added")]), default=None) or item.get("ready_since") or item.get("waiting_since")
            found.append(rules.stall_alarm(str(item.get("key")), info.get("mergeStateStatus"), rollup, required, since, now, data.get("blocked", "")))
        return found

    def jobs_of(run_id):
        data = gh(["api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"])
        jobs = data.get("jobs") if isinstance(data, dict) else None
        return [{"name": j.get("name"), "conclusion": j.get("conclusion"), "status": j.get("status")} for j in jobs] if isinstance(jobs, list) else []

    def main_red():
        runs = gh(["run", "list", "-R", repo, "--workflow", core_facts.ci_file(), "--branch", "main", "--status", "completed", "-L", "1", "--json", "databaseId,headSha,conclusion"])
        if not isinstance(runs, list):
            raise RuntimeError("main runs unreadable")
        if not runs or (runs[0].get("conclusion") or "").lower() == "success":
            return []
        return [rules.main_red_alarm(runs[0], jobs_of(runs[0].get("databaseId")))]

    recent: dict = {}

    def runs():
        if "rows" not in recent:
            rows = gh(["run", "list", "-R", repo, "-L", "100", "--json", "databaseId,workflowName,headBranch,headSha,status,conclusion,createdAt,event"])
            if not isinstance(rows, list):
                raise RuntimeError("recent runs unreadable")
            recent["rows"] = rows
        return recent["rows"]

    def dropped(branch: str) -> bool:
        """The release branch is deleted and every PR from it is closed or merged: nobody waits for this run.
        Any unreadable answer is False, so a blind read keeps the alarm."""
        gone = w.gh.run(["api", f"repos/{repo}/git/ref/heads/{branch}"], timeout=60, retry=False)
        if gone.rc == 0 or "HTTP 404" not in (gone.out or "") + (gone.err or ""):
            return False
        pulls = gh(["pr", "list", "-R", repo, "--head", branch, "--state", "all", "-L", "20", "--json", "number,state"])
        return isinstance(pulls, list) and bool(pulls) and all(p.get("state") in ("CLOSED", "MERGED") for p in pulls)

    def waiting() -> bool:
        """A focused admission prepare or apply is running on this machine."""
        from ..go_lib.probes import parse_process

        try:
            rows = [parse_process(pid, etime, cmd) for pid, etime, cmd in w.processes()]
        except RuntimeError:
            return False
        return any(r and r.get("kind") in ("prepare", "apply", "admit") for r in rows)

    def covered() -> set[str]:
        try:
            return set(legacy.admission_receipt_heads(w.state_dir))
        except OSError:
            return set()

    def slots():
        sizes = slot_sizes(w)
        if sizes is None:
            raise RuntimeError("registry variables unreadable")
        return [rules.slot_alarm(sizes), rules.slot_fill_alarm(sizes)]

    def release():
        try:
            data = legacy.release_receipt(w.state_dir)
        except ValueError:
            data = None
        if not data:
            return []
        active = sum(1 for r in runs() if (r.get("headBranch") or "").startswith("release/v") and r.get("status") in rules.ACTIVE)
        return [rules.release_alarm(data, legacy.release_receipt_changed(w.state_dir), active, now)]

    guarded("queue-stall", stalls)
    guarded("main-red", main_red)
    guarded("candidate", lambda: rules.candidate_alarms(runs(), jobs_of, now, dropped=dropped))
    guarded("admission", lambda: rules.admission_alarms(runs(), covered(), now, waiting=waiting()))
    guarded("registry-slots", slots)
    guarded("release", release)
    guarded("rehearsal", lambda: rules.rehearsal_alarms(rehearsal_state(w), now))
    return out


def slot_sizes(w: World) -> dict | None:
    """Bytes used per focused registry variable; None when unreadable. Values are never returned."""
    data, why = w.gh.try_json(["api", f"repos/{w.repo}/actions/variables?per_page=100"], timeout=60)
    rows = data.get("variables") if isinstance(data, dict) else None
    if why or not isinstance(rows, list):
        return None
    return {r["name"]: len(str(r.get("value", "")).encode()) for r in rows if str(r.get("name", "")).startswith("FOCUSED_CI_PLANS")}


def merged(found: list[dict], recorded: dict, now: float) -> list[dict]:
    """The alarms that hold now with what the alarm state recorded about each (first seen, acknowledged), plus the
    recorded alarms another source raised (the queue loop, a hand) that are not re-derived here. An alarm only the
    watch recorded and that no longer holds is left out: it is over."""
    out, mine = [], set()
    for item in found:
        entry = recorded.get(item["key"]) if isinstance(recorded.get(item["key"]), dict) else None
        mine.add(item["key"])
        out.append({**item, "first": (entry or {}).get("first") or now, "acked": bool((entry or {}).get("acked")), "new": entry is None})
    for key, entry in sorted(recorded.items()):
        if key in mine or not isinstance(entry, dict) or set(entry.get("sources") or []) <= {"watch"}:
            continue
        out.append({"cls": entry.get("cls", "WARN"), "key": key, "text": entry.get("text", ""), "first": entry.get("first"),
                    "acked": bool(entry.get("acked")), "new": False})
    return out


def recorded_rows(recorded: dict) -> list[dict]:
    return [{"cls": e.get("cls", "WARN"), "key": k, "text": e.get("text", ""), "first": e.get("first"), "acked": bool(e.get("acked")), "new": False}
            for k, e in sorted(recorded.items()) if isinstance(e, dict)]
