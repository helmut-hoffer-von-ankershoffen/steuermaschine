"""Which core release flow a version uses: the release PR (default) or tag-on-green-main (ADR 0144, flag).

Flag: the setting queue_release_tag_on_main (was HZ_RELEASE_TAG_ON_MAIN=1), or the file ~/.cache/hz-release-tag-on-main (content ignored).
Default OFF. The flag is read once, when a version starts; the chosen flow is pinned in the sidecar
~/.cache/hz-release-flow/vX.Y.Z.json, so flipping the flag never changes a release that is already running. A version
that has a receipt but no sidecar started before this module existed and stays on the release-PR flow.

The sidecar is never a receipt field (pause, prepare-only and rejection require exact receipt key sets). It holds
{"version", "flow", "bumps": [{"branch", "pr", "sha", "verdict", "why"}], "tag_dispatched_at"}.
"""

import json
import os
import re
import tempfile
from pathlib import Path

from . import edge  # moved: the flag and the directory are settings
from ... import state as statedir  # the plugin's state paths

FLAG = "HZ_RELEASE_TAG_ON_MAIN"  # the old variable of the setting queue_release_tag_on_main
TAG_ON_MAIN, RELEASE_PR = "tag-on-main", "release-pr"
EXACT_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


class FlowError(Exception):
    pass


def flow_dir() -> Path:
    """The setting queue_release_flow_dir only for tests (their conftest isolates it); never set it for real releases."""
    return Path(edge.setting("queue_release_flow_dir") or statedir.path("release-flow"))


def flag_file() -> Path:
    flow = edge.setting("queue_release_flow_dir")
    return Path(flow) / "release-tag-on-main" if flow else statedir.path("release-tag-on-main")


def flag_on() -> bool:
    """The coordinator's switch for NEW versions only; default OFF."""
    return bool(edge.setting("queue_release_tag_on_main")) or flag_file().exists()


def sidecar(version: str) -> Path:
    if not EXACT_VERSION.fullmatch(version or ""):
        raise FlowError("release flow needs an exact version X.Y.Z")
    return flow_dir() / f"v{version}.json"


def read(version: str) -> dict | None:
    """None when absent; a corrupt sidecar fails closed (never silently switches flows)."""
    path = sidecar(version)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise FlowError(f"release flow record {path} is unreadable; preserving it for recovery") from exc
    if not isinstance(data, dict) or data.get("version") != version or data.get("flow") not in (TAG_ON_MAIN, RELEASE_PR):
        raise FlowError(f"release flow record {path} is malformed; preserving it for recovery")
    return data


def write(data: dict) -> None:
    path = sidecar(data["version"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".flow-", delete=False) as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, path)


def update(version: str, **fields) -> dict:
    data = (read(version) or {}) | fields
    write(data)
    return data


def flow_of(version: str) -> str | None:
    data = read(version)
    return data["flow"] if data else None


def choose(version: str, started_before: bool) -> str:
    """Pin the flow of `version` on its first start; later calls return the pinned flow whatever the flag says."""
    pinned = flow_of(version)
    if pinned:
        return pinned
    flow = TAG_ON_MAIN if flag_on() and not started_before else RELEASE_PR
    write({"version": version, "flow": flow, "bumps": []})
    return flow


def is_tag_on_main(version: str | None) -> bool:
    """For other tools (hz-queue, hz-train): unreadable or absent means the release-PR flow (the stricter rules)."""
    try:
        return bool(version) and flow_of(version) == TAG_ON_MAIN
    except FlowError:
        return False


def current_bump(data: dict) -> dict | None:
    bumps = data.get("bumps") or []
    return bumps[-1] if bumps else None
