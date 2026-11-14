"""Record real inputs and the old tool's output once into tests/fixtures/status (run by hand; the tests need no network).

    .venv/bin/python plugins/heinzel/tests/record_status_fixtures.py [NAME]

Reads GitHub (graphql pull request lists, branch rules, rulesets, the byte sizes of the focused-plan variables), Sonar
and the coverage badge once, read-only. Then imports the old status and merge-gate modules from the tools directory and
calls their pure functions (rows, render, slot_line) on what was read, plus the old read-only gate audit. The old
slot-size cache is not touched. A pull request body is cut down to its summary sentence and an action text to 80
characters, so the fixture carries no free text beyond what the table shows. Variable values are never stored: a
value of the recorded byte size made of `x` stands in.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent / "fixtures" / "status"
TOOLS = Path("~/Code/heinzel-management/tools").expanduser()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def load(name: str):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(TOOLS / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=60).stdout


def main(name: str) -> None:
    from steuermaschine_heinzel.native.status_lib import gate, table

    sys.path.insert(0, str(TOOLS))
    old, old_gate = load("hz-status"), load("hz-merge-gate")
    owner, now = old.OWNER, time.time()
    repos = {"heinzel": "", "heinzel-plugins": "plugins ", "heinzel-acme": "acme "}
    answers: dict[str, object] = {}
    prs = {}
    for repo in repos:
        raw = json.loads(run(["gh", "api", "graphql", "-f", f"query={table.QUERY}", "-F", f"owner={owner}", "-F", f"repo={repo}"]))
        for node in raw["data"]["repository"]["pullRequests"]["nodes"]:
            node["body"] = old.summary(node["body"], node["title"]) + "."
        answers[f"graphql repo={repo}"] = raw
        prs[repo] = [old.normalise(n) for n in raw["data"]["repository"]["pullRequests"]["nodes"]]
        answers[f"repos/{owner}/{repo}/rules/branches/main"] = json.loads(run(["gh", "api", f"repos/{owner}/{repo}/rules/branches/main"]) or "null")
        sets = json.loads(run(["gh", "api", f"repos/{owner}/{repo}/rulesets"]) or "[]")
        answers[f"repos/{owner}/{repo}/rulesets"] = sets
        for item in sets:
            answers[f"repos/{owner}/{repo}/rulesets/{item['id']}"] = json.loads(run(["gh", "api", f"repos/{owner}/{repo}/rulesets/{item['id']}"]))
    sizes = json.loads(run(["gh", "api", f"repos/{owner}/heinzel/actions/variables?per_page=100", "--jq",
                            '[.variables[] | select(.name | startswith("FOCUSED_CI_PLANS")) | {(.name): (.value | utf8bytelength)}] | add // {}']))
    answers["actions/variables"] = {"variables": [{"name": k, "value": "x" * v} for k, v in sizes.items()]}
    curl: dict[str, object] = {}
    for repo in repos:
        key = f"{owner}_{repo}"
        for url in (f"{gate.SONAR}/qualitygates/project_status?projectKey={key}", f"{gate.SONAR}/measures/component?component={key}&metricKeys={','.join(gate.RATINGS)}",
                    gate.BADGE.format(repo=f"{owner}/{repo}")):
            out = run(["curl", "--disable", "--silent", "--fail", "--location", "--max-time", "20", url])
            curl[url] = json.loads(out) if out.strip() else None
    actions_all = tomllib.loads((TOOLS / "pr-actions.toml").read_text())
    keys = {f"{prefix}#{pr['number']}" for repo, prefix in repos.items() for pr in prs[repo]} | {str(pr["number"]) for pr in prs["heinzel"]}
    actions = {k: {**v, "action": str(v.get("action", ""))[:80]} for k, v in actions_all.items() if k in keys}
    stamp = datetime.fromtimestamp(now, UTC)
    old.REPOS = repos
    ignored = {repo: gate.advisory_checks_from(answers[f"repos/{owner}/{repo}/rules/branches/main"]) for repo in repos}
    rows = old.rows(prs, actions, stamp, ignored)
    gate_lines = [line for line in old_gate.status_lines() if line.split(":")[0] in repos]
    text = "**Merge gate**\n\n" + "\n".join([*gate_lines, old.slot_line(sizes)]) + "\n\n" + old.render(rows, False)
    markdown = "**Merge gate**\n\n" + "\n".join("- " + line for line in [*gate_lines, old.slot_line(sizes)]) + "\n\n" + old.render(rows, True)
    stale = [f"{r['pr']}  unchanged {old.age(r['updated'], stamp)}  {r['owner']}  {r['summary']}" for r in old.stale(rows, stamp)]
    doc = {"now": now, "owner": owner, "gh": answers, "curl": curl, "actions": actions, "old_text": text, "old_markdown": markdown, "old_stale": stale}
    path = HERE / f"status-{name}.json"
    path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(f"wrote {path} ({path.stat().st_size} bytes): {len(rows)} pull requests")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else time.strftime("%Y%m%dT%H%M"))
