"""One journal per subject: everything the tool knows about one pull request, version, branch or bead, as dated events
appended to `<home>/journals/<kind>/<subject>.jsonl`. Nothing is rewritten in place; the newest event of a name wins.

    j = Journals(home / "journals")
    j.append("pr", "890", "queue", {"status": "queued"}, now)       # one line; the same data again is not repeated
    j.latest("pr", "890", "queue")                                 # {"status": "queued", ...} or None
    j.keys("version")                                              # every version with a journal

`JournalStore` keeps the pipeline runs (`runs.<pipeline>`) in these journals instead of one state file each: a release
run in the journal of its version, a head run in the journal of its branch. `go` reads them from there, so there is
one place per subject and a run that is history is visibly history (its last event says so).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .defaults import FileStore

KINDS = ("pr", "version", "branch", "bead")
SAFE = re.compile(r"[^A-Za-z0-9._-]+")
DIR = "journals"


def safe(key: str) -> str:
    """A subject key as a file name: `fix/topic` -> `fix--topic`; anything else outside [A-Za-z0-9._-] -> `_`."""
    return SAFE.sub("_", str(key).replace("/", "--")).strip("_") or "_"


def unsafe(name: str) -> str:
    return name.replace("--", "/")


class Journals:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, kind: str, key: str) -> Path:
        if kind not in KINDS:
            raise ValueError(f"no subject kind {kind!r}: one of {', '.join(KINDS)}")
        return self.root / kind / f"{safe(key)}.jsonl"

    def append(self, kind: str, key: str, event: str, data: dict[str, Any], now: float, *, repeat: bool = False) -> bool:
        """Append one event; an event whose data equals the newest one of the same name is skipped unless `repeat`.
        Returns whether a line was written. The file lock makes concurrent appends whole lines."""
        path = self.path(kind, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": now, "event": event, "data": data}, sort_keys=True, default=str) + "\n"
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            if not repeat:
                handle.seek(0)
                last = None
                for row in _rows(handle.read()):
                    if row.get("event") == event:
                        last = row.get("data")
                if last == json.loads(json.dumps(data, sort_keys=True, default=str)):
                    return False
            handle.seek(0, os.SEEK_END)
            handle.write(line)
        return True

    def events(self, kind: str, key: str) -> list[dict[str, Any]]:
        try:
            return list(_rows(self.path(kind, key).read_text(encoding="utf-8")))
        except FileNotFoundError:
            return []

    def latest(self, kind: str, key: str, event: str) -> Any:
        found = [row.get("data") for row in self.events(kind, key) if row.get("event") == event]
        return found[-1] if found else None

    def keys(self, kind: str) -> list[str]:
        folder = self.root / kind
        return sorted(unsafe(p.stem) for p in folder.glob("*.jsonl")) if folder.is_dir() else []

    def all(self, kind: str) -> Iterator[tuple[str, list[dict[str, Any]]]]:
        for key in self.keys(kind):
            yield key, self.events(kind, key)


def _rows(text: str) -> Iterator[dict[str, Any]]:
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


# ----------------------------------------------------------------------------- pipeline runs in the journals
RUN = "run"  # the event of a run's state; data: {"pipeline", "key", "state"}; state None: the run was deleted


def subject_of(pipeline: str, key: str, state: dict[str, Any] | None) -> tuple[str, str]:
    """Where a run lives: a release run in its version's journal, a head run in its branch's (the first word of its label)."""
    if pipeline.startswith("release."):
        return "version", key
    label = str((state or {}).get("label") or "").split()
    return "branch", label[0] if label else key


class JournalStore(FileStore):
    """A FileStore that keeps the kinds `runs.*` (pipeline runs) in the subject journals. Every other kind, the
    receipts and the locks stay files."""

    def __init__(self, root: Path):
        super().__init__(root)
        self.journals = Journals(Path(root) / DIR)

    def _runs(self, pipeline: str) -> dict[str, tuple[str, str, dict[str, Any] | None]]:
        """key -> (kind, subject, newest state or None) of every run of `pipeline` in the journals."""
        out: dict[str, tuple[str, str, dict[str, Any] | None]] = {}
        for kind in ("version", "branch"):
            for subject, rows in self.journals.all(kind):
                for row in rows:
                    data = row.get("data") or {}
                    if row.get("event") == RUN and data.get("pipeline") == pipeline:
                        out[str(data.get("key"))] = (kind, subject, data.get("state"))
        return out

    def load(self, kind: str, key: str) -> dict[str, Any] | None:
        if not kind.startswith("runs."):
            return super().load(kind, key)
        found = self._runs(kind.removeprefix("runs.")).get(key)
        return dict(found[2]) if found and found[2] is not None else None

    def save(self, kind: str, key: str, data: dict[str, Any]) -> None:
        if not kind.startswith("runs."):
            return super().save(kind, key, data)
        pipeline = kind.removeprefix("runs.")
        known = self._runs(pipeline).get(key)
        subject_kind, subject = (known[0], known[1]) if known else subject_of(pipeline, key, data)
        self.journals.append(subject_kind, subject, RUN, {"pipeline": pipeline, "key": key, "state": data}, _now(data), repeat=False)
        return None

    def keys(self, kind: str) -> list[str]:
        if not kind.startswith("runs."):
            return super().keys(kind)
        return sorted(k for k, (_, _, state) in self._runs(kind.removeprefix("runs.")).items() if state is not None)

    def delete(self, kind: str, key: str) -> None:
        if not kind.startswith("runs."):
            return super().delete(kind, key)
        known = self._runs(kind.removeprefix("runs.")).get(key)
        if known is not None and known[2] is not None:
            self.journals.append(known[0], known[1], RUN, {"pipeline": kind.removeprefix("runs."), "key": key, "state": None}, 0.0, repeat=True)
        return None

    def kinds(self) -> list[str]:
        files = [p.name for p in (self.root / "state").iterdir() if p.is_dir() and not p.name.startswith("runs.")] if (self.root / "state").is_dir() else []
        runs = {f"runs.{(row.get('data') or {}).get('pipeline')}" for kind in ("version", "branch") for _, rows in self.journals.all(kind)
                for row in rows if row.get("event") == RUN}
        return sorted(set(files) | runs)


def _now(state: dict[str, Any]) -> float:
    import time

    try:
        return time.mktime(time.strptime(str(state.get("updated") or ""), "%Y-%m-%dT%H:%M:%S%z"))
    except ValueError:
        return time.time()


def migrate_runs(root: Path) -> list[str]:
    """Move every run state file of an earlier release (`state/runs.*/KEY.json`) into its subject journal, then remove the
    file. Returns one line per moved run. Idempotent: a file that is gone is not moved twice."""
    store = JournalStore(root)
    done = []
    for folder in sorted((Path(root) / "state").glob("runs.*")):
        for file in sorted(folder.glob("*.json")):
            try:
                state = json.loads(file.read_text())
            except ValueError:
                continue
            if isinstance(state, dict):
                store.save(folder.name, file.stem, state)
                kind, subject = subject_of(folder.name.removeprefix("runs."), file.stem, state)
                done.append(f"{folder.name}/{file.name} -> {DIR}/{kind}/{safe(subject)}.jsonl")
            file.unlink()
        if not any(folder.iterdir()):
            folder.rmdir()
    return done


def unmigrate_runs(root: Path) -> list[str]:
    """For a rollback to a release that reads state files: write the newest state of every run back to
    `state/runs.<pipeline>/KEY.json`. The journals stay."""
    store = JournalStore(root)
    done = []
    for kind in store.kinds():
        if kind.startswith("runs."):
            for key in store.keys(kind):
                state = store.load(kind, key)
                if state is not None:
                    FileStore.save(store, kind, key, state)
                    done.append(f"{DIR} -> state/{kind}/{key}.json")
    return done
