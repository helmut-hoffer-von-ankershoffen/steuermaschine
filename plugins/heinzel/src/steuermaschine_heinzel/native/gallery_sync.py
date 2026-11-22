"""Plugin screenshot galleries in `sm check` and `sm ship` (bead heinzel-management-0tkk8): regenerate, then ship.

Taking screenshots needs no AI. The plugins repository captures deterministically (its hand-edited
`docs/screenshot-definition.json`, Playwright, fixture scenes, fictional data, text guards) and its offline check says
which galleries are stale, that is whose UI inputs changed: its frozen `screenshots-check --all --json`. sm adds
nothing to that judgement; it reads the repository's own answer.

  sm check   on a plugins worktree: every stale gallery is regenerated with the frozen capture of `sm plugin gallery`
             (run lock, allowlisted environment, pushes blocked), and the paths that capture changed are recorded for
             the head they were made on. A capture that fails names the exact command to run.
  sm ship    takes exactly those recorded paths into the shipped commit: amended into the head commit while the head
             is not on the remote yet, else one `docs(screenshots)` commit (sm never force-pushes). A gallery that is
             still stale is regenerated first; when that cannot be done, it refuses naming the exact command.

Uncommitted changes that sm did not regenerate are never committed by sm: the PR step refuses a dirty tree as before.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from steuermaschine.config import child_environment
from steuermaschine.engine import Refuse

from . import plugin_gallery as pg

KIND = "heinzel.gallery.regenerated"  # state store: per worktree, the head and the paths sm check's capture changed
PLUGINS_REPO = re.compile(r"(?:^|/)heinzel-plugins$")
SUBJECT = "docs(screenshots): regenerate the screenshot gallery"
CHECK_TIMEOUT = 600


def origin_repo(commands: Any, worktree: Path) -> str:
    """`owner/name` of the worktree's origin, or ""."""
    done = commands.run(["git", "-C", str(worktree), "remote", "get-url", "origin"], timeout=30)
    url = str(done.out).strip() if done.rc == 0 else ""
    return re.sub(r"\.git$", "", re.sub(r"^.*github\.com[:/]", "", url))


def is_plugins(repo: str) -> bool:
    return bool(PLUGINS_REPO.search(repo.lower()))


def check_command() -> list[str]:
    return [*pg.FROZEN, "screenshots-check", "--all", "--json"]


def stale(commands: Any, worktree: Path) -> list[dict[str, str]]:
    """The stale galleries of the worktree, each {plugin, command, error}; Refuse when the check cannot be read."""
    environ = child_environment()
    env = {key: environ[key] for key in pg.ENV_ALLOWED if key in environ}
    done = commands.run(check_command(), cwd=str(worktree), timeout=CHECK_TIMEOUT, env=env)
    report: dict[str, Any] | None = None
    for line in reversed(str(done.out).splitlines()):
        if line.startswith("{"):
            try:
                report = json.loads(line)
            except ValueError:
                continue
            break
    if not isinstance(report, dict) or not isinstance(report.get("stale"), list):
        raise Refuse(f"the gallery check of {worktree} gave no readable answer (exit {done.rc})",
                     f"cd {worktree} && {' '.join(check_command())}")
    if report.get("failed"):
        first = report["failed"][0]
        raise Refuse(f"the gallery of {first.get('plugin')} is broken, not stale: {first.get('error')}",
                     f"cd {worktree} && make screenshots-check NAME={first.get('plugin')}")
    return [item for item in report["stale"] if isinstance(item, dict) and item.get("plugin")]


def dirty(commands: Any, worktree: Path) -> list[str]:
    """Repository-relative paths with uncommitted changes (untracked included)."""
    done = commands.run(["git", "-C", str(worktree), "status", "--porcelain", "--untracked-files=all"], timeout=60)
    if done.rc:
        raise Refuse(f"cannot read the status of {worktree}", f"git -C {worktree} status")
    paths = []
    for line in str(done.out).splitlines():
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if path:
            paths.append(path)
    return sorted(set(paths))


def head(commands: Any, worktree: Path) -> str:
    done = commands.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], timeout=30)
    return str(done.out).strip() if done.rc == 0 else ""


def regenerate(rt: Any, worktree: Path, items: list[dict[str, str]]) -> tuple[list[str], list[tuple[str, str]]]:
    """Capture every stale gallery; record the changed paths. -> (lines, failures as (message, exact command))."""
    commands, store = rt.providers.commands, rt.providers.store
    before = set(dirty(commands, worktree))
    lines: list[str] = []
    failures: list[tuple[str, str]] = []
    for item in items:
        plugin = str(item["plugin"])
        command = str(item.get("command") or f"make screenshots NAME={plugin}")
        out = pg.plugin_gallery(name=plugin, repo=worktree)
        if out.problems or out.exit:
            why = "; ".join(out.problems) or out.error or f"the capture exited {out.exit}"
            failures.append((f"{plugin}: the gallery is stale and could not be regenerated: {why}", f"cd {worktree} && {command}"))
            continue
        lines.append(f"{plugin}: regenerated ({len(out.changed)} image(s) changed)")
    changed = sorted(set(dirty(commands, worktree)) - before)
    if changed:
        kept = store.load(KIND, str(worktree)) or {}
        sha = head(commands, worktree)
        paths = sorted(set(kept.get("paths", [])) | set(changed)) if kept.get("head") == sha else changed
        store.save(KIND, str(worktree), {"head": sha, "paths": paths})
    return lines, failures


def fold(rt: Any, worktree: Path) -> list[str]:
    """Commit the paths sm regenerated for this head, and nothing else. -> lines saying what it did."""
    commands, store = rt.providers.commands, rt.providers.store
    record = store.load(KIND, str(worktree)) or {}
    if not record:
        return []  # sm regenerated nothing here: never touch the tree
    changes = dirty(commands, worktree)
    if not changes:
        store.delete(KIND, str(worktree))
        return []
    if record.get("head") != head(commands, worktree):
        return ["GALLERY  uncommitted changes sm did not regenerate for this head: commit or remove them yourself"]
    others = sorted(set(changes) - set(record.get("paths", [])))
    if others:
        return [f"GALLERY  uncommitted changes besides the regenerated gallery ({len(others)} path(s), first {others[0]}): commit them yourself"]

    def git(*args: str, timeout: float = 600) -> Any:
        return commands.run(["git", "-C", str(worktree), *args], timeout=timeout)

    added = git("add", "--all", "--", *changes)
    if added.rc:
        raise Refuse(f"cannot stage the regenerated gallery: {(added.err or added.out).strip()[:300]}", f"git -C {worktree} status")
    pushed = git("branch", "-r", "--contains", "HEAD", timeout=60)
    amend = pushed.rc == 0 and not str(pushed.out).strip()
    done = git("commit", "--amend", "--no-edit") if amend else git("commit", "-m", SUBJECT)
    if done.rc:
        raise Refuse(f"the commit of the regenerated gallery failed (its hooks ran): {(done.err or done.out).strip()[-400:]}",
                     f"git -C {worktree} status, fix what the hook names, then sm ship {worktree} again")
    store.delete(KIND, str(worktree))
    how = "amended into the head commit" if amend else f"committed as '{SUBJECT}' (the head was already pushed)"
    return [f"GALLERY  {len(changes)} regenerated gallery file(s) {how}: {head(commands, worktree)[:12]}"]


def into_head(rt: Any, worktree: Path) -> list[str]:
    """`sm ship` on a plugins worktree: the shipped commit carries a fresh gallery, or this refuses naming the command."""
    lines = fold(rt, worktree)
    items = stale(rt.providers.commands, worktree)
    if not items:
        return lines
    done, failures = regenerate(rt, worktree, items)
    if failures:
        message, command = failures[0]
        raise Refuse(message + "; nothing was pushed", f"{command}, then sm ship {worktree} again")
    lines += [f"GALLERY  {line}" for line in done]
    lines += fold(rt, worktree)
    left = stale(rt.providers.commands, worktree)
    if left:
        first = left[0]
        raise Refuse(f"{first['plugin']}: the gallery is still stale after regeneration; nothing was pushed",
                     f"cd {worktree} && {first.get('command')}, then sm ship {worktree} again")
    return lines
