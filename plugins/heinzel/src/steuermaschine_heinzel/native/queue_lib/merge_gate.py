#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""hz-merge-gate [audit]  |  flip [--dry-run]  |  harden

The owner's rule: nothing merges unless Sonar's quality gate passes and overall reliability, security and maintainability
are A, or at a coverage where the badge is red. `merge-gate` and `coverage-floor` (.github/workflows/merge-gate.yml in each
repo) judge that; this tool makes main require them, one at a time, the moment a repo passes.

`audit` prints one line per repo. `flip` adds each passing check to the repo's main ruleset, logs the date with hz-note and
only ever adds: it refuses a write that would drop a required check. hz-hour and hz-queue call it. `harden` empties the
ruleset's bypass list, so `gh pr merge --admin` can't pass the gate. The owner's emergency route is AGENTS.md section 8."""

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import edge  # moved: siblings are imported relatively, no sys.path entry
from . import hzlib  # noqa: E402

SONAR = "https://sonarcloud.io/api"
RATINGS = {"reliability_rating": "reliability", "security_rating": "security", "sqale_rating": "maintainability"}
LETTERS = {"1.0": "A", "2.0": "B", "3.0": "C", "4.0": "D", "5.0": "E"}
PUBLIC = ("heinzel", "heinzel-plugins", "heinzel-acme")
PRIVATE = {
    "heinzel-sf": "private: coverage-floor required by merge train only; GitHub enforces nothing without Pro; no Sonar project  <<< OWNER DECISION",
    "heinzel-management": "private, no CI: nothing to gate",
}
ACTIONS_APP = 15368  # GitHub Actions: only its check runs satisfy the requirement, not a status anyone can post
BADGE = "https://raw.githubusercontent.com/{repo}/python-coverage-comment-action-data/endpoint.json"
RED_BELOW = re.compile(r"^\s*MINIMUM_ORANGE:\s*[\"']?(\d+)", re.M)


def http_json(url: str):
    try:
        with urllib.request.urlopen(url, timeout=20) as reply:  # noqa: S310 - fixed https bases
            return json.load(reply)
    except (OSError, ValueError):
        return None


def sonar_state(repo: str) -> tuple[bool, str]:
    """(passes, words). No answer is not a pass."""
    key = f"{hzlib.OWNER}_{repo}"
    status = http_json(f"{SONAR}/qualitygates/project_status?projectKey={key}")
    measures = http_json(f"{SONAR}/measures/component?component={key}&metricKeys={','.join(RATINGS)}")
    if not status or not measures:
        return False, "Sonar UNAVAILABLE"
    gate = status.get("projectStatus", {}).get("status", "?")
    values = {m["metric"]: m.get("value") for m in measures.get("component", {}).get("measures", [])}
    letters = {label: LETTERS.get(values.get(metric) or "", "?") for metric, label in RATINGS.items()}
    words = f"gate {gate}, " + " ".join(f"{label} {letter}" for label, letter in letters.items())
    return gate == "OK" and set(letters.values()) == {"A"}, words


def coverage_state(repo: str) -> tuple[bool, str]:
    """(badge isn't red, words)."""
    badge = http_json(BADGE.format(repo=f"{hzlib.OWNER}/{repo}"))
    if not badge or "message" not in badge:
        return False, "coverage UNKNOWN"
    return badge.get("color") != "red", f"coverage {badge['message']} {badge.get('color')}"


def ruleset(repo: str) -> dict | None:
    """The active branch ruleset for main that has a required_status_checks rule."""
    full = f"{hzlib.OWNER}/{repo}"
    for item in hzlib.ghj("api", f"repos/{full}/rulesets", default=[]) or []:
        data = hzlib.ghj("api", f"repos/{full}/rulesets/{item['id']}", default={}) or {}
        if data.get("enforcement") == "active" and any(r["type"] == "required_status_checks" for r in data.get("rules", [])):
            return data
    return None


def required_in(data: dict) -> list[dict]:
    return next(r for r in data["rules"] if r["type"] == "required_status_checks")["parameters"]["required_status_checks"]


def write_ruleset(repo: str, data: dict, before: set[str]) -> bool:
    """PUT the ruleset back. Refuses to drop a required check: the owner allowed adding, nothing else."""
    if not before <= {c["context"] for c in required_in(data)}:
        print(f"{repo}: refusing a write that would drop a required check", file=sys.stderr)
        return False
    body = {k: data[k] for k in ("name", "target", "enforcement", "conditions", "rules", "bypass_actors") if k in data}
    try:
        done = edge.run(
            ["gh", "api", "-X", "PUT", f"repos/{hzlib.OWNER}/{repo}/rulesets/{data['id']}", "--input", "-"],
            input=json.dumps(body), capture_output=True, text=True, check=False, timeout=hzlib.GH_WRITE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(f"{repo}: the ruleset write timed out after {hzlib.GH_WRITE_TIMEOUT}s; its state is unknown, read it again", file=sys.stderr)
        return False
    if done.returncode != 0:
        print(f"{repo}: GitHub refused the ruleset: {done.stdout.strip()} {done.stderr.strip()}", file=sys.stderr)
    return done.returncode == 0


def workflow_on_main(repo: str) -> bool:
    """A required check nothing reports would block every merge, so the workflow must be on main first."""
    return hzlib.gh("api", f"repos/{hzlib.OWNER}/{repo}/contents/.github/workflows/merge-gate.yml")[0] == 0


def reported_on_a_pull_request(repo: str, check: str) -> bool:
    """Proof the check reaches a pull request's head and can pass there, as the requirement will look for it."""
    full = f"{hzlib.OWNER}/{repo}"
    pulls = hzlib.ghj("pr", "list", "-R", full, "--state", "all", "-L", "15", "--json", "headRefOid", default=[]) or []
    for pull in pulls:
        runs = hzlib.ghj("api", f"repos/{full}/commits/{pull['headRefOid']}/check-runs?check_name={check}", default={}) or {}
        if any(r.get("conclusion") == "success" and r.get("app", {}).get("id") == ACTIONS_APP for r in runs.get("check_runs", [])):
            return True
    return False


def passing(repo: str) -> dict[str, tuple[bool, str]]:
    return {"merge-gate": sonar_state(repo), "coverage-floor": coverage_state(repo)}


def flip(dry_run: bool = False) -> list[str]:
    """Require every check that passes now. Returns the flips, in words."""
    flipped = []
    for repo in PUBLIC:
        data = ruleset(repo)
        if not data or not workflow_on_main(repo):
            continue
        for check, (ok, _words) in passing(repo).items():
            have = required_in(data)
            names = {c["context"] for c in have}
            if not ok or check in names or not reported_on_a_pull_request(repo, check):
                continue
            line = f"{repo}: {check} is required from now"
            if dry_run:
                flipped.append(f"would flip {line}")
                continue
            have.append({"context": check, "integration_id": ACTIONS_APP})
            if write_ruleset(repo, data, names):
                flipped.append(line)
                hzlib.note(f"sm ci merge-gate: {line}")
    return flipped


def harden() -> None:
    for repo in PUBLIC:
        data = ruleset(repo)
        if data and data.get("bypass_actors"):
            data["bypass_actors"] = []
            ok = write_ruleset(repo, data, {c["context"] for c in required_in(data)})
            print(f"{repo}: bypass list {'emptied' if ok else 'NOT changed'}")
            if ok:
                hzlib.note(f"sm ci merge-gate: {repo}: admin bypass removed from main's ruleset")


def status_lines() -> list[str]:
    """One line per repo; `NOT A` or `RED` or a bypass is what the hourly reader must see."""
    out = []
    for repo in PUBLIC:
        states = passing(repo)
        data = ruleset(repo)
        names = {c["context"] for c in required_in(data)} if data else set()
        marks = [] if states["merge-gate"][0] else ["NOT A"]
        marks += [] if states["coverage-floor"][0] else ["RED"]
        marks += ["BYPASS"] if data and data.get("bypass_actors") else []
        marks += ["NO RULESET"] if not data else []
        need = [c for c in states if c in names]
        missing = [c for c in states if c not in names]
        out.append(
            f"{repo}: {'; '.join(s[1] for s in states.values())}; required: {', '.join(need) or 'neither'}"
            + (f"; not yet: {', '.join(missing)}" if missing else "")
            + (f"  <<< {', '.join(marks)}" if marks else "")
        )
    return out + [f"{repo}: {why}" for repo, why in PRIVATE.items()]


# Only hz-ultra calls these helpers; normal audit/flip/harden policy is unchanged.
ULTRA_ADMIN = {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}


def ultra_main_rulesets(api) -> list[dict]:
    """Read the repository rulesets GitHub actually applies to core main; unknown/inherited policy refuses ultra."""
    rows = api("GET", f"repos/{hzlib.CORE}/rules/branches/main")
    if not isinstance(rows, list) or not rows:
        raise ValueError("main protection evidence is unavailable")
    ids = set()
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("ruleset_id")) is not int
                or row.get("ruleset_source_type") != "Repository" or row.get("ruleset_source") != hzlib.CORE):
            raise ValueError("unknown or inherited main protection")
        ids.add(row["ruleset_id"])
    if not 1 <= len(ids) <= 5:
        raise ValueError("unexpected main ruleset count")
    result = []
    for identity in sorted(ids):
        rule = api("GET", f"repos/{hzlib.CORE}/rulesets/{identity}")
        if (not isinstance(rule, dict) or rule.get("id") != identity or rule.get("target") != "branch"
                or rule.get("enforcement") != "active" or not isinstance(rule.get("rules"), list)
                or not isinstance(rule.get("bypass_actors"), list)):
            raise ValueError("main ruleset is unreadable")
        result.append(rule)
    if not any(r.get("type") == "required_status_checks" for rule in result for r in rule["rules"]):
        raise ValueError("required checks are unavailable")
    return result


def ultra_policy(rule: dict) -> dict:
    """Stable policy identity, excluding the temporary actor and server metadata; never used as a restore payload."""
    return {k: rule[k] for k in ("name", "target", "enforcement", "conditions", "rules") if k in rule}


def ultra_bypass(identity: int, enabled: bool, api, before_write=None) -> None:
    """Change only the owned PR-only actor, fresh-reading and retaining every other policy field and actor.

    The update endpoint's other fields are optional. Sending only bypass_actors avoids replaying a stale
    rules/name/conditions snapshot over a concurrent edit. Restoration removes only our exact tuple.
    """
    endpoint = f"repos/{hzlib.CORE}/rulesets/{identity}"
    current = api("GET", endpoint)
    if not isinstance(current, dict) or current.get("id") != identity or not isinstance(current.get("bypass_actors"), list):
        raise ValueError("cannot read temporary bypass ownership")
    actors = current["bypass_actors"]
    if any(not isinstance(actor, dict) for actor in actors):
        raise ValueError("malformed bypass actors")
    if enabled:
        if any(a.get("actor_type") == "RepositoryRole" and a.get("actor_id") == 5 for a in actors):
            raise ValueError("admin actor already exists; ultra cannot own it")
        if before_write is None:
            raise ValueError("owned journal must be armed before enabling a bypass")
        expected = [*actors, ULTRA_ADMIN]
        before_write()
    else:
        expected = [a for a in actors if a != ULTRA_ADMIN]
        if expected == actors:
            return
    api("PUT", endpoint, {"bypass_actors": expected})
    after = api("GET", endpoint)
    observed = after.get("bypass_actors") if isinstance(after, dict) else None
    if (not isinstance(observed, list) or any(a not in observed for a in expected)
            or (enabled and observed.count(ULTRA_ADMIN) != 1) or (not enabled and ULTRA_ADMIN in observed)):
        raise ValueError("temporary bypass readback is unknown; recover required")


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "audit"
    if command == "audit":
        print("\n".join(status_lines()))
    elif command == "flip":
        for line in flip("--dry-run" in argv):
            print(line)
    elif command == "harden":
        harden()
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0

