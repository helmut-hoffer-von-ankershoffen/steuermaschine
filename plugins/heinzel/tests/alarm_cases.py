"""Inputs for the alarm rules, shared by the recorder (old tool) and the parity test (native). JSON only."""

NOW = 1_800_000_000
OK = {"name": "CI OK", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2027-01-15T07:00:00Z"}
GATE = {"name": "merge-gate", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2027-01-15T07:10:00Z"}
FLOOR = {"name": "coverage-floor", "status": "COMPLETED", "conclusion": "FAILURE", "completedAt": "2027-01-15T07:10:00Z"}
PENDING = {"name": "web-1", "status": "IN_PROGRESS", "conclusion": None}
REQ = ["CI OK", "merge-gate", "coverage-floor"]


def run(run_id, branch="main", status="completed", conclusion="success", created=NOW - 3600, workflow="CI", sha="a" * 40):
    import datetime

    at = datetime.datetime.fromtimestamp(created, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"databaseId": run_id, "headBranch": branch, "status": status, "conclusion": conclusion, "createdAt": at, "workflowName": workflow, "headSha": sha}


JOBS = {"5": [{"name": "web-2 (ubuntu-24.04)", "conclusion": "failure"}, {"name": "CI OK", "conclusion": "failure"}, {"name": "lint", "conclusion": "success"}],
        "6": [{"name": "cli", "conclusion": "cancelled"}], "7": [{"name": "lint", "conclusion": "success"}]}

CASES = [
    ["stall_alarm", ["801", "BLOCKED", [OK], REQ, NOW - 277 * 60, NOW], {}],
    ["stall_alarm", ["801", "BLOCKED", [OK, GATE, FLOOR], REQ, NOW - 40 * 60, NOW], {"everything": [OK, GATE, FLOOR]}],
    ["stall_alarm", ["801", "BLOCKED", [OK, GATE], ["CI OK", "merge-gate"], NOW - 40 * 60, NOW], {}],
    ["stall_alarm", ["plugins 38", "CLEAN", [OK], REQ, NOW - 40 * 60, NOW, "merge freeze is on"], {}],
    ["stall_alarm", ["801", "CLEAN", [OK], REQ, NOW - 40 * 60, NOW, ""], {}],
    ["stall_alarm", ["801", "CLEAN", [OK], REQ, NOW - 40 * 60, NOW, "odd block"], {}],
    ["stall_alarm", ["801", "CLEAN", [OK], REQ, NOW - 5 * 60, NOW], {}],
    ["stall_alarm", ["801", "CLEAN", [OK, PENDING], REQ, NOW - 50 * 60, NOW], {}],
    ["stall_alarm", ["801", None, [], REQ, NOW - 50 * 60, NOW], {}],
    ["main_red_alarm", [{"databaseId": 5, "headSha": "f19a3782232c", "conclusion": "failure"}, JOBS["5"]], {}],
    ["main_red_alarm", [{"databaseId": 5, "headSha": "f19a3782232c", "conclusion": "timed_out"}, []], {}],
    ["main_red_alarm", [{"databaseId": 5, "headSha": "f19a3782232c", "conclusion": "success"}, JOBS["5"]], {}],
    ["main_red_alarm", [None, []], {}],
    ["candidate_alarms", [[run(5, "release/v0.9.40", conclusion="failure"), run(6, "release/v0.9.40", status="in_progress", conclusion=None, workflow="Acceptance"),
                           run(7, "release/v0.9.41"), run(8, "release/v0.9.30", conclusion="failure", created=NOW - 20 * 3600), run(9, "fix/x", conclusion="failure")], JOBS, NOW], {}],
    ["candidate_alarms", [[run(5, "release/v0.9.40", conclusion="failure")], JOBS, NOW], {"dropped": ["release/v0.9.40"]}],
    ["admission_alarms", [[run(11, "fix/a", status="in_progress", conclusion=None, created=NOW - 50 * 60), run(12, "rehearsal/x", status="queued", conclusion=None, created=NOW - 30 * 60),
                           run(13, "fix/b", status="queued", conclusion=None, created=NOW - 60), run(14, "fix/c", status="queued", conclusion=None, created=NOW - 90 * 60, sha="c" * 40)],
                          ["c" * 40], NOW], {"waiting": True}],
    ["admission_alarms", [[run(11, "main", status="in_progress", conclusion=None, created=NOW - 50 * 60)], [], NOW], {"waiting": False}],
    ["admission_alarms", [[run(11, "main")], [], NOW], {"waiting": True}],
    ["slot_alarm", [{"FOCUSED_CI_PLANS": 48000, "FOCUSED_CI_PLANS_V2_0": 39900, "FOCUSED_CI_PLANS_V2_1": 39900, "FOCUSED_CI_PLANS_V2_2": 39900, "FOCUSED_CI_PLANS_V2_3": 39000}], {}],
    ["slot_alarm", [{"FOCUSED_CI_PLANS": 1000}], {}],
    ["slot_alarm", [None], {}],
    ["release_alarm", [{"version": "0.9.40", "hold_events": [{"action": "pause", "at": "2027-01-15T06:00:00Z"}]}, NOW - 60, 0, NOW], {}],
    ["release_alarm", [{"version": "0.9.40"}, NOW - 45 * 60, 0, NOW], {}],
    ["release_alarm", [{"version": "0.9.40"}, NOW - 45 * 60, 2, NOW], {}],
    ["release_alarm", [{"version": "0.9.40", "finished": True}, NOW - 45 * 60, 0, NOW], {}],
    ["release_alarm", [None, None, 0, NOW], {}],
    ["rehearsal_alarms", [{"current": {"main": "f19a3782232cb24b", "started": NOW - 300 * 60},
                           "last": {"main": "da1362306815dc5e", "verdict": "red", "failures": ["web-2 run 5", "consumer google run 6"], "suspects": ["#790 fix a", "#791 fix b"]}}, NOW], {}],
    ["rehearsal_alarms", [{"last": {"main": "da1362306815dc5e", "verdict": "red"}}, NOW], {}],
    ["rehearsal_alarms", [{"last": {"main": "da1362306815dc5e", "verdict": "incomplete", "preempted": True, "yielded": "yielded to PR 801"}}, NOW], {}],
    ["rehearsal_alarms", [{"last": {"main": "da1362306815dc5e", "verdict": "incomplete"}}, NOW], {}],
    ["rehearsal_alarms", [{"last": {"main": "da1362306815dc5e", "verdict": "green"}}, NOW], {}],
    ["rehearsal_alarms", [None, NOW], {}],
    ["required_contexts", [[{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "CI OK"}, {"context": "merge-gate"}]}}]], {}],
    ["required_contexts", [None], {}],
    ["required_contexts", [[{"type": "required_status_checks"}]], {}],
]


def call(module, name, args, kwargs):
    args, kwargs = list(args), dict(kwargs)
    if name == "candidate_alarms":
        jobs = args[1]
        args[1] = lambda run_id: jobs.get(str(run_id))
        if "dropped" in kwargs:
            gone = set(kwargs["dropped"])
            kwargs["dropped"] = lambda branch: branch in gone
    if name == "admission_alarms":
        args[1] = set(args[1])
    return getattr(module, name)(*args, **kwargs)
