"""Moved from the former next-actions tool and the former startup tool, for `sm go --view next|start`: the ranking of
the owner's order, the ask and paused-work rows of the tracking document, the hourly-update clock, the item list
renderer, the latest checkpoint and the timer rows.

Verbatim where tests/views_verbatim.py says so. The differences are meant and listed there: the tracking document's
lines, the clock and the adopted state are parameters (the tools read files under the home directory themselves), and
the commands they name are sm commands.
"""

import hashlib
import re
import shlex
import time
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ASK_MINUTES = 15

MAX_LINES = 40

PER_PRIO = 3

STAMP = re.compile(r"\|\s*ask (\d{4}-\d\d-\d\d \d\d:\d\d)\s*\|")

PR = re.compile(r"(?<![\w])(?:plugins |acme |sf )?#\d+")

PAUSED = re.compile(r"paused|waiting for (a )?slot|no slot", re.I)

LOG_LINE = re.compile(r"^- (\d\d-\d\d \d\d:\d\d) (.*)")


def slug(prefix: str, text: str) -> str:
    return f"{prefix}-{hashlib.sha1(' '.join(text.split()).encode()).hexdigest()[:6]}"


# Owner's order (AGENTS.md section 4): owner messages first, then 1 flakiness (main red, load-driven push failures),
# 2 quality and security, 3 throughput, 4 known bugs, 5 features; an ask that serves none is ranked 2.5, right after 2.
RANK_BY_PRIO = {1: 0, 2: 1, 3: 3, 4: 2.5, 5: 3, 6: 2, 7: 3, 8: 3, 9: 6}
RANK_WORDS = [(1, r"flak|main red|red main|\bci\b|push fail|load"), (2, r"sonar|secur|quality|coverage|rating|codeql|vuln"),
              (3, r"merge|queue|throughput|release|speed|faster|parallel|10x|slot"), (4, r"\bbug|broken|crash|regression|fails?\b|error"),
              (5, r"feature|add |new |button|icon|page|site|header|footer|readme|design|app:|plugin")]


def classify(text: str) -> float:
    return next((rank for rank, words in RANK_WORDS if re.search(words, text, re.I)), 2.5)


def brief_cmd(topic: str, branch: str, task: str, repo: str = "core") -> str:
    """The exact `sm beads brief` line to run: it takes the task from --task, so every `do:` line carries it."""
    where = f" --repo {repo}" if repo != "core" else ""
    return f"sm beads brief {topic} {branch}{where} --task {shlex.quote(' '.join(task.split()))}"


def item(prio: int, key: str, what: str, do: str, rank: float | None = None) -> dict:
    return {"prio": prio, "key": key, "what": what, "do": do, "rank": RANK_BY_PRIO[prio] if rank is None else rank}


def open_ask_rows(lines: list[str], now: float) -> list[tuple[float, str]]:
    found = []
    for line in lines:
        match = STAMP.search(line)
        if match and not PR.search(line):
            minutes = (now - datetime.strptime(match.group(1), "%Y-%m-%d %H:%M").timestamp()) / 60
            if minutes >= ASK_MINUTES:
                found.append((minutes, line.split("|")[1].strip()))
    return found


TEST_OR_CI = re.compile(r"\btests?\b|\bci\b|flak|pytest|workflow|runner|sonar|coverage|codeql|\bmerge|queue|\bhz-", re.I)


def ship_hint(text: str) -> str:
    """The scripts/ship.py lane is for small text or UI changes; an ask about tests or CI never gets the hint."""
    return "" if TEST_OR_CI.search(text) else " (small text or UI: scripts/ship.py lane)"


def src_paused(lines: list[str], now: float) -> list[dict]:
    out, cutoff = [], now - 3 * 3600
    for line in lines:
        if line.startswith("|") and PAUSED.search(line):
            text = line.split("|")[1].strip()
            out.append(item(5, slug("paused", text), f"paused or waiting for a slot: {text[:80]}", brief_cmd(slug("paused", text), f"paused/{slug('paused', text)[7:]}", f"Resume the paused work: {text[:300]}. Done = a PR.") + "; check sm gate status for the slot, or log why not", 1 if re.search(r"push|load|flak", text, re.I) else None))
        match = LOG_LINE.match(line)
        if match and PAUSED.search(match.group(2)):
            try:
                stamp = datetime.strptime(f"{time.localtime(now).tm_year}-{match.group(1)}", "%Y-%m-%d %H:%M").timestamp()
            except ValueError:
                continue
            if stamp > cutoff:
                out.append(item(5, slug("paused", match.group(2)), f"logged {match.group(1)}: {match.group(2)[:80]}", brief_cmd(slug("paused", match.group(2)), f"paused/{slug('paused', match.group(2))[7:]}", f"Restart the paused work: {match.group(2)[:300]}. Done = a PR.") + "; or log why not", 1 if re.search(r"push|load|flak", match.group(2), re.I) else None))
    return out


def update_due(now: float, last: float) -> bool:
    if not 7 <= datetime.fromtimestamp(now, ZoneInfo("Europe/Berlin")).hour < 23:
        return False
    return now - last >= 3600


def render(items: list[dict], claims: dict, now: float, show_all: bool = False, bugs_line: str = "known bugs open: ?", flake_lines: list[str] | None = None,
           main_ci: str = "main CI: unknown") -> list[str]:
    todo = [i for i in items if i["key"] not in claims]
    watching = [i for i in items if i["key"] in claims]
    lines = [main_ci, f"next {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}: {len(todo)} to do, {len(watching)} watching"]
    lines.append(bugs_line)
    lines += flake_lines or []
    seen: dict[int, int] = {}
    hidden = shown = 0
    for i in todo:
        seen[i["rank"]] = seen.get(i["rank"], 0) + 1
        if not show_all and seen[i["rank"]] > PER_PRIO:
            hidden += 1
            continue
        shown += 1
        lines += [f"{shown}. [{i['key']}] {i['what']}", f"   do: {i['do']}"]
    if hidden:
        lines.append(f"... {hidden} more of the same kinds, `sm go --view next --all` lists them")
    for i in watching:
        c = claims[i["key"]]
        lines.append(f"watching [{i['key']}] {c['owner']} until {time.strftime('%H:%M', time.localtime(c['until']))}: {i['what'][:60]}")
    if not todo:
        lines.append("nothing to do" + (" (everything open has a live claim)" if watching else ""))
    if not show_all and len(lines) > MAX_LINES:
        lines = lines[: MAX_LINES - 1] + [f"... {len(lines) - MAX_LINES + 1} lines cut, `sm go --view next --all`"]
    return lines


# ---------------------------------------------------------------- the startup orientation
def latest_checkpoint(text: str) -> str:
    section = text.partition("\n## 5.")[2].partition("\n## 6.")[0]
    checkpoints = re.split(r"^### ", section, flags=re.MULTILINE)
    if len(checkpoints) < 2:
        return "No dated checkpoint found. Reconcile live state."
    return "\n".join(checkpoints[1].strip().splitlines()[:14])


def timer_rows(manifest: dict, codex_home: Path | None, thread: str | None) -> list[str]:
    rows = []
    for name, wanted in manifest.items():
        cadence = wanted["every_minutes"]
        automation_id = wanted.get("codex_id")
        if not automation_id:
            rows.append(f"{name} ({cadence} min) | launchd | inspect separately")
            continue
        if codex_home is None:
            rows.append(f"{automation_id} ({cadence} min) | no local record (setting go_codex_home is not set)")
            continue
        config = codex_home / "automations" / automation_id / "automation.toml"
        try:
            record = tomllib.loads(config.read_text())
        except FileNotFoundError:
            rows.append(f"{automation_id} ({cadence} min) | no local record")
            continue
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            rows.append(f"{automation_id} ({cadence} min) | unreadable local record")
            continue
        if record.get("id") != automation_id or record.get("kind") != "heartbeat":
            rows.append(f"{automation_id} ({cadence} min) | unexpected local record")
            continue
        target = record.get("target_thread_id")
        binding = "unbound"
        if target:
            binding = target
            if thread is not None:
                binding += " (this chat)" if target == thread else " (other chat)"
        rules = dict(part.split("=", 1) for part in str(record.get("rrule", "")).split(";") if "=" in part)
        frequency = {"MINUTELY": 1, "HOURLY": 60}.get(rules.get("FREQ"))
        interval = rules.get("INTERVAL", "1")
        matches = (
            set(rules) <= {"FREQ", "INTERVAL"}
            and frequency is not None
            and interval.isascii()
            and interval.isdecimal()
            and int(interval) * frequency == cadence
        )
        schedule = "cadence matches" if matches else "check cadence in app"
        rows.append(f"{automation_id} ({cadence} min) | {record.get('status', 'unknown')} | {binding} | {schedule}")
    return rows
