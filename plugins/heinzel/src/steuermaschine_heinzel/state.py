"""Where the plugin's state files live, and the move from the names of the scripted era.

Every state file of the merge queue, the release, the train, the run lock, the alarms and the admission is in the
project's state directory (`sm self config` shows it as `state`), under a plain name: `queue.json`, `train/`,
`release.json`, `gate.lock` and so on. The scripted era kept the same files in ~/.cache as `hz-<name>` (and two in
~/Library/Logs and ~/.local/state). `sm self migrate-state` moves them in one pass (bin/promote runs it before the
switch) and leaves a link at each old name; nothing here reads an old name or moves a file. A path whose state is
still at its old name refuses (`steuermaschine.statemove.MigrationError`) and names the command that moves it.

`moves(home)` is the plugin's `sm_state_moves`: one move per old path that exists (old side) or per new path that
exists (new side, so that `--back` finds what this release made first)."""

from __future__ import annotations

import fnmatch
from pathlib import Path

from steuermaschine import config
from steuermaschine.statemove import MigrationError, Move, pending, require_migrated

CACHE = (".cache",)  # the scripted era's directory under the home directory
OLD_PREFIX = "hz-"
# The names in CACHE that are this plugin's state, as glob patterns of the new name (old name: OLD_PREFIX + name).
# Nothing else in that directory is touched (builders keep their own files there).
NAMES: tuple[str, ...] = (
    "queue.json", "queue.lock", "queue.log",
    "train",
    "release.json", "release.json.*", "release.lock", "release", "release-holds", "release-flow", "release-tag-on-main",
    "release-dropped", "release-rejections", "release-plugins.json", "release-waiver.json", "release-main-gate.json",
    "merge-freeze",
    "gate.*", "gate-allow-full.log",
    "alarm.json", "alarm.lock",
    "note.lock", "ask.lock", "ask-watermark.json",
    "flakes", "ci-deferred.txt", "assets-health.json",
    "focused-pr-registry.lock", "focused-admission-receipts", "focused-release-admission-attempts", "focused-admission-work",
    "focused-validator",
    "go-focus.json", "governor.json", "status-slots.json", "next-claims.json", "next.json", "rehearse.json", "live-uv.log", "crash-ack.ts", "uv-ack.ts",
)
# Old paths outside CACHE (relative to the home directory) -> new name.
ELSEWHERE: dict[str, str] = {
    "Library/Logs/hz-gate-history.jsonl": "gate-history.jsonl",
    ".local/state/hz-leakwatch": "leakwatch",
    ".local/state/hz-loadguard": "loadguard-legacy",
}


def root() -> Path:
    """The project's state directory: the running command's, else (a moved function called directly) the one of the
    project `heinzel` in the state home. Refuses while any state of this plugin is still at its old name."""
    try:
        from steuermaschine.native import runtime

        base = Path(runtime().project.home)
    except (RuntimeError, AttributeError):
        base = config.state_home() / "heinzel"
    key = (base, Path.home())
    if key not in _VERIFIED:
        if waiting := pending(moves(base)):
            raise MigrationError(f"{len(waiting)} state path(s) are not migrated yet (first: {waiting[0].old} -> {waiting[0].new}): "
                                 "run `sm self migrate-state` (bin/promote runs it)")
        _VERIFIED.add(key)
    return base


_VERIFIED: set[tuple[Path, Path]] = set()  # state directories whose moves were all done, in this process


def old_of(name: str, home: Path | None = None) -> Path:
    """The scripted era's path of the new name `name` (its first part: `train/v1.json` -> ~/.cache/hz-train)."""
    base = Path.home() if home is None else home
    top = name.split("/", 1)[0]
    for old, new in ELSEWHERE.items():
        if new == top:
            return base / old
    return base.joinpath(*CACHE) / f"{OLD_PREFIX}{top}"


def known(name: str) -> bool:
    top = name.split("/", 1)[0]
    return top in ELSEWHERE.values() or any(fnmatch.fnmatchcase(top, pattern) for pattern in NAMES)


def path(name: str, under: Path | None = None) -> Path:
    """The path of the state file or directory `name` (`queue.json`, `train/v1.2.3.json`). Refuses while that state
    is still at its old name. `under`: another state directory (a test's)."""
    base = root() if under is None else under
    base.mkdir(parents=True, exist_ok=True)  # the directory itself; what is in it is made by its writer, as in ~/.cache
    top = name.split("/", 1)[0]
    if known(name):
        require_migrated(Move(old_of(top), base / top))
    return base / name


def moves(home: Path) -> list[Move]:
    """Every move this plugin knows: one per plain name, and for a pattern (`gate.*`) one per old or new path that
    exists now (the new side, so that `--back` finds what this release made first)."""
    home = Path(home)
    old_dir = Path.home().joinpath(*CACHE)
    tops: set[str] = set()
    for pattern in NAMES:
        if not any(char in pattern for char in "*?["):
            tops.add(pattern)
            continue
        tops |= {p.name[len(OLD_PREFIX):] for p in old_dir.glob(OLD_PREFIX + pattern)}
        tops |= {p.name for p in home.glob(pattern)} if home.is_dir() else set()
    out = [Move(old_dir / f"{OLD_PREFIX}{top}", home / top) for top in sorted(tops)]
    return out + [Move(Path.home() / old, home / new) for old, new in ELSEWHERE.items()]


def dashboard_dir(settings: object) -> Path:
    """The directory the read-only views read state files from (setting legacy_state_dir); none: the state directory."""
    value = settings.get("legacy_state_dir") if hasattr(settings, "get") else None
    return Path(str(value)).expanduser() if value else root()
