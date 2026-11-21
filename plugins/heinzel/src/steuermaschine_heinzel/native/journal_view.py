"""The state of the merge queue, the release train and the alarms, in the subject journals (steuermaschine.subjects).

The queue loop, the release runner and the train write their own working files (queue.json, release.json,
release/vX.Y.Z.json, train/vX.Y.Z.json, alarm.json, the hold files); those programs are not changed here. Every command
that runs them (`daemon queue`, `daemon train`, `queue add|drop|hold|unhold`, `head ship`, `release resume`,
`sm release VERSION`) and `sm self migrate-state` call `mirror`: what changed goes into the journal of its subject:

    pr/<N>            queue    the queue entry of the PR (status, detail, added, wait reason)
    version/<X.Y.Z>   train    the train of that version; tail: its release tail; receipt: the receipt in flight
    branch/main       queue    the queue as a whole (which PRs, blocked, last batch, main's failure and reruns)
                      freeze, holds, releases (accepted, dropped), trains, tails, receipt, alarms

`go` reads only these journals and GitHub (`view(...)` gives the old readers' shapes back), so a NEXT never comes from a
file no command keeps current.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from steuermaschine.subjects import Journals

from .go_lib import legacy

MAIN = ("branch", "main")
ITEMS = "items"


def mirror(journals: Journals | None, state_dir: Path, now: float) -> list[str]:
    """Append every changed piece of the working files to its journal. Returns the subjects that changed. Never
    raises for a file that cannot be read: that piece is skipped (the journal keeps the last good one)."""
    if journals is None:
        return []
    changed: list[str] = []

    def put(kind: str, key: str, event: str, data: Any) -> None:
        if journals.append(kind, key, event, data if isinstance(data, dict) else {"value": data}, now):
            changed.append(f"{kind}/{key} {event}")

    queue = legacy.queue_state(state_dir)
    keys = []
    for item in queue[ITEMS]:
        if isinstance(item, dict) and item.get("key") is not None:
            keys.append(str(item["key"]))
            put("pr", str(item["key"]), "queue", item)
    put(*MAIN, "queue", {**{k: v for k, v in queue.items() if k != ITEMS}, "keys": keys})
    put(*MAIN, "freeze", {"on": legacy.merge_frozen(state_dir)})
    put(*MAIN, "holds", {"files": legacy.hold_files(state_dir), "release": legacy.release_holds(state_dir)})
    for name, read in (("trains", lambda: legacy.active_trains(state_dir)), ("tails", lambda: legacy.release_tails(state_dir)),
                       ("receipt", lambda: legacy.release_receipt(state_dir)), ("alarms", lambda: legacy.alarm_state(state_dir))):
        try:
            value = read()
        except (OSError, ValueError):
            continue
        if name == "trains":
            for train in value:
                put("version", str(train.get("version")), "train", train)
            put(*MAIN, "trains", {"versions": [str(t.get("version")) for t in value]})
        elif name == "tails":
            for version, data in value:
                put("version", version, "tail", data)
            put(*MAIN, "tails", {"versions": [v for v, _ in value]})
        elif name == "receipt":
            if isinstance(value, dict) and value.get("version"):
                put("version", str(value["version"]), "receipt", value)
            put(*MAIN, "receipt", {"version": str(value.get("version")) if isinstance(value, dict) and value.get("version") else ""})
        else:
            put(*MAIN, "alarms", value)
    put(*MAIN, "releases", {"accepted": legacy.accepted_versions(state_dir), "dropped": legacy.dropped_candidates(state_dir)})
    return changed


class View:
    """The old readers (go_lib.legacy) over the journals: the same shapes, read from journal events only."""

    def __init__(self, journals: Journals):
        self.j = journals

    def main(self, event: str, default: Any = None) -> Any:
        found = self.j.latest(*MAIN, event)
        return default if found is None else found

    def queue_state(self) -> dict[str, Any]:
        top = self.main("queue")
        if top is None:
            return {"items": [], "last_batch": 0, "blocked": "", "batch": {}, "red_batch": {}, "main_failure": {}, "main_reruns": {},
                    "main_restored": {}, "main_rerun_last": "", "readable": False}
        items = [found for key in top.get("keys", []) if (found := self.j.latest("pr", key, "queue")) is not None]
        return {**{k: v for k, v in top.items() if k != "keys"}, "items": items}

    def merge_frozen(self) -> bool:
        return bool(self.main("freeze", {}).get("on"))

    def hold_files(self) -> list[str]:
        return list(self.main("holds", {}).get("files", []))

    def release_holds(self) -> list[str]:
        return list(self.main("holds", {}).get("release", []))

    def active_trains(self) -> list[dict[str, Any]]:
        return [t for v in self.main("trains", {}).get("versions", []) if (t := self.j.latest("version", v, "train")) is not None]

    def release_tails(self) -> list[tuple[str, dict[str, Any]]]:
        return [(v, t) for v in self.main("tails", {}).get("versions", []) if (t := self.j.latest("version", v, "tail")) is not None]

    def release_receipt(self) -> dict[str, Any] | None:
        version = self.main("receipt", {}).get("version")
        return self.j.latest("version", version, "receipt") if version else None

    def accepted_versions(self) -> list[str]:
        return list(self.main("releases", {}).get("accepted", []))

    def dropped_candidates(self) -> list[str]:
        return list(self.main("releases", {}).get("dropped", []))

    def alarm_state(self) -> dict[str, Any]:
        found = self.main("alarms", {})
        return found if isinstance(found, dict) else {}


def mirror_now() -> list[str]:
    """`mirror` inside a native command: the runtime's journals and the plugin's state directory."""
    from steuermaschine.native import runtime

    from .. import state as statedir

    rt = runtime()
    return mirror(getattr(rt.providers.store, "journals", None), statedir.dashboard_dir(rt.settings), rt.providers.clock.now())


class Files:
    """The same readers over the working files: only where no journals exist (a store without them, as in tests of
    the old readers). A real run always has journals."""

    def __init__(self, state_dir: Path):
        self.root = state_dir

    def __getattr__(self, name: str) -> Any:
        read = getattr(legacy, name)
        return lambda: read(self.root)


def source(w: Any) -> View | Files:
    journals = getattr(w, "journals", None)
    return View(journals) if journals is not None else Files(w.state_dir)


def mirrored(fn: Any) -> Any:
    """For a command that runs one of the programs that write the working files: after it, `mirror_now()`. A failing
    mirror never changes the command's result; it says so on stderr and the next command mirrors again."""
    import functools

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        finally:
            try:
                mirror_now()
            except Exception as error:
                import sys

                print(f"sm: the subject journals were not updated ({type(error).__name__}: {error})", file=sys.stderr)

    run.__annotations__ = dict(getattr(fn, "__annotations__", {}))  # Python 3.14+: wraps copies __annotate__, which a set __annotations__ voids
    return run
