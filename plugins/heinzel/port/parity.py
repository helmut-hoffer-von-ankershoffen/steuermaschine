#!/usr/bin/env python3
"""Old tool against native command, on fakes: same state file in, same scripted `gh`, then compare what each printed,
its exit code, the calls it made in order (the transcript), and the files it left.

    port/parity.py OLD_TOOLS_DIR [SCENARIO ...] [--show]     exit 1 when a scenario differs beyond the declared rules

Nothing real is touched: each side runs in its own throwaway HOME, `gh` and `bd` are port/fakebin/tool.py first on
PATH, the owner push is off. The old side runs a copy of the old tools placed in that HOME. The native side is the
`sm` command tree started by port/native_cli.py, which sets the test bypass of the review gate and refuses to run
outside this harness.

Declared differences (applied to the OLD side before comparing; everything else must be equal):
  N1  the command name in printed hints and log lines: `hz-queue` -> `sm queue`; `hz-queue run started` -> `queue run started`
  N2  times: log line stamps, note line stamps, epoch fields in the state file, tick duration, the process id
  N3  the throwaway HOME path
  N4  `drop` printed nothing; native prints one line per PR (`dropped KEY` or `not in the queue: KEY`)
  N6  a usage error (exit 2 on both sides): the text is the argument parser's on the old side, `sm`'s on the native side
  N5  a command that prints nothing on stdout prints one empty line (the core prints the rendered result)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
PYTHON = REPO / ".venv" / "bin" / "python"  # the environment `sm` is installed in
FIXTURE = HERE.parent / "tests" / "fixtures" / "queue" / "state-2026-11-12.json"
CORE = "helmut-hoffer-von-ankershoffen/heinzel"
HEAD = "e9c943810ea75cfad155a431a2f168b2a9559828"
MAIN = "1111111111111111111111111111111111111111"
TIME_KEYS = {"added", "ready_since", "waiting_since", "last_batch", "at", "first", "last", "notified"}


def gh(needle: str, out="", rc: int = 0, err: str = "", seq=None) -> dict:
    return {"tool": "gh", "needle": needle, **({"seq": seq} if seq else {"out": out, "rc": rc, "err": err})}


GREEN = [{"name": name, "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-11-12T20:00:00Z", "workflowName": "CI",
          "detailsUrl": f"https://github.com/{CORE}/actions/runs/77/job/1"} for name in ("CI OK", "merge-gate", "coverage-floor")]
RULES = [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": c} for c in ("CI OK", "merge-gate", "coverage-floor")]}}]
MAIN_RUNS = [{"status": "completed", "conclusion": "success", "databaseId": 500, "headSha": MAIN, "attempt": 1, "displayTitle": "feat: x (#700)",
              "createdAt": "2026-11-12T19:00:00Z", "event": "push", "headBranch": "main"}]
DIFF = "diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n+++ b/src/x.py\n@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n"
WEAKER = "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -10,1 +10,2 @@\n   steps:\n+    continue-on-error: true\n"
PR_OPEN = {"state": "OPEN", "isDraft": False, "headRefOid": HEAD, "headRefName": "fix/x", "mergeStateStatus": "CLEAN", "statusCheckRollup": GREEN,
           "title": "fix: x", "body": "one fix"}
PR_MERGED = {**PR_OPEN, "state": "MERGED"}
BASE = [gh("pr diff 801", DIFF), gh("pr view 801", PR_OPEN), gh("rules/branches/main", RULES), gh("run list", MAIN_RUNS)]


OLD_STAMP = (datetime.now(timezone.utc) - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
STARVED = [gh("--status queued --branch main", [{"createdAt": OLD_STAMP}]), gh("--branch main --event push", [{"databaseId": 9, "status": "queued"}]),
           gh("actions/runs/9/jobs", "0\n"), gh("pr list", []), gh("pr view 801", PR_OPEN), gh("run list", [])]
CANDIDATE = [gh("--status queued --limit 50", [{"databaseId": 77, "headBranch": "release/v0.9.40", "status": "queued", "createdAt": OLD_STAMP}]),
             gh("--status in_progress --limit 50", []), gh("pr view 801", PR_OPEN), gh("run list", [])]
OLD_INTERPRETER = {"hz-ship": ["/bin/sh"]}  # everything else is started with this Python
SHIP_NAMES = {"hz-push-gate <PR>": "sm head push <PR>", "hz-push-gate ": "sm head push ", "hz-ship:": "sm head ship:", "hz-ship ": "sm head ship ",
              "`hz-wait`": "`sm go`", "via hz-gate": "via sm gate run", "hz-diff-guard": "sm head guard"}


def queued(key: str = "801", **more) -> dict:
    return {"key": key, "repo": CORE, "pr": key, "after": [], "status": "queued", "detail": "", "reruns": 0, "noted": "", "added": 1794516764,
            "fixes_main": False, "diagnostics_main": "", "safety_fix_main": "", "mitigation_main": "", "mitigation_evidence": "",
            "reviewed_progress_main": "", "progress_anchor": {}, "reviewed_head": "", "bead": "", "waiting_since": None, "ready_since": None,
            "wait_reason": "", **more}


def state(*extra: dict, **fields) -> dict:
    data = json.loads(FIXTURE.read_text())
    data["items"] += list(extra)
    data.update(fields)
    return data


# name -> (old argv, native argv, state file, answers)
SCENARIOS: dict[str, tuple[list[str], list[str], dict, list[dict]]] = {
    "list": (["list"], ["queue", "list"], state(), []),
    "list-active": (["list", "--active"], ["queue", "list", "--active"], state(queued()), []),
    "why": (["why"], ["queue", "why"], state(queued()), BASE),
    "add-pass": (["add", "801"], ["queue", "add", "801"], state(), BASE),
    "add-after-bead": (["add", "--after", "796", "--bead", "abc", "801"], ["queue", "add", "--after", "796", "--bead", "abc", "801"], state(), BASE),
    "add-flagged-refused": (["add", "801"], ["queue", "add", "801"], state(), [gh("pr diff 801", WEAKER), *BASE[1:]]),
    "add-flagged-reviewed": (["add", "--reviewed", "review: research/review-pr801.md", "801"],
                             ["queue", "add", "--reviewed", "review: research/review-pr801.md", "801"], state(), [gh("pr diff 801", WEAKER), *BASE[1:]]),
    "add-reviewed-head": (["add", "--reviewed", "review: research/review-pr801.md", "--reviewed-head", HEAD, "801"],
                          ["queue", "add", "--reviewed", "review: research/review-pr801.md", "--reviewed-head", HEAD, "801"], state(), BASE),
    "add-reviewed-head-moved": (["add", "--reviewed", "r", "--reviewed-head", "f" * 40, "801"],
                                ["queue", "add", "--reviewed", "r", "--reviewed-head", "f" * 40, "801"], state(), BASE),
    "add-reviewed-head-empty": (["add", "--reviewed", "r", "--reviewed-head", "", "801"], ["queue", "add", "--reviewed", "r", "--reviewed-head", "", "801"], state(), BASE),
    "add-diagnostics-empty": (["add", "--diagnostics-main", "", "801"], ["queue", "add", "--diagnostics-main", "", "801"], state(), BASE),
    "add-mitigation-evidence-empty": (["add", "--mitigation-evidence", "", "801"], ["queue", "add", "--mitigation-evidence", "", "801"], state(), BASE),
    "add-two-kinds": (["add", "--fixes-main", "--fix-forward", "801"], ["queue", "add", "--fixes-main", "--fix-forward", "801"], state(), BASE),
    "add-other-repository": (["add", "plugins", "38", "801"], ["queue", "add", "plugins", "38", "801"], state(), [gh("pr diff 38", DIFF), gh("pr view 38", PR_OPEN), *BASE]),
    "add-fixes-main": (["add", "--fixes-main", "801"], ["queue", "add", "--fixes-main", "801"], state(), BASE),
    "add-unreadable-diff": (["add", "801"], ["queue", "add", "801"], state(), BASE[1:]),
    "drop": (["drop", "801", "796"], ["queue", "drop", "801", "796"], state(queued()), []),
    # --- ship area: (old tool, its arguments) against `sm head ...`
    "ship-pass": (("hz-ship", ["801"]), ["head", "ship", "801"], state(), [*BASE, gh("--event pull_request", "2\n")]),
    "ship-flagged-needs-review": (("hz-ship", ["801"]), ["head", "ship", "801"], state(), [gh("pr diff 801", WEAKER), *BASE[1:], gh("--event pull_request", "7\n")]),
    "ship-reviewed": (("hz-ship", ["801", "independent review: research/review-pr801.md"]), ["head", "ship", "801", "independent review: research/review-pr801.md"],
                      state(), [gh("pr diff 801", WEAKER), *BASE[1:]]),
    "ship-reviewed-head": (("hz-ship", ["801", "review r", "--reviewed-head", HEAD]), ["head", "ship", "801", "review r", "--reviewed-head", HEAD], state(), BASE),
    "ship-reviewed-head-moved": (("hz-ship", ["801", "review r", "--reviewed-head", "f" * 40]), ["head", "ship", "801", "review r", "--reviewed-head", "f" * 40], state(), BASE),
    "ship-cleared": (("hz-ship", ["801"]), ["head", "ship", "801"], state(queued()), BASE),
    "ship-usage": (("hz-ship", ["801", "--reviewed-head"]), ["head", "ship", "801", "--reviewed-head", HEAD], state(), BASE),
    "ship-fix-forward-reviewed-head": (("hz-ship", ["801", "--fix-forward", "--for", "web-2=9kk", "--reviewed", "r", "--reviewed-head", HEAD]),
                                       ["head", "ship", "801", "--fix-forward", "--for", "web-2=9kk", "--reviewed", "r", "--reviewed-head", HEAD], state(), BASE),
    "ship-fix-forward-reason": (("hz-ship", ["801", "--fix-forward", "--for", "web-2=9kk", "review r"]),
                                ["head", "ship", "801", "--fix-forward", "--for", "web-2=9kk", "review r"], state(), BASE),
    "guard-pass": (("hz-diff-guard", ["801"]), ["head", "guard", "801"], state(), BASE),
    "guard-flag": (("hz-diff-guard", ["801"]), ["head", "guard", "801"], state(), [gh("pr diff 801", WEAKER)]),
    "guard-unreadable": (("hz-diff-guard", ["plugins", "38"]), ["head", "guard", "plugins", "38"], state(), []),
    "push-gate-ok": (("hz-push-gate", ["801"]), ["head", "push", "801", "--gate-only"], state(), [gh("run list", [])]),
    "push-gate-wait-main-starved": (("hz-push-gate", ["801"]), ["head", "push", "801", "--gate-only"], state(), STARVED),
    "push-gate-wait-release-candidate": (("hz-push-gate", ["801"]), ["head", "push", "801", "--gate-only"], state(), CANDIDATE),
    "push-gate-waiver": (("hz-push-gate", ["801", "--min", "15"]), ["head", "push", "801", "--min", "15"], state(), CANDIDATE),
    "push-gate-github-down": (("hz-push-gate", ["fix/x"]), ["head", "push", "fix/x", "--gate-only"], state(), []),
    "main-rerun-dry-nothing": (["main-rerun", "--dry-run"], ["queue", "main-rerun", "--dry-run"], state(), BASE),
    "main-rerun-refused-unknown-main": (["main-rerun"], ["queue", "main-rerun"], state(), []),
    "main-rerun-nothing": (["main-rerun"], ["queue", "main-rerun"], state(), BASE),
    "tick-nothing-queued": (["run", "--once"], ["daemon", "queue"], state(), BASE),
    "tick-merge": (["run", "--once"], ["daemon", "queue"], state(queued(), last_batch=0),
                   [*BASE, gh("pr merge 801", "merged"), gh("commits/main", {"sha": MAIN})]),
    "tick-merge-refused-by-forge": (["run", "--once"], ["daemon", "queue"], state(queued(), last_batch=0),
                                    [*BASE, gh("pr merge 801", rc=1, err="GraphQL: Required status check \"CI OK\" is expected. (mergePullRequest)\n")]),
    "tick-waits-on-freeze": (["run", "--once"], ["daemon", "queue"], state(queued(), last_batch=0), [*BASE, {"tool": "home", "needle": ".cache/hz-merge-freeze"}]),
    "tick-reviewed-head-moved": (["run", "--once"], ["daemon", "queue"], state(queued(reviewed_head="f" * 40), last_batch=0), BASE),
    "tick-blocked-with-green-checks": (["run", "--once"], ["daemon", "queue"], state(queued(waiting_since=1), last_batch=0),
                                       [gh("pr view 801", {**PR_OPEN, "mergeStateStatus": "BLOCKED"}), *[a for a in BASE if a["needle"] != "pr view 801"]]),
}


def prepare(home: Path, data: dict, answers: list[dict], tools: Path | None, source_tools: Path) -> None:
    (home / ".cache").mkdir(parents=True)
    (home / ".cache" / "hz-queue.json").write_text(json.dumps(data, indent=1))
    records = home / "Code" / "heinzel-management"
    (records / "research").mkdir(parents=True)
    (records / "AGENTS.md").write_text("## 9. Log\n")
    (records / "research" / "known-failures.toml").write_text("")
    (records / "research" / "mini-onboarding-2026-11-07").mkdir()
    (home / ".local" / "bin").mkdir(parents=True)
    for answer in answers:
        if answer["tool"] == "home":
            (home / answer["needle"]).touch()
    if tools is not None:
        shutil.copytree(tools, records / "tools", ignore=shutil.ignore_patterns("__pycache__", "tests", ".pytest_cache"))
        for script in (records / "tools").glob("hz-*"):  # the old ship script finds the other tools by name on its PATH
            (home / ".local" / "bin" / script.name).symlink_to(script)
    else:
        (records / "tools").mkdir()
        (records / "tools" / "pr-actions.toml").write_text((source_tools / "pr-actions.toml").read_text())  # data: the holds
    (home / "answers.json").write_text(json.dumps([a for a in answers if a["tool"] != "home"]))


def run(home: Path, argv: list[str], cwd: Path, extra: dict) -> dict:
    env = {"HOME": str(home), "PATH": f"{HERE / 'fakebin'}:{os.environ['PATH']}", "PARITY_ANSWERS": str(home / "answers.json"),
           "PARITY_TRANSCRIPT": str(home / "transcript.jsonl"), "HZ_ALARM_NOTIFY": "off", "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8",
           "TZ": os.environ.get("TZ", "Europe/Berlin"), **extra}
    done = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=300)
    transcript = home / "transcript.jsonl"
    calls = [json.loads(line) for line in transcript.read_text().splitlines()] if transcript.exists() else []
    read = lambda rel: (home / rel).read_text() if (home / rel).exists() else None  # noqa: E731
    return {"exit": done.returncode, "stdout": done.stdout, "stderr": done.stderr,
            "calls": [{"tool": c["tool"], "argv": c["argv"], "stdin": c["stdin"]} for c in calls],
            "state": json.loads(read(".cache/hz-queue.json") or "null"), "notes": read("Code/heinzel-management/AGENTS.md"),
            "alarms": json.loads(read(".cache/hz-alarm.json") or "null"), "log": read(".cache/hz-queue.log"),
            "interventions": read("Code/heinzel-management/research/mini-onboarding-2026-11-07/interventions.log"),
            "lock": (home / ".cache/hz-queue.lock").exists()}


def mask_times(value):
    if isinstance(value, dict):
        return {k: ("<time>" if k in TIME_KEYS and isinstance(v, int | float) and v else mask_times(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_times(v) for v in value]
    return value


def normal_text(text: str | None, home: Path, old: bool) -> str | None:
    if text is None:
        return None
    text = text.replace(str(home), "<home>")
    text = re.sub(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ", "<stamp> ", text, flags=re.MULTILINE)  # N2 log lines
    text = re.sub(r"^- \d\d-\d\d \d\d:\d\d ", "- <stamp> ", text, flags=re.MULTILINE)  # N2 note lines
    text = re.sub(r"tick finished in \d+\.\ds", "tick finished in <n>s", text)
    text = re.sub(r"pid \d+", "pid <n>", text)
    text = re.sub(r"last batch \d\d-\d\d \d\d:\d\d", "last batch <stamp>", text)
    text = re.sub(r"(last batch|waiting|ago) \d+ min", r"\1 <n> min", text)
    text = re.sub(r"(queued|\(release/v\*,|active|active \() ?\d+ min", r"\1 <n> min", text)
    text = re.sub(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ", "<stamp> ", text, flags=re.MULTILINE)  # N2 interventions log
    return text


def normal(result: dict, home: Path, old: bool) -> dict:
    out = dict(result)
    for key in ("stdout", "stderr", "notes", "log", "interventions"):
        out[key] = normal_text(result[key], home, old)
    if old:  # N1: the command name in what is printed and logged (never in notes or state)
        for key in ("stdout", "stderr", "log"):
            if out[key] is not None:
                out[key] = out[key].replace("hz-queue run started", "queue run started").replace("`hz-queue list`", "`sm queue list`")
                out[key] = out[key].replace("hz-queue ", "sm queue ").replace("hz-queue:", "sm queue:")
                for was, now in SHIP_NAMES.items():
                    out[key] = out[key].replace(was, now)
    out["state"], out["alarms"] = mask_times(result["state"]), mask_times(result["alarms"])
    out["calls"] = [{**c, "argv": [a.replace(str(home), "<home>") for a in c["argv"]]} for c in result["calls"]]
    return out


def compare(name: str, tools: Path, show: bool) -> list[str]:
    old_argv, native_argv, data, answers = SCENARIOS[name]
    old_tool, old_argv = old_argv if isinstance(old_argv, tuple) else ("hz-queue", old_argv)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        old_home, new_home, project = root / "old", root / "new", root / "project"
        prepare(old_home, data, answers, tools, tools)
        prepare(new_home, data, answers, None, tools)
        project.mkdir()
        (project / "steuermaschine.toml").write_text('[project]\nname = "parity"\nplugin = "heinzel"\n')
        old = run(old_home, [*OLD_INTERPRETER.get(old_tool, [sys.executable]), str(old_home / "Code/heinzel-management/tools" / old_tool), *old_argv],
                  old_home / "Code/heinzel-management", {})
        new = run(new_home, [str(PYTHON), str(HERE / "native_cli.py"), *native_argv], project, {})
        a, b = normal(old, old_home, True), normal(new, new_home, False)
    if old_argv[0] == "drop":  # N4
        a["stdout"] = b["stdout"] if re.fullmatch(r"((dropped|not in the queue:) [^\n]+\n)+", b["stdout"] or "") else a["stdout"]
    if a["exit"] == b["exit"] == 2:  # N6
        a["stderr"] = b["stderr"] = "<usage>"
    if a["stdout"] == "" and b["stdout"] == "\n":  # N5
        b["stdout"] = ""
    differences = []
    for key in ("exit", "stdout", "stderr", "calls", "state", "notes", "alarms", "log", "lock", "interventions"):
        if a[key] != b[key]:
            differences.append(key)
    writes = [" ".join(c["argv"]) for c in b["calls"] if c["tool"] == "gh" and any(m in " ".join(c["argv"]) for m in ("pr merge", "-X PUT", "-X POST", "--method POST", "run rerun", "update-branch", "pr close"))]
    print(f"### {name}: {'SAME' if not differences else 'DIFFERENT in ' + ', '.join(differences)}")
    print(f"- old `{old_tool} {' '.join(old_argv)}`, native `sm {' '.join(native_argv)}`; exit {a['exit']} / {b['exit']}; "
          f"{len(a['calls'])} / {len(b['calls'])} calls; writes to the forge: {writes or 'none'}")
    if show or differences:
        for key in differences or ["stdout", "stderr", "calls"]:
            print(f"- {key} old:    {json.dumps(a[key], sort_keys=True)[:3000]}")
            print(f"- {key} native: {json.dumps(b[key], sort_keys=True)[:3000]}")
    return differences


def main(argv: list[str]) -> int:
    show = "--show" in argv
    argv = [a for a in argv if a != "--show"]
    if not argv:
        sys.exit(__doc__)
    tools = Path(argv[0]).expanduser().resolve()
    names = argv[1:] or list(SCENARIOS)
    bad = [name for name in names if compare(name, tools, show)]
    print(f"\n{len(names) - len(bad)} of {len(names)} scenarios the same" + (f"; DIFFERENT: {', '.join(bad)}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
