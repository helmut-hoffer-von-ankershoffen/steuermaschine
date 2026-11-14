"""Ask rows and the owner-message audit.

An ask row is a row of the table below the asks heading of the tracking document whose status cell is
`ask YYYY-MM-DD HH:MM`. It is open while the row names no pull request. The audit reads the coordinator's session
transcript and lists what the owner typed after the watermark. Transcript text is data only: it is shown truncated
and never executed or interpreted.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from steuermaschine import journal as core_journal
from steuermaschine.models import Model
from steuermaschine.native import runtime

from ... import state as statedir  # the plugin's state paths
from ..coord_lib.base import document, journal_lock, optional_path, setting
from . import legacy

STAMP = re.compile(r"\|\s*ask (\d{4}-\d\d-\d\d \d\d:\d\d)\s*\|")
PR = re.compile(r"(?<![\w])(?:plugins |acme |sf )?#\d+")
CONTEXT_BLOCK = re.compile(r"\A<artifact-view-context\b.*?</artifact-view-context>\s*", re.S)
LINE_WIDTH = 160
NEXT_STEP = "Name an agent within the hour; small text or UI goes through the small-change lane"
WATERMARK = ("heinzel.coord.asks", "watermark")  # store kind and key


class AsksSettings(BaseModel):
    """Settings of the owner asks (mixin of the plugin's settings table)."""

    asks_heading: str = Field("### Now", description="A line of the tracking document that starts with this is the heading of the asks table.")
    asks_lock_file: Path | None = Field(None, description="A second lock held while an ask row is added, besides the journal's; name the former ask tool's lock file while that tool still runs.")
    asks_transcripts_dir: Path | None = Field(None, description="Directory of the coordinator's session transcripts (*.jsonl); the newest top-level file is read. None: --transcript is required.")
    asks_open_hours: float = Field(1.0, description="An ask row without a pull request is reported once it is this old.")


def get(settings: Mapping[str, Any] | None, name: str) -> Any:
    return setting(settings, name, AsksSettings)


# ----------------------------------------------------------------------------- ask rows
class AskAdded(Model):
    """One ask row added to the tracking document."""

    text: str
    row: str
    at: str  # local time, YYYY-MM-DD HH:MM
    document: str

    def render(self) -> list[str]:
        return [f"asked {self.at[-5:]}: {self.text}"]


class OpenAsk(Model):
    age_hours: float
    text: str


def add(text: str, rt: Any = None) -> AskAdded:
    """Add an ask row with the time, before any work on the ask. Raises ValueError for empty text and LookupError
    when the document has no asks table."""
    rt = rt or runtime()
    if not text.strip():
        raise ValueError("no text")
    s = rt.settings
    target, at = document(s), time.strftime("%Y-%m-%d %H:%M", time.localtime(rt.providers.clock.now()))
    own = optional_path(s, "asks_lock_file", AsksSettings)
    locks = [*([own] if own else []), journal_lock(s)]
    row = core_journal.add_table_row(target, str(get(s, "asks_heading")), [text, f"ask {at}", NEXT_STEP, ""], locks)
    return AskAdded(text=text, row=row, at=at, document=str(target))


def open_asks(text: str, hours: float, now: float) -> list[tuple[float, str]]:
    """(age in hours, ask) of every ask row in `text` that names no pull request and is at least `hours` old."""
    found = []
    for line in text.splitlines():
        match = STAMP.search(line)
        if match and not PR.search(line):
            age = (now - datetime.strptime(match.group(1), "%Y-%m-%d %H:%M").timestamp()) / 3600
            if age >= hours:
                found.append((age, line.split("|")[1].strip()))
    return found


# ----------------------------------------------------------------------------- the transcript (moved)
def owner_text(content: Any) -> str:
    """The text of a human message: strings as is, lists by their text parts (images count as [image])."""
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and not str(part.get("text", "")).startswith("[Image: source:"):
                parts.append(str(part.get("text", "")))
            elif part.get("type") == "image":
                parts.append("[image]")
    else:
        return ""
    text = CONTEXT_BLOCK.sub("", " ".join(parts).strip())
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", text).split())


def owner_messages(transcript: Path) -> list[tuple[str, str]]:
    """(ISO timestamp, text) of every real owner-typed message, oldest first. Two record shapes carry them: a `user` record
    with origin.kind == human, and an `attachment` of type queued_command with origin.kind == human (a message sent while the
    agent was working). Tool results, task notifications, meta records and compaction summaries have another origin or flag."""
    found, seen = [], set()
    for raw in transcript.read_text(errors="replace").splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("isSidechain") or rec.get("isMeta") or rec.get("isCompactSummary"):
            continue
        if rec.get("type") == "user" and (rec.get("origin") or {}).get("kind") == "human":
            content, key = (rec.get("message") or {}).get("content"), rec.get("uuid")
        elif rec.get("type") == "attachment" and isinstance(rec.get("attachment"), dict):
            att = rec["attachment"]
            if att.get("type") != "queued_command" or (att.get("origin") or {}).get("kind") != "human":
                continue
            content, key = att.get("prompt"), att.get("source_uuid") or rec.get("uuid")
        else:
            continue
        text, stamp = owner_text(content), str(rec.get("timestamp") or "")
        if text and stamp and (key or (stamp, text)) not in seen:
            seen.add(key or (stamp, text))
            found.append((stamp, text))
    return sorted(found)


def short_time(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return stamp[:16]


def title(text: str, n: int = 72) -> str:
    first = re.split(r"(?<=[.!?])\s|\n", text.strip(), maxsplit=1)[0]
    first = re.sub(r"\s+", " ", first).strip(" .")
    return (first[: n - 1] + "…") if len(first) > n else first


# ----------------------------------------------------------------------------- the audit
class OwnerMessage(Model):
    at: str  # the transcript's timestamp, ISO 8601
    text: str
    title: str  # a suggested bead title
    command: str  # the one command that records it as a bead and an ask row

    def line(self) -> str:
        return f"MSG {short_time(self.at)} {self.text if len(self.text) <= LINE_WIDTH else self.text[:LINE_WIDTH - 3] + '...'}"


class Asks(Model):
    """Owner messages nobody reviewed yet, and ask rows that are old and name no pull request."""

    messages: list[OwnerMessage] = []
    open: list[OpenAsk] = []
    through: str = ""  # the newest message shown; `sm doc asks-ack` takes it
    acknowledged_through: str = ""
    transcript: str = ""
    open_hours: float = 1.0

    @property
    def exit_code(self) -> int:
        return 1 if self.messages or self.open else 0

    def render(self) -> list[str]:
        lines = [part for m in self.messages for part in (m.line(), f"    {m.command}")]
        if self.messages:
            lines.append(f"{len(self.messages)} unreviewed owner message(s): run the line below each one that is an ask, then: sm doc asks-ack {self.through}")
        else:
            lines.append("no unreviewed owner messages")
        lines += [f"ASK {a.age_hours:.1f} h with no PR: {a.text}" for a in self.open]
        if not self.open:
            lines.append(f"no open asks older than {self.open_hours:g} h")
        return lines


class AsksAcked(Model):
    through: str
    moved: bool

    def render(self) -> list[str]:
        return [f"acknowledged through {short_time(self.through) if self.through else 'nothing'}" + ("" if self.moved else " (unchanged)")]


def newest_transcript(directory: Path) -> Path | None:
    files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def read_mark(rt: Any) -> str:
    """The watermark: the later of this tool's and the former tool's (read-only)."""
    own = str((rt.providers.store.load(*WATERMARK) or {}).get("ts", "") or "")
    state_dir = statedir.dashboard_dir(rt.settings or {})
    return max(own, legacy.watermark(state_dir))


def audit(transcript: Path | None = None, hours: float | None = None, rt: Any = None) -> Asks:
    """Read-only. Raises FileNotFoundError when there is no transcript."""
    rt = rt or runtime()
    s = rt.settings
    directory = optional_path(s, "asks_transcripts_dir", AsksSettings)
    transcript = transcript or (newest_transcript(directory) if directory else None)
    if transcript is None or not transcript.exists():
        raise FileNotFoundError(str(transcript or directory or "setting asks_transcripts_dir is not set"))
    mark = read_mark(rt)
    fresh = [m for m in owner_messages(transcript) if m[0] > mark]
    messages = [OwnerMessage(at=stamp, text=text, title=title(text), command=f"sm beads tasks add {shlex.quote(title(text))} --ask") for stamp, text in fresh]
    hours = float(get(s, "asks_open_hours")) if hours is None else hours
    target = optional_path(s, "coord_document")
    rows = open_asks(target.read_text(), hours, rt.providers.clock.now()) if target and target.is_file() else []
    return Asks(messages=messages, open=[OpenAsk(age_hours=round(age, 1), text=text) for age, text in rows], through=fresh[-1][0] if fresh else "",
                acknowledged_through=mark, transcript=str(transcript), open_hours=hours)


def ack(through: str, rt: Any = None) -> AsksAcked:
    """Move the watermark to `through` (a timestamp printed by the audit); never backwards. Raises ValueError for
    something that is not an ISO 8601 timestamp."""
    rt = rt or runtime()
    datetime.fromisoformat(through.replace("Z", "+00:00"))
    mark = read_mark(rt)
    if through > mark:
        rt.providers.store.save(*WATERMARK, {"ts": through})
        return AsksAcked(through=through, moved=True)
    return AsksAcked(through=mark, moved=False)
