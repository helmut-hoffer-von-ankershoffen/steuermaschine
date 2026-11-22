"""`sm release VERSION`: the release driver behind the visible verb. It takes every option of `sm release resume`
(the same function runs) and refuses first what is surely a typo: a VERSION that is no version, or one that is neither
the release in flight, the latest release, nor the next patch or minor after it. The release train calls
`sm release resume` itself, so its runs never pass this guard; its `daemon train` tick brings the journals up to date.
After a run the subject journals follow the working files (journal_view.mirrored)."""

from __future__ import annotations

import contextlib
import inspect
import re
from typing import Any

from steuermaschine.engine import Refuse, Wait
from steuermaschine.helpdoc import H
from steuermaschine.native import native

from . import release_ios
from .coord_lib import journal
from .go import world
from .journal_view import mirrored
from .release import ReleaseRun, release_resume

VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
GAP_EXITS = (1, 70)  # a refusal or a tool error: the driver writes the `sm gap` note itself
WORDS = ("patch",)  # the unfinished release, else the next patch: the runner resolves it


def released(rows: list[dict[str, Any]]) -> str:
    """The highest X.Y.Z among the release tags."""
    found = [tuple(int(x) for x in m.groups()) for r in rows if (m := VERSION.fullmatch(str(r.get("tagName", "")).removeprefix("v")))]
    return ".".join(map(str, max(found))) if found else ""


def allowed(latest: str, in_flight: str) -> list[str]:
    """The versions `sm release VERSION` takes: in flight, the latest, and the next patch and minor after the latest."""
    out = [v for v in (in_flight, latest) if v]
    if (m := VERSION.fullmatch(latest)) is not None:
        major, minor, patch = (int(x) for x in m.groups())
        out += [f"{major}.{minor}.{patch + 1}", f"{major}.{minor + 1}.0"]
    return list(dict.fromkeys(out))


def guard(version: str, rows: list[dict[str, Any]] | None, in_flight: str) -> None:
    if version in WORDS:
        return
    if not VERSION.fullmatch(version):
        raise Refuse(f"'{version}' is no version: give X.Y.Z (for example 0.9.55) or `patch`", "sm release patch", code=2)
    if rows is None:
        raise Wait("the release list of GitHub could not be read, so the version cannot be checked", "GitHub")
    latest = released(rows)
    ok = allowed(latest, in_flight)
    if version not in ok:
        nxt = allowed(latest, "")[1:2] or ["patch"]
        raise Refuse(f"{version} is neither the release in flight nor the latest or the next one ({', '.join(ok) or 'none known'})",
                     f"sm release {in_flight or nxt[0]}", code=2)


def release_drive(**options: Any) -> ReleaseRun:
    version = str(options.get("version") or "")
    if not options.get("absorb"):  # --absorb names a finished release by its version: the runner checks it
        w = world()
        try:
            rows = w.gh_json(["release", "list", "-R", w.repo, "-L", "10", "--json", "tagName"]) or []
        except RuntimeError:
            rows = None
        from .release_read import release_status

        try:
            status = release_status(line=False)
            in_flight = status.version if status.active else ""
        except Refuse:
            in_flight = ""
        guard(version, rows, in_flight)
    try:
        done = release_resume(**options)
    except Refuse as refusal:
        if refusal.code in GAP_EXITS:
            gap_note(version, refusal.code, refusal.message)
        raise
    except Exception as error:  # a tool error ends with exit 70: the gap is written before it propagates
        gap_note(version, 70, f"{type(error).__name__}: {str(error)[:200]}")
        raise
    if not isinstance(done, ReleaseRun):  # the train's stand-ins and tests hand back other shapes: nothing to add
        return done
    if done.exit == 0 and done.finished and done.accepted and not done.ios:
        done.ios = release_ios.after_accepted(world(), done.version)  # the iOS build of the version just released is a tail stage
    if done.exit in GAP_EXITS:
        gap_note(version, done.exit, done.stopped or next((line for line in reversed(done.lines) if line.strip()), "no reason shown"))
    return done


def gap_note(version: str, code: int, reason: str) -> None:
    """What `sm note` does: one line `sm gap: ...` in the log of the day. It never raises: the run's own exit stays."""
    text = " ".join(f"sm gap: `sm release {version}` exited {code}: {reason}".split())
    with contextlib.suppress(Exception):
        journal.note(text)


release_drive.__signature__ = inspect.signature(release_resume, eval_str=True)  # type: ignore[attr-defined]  # every option of `release resume`
release_drive.__annotations__ = {name: p.annotation for name, p in release_drive.__signature__.parameters.items()} | {"return": ReleaseRun}
release_drive = native("release drive", writes=True, help=H(
    "Release one version: the release driver of `sm release VERSION`, with a guard against a mistyped version.",
    "Use when you type `sm release VERSION`; the release train calls `sm release resume`, which has no guard.",
    "what `sm release resume` changes: the freeze, the release PR and its merge, the tag, PyPI, compose, acceptance and the iOS build (TestFlight).",
    "sm release 0.9.55 --live", "sm why 0.9.55",
    "VERSION must be X.Y.Z or `patch`, and the release in flight, the latest release or the next patch or minor after it;\n"
    "anything else refuses with exit 2 before anything runs. Every option of `sm release resume` works here.\n"
    "After ACCEPTED it runs the iOS stage for that version (NOT-CLEAN is an alarm, never an un-publish). Exit 75: a fact is\n"
    "not true yet (the wheel asset or PyPI), run the same command again. Every exit 1 or 70 writes its own gap note."))(mirrored(release_drive))
