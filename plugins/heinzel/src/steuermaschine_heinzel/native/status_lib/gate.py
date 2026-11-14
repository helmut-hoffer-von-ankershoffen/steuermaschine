"""The merge gate per repository, read-only: Sonar's verdict and ratings (through the core Sonar provider), the coverage
badge (the same injected HTTP GET), and which of the two
gate checks main requires. `NOT A`, `RED`, `BYPASS` or `NO RULESET` is what the reader must act on;
`RULESET UNREAD` means GitHub did not answer (never read as `NO RULESET`).

Heinzelmännchen knowledge: the checks `merge-gate` and `coverage-floor` come from their own workflow, attached to a
pull request beside the CI suite; a repository requires each of them only from the day it passes.
"""

from __future__ import annotations

import json
from typing import Any

from steuermaschine.github.client import Gh
from steuermaschine.github.sonar import Fetch, Sonar

RATINGS = {"reliability_rating": "reliability", "security_rating": "security", "sqale_rating": "maintainability"}
LETTERS = {"1.0": "A", "2.0": "B", "3.0": "C", "4.0": "D", "5.0": "E"}
GATE_CHECKS = ("merge-gate", "coverage-floor")
ADVISORY_REVIEW = frozenset({"review", "attempt", "Claude review"})
BADGE = "https://raw.githubusercontent.com/{repo}/python-coverage-comment-action-data/endpoint.json"


def badge(fetch: Fetch, full: str, timeout: float = 20.0) -> Any:
    """The coverage badge document of a repository (a public JSON file on its data branch). None without an answer."""
    status, body = fetch(BADGE.format(repo=full), {"Accept": "application/json"}, timeout)
    if status != 200:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def sonar_state(sonar: Sonar) -> tuple[bool, str]:
    """(passes, words) from the core Sonar provider. No answer is not a pass."""
    status, _ = sonar.get("qualitygates/project_status", {"projectKey": sonar.key})
    measures, _ = sonar.get("measures/component", {"component": sonar.key, "metricKeys": ",".join(RATINGS)})
    if not status or not measures:
        return False, "Sonar UNAVAILABLE"
    gate = status.get("projectStatus", {}).get("status", "?")
    values = {m["metric"]: m.get("value") for m in measures.get("component", {}).get("measures", [])}
    letters = {label: LETTERS.get(values.get(metric) or "", "?") for metric, label in RATINGS.items()}
    words = f"gate {gate}, " + " ".join(f"{label} {letter}" for label, letter in letters.items())
    return gate == "OK" and set(letters.values()) == {"A"}, words


def coverage_state(fetch: Fetch, full: str) -> tuple[bool, str]:
    """(the badge is not red, words)."""
    doc = badge(fetch, full)
    if not doc or "message" not in doc:
        return False, "coverage UNKNOWN"
    return doc.get("color") != "red", f"coverage {doc['message']} {doc.get('color')}"


def ruleset(gh: Gh, full: str) -> tuple[dict[str, Any] | None, str]:
    """(the active branch ruleset that has a required_status_checks rule or None, why it could not be read)."""
    items, why = gh.try_json(["api", f"repos/{full}/rulesets"])
    if why and "HTTP 404" not in why:
        return None, why
    for item in items if isinstance(items, list) else []:
        data, _ = gh.try_json(["api", f"repos/{full}/rulesets/{item['id']}"])
        if isinstance(data, dict) and data.get("enforcement") == "active" and any(r.get("type") == "required_status_checks" for r in data.get("rules", [])):
            return data, ""
    return None, ""


def required_in(data: dict[str, Any]) -> list[dict[str, Any]]:
    rule = next(r for r in data["rules"] if r["type"] == "required_status_checks")
    checks: list[dict[str, Any]] = rule["parameters"]["required_status_checks"]
    return checks


def gate_row(sonar: Sonar, fetch: Fetch, gh: Gh, owner: str, repo: str) -> dict[str, Any]:
    """One repository: the words, what is required, and the marks. `sonar` is the core provider for its project."""
    states = {"merge-gate": sonar_state(sonar), "coverage-floor": coverage_state(fetch, f"{owner}/{repo}")}
    data, unread = ruleset(gh, f"{owner}/{repo}")
    names = {c["context"] for c in required_in(data)} if data else set()
    marks = [] if states["merge-gate"][0] else ["NOT A"]
    marks += [] if states["coverage-floor"][0] else ["RED"]
    marks += ["BYPASS"] if data and data.get("bypass_actors") else []
    marks += [] if data else ["RULESET UNREAD"] if unread else ["NO RULESET"]
    return {"repo": repo, "sonar": states["merge-gate"][1], "coverage": states["coverage-floor"][1],
            "required": [c for c in states if c in names], "not_yet": [c for c in states if c not in names], "marks": marks, "note": "",
            "unknown": f"ruleset of {repo}: {unread}" if unread else ""}


def gate_line(row: dict[str, Any]) -> str:
    if row.get("note"):
        return f"{row['repo']}: {row['note']}"
    return (f"{row['repo']}: {row['sonar']}; {row['coverage']}; required: {', '.join(row['required']) or 'neither'}"
            + (f"; not yet: {', '.join(row['not_yet'])}" if row["not_yet"] else "")
            + (f"  <<< {', '.join(row['marks'])}" if row["marks"] else ""))


def advisory_checks_from(rules: Any) -> set[str]:
    """Checks whose failure does not hold a pull request: the review, and the gate checks until main requires them.
    GitHub's rules decide. When they cannot be read only the review is advisory: never a looser gate."""
    if not isinstance(rules, list):
        return set(ADVISORY_REVIEW)
    required = {check["context"] for rule in rules if rule.get("type") == "required_status_checks"
                for check in rule["parameters"]["required_status_checks"]}
    return set(ADVISORY_REVIEW) | (set(GATE_CHECKS) - required)


def advisory_checks(gh: Gh, full: str) -> set[str]:
    rules, _ = gh.try_json(["api", f"repos/{full}/rules/branches/main"])
    return advisory_checks_from(rules)
