"""`sm release VERSION`: the release driver behind the visible verb. It takes every option of `sm release resume`
(the same function runs) and refuses first what is surely a typo: a VERSION that is no version, or one that is neither
the release in flight, the latest release, nor the next patch or minor after it. The release train calls
`sm release resume` itself, so its runs never pass this guard; its `daemon train` tick brings the journals up to date.
After a run the subject journals follow the working files (journal_view.mirrored)."""

from __future__ import annotations

import inspect
import re
from typing import Any

from steuermaschine.engine import Refuse, Wait
from steuermaschine.helpdoc import H
from steuermaschine.native import native

from .go import world
from .journal_view import mirrored
from .release import ReleaseRun, release_resume

VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
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
    return release_resume(**options)


release_drive.__signature__ = inspect.signature(release_resume, eval_str=True)  # type: ignore[attr-defined]  # every option of `release resume`
release_drive.__annotations__ = {name: p.annotation for name, p in release_drive.__signature__.parameters.items()} | {"return": ReleaseRun}
release_drive = native("release drive", writes=True, help=H(
    "Release one version: the release driver of `sm release VERSION`, with a guard against a mistyped version.",
    "Use when you type `sm release VERSION`; the release train calls `sm release resume`, which has no guard.",
    "what `sm release resume` changes: the freeze, the release PR and its merge, the tag, PyPI, compose and acceptance.",
    "sm release 0.9.55 --live", "sm why 0.9.55",
    "VERSION must be X.Y.Z or `patch`, and the release in flight, the latest release or the next patch or minor after it;\n"
    "anything else refuses with exit 2 before anything runs. Every option of `sm release resume` works here."))(mirrored(release_drive))
