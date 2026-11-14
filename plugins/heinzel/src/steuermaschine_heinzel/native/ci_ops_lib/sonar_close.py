"""Close reviewed main findings, or exact reviewed PR issues, through `sonar`.

Default: preview main's legacy file/rule rows in the reviewed manifest (setting `ci_ops_sonar_closures_file`).
--config selects another reviewed project manifest; all exact-scope checks remain mandatory.
PR: --pull-request NUMBER --issue KEY [--issue KEY ...] selects only exact,
PR-scoped reviewed rows. Only --apply changes Sonar; every closure has a reason.

Moved from the former closing tool with every check unchanged. What changed: the `sonar` CLI is started through the
`Commands` provider (`Edge`), the options arrive typed instead of through argparse, and what was printed is collected
(`Edge.out`, `Edge.written`) for the result model. The CLI is signed in by itself: no token passes through here.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

TRANSITIONS = {"falsepositive", "accept"}


class Edge:
    """How the functions below reach the world. `bind` sets it for one command."""

    def __init__(self, commands: Any = None, out: Callable[[str], None] | None = None) -> None:
        self.commands, self.out = commands, out or (lambda text: None)
        self.plan: list[tuple[dict, dict]] = []  # the validated plan: (reviewed row, open issue)
        self.written: list[tuple[str, str]] = []  # (issue key, "comment" | "transition") for every write that succeeded, in order
        self.open_before: int | None = None
        self.open_after: int | None = None
        self.project = ""


EDGE = Edge()


def bind(edge: Edge) -> None:
    global EDGE
    EDGE = edge


class Stop(ValueError):
    """Refuse an ambiguous or unreviewed closure before any mutation."""


def api(method: str, endpoint: str, data: dict[str, str] | None = None) -> str:
    command = ["sonar", "api", method, endpoint]
    if data is not None:
        command += ["--data", json.dumps(data)]
    done = EDGE.commands.run(command, timeout=60)
    if done.rc != 0:
        raise Stop(f"sonar api {method} {endpoint}: {(done.err or done.out).strip()[:200]}")
    return done.out


def open_findings(project: str, pull_request: int | None = None, keys: list[str] | None = None) -> tuple[int, list[dict]]:
    params = {"componentKeys": project, "types": "VULNERABILITY,BUG", "issueStatuses": "OPEN,CONFIRMED", "ps": "500"}
    if pull_request is not None:
        params["pullRequest"] = str(pull_request)
        params["issues"] = ",".join(keys or [])
    found = json.loads(api("get", "/api/issues/search?" + urlencode(params)))
    if not isinstance(found, dict) or type(found.get("total")) is not int or not isinstance(found.get("issues"), list):
        raise Stop("invalid Sonar search response")
    return found["total"], found["issues"]


def required_text(row: dict, name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise Stop(f"reviewed {name} must be a nonempty string without surrounding whitespace")
    return value


def reviewed_rows(config: dict) -> list[dict]:
    required_text(config, "project")
    rows = config.get("closure")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise Stop("closure must be an array of reviewed rows")
    seen = set()
    for row in rows:
        if set(row) - {"file", "rule", "reason", "transition", "pull_request", "issue"}:
            raise Stop("unknown reviewed closure field")
        for field in ("file", "rule", "reason"):
            required_text(row, field)
        if row.get("transition", "falsepositive") not in TRANSITIONS:
            raise Stop("transition must be falsepositive or accept")
        if "pull_request" in row or "issue" in row:
            if type(row.get("pull_request")) is not int or row["pull_request"] <= 0:
                raise Stop("exact issue rows require a positive pull_request")
            issue = required_text(row, "issue")
            if "," in issue or any(c.isspace() for c in issue):
                raise Stop("reviewed issue must be one exact key")
            if ":" not in row["rule"] or not all(row["rule"].split(":")):
                raise Stop("PR rows require the full Sonar rule, such as python:S5332")
            required_text(row, "transition")
            identity = (row["pull_request"], issue)
            if identity in seen:
                raise Stop("duplicate reviewed PR issue")
            seen.add(identity)
    return rows


def exact_plan(project: str, pull_request: int, keys: list[str], rows: list[dict], total: int, issues: list[dict]) -> list[tuple[dict, dict]]:
    if total != len(keys) or len(issues) != len(keys):
        raise Stop("PR search must return every requested open issue exactly once; no changes made")
    by_key = {}
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("key") not in keys or issue["key"] in by_key:
            raise Stop("PR search returned an unexpected or duplicate issue")
        by_key[issue["key"]] = issue
    plan = []
    for row in rows:
        issue = by_key[row["issue"]]
        if issue.get("project") != project or issue.get("component") != project + ":" + row["file"]:
            raise Stop(f"project/file mismatch for {row['issue']}")
        if issue.get("rule") != row["rule"]:
            raise Stop(f"rule mismatch for {row['issue']}")
        # Some Sonar versions omit PR metadata. The scoped search is authoritative;
        # if metadata is returned it must agree too, never silently contradict it.
        if "pullRequest" in issue and str(issue["pullRequest"]) != str(pull_request):
            raise Stop(f"pull request mismatch for {row['issue']}")
        if issue.get("issueStatus", issue.get("status")) not in {"OPEN", "CONFIRMED"}:
            raise Stop(f"issue is not open: {row['issue']}")
        if issue.get("type") not in {"VULNERABILITY", "BUG"}:
            raise Stop(f"unexpected issue type for {row['issue']}")
        plan.append((row, issue))
    return plan


def rule_matches(reviewed: str, found: str) -> bool:
    """Exact: a full rule (python:S5332) equals the issue's rule; a bare key (S5332) equals the key part of it."""
    return found == reviewed if ":" in reviewed else found.partition(":")[2] == reviewed


def main_plan(project: str, rows: list[dict], issues: list[dict]) -> list[tuple[dict, dict]]:
    """The main route: every open finding of a reviewed file and rule. Each planned issue is checked on its own,
    as on the PR route: project, open status and type (bug or vulnerability), so nothing depends on the server
    honouring the search filter. A mismatch refuses the whole plan."""
    plan = []
    for row in rows:
        for issue in issues:
            if not isinstance(issue, dict) or issue.get("component") != project + ":" + row["file"] or not rule_matches(row["rule"], str(issue.get("rule", ""))):
                continue
            if issue.get("project", project) != project:
                raise Stop(f"project mismatch for {issue.get('key')}")
            if issue.get("issueStatus", issue.get("status")) not in {"OPEN", "CONFIRMED"}:
                raise Stop(f"issue is not open: {issue.get('key')}")
            if issue.get("type") not in {"VULNERABILITY", "BUG"}:
                raise Stop(f"unexpected issue type for {issue.get('key')}")
            plan.append((row, issue))
    return plan


def close_reviewed(args: Any) -> int:
    """`args`: config (Path), apply (bool), pull_request (int | None), issue (list of keys)."""
    if args.pull_request is None and args.issue:
        raise Stop("--issue requires --pull-request")
    if args.pull_request is not None and (args.pull_request <= 0 or not args.issue):
        raise Stop("--pull-request requires a positive number and at least one --issue")
    if len(args.issue) > 500 or len(set(args.issue)) != len(args.issue) or any(not key or "," in key or any(c.isspace() for c in key) for key in args.issue):
        raise Stop("--issue must name distinct exact keys")
    config = tomllib.loads(args.config.read_text())
    rows = reviewed_rows(config)
    project = EDGE.project = config["project"]
    if args.pull_request is not None:
        selected = [row for row in rows if row.get("pull_request") == args.pull_request and row.get("issue") in args.issue]
        if len(selected) != len(args.issue):
            raise Stop("every requested key must have one reviewed row for this pull request")
        total, issues = open_findings(project, args.pull_request, args.issue)
        plan = exact_plan(project, args.pull_request, args.issue, selected, total, issues)
    else:
        total, issues = open_findings(project)
        plan = main_plan(project, [row for row in rows if "pull_request" not in row], issues)
    # Validate the complete plan before the first comment or transition.
    EDGE.plan, EDGE.open_before = plan, total
    EDGE.out(f"open before: {total}")
    for row, issue in plan:
        EDGE.out(f"{'closing' if args.apply else 'would close'} {issue['key']} {row['file']}:{issue.get('line')} {row['rule']}")
        if args.apply:
            api("post", "/api/issues/add_comment", {"issue": issue["key"], "text": row["reason"]})
            EDGE.written.append((issue["key"], "comment"))
            api("post", "/api/issues/do_transition", {"issue": issue["key"], "transition": row.get("transition", "falsepositive")})
            EDGE.written.append((issue["key"], "transition"))
    if args.apply:
        EDGE.open_after = open_findings(project, args.pull_request, args.issue)[0]
        EDGE.out(f"open after: {EDGE.open_after}")
    return 0


REFUSALS = (Stop, OSError, ValueError, KeyError, TypeError)  # what the command turns into a refusal (exit 1), as before
