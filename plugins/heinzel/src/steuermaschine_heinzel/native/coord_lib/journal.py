"""The journal of the tracking document and the release stamps on beads.

Two functions other areas import (inside a native command, where `runtime()` is set; or pass `rt`):

    from steuermaschine_heinzel.native.coord_lib.journal import note, stamp
    note("queue: #12 merged")                       # one dated line in the log of the day (coord_log_dir)
    stamp("abc", merged=12)                         # `merged_in:` note and label merged-12 on the bead, once
    stamp("abc", released="0.9.40")                 # `released_in:` note and label released-v0.9.40, once

`note` raises ValueError for empty text and `Refuse` (usage) while the configuration names no tracking document. `stamp` never raises for a refusal: it returns `Stamped` with `refused` set
(the pull request is not merged, the release is not published) and `exit_code` 1.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterable
from typing import Any

from steuermaschine import journal as core_journal
from steuermaschine.github import Gh
from steuermaschine.models import Model
from steuermaschine.native import runtime

from .base import child_env, journal_lock, log_file, setting, tracker_dir

TAG = re.compile(r"v?(\d+)\.(\d+)\.(\d+)$")


# ----------------------------------------------------------------------------- note
class Noted(Model):
    """One line appended to the log of the day."""

    line: str
    document: str

    def render(self) -> list[str]:
        return ["noted"]


def note(line: str, rt: Any = None) -> Noted:
    """Append one dated line to the log (the file of the day in coord_log_dir), never to the tracking document: only
    `sm status --write` writes state there. Safe when several agents write at once."""
    if not line.strip():
        raise ValueError("no text")
    rt = rt or runtime()
    now = rt.providers.clock.now()
    target = log_file(rt.settings, now)
    target.parent.mkdir(parents=True, exist_ok=True)
    written = core_journal.append(target, line, now, lock_file=journal_lock(rt.settings))
    return Noted(line=written, document=str(target))


# ----------------------------------------------------------------------------- stamp: pure parts (moved; only type annotations added)
def merged_line(pr: int, sha: str, at: str) -> str: return f"merged_in: PR #{pr} sha={sha[:8]} at={at}"
def released_line(version: str, at: str) -> str: return f"released_in: {version} released_at={at}"


def earliest_tag(tags: Iterable[str]) -> str:
    """Lowest semver tag among `tags` (ignores non-release tags), or ''."""
    ok = sorted((tuple(map(int, m.groups())), t) for t in tags if (m := TAG.match(t)))
    return ok[0][1] if ok else ""


def merged_prs(labels: Iterable[str]) -> list[int]: return [int(m.group(1)) for l in labels if (m := re.fullmatch(r"merged-(\d+)", l))]  # noqa: E741
def is_released(labels: Iterable[str]) -> bool: return any(l.startswith("released-") for l in labels)  # noqa: E741


# ----------------------------------------------------------------------------- stamp: the world
class StampRow(Model):
    bead: str
    line: str
    label: str
    status: str  # stamped | would_stamp | failed


class Stamped(Model):
    """What a stamp did to one bead. No row and no refusal: every asked stamp was already there."""

    bead: str
    rows: list[StampRow] = []
    refused: str = ""

    @property
    def exit_code(self) -> int:
        return 1 if self.refused or any(r.status == "failed" for r in self.rows) else 0

    def render(self) -> list[str]:
        lines = [f"would stamp {r.bead}: {r.line}" if r.status == "would_stamp" else f"{r.bead}: {r.line}" + (" (FAILED: the tracker refused)" if r.status == "failed" else "")
                 for r in self.rows]
        if self.refused:
            lines.append(f"refused: {self.refused}")
        return lines or [f"{self.bead}: already stamped"]


class StampSweep(Model):
    """Released stamps written after a publish."""

    stamped: int
    beads: list[Stamped] = []
    dry_run: bool = False

    def render(self) -> list[str]:
        return [*(line for b in self.beads for line in b.render()), f"sweep: {self.stamped} bead(s) stamped released"]


class Stamper:
    """bd, gh and git for the stamps, through the one process runner."""

    def __init__(self, rt: Any):
        s = rt.settings
        self.commands, self.clock, self.env = rt.providers.commands, rt.providers.clock, child_env(s)
        self.root, self.prefix = str(tracker_dir(s)), str(setting(s, "coord_bead_prefix"))
        self.core, self.core_dir = str((s or {}).get("repo") or ""), os.path.expanduser(str((s or {}).get("core_repo_dir") or "~/Code/heinzel"))

    def bid(self, x: str) -> str:
        return x if x.startswith(self.prefix) else self.prefix + x

    def bd(self, *args: str) -> tuple[int, str]:
        done = self.commands.run(["bd", *args], cwd=self.root, env=self.env, timeout=60)
        return done.rc, done.out.strip()

    def pr_facts(self, pr: int, repo: str) -> tuple[str, str] | None:
        d, _why = Gh(self.commands, repo, clock=self.clock).try_json(["pr", "view", str(pr), "-R", repo, "--json", "state,mergeCommit,mergedAt"])
        if not isinstance(d, dict) or d.get("state") != "MERGED":
            return None
        return d["mergeCommit"]["oid"], d.get("mergedAt", "")

    def release_facts(self, tag: str) -> str | None:
        """publishedAt of a non-draft release, else None."""
        d, _why = Gh(self.commands, self.core, clock=self.clock).try_json(["release", "view", tag, "-R", self.core, "--json", "isDraft,publishedAt"])
        if not isinstance(d, dict):
            return None
        return None if d.get("isDraft") or not d.get("publishedAt") else str(d["publishedAt"])

    def has_label(self, bead: str, label: str) -> bool:
        _code, out = self.bd("show", self.bid(bead), "--json")
        try:
            d = json.loads(out)
            d = d[0] if isinstance(d, list) else d
        except (ValueError, IndexError):
            return False
        return label in (d.get("labels") or [])

    def write(self, bead: str, line: str, label: str, dry: bool) -> StampRow:
        if dry:
            return StampRow(bead=bead, line=line, label=label, status="would_stamp")
        c1, _ = self.bd("update", self.bid(bead), "--append-notes", f"{time.strftime('%H:%M', time.localtime(self.clock.now()))} {line}")
        c2, _ = self.bd("label", "add", self.bid(bead), label)
        return StampRow(bead=bead, line=line, label=label, status="stamped" if c1 == c2 == 0 else "failed")

    def stamp(self, bead: str, pr: int | None = None, released: str | None = None, repo: str | None = None, dry: bool = False) -> Stamped:
        result = Stamped(bead=bead)
        if pr:
            facts = self.pr_facts(pr, repo or self.core)
            if not facts:
                result.refused = f"PR #{pr} in {repo or self.core} is not merged"
                return result
            if not self.has_label(bead, f"merged-{pr}"):
                result.rows.append(self.write(bead, merged_line(pr, *facts), f"merged-{pr}", dry))
        if released:
            tag = released if released.startswith("v") else "v" + released
            at = self.release_facts(tag)
            if not at:
                result.refused = f"release {tag} is not published"
                return result
            if not self.has_label(bead, f"released-{tag}"):
                result.rows.append(self.write(bead, released_line(tag, at), f"released-{tag}", dry))
        return result

    def sweep(self, version: str | None = None, dry: bool = False) -> StampSweep:
        code, out = self.bd("list", "--status", "open", "--json", "--limit", "0")
        try:
            d = json.loads(out) if code == 0 else []
            beads = d if isinstance(d, list) else d.get("issues", [])
        except ValueError:
            beads = []
        done: list[Stamped] = []
        for b in beads:
            labels = b.get("labels") or []
            prs = merged_prs(labels)
            if not prs or is_released(labels):
                continue
            facts = self.pr_facts(prs[-1], self.core)
            if not facts:
                continue
            tags = self.commands.run(["git", "tag", "--contains", facts[0]], cwd=self.core_dir, env=self.env, timeout=60).out
            tag = earliest_tag(tags.split())
            if not tag or (version and tag.lstrip("v") != version.lstrip("v")):
                continue
            done.append(self.stamp(b["id"], released=tag, dry=dry))
        return StampSweep(stamped=sum(1 for s in done if s.exit_code == 0), beads=done, dry_run=dry)


def stamp(bead: str, merged: int | None = None, released: str | None = None, repo: str | None = None, dry_run: bool = False, rt: Any = None) -> Stamped:
    """Stamp one bead: `merged` after gh says the pull request is merged, `released` once the tag's release is
    published. Idempotent (the label decides). The id may be the short suffix."""
    return Stamper(rt or runtime()).stamp(bead, merged, released, repo, dry_run)


def stamp_released(version: str | None = None, dry_run: bool = False, rt: Any = None) -> StampSweep:
    """After a publish: every open bead labelled merged-N without released-* whose merge commit is contained in a
    published release tag (the earliest) gets the released stamp."""
    return Stamper(rt or runtime()).sweep(version, dry_run)

