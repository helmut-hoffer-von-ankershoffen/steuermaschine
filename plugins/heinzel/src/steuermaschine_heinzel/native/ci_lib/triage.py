# ruff: noqa: RUF005, SIM115  (the definitions down to the marker are moved verbatim; see tests/ci_verbatim.py)
"""Triage of red pull requests: one state and one suggested action each, the review verdict of a flagged pull request,
and the deadline view (counts and details of genuine failures). Pure functions over what GitHub answered.

Moved from the triage and digest modules of the scripted era; the commands in the action texts are sm commands.
"""

import os
import re
from collections import Counter

from ..queue_lib import known_failures

REAL_BAD = ("FAILURE", "ACTION_REQUIRED", "TIMED_OUT")


def is_focused(name):
    """A check run of core's focused-job.yml (a hosted dispatch): diagnostic, never blocking."""
    return (name or "").lower().startswith("focused ")


def classify_pr(roll):
    """ready | pending | deferred (mass-cancel plus aggregate 'CI OK' red: placeholder, not a code failure) | failing.
    A dispatched `Focused ...` check runs attach to the head too; they are diagnostics and never count."""
    roll = [c for c in roll or [] if not is_focused(c.get("name"))]
    if not roll:
        return "pending", []
    if any(c.get("status") not in ("COMPLETED", None) and c.get("conclusion") in (None, "") for c in roll):
        return "pending", []
    bad = [c["name"] for c in roll if c.get("conclusion") in REAL_BAD + ("CANCELLED",)]
    if not bad:
        return "ready", []
    cancelled = [c for c in roll if c.get("conclusion") == "CANCELLED"]
    real = sorted({c["name"] for c in roll if c.get("conclusion") in REAL_BAD and c["name"] not in ("CI OK", "coverage-floor")})
    if cancelled and not real:
        return "deferred", []
    return "failing", real or sorted(set(bad))


R = "helmut-hoffer-von-ankershoffen/heinzel"


VERDICT = re.compile(r"^#+\s*Verdict:\s*(\w[\w -]*)", re.M | re.I)


DEADLINE_ACTIVE = ("REQUESTED", "QUEUED", "IN_PROGRESS", "WAITING", "PENDING")


DEADLINE_FAILURE = REAL_BAD + ("STARTUP_FAILURE", "STALE")


DEADLINE_COMPLETE = DEADLINE_FAILURE + ("CANCELLED", "SUCCESS", "SKIPPED", "NEUTRAL")


DEADLINE_AGGREGATES = ("CI OK", "coverage-floor")


DEADLINE_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 100, states: OPEN) {
      totalCount
      nodes {
        number isDraft headRefOid
        commits(last: 1) {
          nodes { commit {
            oid
            statusCheckRollup {
              contexts(first: 100) {
                totalCount
                nodes {
                  __typename
                  ... on CheckRun { name status conclusion detailsUrl }
                  ... on StatusContext { context state }
                }
              }
            }
          } }
        }
      }
    }
  }
}
"""


def triage_pr(number, roll, known):
    """-> one line: `PR N STATE detail -> action`."""
    state, real = classify_pr(roll)
    if state == "ready":
        return f"PR {number} READY -> sm head ship {number} (queue merges when main is green)"
    if state == "pending":
        return f"PR {number} PENDING -> wait; no push (sm head push)"
    if state == "deferred":
        return f"PR {number} DEFERRED placeholder, not a code failure -> sm ci rerun when main CI is not starved"
    bead = known_failures.known_bead(real, known)
    if bead:
        return f"PR {number} KNOWN({bead}) {','.join(real)[:60]} -> sm ci one rerun {number} --job NAME --wait (once), no code change"
    return f"PR {number} FAIL {','.join(real)[:60]} -> sm ci why {number}; open a flaky bead and add it to known-failures.toml, or fix"


def review_verdict(path):
    """APPROVE / APPROVE-WITH-CHANGES style verdict word of a review file, or None."""
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None
    m = VERDICT.search(text)
    return m.group(1).strip().upper().replace(" ", "-") if m else None


def review_line(n, research_dir):
    v = review_verdict(os.path.join(research_dir, f"review-{n}.md"))
    if v and v.startswith("APPROVE"):
        return f"{n} CLEARED ({v}) -> sm head ship {n} --reviewed research/review-{n}.md"
    if v:
        return f"{n} NEEDS_REVIEW verdict {v}: address the findings, then re-review"
    return f"{n} NEEDS_REVIEW no research/review-{n}.md: dispatch an independent reviewer (read-only, not the builder's model)"


def complete_nodes(connection):
    nodes = connection["nodes"]
    count = connection["totalCount"]
    if type(count) is not int or not isinstance(nodes, list) or count != len(nodes):
        raise ValueError("incomplete connection")
    return nodes


def deadline_checks(rollup):
    return normalize_deadline_checks(complete_nodes(rollup["contexts"])) if rollup is not None else []


def normalize_deadline_checks(nodes):
    checks = []
    for check in nodes:
        kind = check["__typename"]
        if kind == "CheckRun":
            name, status, conclusion = check["name"], check["status"], check["conclusion"]
        elif kind == "StatusContext":
            name, state = check["context"], check["state"]
            status, conclusion = {
                "ERROR": ("COMPLETED", "FAILURE"), "FAILURE": ("COMPLETED", "FAILURE"),
                "SUCCESS": ("COMPLETED", "SUCCESS"), "EXPECTED": ("QUEUED", None),
                "PENDING": ("IN_PROGRESS", None),
            }[state]
        else:
            raise ValueError("unknown status context")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("incomplete check identity")
        if status == "COMPLETED":
            if conclusion not in DEADLINE_COMPLETE:
                raise ValueError("unknown completed check conclusion")
        elif status not in DEADLINE_ACTIVE or conclusion is not None:
            raise ValueError("unknown or inconsistent check status")
        checks.append({"name": name, "status": status, "conclusion": conclusion,
                       "detailsUrl": check.get("detailsUrl") if kind == "CheckRun" else None})
    return checks


def deferred_check(check, recorded, repo=R):
    url = check.get("detailsUrl")
    if not isinstance(url, str):
        return False
    match = re.fullmatch(r"https://github\.com/" + re.escape(repo)
                         + r"/actions/runs/([1-9][0-9]*)/job/[1-9][0-9]*(?:\?[^#]*)?", url)
    return bool(match and match.group(1) in recorded)


def classify_deadline_checks(checks, recorded=(), repo=R):
    """Report completed failures even while other required checks are active."""
    checks = [check for check in checks if not is_focused(check["name"])
              or check["name"].lower().startswith("focused tests (")]
    failures = [(check["name"], check["conclusion"]) for check in checks if check["conclusion"] in DEADLINE_FAILURE]
    genuine = {name for name, conclusion in failures if name not in DEADLINE_AGGREGATES or conclusion != "FAILURE"}
    if genuine:
        return "failing", sorted(genuine)

    # A failed aggregate gate can merely reflect a cancelled constituent job.
    cancelled = [check for check in checks if check["conclusion"] == "CANCELLED"]
    if failures and not cancelled:
        return "failing", sorted({name for name, _ in failures})
    if not checks or any(check["status"] != "COMPLETED" for check in checks):
        return "pending", []
    if cancelled:
        return ("deferred" if all(deferred_check(check, recorded, repo) for check in cancelled)
                else "cancellation-unverified"), []
    return "ready", []


# ---------------------------------------------------------------- new code below: the reads are done by the caller
def recorded_deferrals(text):
    """Run ids of the active defer list (run, branch, workflow per line); absence or an unreadable list proves nothing."""
    return {parts[0] for line in text.splitlines() if len(parts := line.split()) >= 3 and re.fullmatch(r"[1-9][0-9]*", parts[0])}


def deadline_snapshot(response, known, recorded, repo=R):
    """{number: (head, draft, state, failures)} from the GraphQL answer; raises when the inventory is incomplete."""
    if response.get("errors"):
        raise ValueError("partial GraphQL response")
    snapshot = {}
    for pr in complete_nodes(response["data"]["repository"]["pullRequests"]):
        number, draft, head = pr["number"], pr["isDraft"], pr["headRefOid"]
        if type(number) is not int or number < 1 or number in snapshot or type(draft) is not bool:
            raise ValueError("invalid or duplicate PR identity")
        (node,) = pr["commits"]["nodes"]
        commit = node["commit"]
        if not re.fullmatch(r"[0-9a-f]{40}", head) or head != commit["oid"]:
            raise ValueError("check rollup does not identify the PR head")
        state, failures = classify_deadline_checks(deadline_checks(commit["statusCheckRollup"]), recorded, repo)
        if state == "failing":
            failures = [failure for failure in failures if not known_failures.known_bead([failure], known)]
            state = "genuine" if failures else "known"
        snapshot[number] = (head, draft, state, failures)
    return snapshot


def deadline_rows(snapshot, due, limit):
    """(counts of the open non-draft pull requests by state, detail rows, how many details were left out)."""
    counts = Counter(state for _, draft, state, _ in snapshot.values() if not draft)
    genuine = sorted(number for number, (_, draft, state, _) in snapshot.items() if not draft and state == "genuine")
    detail = list(dict.fromkeys([*due, *genuine]))
    rows = []
    for number in detail[:limit]:
        if number not in snapshot:
            rows.append({"number": number, "head": "", "state": "not-open", "due": True, "draft": False, "failures": []})
            continue
        head, draft, state, failures = snapshot[number]
        rows.append({"number": number, "head": head, "state": state, "due": number in set(due), "draft": draft, "failures": failures})
    return counts, rows, max(len(detail) - limit, 0)
