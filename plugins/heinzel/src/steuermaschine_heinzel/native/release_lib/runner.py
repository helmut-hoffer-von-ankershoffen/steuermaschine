#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""hz-release X.Y.Z|patch [--intro "what changed for users"] [--snapshot-acceptance] [--timeout MINUTES]   |   hz-release --plugin NAME

The release steps of AGENTS.md section 6 as one resumable command. Every step is checked against GitHub or PyPI first,
so running it again continues where it stopped: freeze merges, wait for a green main tip, open and merge the release
PR (`--intro` is needed only to open it), wait for the tag, unfreeze (the freeze covers cut -> tag only), PyPI, wait
until no compose runs, compose, acceptance, then print the verdict with the failing cases. `patch` reuses the version
of an unfinished release.
Dispatch times are kept in ~/.cache/hz-release.json to tell our workflow runs from other people's. Exit 0 accepted.

`--snapshot-acceptance` pins acceptance to an exact, tag-pinned public snapshot (the freeze is lifted at the tag anyway).
The tagged workflow must support expected artifact digests and a run marker; older tags retain the default path.
A release owns its freeze, one core coordinator runs at a time, and a changed resume identity stops for review.
`--pause` releases only an unfinished, pre-tag release-owned hold after fail-closed remote checks; normal resume
reacquires that same hold before contacting GitHub. Incident holds are never removed.
`--prepare-only` resumes the exact paused hold and original PR readiness, audits the preparation and exits before
main validation, merge, dispatch or publication. Refresh the release branch only after this preparation succeeds.
`X.Y.Z --drop-candidate --reason TEXT [--close-pr]` drops a candidate that is provably before publication (a red
candidate that will be cut again), so a fresh `hz-release X.Y.Z` starts clean. It proves from typed replies: no tag
vX.Y.Z and no GitHub release on the remote, PyPI answers 404 for the version, no merged release PR, the release PR
closed or absent, the branch release/vX.Y.Z absent and no workflow run on that branch still active (a candidate
that still runs is never dropped, with or without `--close-pr`: wait for it or cancel it yourself). `--close-pr`
closes the open release PR and deletes that branch first, then proves everything again. It cancels no workflow run. Then it removes ONLY the hold this release
owns (never the incident sentinel or another release's hold) and moves the receipt, with the reason and the evidence,
to ~/.cache/hz-release-dropped/vX.Y.Z-<UTC>.json; an owner waiver recorded for this version moves there too. Every
other case is refused with the exact unmet condition; a transport failure is never read as absence. Not for a
tag-on-green-main release, a finished release or a tail (past its tag: reject or absorb instead).
`--plugin NAME` releases a plugin whose version in heinzel-plugins main is not on PyPI yet, and verifies PyPI.

Pipelined (research/pipelined-release-train-2026-11-08.md): at the tag the receipt moves to ~/.cache/hz-release/vX.json
(lock vX.lock) and the global lock is freed, so the next candidate starts while this tail runs; `hz-release X` resumes
a tail by name. Before its merge (= publication) a release waits at `previous release accepted` for the previous tail's
verdict: ACCEPTED, or `hz-release --absorb X --reason "fixed in Y by #PR"` after a fix-forward (no waiver).
`hz-release migrate-legacy` (one time, no release command running): moves a FINISHED global receipt written by the
pre-handoff hz-release to ~/.cache/hz-release/vX.json; a missing `accepted` is recorded as false (NOT accepted).

Tag-on-green-main (core ADR 0144; research/continuous-release-2026-11-08.md), behind the flag HZ_RELEASE_TAG_ON_MAIN
(env =1 or the file ~/.cache/hz-release-tag-on-main; default OFF; read once per version and pinned in
~/.cache/hz-release-flow/vX.json, see tools/release_flow.py). Steps: main gate, `bump PR open` (release.sh --on-main:
branch bump/vX), `bump PR merged` (hz-ship: the queue merges it like any lean PR; no freeze, no pre-merge guard),
`candidate verified` (release_candidate_policy.verify_commit on the bump commit's first-attempt full main push run, the
existing waiver rule only), `tag` (dispatch release-tag.yml with that sha; the tag must point at it), handoff, release
published, `previous release accepted` (now before PyPI), PyPI, compose, acceptance. A refused candidate stops with
`fix forward on main, then hz-release X --refresh`: --refresh cuts `chore(release): vX (refresh)` at the same version
and its commit is the next candidate. Current restriction: this optional flow only adopts already-created bump or
refresh PRs. Fresh creation is refused until its push path has separately reviewed focused hook inputs; keep the
mode disabled for new cuts. Existing tagged tails and the ordinary release/vX flow remain supported."""

import argparse
import ast
import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from steuermaschine.config import child_environment

from ... import state as statedir  # the plugin's state paths
from ..queue_lib import (
    core_facts,
    hzlib,  # noqa: E402
    release_flow,
)
from . import acceptance_capacity as release_acceptance_capacity
from . import candidate_capacity as release_candidate_capacity
from . import candidate_policy as release_candidate_policy
from . import edge  # moved: siblings are imported relatively, no sys.path entry
from . import notes_audit as release_notes_audit  # noqa: E402
from . import rejection as release_rejection
from . import snapshot as release_snapshot  # noqa: E402

CORE, PLUGINS = hzlib.CORE, hzlib.PLUGINS
POLL = 30
# PyPI lists a fresh upload a few moments after the publishing run ends: a successful run without the version on PyPI
# is a failure only after this many seconds of polling (heinzel-resource 0.1.5, run 43442192106).
PYPI_PROPAGATION = 300
ACCEPTED = ("ACCEPTED", "ACCEPTED_EXCEPT_UNPROVABLE")
# Exact historical rebuild workflows retain their original resume path. Unknown/new workflows require a producer
# manifest; absence can never downgrade them to the historical path. This is readiness only, not upload authority.
LEGACY_PYPI_WORKFLOWS = {
    "cc04ac52621d09a63ac8563cbc8cc5dfa65e6ddac2d7f0b4d9da790334d42ebd",  # e822215e5 (through 0.9.16)
    "d19c2376cca0f47565dba9a348820ee83553736f5810db47ca07f3b766d05d05",  # e29b41f85
    "da07d80b89a55fdd20e7fa3ddefed29cb33d52837d3a1d7b5c164f93aab29dce",  # d6f8113d1
    "4ebd6e9f19cfd67a2207c81c4737c4cdfa5b0c30e1e3f378c58799c52be88147",  # ab415cb7d
    "9d6625b25aef8ef64a3d4f9df4e63dd74eaed3e069510189561e1bdeaef1c466",  # 2cdeb3afd
}
MAX_PRODUCER_MANIFEST = 64 * 1024
VERDICT = re.compile(r'^(?P<job>[^\t]+)\t[^\t]*\t\S+\s+(?P<json>\{"verdict".*\})\s*$')


class Stop(Exception):
    pass


# Pipelined train (research/pipelined-release-train-2026-11-08.md): the global receipt ~/.cache/hz-release.json and
# the global lock ~/.cache/hz-release.lock cover only freeze -> tag (one candidate at a time). At the tag the receipt is
# handed off to ~/.cache/hz-release/vX.Y.Z.json under its own lock vX.Y.Z.lock and the global lock is freed, so the
# next candidate can be cut while this release's tail (PyPI, compose, acceptance) runs. RECEIPT_OVERRIDE then points
# every receipt read and write (state, remember, rejection, capacity) at the tail receipt; key sets are unchanged.
RECEIPT_OVERRIDE: Path | None = None
GLOBAL_LOCK = None  # open file object holding the global release lock (main)
TAIL_LOCK: tuple[str, object] | None = None  # (version, open file object holding vX.Y.Z.lock)
EXACT_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def global_receipt() -> Path:
    return statedir.path("release.json")


def plugin_receipt() -> Path:
    """Plugin release dispatch stamps: their own file, never keys of the core release receipt."""
    return statedir.path("release-plugins.json")


PLUGIN_KEY = re.compile(r"plugin-heinzel-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9]+\.[0-9]+\.[0-9]+(?:_[a-z_]+)?")


def plugin_keys(data: dict) -> dict:
    """The plugin dispatch stamps (and their run bindings) inside a receipt."""
    return {key: value for key, value in data.items() if PLUGIN_KEY.fullmatch(key)}


def paths() -> tuple[Path, Path]:
    return statedir.path("merge-freeze"), RECEIPT_OVERRIDE or global_receipt()


def tail_dir() -> Path:
    return statedir.path("release")


def tail_receipt(version: str) -> Path:
    return tail_dir() / f"v{version}.json"


def tail_lock(version: str) -> Path:
    return tail_dir() / f"v{version}.lock"


def vkey(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def acquire_global_lock() -> None:
    """Core and plugin commands share the global receipt; they take this lock before reading or writing any state."""
    global GLOBAL_LOCK
    path = global_receipt().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise Stop("another release command is running") from exc
    GLOBAL_LOCK = lock


def free_global_lock() -> None:
    global GLOBAL_LOCK
    if GLOBAL_LOCK is not None:
        fcntl.flock(GLOBAL_LOCK, fcntl.LOCK_UN)
        GLOBAL_LOCK.close()
        GLOBAL_LOCK = None


def take_tail_lock(version: str) -> None:
    """One process per release tail. Non-blocking: a second tail command for the same version stops."""
    global TAIL_LOCK
    if TAIL_LOCK is not None:
        if TAIL_LOCK[0] == version:
            return
        raise Stop(f"this command already owns the tail of v{TAIL_LOCK[0]}")
    tail_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = tail_lock(version).open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise Stop(f"tail of v{version} is running") from exc
    TAIL_LOCK = (version, lock)


def free_tail_lock() -> None:
    global TAIL_LOCK
    if TAIL_LOCK is not None:
        fcntl.flock(TAIL_LOCK[1], fcntl.LOCK_UN)
        TAIL_LOCK[1].close()
        TAIL_LOCK = None


def route_to_tail(version: str) -> None:
    """Resume, reject or absorb a release past its tag: its own receipt and lock; the global lock is freed."""
    global RECEIPT_OVERRIDE
    take_tail_lock(version)
    RECEIPT_OVERRIDE = tail_receipt(version)
    free_global_lock()


def fsync_dir(path: Path) -> None:
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def hand_off_tail() -> None:
    """At the tag: move the receipt to ~/.cache/hz-release/vX.json under vX.lock, then free the global lock. The
    global receipt is then absent, which starts the next release (one candidate at a time stays: freeze -> tag).
    Idempotent: a resume past the handoff is already routed to the tail receipt by main()."""
    global RECEIPT_OVERRIDE
    if RECEIPT_OVERRIDE is not None:
        return
    version = state().get("version")
    if not isinstance(version, str) or not EXACT_VERSION.fullmatch(version):
        raise Stop("cannot hand off a receipt without an exact version")
    target = tail_receipt(version)
    take_tail_lock(version)
    if target.exists():
        raise Stop(f"tail receipt {target} already exists next to the global receipt; preserving both for recovery")
    try:
        os.replace(global_receipt(), target)
        fsync_dir(target.parent)
        fsync_dir(global_receipt().parent)
    except OSError as exc:
        raise Stop("cannot hand off the release receipt to its tail receipt") from exc
    RECEIPT_OVERRIDE = target
    free_global_lock()
    hzlib.say(f"ok   handed off to the tail receipt v{version}.json (release lock free for the next candidate)")


def legacy_finished_receipt() -> dict | None:
    """The global receipt when it is a FINISHED release with an exact version (written before the handoff existed)."""
    try:
        data = json.loads(global_receipt().read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise Stop("release receipt is unreadable or corrupt; preserving it for recovery") from exc
    if (isinstance(data, dict) and data.get("finished") and isinstance(data.get("version"), str)
            and EXACT_VERSION.fullmatch(data["version"])):
        return data
    return None


def migrate_legacy_receipt() -> str | None:
    """A FINISHED global receipt written before the handoff existed (e.g. 0.9.31) becomes its tail receipt, so
    `previous release accepted` and `--absorb` see it. A missing `accepted` counts as NOT accepted (the old verdict
    line did not record it): the next release then needs `--absorb X --reason` with a fix PR or a review line.
    Caller holds the global lock. Takes vX.lock only for the move, so N's tail commands are never held up."""
    try:
        data = json.loads(global_receipt().read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise Stop("release receipt is unreadable or corrupt; preserving it for recovery") from exc
    if not isinstance(data, dict) or not data.get("finished"):
        return None
    version = data.get("version")
    if not isinstance(version, str) or not EXACT_VERSION.fullmatch(version):
        raise Stop("finished legacy receipt has no exact version; preserving it for recovery")
    target = tail_receipt(version)
    if target.exists():
        raise Stop(f"tail receipt {target} already exists next to the finished global receipt; preserving both for recovery")
    tail_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    with tail_lock(version).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Stop(f"tail of v{version} is running") from exc
        moved = data | {"accepted": data.get("accepted") is True, "migrated_from_global_at": now_iso()}
        global RECEIPT_OVERRIDE
        saved, RECEIPT_OVERRIDE = RECEIPT_OVERRIDE, target
        try:
            write_state(moved)
        finally:
            RECEIPT_OVERRIDE = saved
        global_receipt().unlink()
        fsync_dir(global_receipt().parent)
    hzlib.say(f"ok   migrated the finished legacy receipt {version} to the tail receipt v{version}.json "
              f"(accepted: {moved['accepted']})")
    return version


def tail_receipts() -> list[tuple[str, dict]]:
    """(version, receipt) of every tail receipt, oldest first. Unreadable tail receipts fail closed."""
    out = []
    for path in tail_dir().glob("v*.json") if tail_dir().is_dir() else []:
        version = path.stem[1:]
        if not EXACT_VERSION.fullmatch(version):
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise Stop(f"tail receipt {path.name} is unreadable or corrupt; preserving it for recovery") from exc
        if not isinstance(data, dict):
            raise Stop(f"tail receipt {path.name} is not an object; preserving it for recovery")
        out.append((version, data))
    return sorted(out, key=lambda item: vkey(item[0]))


def guard_tails(version: str) -> None:
    """Never cut at or below a release that is past its tag: the tail owns its version (resume it by name)."""
    if RECEIPT_OVERRIDE is not None:
        return
    for other, data in tail_receipts():
        if other == version:
            raise Stop(f"release {version} is past its tag (tail receipt v{version}.json): run sm release resume {version}")
        if vkey(other) > vkey(version) and not data.get("finished"):
            raise Stop(f"release {other} is past its tag and unfinished; never cut {version} below it")


def previous_accepted(version: str) -> bool:
    """N+1's merge publishes it, so it waits for N's verdict: ok when N is ACCEPTED or explicitly absorbed."""
    previous = [(other, data) for other, data in tail_receipts() if vkey(other) < vkey(version)]
    legacy = legacy_finished_receipt()  # pre-handoff hz-release: a missing `accepted` is NOT accepted
    if legacy and vkey(legacy["version"]) < vkey(version) and legacy["version"] not in {v for v, _ in previous}:
        previous = sorted(previous + [(legacy["version"], legacy)], key=lambda item: vkey(item[0]))
    if not previous:
        return True
    other, data = previous[-1]
    if not data.get("finished"):
        return False
    if data.get("accepted") is True or data.get("absorbed_by"):
        return True
    raise Stop(f"previous release {other} NOT ACCEPTED (run {data.get('acceptance_run')}): fix forward and "
               f"sm release resume {other} --absorb --reason ..., or sm release train clear")


def newest_core_tag() -> tuple[int, str]:
    """(gh exit, newest vX.Y.Z tag): held pre-releases count (Publish holds every release until acceptance, core
    ADR 0114), so a held or absorbed N is never cut again; edge-v/app-v tags and drafts never count."""
    code, out = hzlib.gh("api", f"repos/{CORE}/releases?per_page=30", "--jq",
                         "[.[] | select(.draft | not) | .tag_name] | .[]")
    tags = [t for t in out.split() if t.startswith("v") and EXACT_VERSION.fullmatch(t[1:])] if code == 0 else []
    if tags:
        return 0, max(tags, key=lambda t: vkey(t[1:]))
    return hzlib.gh("api", f"repos/{CORE}/releases/latest", "--jq", ".tag_name")


def absorber_version(version: str) -> str:
    """The release that absorbs X: the open release/* PR above X, else releases/latest + 1 patch."""
    prs = hzlib.ghj("pr", "list", "-R", CORE, "--state", "open", "--json", "headRefName", default=None)
    if isinstance(prs, list):
        found = sorted((p["headRefName"].removeprefix("release/v") for p in prs if isinstance(p, dict)
                        and str(p.get("headRefName", "")).startswith("release/v")
                        and EXACT_VERSION.fullmatch(p["headRefName"].removeprefix("release/v"))), key=vkey)
        above = [v for v in found if vkey(v) > vkey(version)]
        if above:
            return above[0]
    code, out = newest_core_tag()
    latest = out.strip().removeprefix("v")
    if code != 0 or not EXACT_VERSION.fullmatch(latest):
        raise Stop("cannot determine the absorbing release (no open release PR, latest release unreadable)")
    major, minor, patch = vkey(max(latest, version, key=vkey))
    return f"{major}.{minor}.{patch + 1}"


def absorb_release(version: str, reason: str) -> int:
    """Record that the next release absorbs a finished NOT ACCEPTED release (fix forward), so N+1 may merge."""
    reason = (reason or "").strip()
    if not EXACT_VERSION.fullmatch(version or ""):
        raise Stop("--absorb requires an exact version X.Y.Z")
    if len(reason) < 10 or not re.search(r"#[0-9]+|review:", reason):
        raise Stop('--absorb needs --reason naming the fix PR ("fixed in N+1 by #PR") or, for a harness/infra-only '
                   'failure, an independent review line ("review: ...")')
    if RECEIPT_OVERRIDE != tail_receipt(version):
        raise Stop(f"no tail receipt for {version}: only a release past its tag can be absorbed")
    data = state()
    if data.get("version") != version:
        raise Stop("tail receipt version does not match")
    if not data.get("finished"):
        raise Stop(f"release {version} is unfinished: wait for its verdict before absorbing it")
    if data.get("accepted") is True:
        raise Stop(f"release {version} is ACCEPTED: nothing to absorb")
    if data.get("absorbed_by"):
        if data.get("absorb_reason") == reason:
            hzlib.say(f"ABSORBED {version} by {data['absorbed_by']} (already)")
            return 0
        raise Stop(f"release {version} is already absorbed by {data['absorbed_by']}")
    by = absorber_version(version)
    remember(absorbed_by=by, absorb_reason=reason, absorbed_at=now_iso())
    hzlib.say(f"ABSORBED {version} by {by}: {reason}")
    return 0


def state() -> dict:
    """An absent receipt starts a release; an unreadable or malformed receipt never does."""
    try:
        data = json.loads(paths()[1].read_text())
    except FileNotFoundError:
        if paths()[1] != plugin_receipt() and hzlib.release_holds_present():
            raise Stop("release receipt is missing while a snapshot hold is active")
        return {}
    except (OSError, ValueError) as exc:
        raise Stop("release receipt is unreadable or corrupt; preserving it for recovery") from exc
    if not isinstance(data, dict):
        raise Stop("release receipt is not an object; preserving it for recovery")
    return data


def read_json_object(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise Stop(f"{path.name} is unreadable or corrupt; preserving it for recovery") from exc
    if not isinstance(data, dict):
        raise Stop(f"{path.name} is not an object; preserving it for recovery")
    return data


def repair_receipt(apply: bool) -> int:
    """One-time repair (bead heinzel-management-98cw): plugin dispatch stamps written into the core receipt move to
    their own file. The core receipt keeps every other key; if nothing else is left it is removed, which is the state a
    tag handoff leaves (a global receipt with only `version` would read as an unfinished candidate and block the next
    cut). A backup copy is written first. Without apply it only prints the plan. Caller holds the global lock."""
    core = read_json_object(global_receipt())
    if core is None or not plugin_keys(core):
        hzlib.say("nothing to repair: the release receipt holds no plugin stamps")
        return 0
    moved = plugin_keys(core)
    rest = {key: value for key, value in core.items() if key not in moved}
    plugins = read_json_object(plugin_receipt()) or {}
    clash = sorted(key for key in moved if key in plugins and plugins[key] != moved[key])
    if clash:
        raise Stop(f"plugin receipt already holds other values for {', '.join(clash)}; preserving both for recovery")
    merged = plugins | moved
    tails = sorted((path.stem[1:] for path in tail_dir().glob("v*.json") if EXACT_VERSION.fullmatch(path.stem[1:])), key=vkey)
    latest = read_json_object(tail_receipt(tails[-1])) if tails else None
    backup = global_receipt().with_name(f"release.json.bak-{now_iso().replace(':', '')}")
    hzlib.say(f"core receipt {global_receipt()}: {json.dumps(core, sort_keys=True)}")
    hzlib.say(f"  -> {json.dumps(rest, sort_keys=True) if rest else 'absent (as after the tag handoff)'}")
    hzlib.say(f"plugin receipt {plugin_receipt()}: {json.dumps(plugins, sort_keys=True)}")
    hzlib.say(f"  -> {json.dumps(merged, sort_keys=True)}")
    if latest is not None:
        hzlib.say(f"core evidence: tail receipt v{tails[-1]}.json version={latest.get('version')} finished={latest.get('finished')} "
                  f"accepted={latest.get('accepted')} acceptance_run={latest.get('acceptance_run')}")
    hzlib.say(f"backup {backup}")
    if not apply:
        hzlib.say("DRY-RUN nothing written; run with --apply to repair")
        return 0
    shutil.copy2(global_receipt(), backup)
    global RECEIPT_OVERRIDE
    previous, RECEIPT_OVERRIDE = RECEIPT_OVERRIDE, plugin_receipt()
    try:
        write_state(merged)
    finally:
        RECEIPT_OVERRIDE = previous
    if rest:
        write_state(rest)
    else:
        global_receipt().unlink()
        fsync_dir(global_receipt().parent)
    hzlib.say(f"REPAIRED moved {len(moved)} plugin stamps; backup at {backup}")
    return 0


def write_state(data: dict) -> None:
    """Publish a complete durable receipt atomically while the command owns the shared release lock."""
    target = paths()[1]
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=target.parent, prefix=".sm-release-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise Stop("cannot atomically persist the release receipt") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def remember(**fields) -> None:
    write_state(state() | fields)


def pypi_has(name: str, version: str) -> bool:
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=20) as reply:
            return reply.status == 200
    except (urllib.error.URLError, OSError):
        return False


def now_iso(offset: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def runs(repo: str, workflow: str, **extra) -> list[dict]:
    args = ["run", "list", "-R", repo, "--workflow", workflow, "-L", "10", "--json", "databaseId,status,conclusion,createdAt,headSha,displayTitle,attempt"]
    for flag, value in extra.items():
        args += [f"--{flag}", value]
    return hzlib.ghj(*args, default=[]) or []


def check_capacity_replacement(data: dict[str, Any], run: dict[str, Any], run_id: int) -> None:
    try:
        release_acceptance_capacity.validate_replacement(data, run, CORE, run_id)
    except release_candidate_capacity.InvalidCapacity as exc:
        raise Stop(str(exc)) from None


def ours(repo: str, workflow: str, key: str) -> dict | None:
    """Once a marked run is found, resume by its persisted ID rather than a moving newest-runs window."""
    data = state()
    since, marker, commit = (data.get(name) for name in (key, f"{key}_marker", f"{key}_commit"))
    run_id = data.get(f"{key}_run_id")
    if marker and run_id:
        if type(run_id) is not int or run_id <= 0:
            raise Stop("persisted workflow run ID is invalid")
        run = hzlib.ghj("api", f"repos/{repo}/actions/runs/{run_id}", default=None)
        if not isinstance(run, dict):
            return None
        if (run.get("id") != run_id or run.get("path") != f".github/workflows/{workflow}"
                or run.get("head_sha") != commit or run.get("display_title", "").split()[-1:] != [marker]):
            raise Stop("persisted workflow run identity changed")
        if key == "acceptance" and data.get("acceptance_identity"):
            if type(data.get("acceptance_run_attempt")) is not int or run.get("run_attempt") != data["acceptance_run_attempt"]:
                raise Stop("acceptance run attempt changed; retain original rejection")
        if key == "acceptance":
            check_capacity_replacement(data, run, run_id)
        return {"databaseId": run["id"], "status": run["status"], "conclusion": run.get("conclusion"),
                "headSha": run["head_sha"], "createdAt": run["created_at"], "displayTitle": run["display_title"], "attempt": run.get("run_attempt")}
    asked = data.get(f"{key}_dispatch") if not marker else None
    if isinstance(asked, dict):
        return bound_run(repo, workflow, key, since, asked, data)
    listed = runs(repo, workflow)
    if marker and any(r.get("displayTitle", "").split()[-1:] == [marker]
                      and r.get("headSha") != commit for r in listed):
        raise Stop("marked workflow ref changed after harness resolution")
    found = [r for r in listed if since and r["createdAt"] >= since
             and (not marker or r.get("displayTitle", "").split()[-1:] == [marker])
             and (not commit or r.get("headSha") == commit)]
    if marker and len(found) > 1:
        raise Stop("ambiguous marked workflow dispatch; preserve all runs for review")
    selected = max(found, key=lambda r: r["createdAt"]) if found else None
    if marker and selected:
        if key == "acceptance" and data.get("acceptance_identity") and selected.get("attempt") != 1:
            raise Stop("acceptance discovery requires the original run attempt")
        if key == "acceptance" and data.get("acceptance_capacity") is not None:
            selected_id = selected.get("databaseId")
            if type(selected_id) is not int or selected_id <= 0:
                raise Stop("capacity replacement run ID is invalid")
            run = hzlib.ghj("api", f"repos/{repo}/actions/runs/{selected_id}", default=None)
            if not isinstance(run, dict):
                raise Stop("capacity replacement run identity cannot be verified")
            check_capacity_replacement(data, run, selected_id)
        remember(**{f"{key}_run_id": selected["databaseId"], f"{key}_run_attempt": selected.get("attempt")})
    return selected


def bound_run(repo: str, workflow: str, key: str, since: str | None, asked: dict, data: dict) -> dict | None:
    """The run of an unmarked dispatch: exactly its workflow, event workflow_dispatch, the dispatched head SHA and
    created after the dispatch. Once adopted it is read by its persisted ID. Every run looked at and not taken is
    recorded with the reason under `{key}_rejected`; the adopted one under `{key}_adopted`. Two candidates stop."""
    if asked.get("repo") != repo or asked.get("workflow") != workflow:
        raise Stop(f"the recorded {key} dispatch is for another workflow; preserving it")
    run_id = data.get(f"{key}_run_id")
    if run_id is not None:
        if type(run_id) is not int or run_id <= 0:
            raise Stop("persisted workflow run ID is invalid")
        run = hzlib.ghj("api", f"repos/{repo}/actions/runs/{run_id}", default=None)
        if not isinstance(run, dict):
            return None
        if (run.get("id") != run_id or run.get("path") != f".github/workflows/{workflow}" or run.get("head_sha") != asked.get("sha")
                or run.get("event") != "workflow_dispatch"):
            raise Stop("persisted workflow run identity changed")
        return {"databaseId": run["id"], "status": run["status"], "conclusion": run.get("conclusion"), "headSha": run["head_sha"],
                "createdAt": run["created_at"], "displayTitle": run.get("display_title", ""), "attempt": run.get("run_attempt")}
    found, rejected = [], dict(data.get(f"{key}_rejected") or {})
    listed = hzlib.ghj("run", "list", "-R", repo, "--workflow", workflow, "-L", "20", "--json",
                       "databaseId,status,conclusion,createdAt,headSha,displayTitle,attempt,event", default=[]) or []
    for r in listed:
        if not since or r.get("createdAt", "") < since:
            continue  # before this dispatch: never a candidate, not worth a record
        why = ("not a workflow_dispatch run" if r.get("event") != "workflow_dispatch"
               else f"head {str(r.get('headSha'))[:12]} is not the dispatched {asked.get('sha', '')[:12]}" if r.get("headSha") != asked.get("sha") else "")
        if why:
            rejected[str(r.get("databaseId"))] = why
        else:
            found.append(r)
    if rejected != (data.get(f"{key}_rejected") or {}):
        remember(**{f"{key}_rejected": rejected})
    if len(found) > 1:
        raise Stop(f"ambiguous {workflow} runs {', '.join(str(r['databaseId']) for r in found)} for one {key} dispatch at the same "
                   "head; the run list cannot show their inputs, so none is adopted: preserve all runs for review")
    if not found:
        return None
    selected = found[0]
    remember(**{f"{key}_run_id": selected["databaseId"], f"{key}_run_attempt": selected.get("attempt"),
                f"{key}_adopted": {"run": selected["databaseId"], "sha": selected.get("headSha"), "created": selected.get("createdAt"), "at": now_iso()}})
    return selected


def wait(label: str, done, timeout: float, check=None) -> None:
    """Poll `done()` until true. `check()` may raise Stop for a failure that waiting will not fix."""
    deadline = time.time() + timeout
    while not done():
        if check:
            check()
        if time.time() > deadline:
            raise Stop(f"{label}: timed out")
        hzlib.sleep(POLL)


def step(name: str, done, act, timeout: float, check=None) -> None:
    if done():
        hzlib.say(f"ok   {name} (already)")
        return
    if act:
        act()
    wait(name, done, timeout, check)
    hzlib.say(f"ok   {name}")


def release_published(tag: str) -> bool:
    return hzlib.gh("api", f"repos/{CORE}/releases/tags/{tag}", "--jq", ".draft")[1].strip() == "false"


def release_producer_ready(tag: str) -> bool:
    """Wait for the actual manifest producer before requesting PyPI; public verification still authorizes upload."""
    if not release_snapshot.VERSION.fullmatch(tag.removeprefix("v")) or not tag.startswith("v"):
        raise Stop("release producer requires a canonical final version tag")
    ref = hzlib.ghj("api", f"repos/{CORE}/git/ref/tags/{tag}", default=None)
    if not isinstance(ref, dict) or ref.get("ref") != f"refs/tags/{tag}" or not isinstance(ref.get("object"), dict):
        raise Stop("cannot resolve the exact release producer tag")
    source = ref["object"]
    if (source.get("type") == "tag" and isinstance(source.get("sha"), str)
            and release_snapshot.COMMIT.fullmatch(source["sha"])):
        annotated = hzlib.ghj("api", f"repos/{CORE}/git/tags/{source['sha']}", default=None)
        source = annotated.get("object") if isinstance(annotated, dict) else None
    if (not isinstance(source, dict) or source.get("type") != "commit"
            or not isinstance(source.get("sha"), str) or not release_snapshot.COMMIT.fullmatch(source["sha"])):
        raise Stop("release producer tag does not resolve to an exact commit")
    commit = source["sha"]
    code, workflow = hzlib.gh("api", f"repos/{CORE}/contents/.github/workflows/pypi.yml?ref={commit}",
                              "-H", "Accept: application/vnd.github.raw+json")
    if code or not workflow or len(workflow) > 128 * 1024:
        raise Stop("cannot read the exact tagged PyPI publisher")
    if hashlib.sha256(workflow.encode()).hexdigest() in LEGACY_PYPI_WORKFLOWS:
        return True
    release = hzlib.ghj("api", f"repos/{CORE}/releases/tags/{tag}", default=None)
    if (not isinstance(release, dict) or release.get("tag_name") != tag or release.get("draft") is not False
            or not isinstance(release.get("prerelease"), bool) or not isinstance(release.get("assets"), list)):
        raise Stop("release producer requires the genuine published final release")
    assets = [asset for asset in release["assets"] if isinstance(asset, dict) and asset.get("name") == "artifact-manifest.json"]
    if not assets:
        return False
    if (len(assets) != 1 or type(assets[0].get("id")) is not int or assets[0]["id"] <= 0
            or assets[0].get("browser_download_url") != f"https://github.com/{CORE}/releases/download/{tag}/artifact-manifest.json"):
        raise Stop("release producer manifest asset identity is invalid")
    code, content = hzlib.gh("api", f"repos/{CORE}/releases/assets/{assets[0]['id']}",
                             "-H", "Accept: application/octet-stream")
    if (code or len(content.encode()) > MAX_PRODUCER_MANIFEST
            or assets[0].get("digest") != "sha256:" + hashlib.sha256(content.encode()).hexdigest()):
        raise Stop("release producer manifest bytes differ from the published asset")
    try:
        manifest = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise Stop("release producer manifest is malformed") from exc
    producer = manifest.get("producer") if isinstance(manifest, dict) else None
    if (not isinstance(manifest, dict) or type(manifest.get("schema")) is not int or manifest["schema"] != 1
            or manifest.get("project") != "heinzel" or manifest.get("version") != tag[1:]
            or manifest.get("commit") != commit or not isinstance(producer, dict)
            or type(producer.get("run_id")) is not int or producer["run_id"] <= 0
            or type(producer.get("attempt")) is not int or producer["attempt"] <= 0):
        raise Stop("release producer manifest identity is invalid")
    source_ref = producer.get("source_ref")
    caller = ".github/workflows/release-tag.yml" if source_ref == "refs/heads/main" else ".github/workflows/publish.yml"
    if (not isinstance(source_ref, str) or source_ref not in {"refs/heads/main", f"refs/tags/{tag}"}
            or producer.get("workflow") != caller):
        raise Stop("release producer caller workflow or ref is invalid")
    run = hzlib.ghj("api", f"repos/{CORE}/actions/runs/{producer['run_id']}", default=None)
    if (not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] != producer["run_id"]
            or type(run.get("run_attempt")) is not int or run["run_attempt"] != producer["attempt"]
            or not isinstance(run.get("repository"), dict) or run["repository"].get("full_name") != CORE
            or run.get("head_sha") != commit or run.get("path") != caller
            or run.get("head_branch") != source_ref.removeprefix("refs/heads/").removeprefix("refs/tags/")
            or run.get("event") not in ({"push", "workflow_dispatch"} if source_ref == "refs/heads/main" else {"push"})):
        raise Stop("release producer run identity differs from the exact manifest producer")
    if run.get("status") == "completed":
        if run.get("conclusion") != "success":
            raise Stop(f"release producer run {producer['run_id']} ended {run.get('conclusion')}")
        return True
    if run.get("status") not in {"queued", "in_progress", "requested", "waiting", "pending"} or run.get("conclusion") is not None:
        raise Stop("release producer run status is malformed")
    return False


def has_tag(repo: str, tag: str) -> bool:
    """True or False only on a clear answer; any other gh failure stops: no tag is never assumed from an error."""
    code, out = hzlib.gh("api", f"repos/{repo}/git/ref/tags/{tag}")
    if code == 0:
        return True
    try:
        body = json.loads(out)
    except ValueError:
        body = None
    if isinstance(body, dict) and (body.get("status") == "404" or body.get("message") == "Not Found"):
        return False
    raise Stop(f"cannot tell whether {tag} exists (gh exited {code}); not acting as if there is no tag")


def next_version(request: str) -> tuple[str, str]:
    """-> (X.Y.Z, bump kind for release.sh)."""
    code, out = newest_core_tag()
    current = tuple(int(p) for p in out.strip().lstrip("v").split("."))
    if code != 0 or len(current) != 3:
        raise Stop("cannot read the latest core release")
    if request == "patch":
        pending = state().get("version")
        if pending and not state().get("finished"):
            return pending, "patch"
        return ".".join(map(str, (*current[:2], current[2] + 1))), "patch"
    wanted = tuple(int(p) for p in request.split("."))
    kinds = {(current[0], current[1], current[2] + 1): "patch", (current[0], current[1] + 1, 0): "minor", (current[0] + 1, 0, 0): "major"}
    if wanted == current:  # resuming after the tag exists
        return request, "patch"
    if wanted not in kinds:
        raise Stop(f"{request} is not the next patch, minor or major after {'.'.join(map(str, current))}")
    return request, kinds[wanted]


def release_pr(tag: str) -> dict | None:
    """The release PR of the tag: the OPEN one, else the MERGED one; a CLOSED PR (of a dropped candidate) is no release
    PR. More than one open PR on the branch is ambiguous and refuses: nothing is guessed from gh's order."""
    found = hzlib.ghj("pr", "list", "-R", CORE, "--head", f"release/{tag}", "--state", "all", "--json", "number,state,isCrossRepository", default=[]) or []
    found = [pr for pr in found if not pr.get("isCrossRepository")]  # a fork's PR with the same branch name is no release PR of ours
    opened = [pr for pr in found if pr.get("state") == "OPEN"]
    if len(opened) > 1:
        raise Stop(f"more than one open release PR for {tag} ({', '.join('#' + str(pr.get('number')) for pr in opened)}): close all but one")
    if opened:
        return dict(opened[0])
    return next((pr for pr in found if pr.get("state") == "MERGED"), None)


def open_release_pr(tag: str, bump: str, intro: str) -> None:
    if release_pr(tag):
        return
    if not intro:
        raise Stop(f"no release PR for {tag} yet: pass --intro \"what changed for users\"")
    work = edge.release_dir()  # moved: the release checkout comes from the settings of the native command
    checked_head = prepare_release_pr(work, tag, bump, intro)
    publish_release_pr(work, tag, checked_head)


RELEASE_PR_SCOPE = frozenset({
    "CHANGELOG.md",
    "agent-skills/.claude-plugin/plugin.json",
    "copier.yml",
    "pyproject.toml",
    "scripts/acceptance/run_acceptance.py",
    "uv.lock",
})
RELEASE_PR_UNIT_TARGETS = " ".join((
    "tests/unit/release_summary_test.py",
    "tests/unit/release_version_test.py",
    "tests/unit/changelog_history_test.py",
    "tests/unit/release_notes_test.py",
    "tests/unit/acceptance_test.py::test_installed_metadata_selects_the_canonical_foreground_command",
    "tests/unit/acceptance_test.py::test_unrecognized_or_wrong_installed_version_never_starts_foreground",
    "tests/unit/acceptance_test.py::test_missing_installed_metadata_never_starts_foreground",
))
RELEASE_PR_PUSH_ENV = {
    "MAKEFLAGS": "WEB_TESTS= MANAGED_CHECK=",
    "FAST_TYPES": '"&mypy=uv run mypy" "&pyright=uv run pyright"',
    "PYTEST_XDIST_AUTO_NUM_WORKERS": "2",
    "UNIT_TARGETS": RELEASE_PR_UNIT_TARGETS,
}


def prepare_release_pr(work: str, tag: str, bump: str, intro: str) -> str:
    """Prepare the release branch locally; the caller verifies its diff before any push."""
    status = edge.run(["git", "-C", work, "status", "--porcelain"], capture_output=True, text=True, check=False)
    if status.returncode != 0 or status.stdout.strip():
        raise Stop("release checkout is dirty or unreadable; preserving it without switching branches")
    branch = f"release/{tag}"
    local = edge.run(["git", "-C", work, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
                           capture_output=True, text=True, check=False)
    if local.returncode not in (0, 1):
        raise Stop("cannot verify the existing local release branch; preserving checkout")
    remote = edge.run(["git", "-C", work, "ls-remote", "--heads", "origin", branch],
                            capture_output=True, text=True, check=False)
    if remote.returncode != 0:
        raise Stop("cannot verify the existing release branch; preserving local and remote state")
    if local.returncode == 0 or remote.stdout.strip():
        raise Stop(
            f"release branch {branch} already exists; it was preserved. Inspect exact heads with "
            f"`git -C {work} rev-parse refs/heads/{branch}` and `git -C {work} ls-remote origin refs/heads/{branch}`. "
            "Review the local diff against origin/main. If the remote branch is absent and the exact local head is "
            f"approved, from {work} resume with `{' '.join(edge.gate())} git push --quiet "
            f"origin <reviewed-sha>:refs/heads/{branch}` and the RELEASE_PR_PUSH_ENV selectors. Verify the remote "
            "full SHA equals the reviewed head before PR creation; preserve any existing remote branch."
        )
    for cmd in (["git", "-C", work, "switch", "-q", "main"], ["git", "-C", work, "pull", "--ff-only", "-q"]):
        result = edge.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise Stop(f"release checkout preparation failed ({cmd[-1]}): {(result.stderr or '').strip()[-300:]}")
    done = run_release_script(work, bump, intro)
    if done.returncode != 0:
        tail = (done.stderr or "").strip().splitlines()[-10:]
        hzlib.say("release.sh preparation failed; stderr tail:")
        for line in tail:
            hzlib.say("  " + line)
        raise Stop("release.sh preparation failed; local branch and files were preserved")
    output_tag = next((line.strip() for line in reversed(done.stdout.splitlines()) if line.strip()), "")
    if output_tag != tag:
        raise Stop(f"release.sh prepared {output_tag or 'no version'}, expected {tag}; branch left untouched")
    head = verify_release_pr_scope(work, tag)
    absent = release_notes_audit.audit(work, "origin/main")
    if absent:
        raise Stop("release notes drop combine members " + ", ".join("#" + n for n in absent)
                   + ": fix the combine PR body lines (Combines #N type: title), then cut again; branch preserved, nothing pushed")
    return head


def verify_release_pr_scope(work: str, tag: str) -> str:
    """Fail closed unless the generated release diff matches the reviewed release-only check scope."""
    expected_branch = f"release/{tag}"
    branch = edge.run(["git", "-C", work, "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True, check=False)
    if branch.returncode != 0 or branch.stdout.strip() != expected_branch:
        raise Stop(f"release checkout is not on {expected_branch}; preserving it without push")
    status = edge.run(["git", "-C", work, "status", "--porcelain"], capture_output=True, text=True, check=False)
    if status.returncode != 0 or status.stdout.strip():
        raise Stop("prepared release branch is dirty or unreadable; preserving it without push")
    refs = {}
    for ref in ("refs/heads/main", "refs/remotes/origin/main", "HEAD"):
        result = edge.run(["git", "-C", work, "rev-parse", "--verify", ref], capture_output=True, text=True, check=False)
        if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", result.stdout.strip()):
            raise Stop(f"cannot verify release base/head {ref}; preserving branch without push")
        refs[ref] = result.stdout.strip()
    base, head = refs["refs/remotes/origin/main"], refs["HEAD"]
    if refs["refs/heads/main"] != base:
        raise Stop("release checkout main differs from origin/main; preserve branch and refresh by review")
    ancestry = edge.run(["git", "-C", work, "rev-list", "--parents", "-n", "1", head],
                              capture_output=True, text=True, check=False)
    commits = ancestry.stdout.split()
    if ancestry.returncode != 0 or commits != [head, base]:
        raise Stop("release HEAD is not one commit directly on the captured main base; preserving branch without push")
    changed = edge.run(["git", "-C", work, "diff", "--raw", "-z", "--no-abbrev", "--no-renames", base, head, "--"],
                             capture_output=True, text=True, check=False)
    if changed.returncode != 0:
        raise Stop("cannot enumerate release diff; preserving branch without push")
    records = changed.stdout.split("\0")
    if records[-1] or len(records) % 2 != 1:
        raise Stop("cannot parse release diff file modes/types; preserving branch without push")
    paths = set()
    for index in range(0, len(records) - 1, 2):
        match = re.fullmatch(r":(100644|100755) \1 [0-9a-f]{40} [0-9a-f]{40} M", records[index])
        if not match:
            raise Stop("release diff includes file mode/type changes or additions/deletions; preserving branch without push")
        paths.add(records[index + 1])
    unknown = sorted(paths - RELEASE_PR_SCOPE)
    if not paths or unknown:
        detail = ", ".join(unknown) if unknown else "empty diff"
        raise Stop(f"release diff is outside the reviewed release-only scope ({detail}); preserving branch without push")
    version = tag.removeprefix("v")
    if "pyproject.toml" not in paths or "uv.lock" not in paths:
        raise Stop("release diff does not contain the expected version and root lock updates; preserving branch")
    old_version = _verify_release_lock(work, base, head, version)
    _verify_release_generated_files(work, base, head, old_version, version)
    return head


def _git_show_text(work: str, spec: str) -> str:
    result = edge.run(["git", "-C", work, "show", spec], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise Stop(f"cannot read release input {spec}; preserving branch without push")
    return result.stdout


def _verify_release_lock(work: str, base: str, head: str, version: str) -> str:
    """Permit only the root heinzel version bump in uv.lock; dependency churn needs a reviewed scope."""
    try:
        old = tomllib.loads(_git_show_text(work, f"{base}:uv.lock"))
        new = tomllib.loads(_git_show_text(work, f"{head}:uv.lock"))
        old_project = tomllib.loads(_git_show_text(work, f"{base}:pyproject.toml"))
        new_project = tomllib.loads(_git_show_text(work, f"{head}:pyproject.toml"))
    except tomllib.TOMLDecodeError as exc:
        raise Stop(f"release lock/version metadata is invalid; preserving branch: {exc}") from exc
    old_root = [package for package in old.get("package", []) if package.get("name") == "heinzel"]
    new_root = [package for package in new.get("package", []) if package.get("name") == "heinzel"]
    if (len(old_root) != 1 or len(new_root) != 1 or old_root[0].get("version") == version
            or new_root[0].get("version") != version):
        raise Stop("uv.lock does not identify exactly one expected Core version bump; preserving branch")
    old_project_data, new_project_data = old_project.get("project", {}), new_project.get("project", {})
    old_bump = old_project.get("tool", {}).get("bumpversion", {}).get("current_version")
    new_bump = new_project.get("tool", {}).get("bumpversion", {}).get("current_version")
    old_version = old_root[0].get("version")
    if (not isinstance(old_version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", old_version)
            or old_project_data.get("name") != "heinzel" or old_project_data.get("version") != old_version
            or old_bump != old_version):
        raise Stop("main root lock/project/bumpversion versions disagree; preserving branch")
    if (new_project_data.get("name") != "heinzel" or new_project_data.get("version") != version
            or new_bump != version):
        raise Stop("pyproject.toml project/bumpversion versions do not identify the expected Core release; preserving branch")
    old_project_normalized, new_project_normalized = old_project.copy(), new_project.copy()
    old_project_normalized["project"] = dict(old_project_data)
    new_project_normalized["project"] = dict(new_project_data)
    old_project_normalized["project"].pop("version", None)
    new_project_normalized["project"].pop("version", None)
    if "tool" in old_project_normalized or "tool" in new_project_normalized:
        old_tool = old_project_normalized.setdefault("tool", {})
        new_tool = new_project_normalized.setdefault("tool", {})
        old_bump = dict(old_tool.get("bumpversion", {}))
        new_bump = dict(new_tool.get("bumpversion", {}))
        old_bump.pop("current_version", None)
        new_bump.pop("current_version", None)
        old_tool["bumpversion"] = old_bump
        new_tool["bumpversion"] = new_bump
    if old_project_normalized != new_project_normalized:
        raise Stop("pyproject.toml includes changes beyond the release version fields; preserving branch")
    old_normalized, new_normalized = old.copy(), new.copy()
    old_normalized["package"] = [dict(package) for package in old.get("package", [])]
    new_normalized["package"] = [dict(package) for package in new.get("package", [])]
    old_index = old["package"].index(old_root[0])
    new_index = new["package"].index(new_root[0])
    old_normalized["package"][old_index].pop("version", None)
    new_normalized["package"][new_index].pop("version", None)
    if old_normalized != new_normalized:
        raise Stop("uv.lock includes dependency churn beyond the root Core version bump; preserving branch")
    return old_version


def _foreground_ast(source: str) -> tuple[ast.Module, ast.Dict]:
    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise Stop(f"acceptance harness is invalid; preserving branch: {exc}") from exc
    for node in module.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "FOREGROUND_UI"
                                                for target in node.targets) and isinstance(node.value, ast.Dict):
            return module, node.value
    raise Stop("acceptance foreground policy is missing or has an unsupported shape; preserving branch")


def _verify_release_generated_files(work: str, base: str, head: str, old_version: str, version: str) -> None:
    """Allow only known version-derived edits in release-generated metadata and acceptance policy."""
    try:
        old_plugin = json.loads(_git_show_text(work, f"{base}:agent-skills/.claude-plugin/plugin.json"))
        new_plugin = json.loads(_git_show_text(work, f"{head}:agent-skills/.claude-plugin/plugin.json"))
    except (json.JSONDecodeError, TypeError) as exc:
        raise Stop(f"plugin metadata is invalid; preserving branch: {exc}") from exc
    if not isinstance(old_plugin, dict) or not isinstance(new_plugin, dict):
        raise Stop("plugin metadata is not a JSON object; preserving branch")
    if old_plugin.get("version") != old_version or new_plugin.get("version") != version:
        raise Stop("plugin metadata versions do not match the release bump; preserving branch")
    old_plugin.pop("version")
    new_plugin.pop("version")
    if old_plugin != new_plugin:
        raise Stop("plugin metadata changed beyond its version field; preserving branch")

    old_copier = _git_show_text(work, f"{base}:copier.yml")
    new_copier = _git_show_text(work, f"{head}:copier.yml")
    old_setting = (f'heinzel_version:\n  type: str\n  default: "{old_version}" # bumped by the release process\n'
                   "  when: false")
    new_setting = (f'heinzel_version:\n  type: str\n  default: "{version}" # bumped by the release process\n'
                   "  when: false")
    if old_copier.count(old_setting) != 1 or old_copier.replace(old_setting, new_setting, 1) != new_copier:
        raise Stop("copier.yml changed beyond the known Heinzel version default; preserving branch")

    old_acceptance = _git_show_text(work, f"{base}:scripts/acceptance/run_acceptance.py")
    new_acceptance = _git_show_text(work, f"{head}:scripts/acceptance/run_acceptance.py")
    old_module, old_policy = _foreground_ast(old_acceptance)
    new_module, new_policy = _foreground_ast(new_acceptance)
    def explicit_rows(policy: ast.Dict, target: str) -> list[int]:
        return [index for index, key in enumerate(policy.keys)
                if isinstance(key, ast.Constant) and key.value == target]

    supported = ast.dump(ast.parse('("ui", "serve")', mode="eval").body)
    prior_rows = explicit_rows(old_policy, old_version)
    old_rows = explicit_rows(old_policy, version)
    new_rows = explicit_rows(new_policy, version)
    if len(prior_rows) != 1 or ast.dump(old_policy.values[prior_rows[0]]) != supported:
        raise Stop("acceptance foreground policy has no supported explicit prior version row; preserving branch")
    if len(new_rows) != 1 or ast.dump(new_policy.values[new_rows[0]]) != supported:
        raise Stop("acceptance foreground policy needs exactly one supported explicit release row; preserving branch")
    if old_rows:
        if len(old_rows) != 1 or ast.dump(old_policy.values[old_rows[0]]) != supported:
            raise Stop("acceptance foreground row is not the existing release policy; preserving branch")
    else:
        # Compare the complete module after removing only the verified new literal row.
        # Existing unpack/comprehension nodes remain intact and are never evaluated.
        del new_policy.keys[new_rows[0]]
        del new_policy.values[new_rows[0]]
    if ast.dump(old_module) != ast.dump(new_module):
        raise Stop("acceptance harness changed beyond the known foreground version row; preserving branch")


def _verify_checked_release_head(work: str, branch: str, checked_head: str) -> None:
    if not isinstance(checked_head, str) or not re.fullmatch(r"[0-9a-f]{40}", checked_head):
        raise Stop("checked release head is missing or invalid; preserving branch")
    for args, expected in ((["rev-parse", "--abbrev-ref", "HEAD"], branch),
                           (["rev-parse", "--verify", "HEAD"], checked_head),
                           (["status", "--porcelain"], "")):
        result = edge.run(["git", "-C", work, *args], capture_output=True, text=True, check=False)
        if result.returncode != 0 or result.stdout.strip() != expected:
            raise Stop("checked release head/branch changed or checkout is dirty; preserving branch without PR creation")


def publish_release_pr(work: str, tag: str, checked_head: str) -> None:
    """Push through the ordinary hook with the reviewed release-only selectors, then open its PR."""
    branch = f"release/{tag}"
    gate = edge.gate()  # moved: the run lock is `sm gate run --`, no script is started
    env = child_environment() | RELEASE_PR_PUSH_ENV
    _verify_checked_release_head(work, branch, checked_head)
    # Changelog screenshots: pin images to the checked release commit (the tag does not exist yet) once core's
    # release_notes.py accepts --ref; an older script is called as before.
    script = Path(work) / "scripts" / "release_notes.py"
    ref = ["--ref", checked_head] if re.fullmatch(r"[0-9a-f]{40}", checked_head) and "--ref" in script.read_text(encoding="utf-8", errors="replace") else []
    notes = edge.run(["uv", "run", "--frozen", "--no-sync", "--no-build", "python",
                            "scripts/release_notes.py", tag, *ref], cwd=work, env=env,
                           capture_output=True, text=True, check=False)
    if notes.returncode != 0:
        raise Stop(f"release notes could not be prepared; branch preserved without push: {(notes.stderr or '').strip()[-300:]}")
    if not notes.stdout.strip():
        raise Stop("release notes are empty; branch preserved without push")
    with tempfile.TemporaryDirectory(prefix="sm-release-pr-") as directory:
        body = Path(directory) / "notes.md"
        body.write_text(notes.stdout, encoding="utf-8")
        _verify_checked_release_head(work, branch, checked_head)
        destination = f"refs/heads/{branch}"
        existing = edge.run(["git", "-C", work, "ls-remote", "--heads", "origin", destination],
                                  capture_output=True, text=True, check=False)
        if existing.returncode != 0 or existing.stdout.strip():
            raise Stop("remote release branch exists or cannot be checked; preserving it without push")
        pushed = edge.run([*gate, "git", "push", "--quiet", "origin", f"{checked_head}:{destination}"],
                                cwd=work, env=env, capture_output=True, text=True, check=False)
        if pushed.returncode != 0:
            raise Stop(f"release branch push failed; preserving branch without deletion: {(pushed.stderr or '').strip()[-300:]}")
        _verify_checked_release_head(work, branch, checked_head)
        remote = edge.run(["git", "-C", work, "ls-remote", "--heads", "origin", destination],
                                capture_output=True, text=True, check=False)
        if remote.returncode != 0 or remote.stdout.splitlines() != [f"{checked_head}\t{destination}"]:
            raise Stop("remote release branch does not match the checked release head; preserving branch without PR creation")
        try:
            created = edge.run(["gh", "pr", "create", "--base", "main", "--head", branch,
                                "--title", f"chore(release): {tag}", "--body-file", str(body)],
                               cwd=work, env=env, capture_output=True, text=True, check=False, timeout=hzlib.GH_WRITE_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise Stop(f"release PR creation timed out after {hzlib.GH_WRITE_TIMEOUT}s; whether the PR exists is unknown, "
                       "the pushed branch is preserved: check the open PRs before a resume") from None
        if created.returncode != 0:
            raise Stop(f"release PR creation failed; pushed branch is preserved: {(created.stderr or '').strip()[-300:]}")


RELEASE_PR_TITLE = "chore(release): {tag}"


def open_reviewed_release_pr(version: str, reviewed_head: str, dry_run: bool) -> int:
    """`sm release pr`: push the reviewed, already prepared release branch and open its PR; never merge it.

    The branch is the one `scripts/release.sh` prepared in the release checkout (resume refuses to touch an existing
    branch and preserves it). Everything resume checks before its own push is checked again: no tag, no open or merged
    release PR (a CLOSED PR of a dropped candidate is ignored), the receipt is this release or none, the checkout is on release/vX.Y.Z, clean, exactly one commit on
    origin/main with only the reviewed release-only changes and the version, and its head is exactly the reviewed SHA.
    The push runs under the run lock with the release-only hook selectors; the PR is checked after creation."""
    tag = f"v{version}"
    if not re.fullmatch(r"[0-9a-f]{40}", reviewed_head or ""):
        raise Stop("--reviewed-head must be the full 40-character SHA of the reviewed release commit")
    if has_tag(CORE, tag):
        raise Stop(f"{tag} is already tagged; there is no release PR to open")
    found = release_pr(tag)
    if found:
        raise Stop(f"release PR #{found.get('number')} for {tag} already exists ({found.get('state')}); nothing pushed")
    pending = state()
    if pending.get("version") not in (None, version) and not pending.get("finished"):
        raise Stop(f"the receipt belongs to the unfinished release {pending.get('version')}; preserving it, nothing pushed")
    work = edge.release_dir()  # moved: the release checkout comes from the settings of the native command
    head = verify_release_pr_scope(work, tag)
    if head != reviewed_head:
        raise Stop(f"release head {head} is not the reviewed head {reviewed_head}; branch preserved, nothing pushed")
    absent = release_notes_audit.audit(work, "origin/main")
    if absent:
        raise Stop("release notes drop combine members " + ", ".join("#" + n for n in absent)
                   + ": fix the combine PR body lines (Combines #N type: title), then cut again; branch preserved, nothing pushed")
    title = RELEASE_PR_TITLE.format(tag=tag)
    if dry_run:
        hzlib.say(f"DRY RUN: checks passed. Would push {head} to refs/heads/release/{tag} under `{' '.join(edge.gate())}` with the "
                  f"release-only hook selectors and open \"{title}\" against main (never auto-merged). Nothing was pushed.")
        return 0
    publish_release_pr(work, tag, head)
    opened = hzlib.ghj("pr", "view", f"release/{tag}", "-R", CORE, "--json", "number,state,title,baseRefName,headRefOid,autoMergeRequest,body",
                       default=None)
    if (not isinstance(opened, dict) or opened.get("state") != "OPEN" or opened.get("title") != title or opened.get("baseRefName") != "main"
            or opened.get("headRefOid") != head or opened.get("autoMergeRequest") is not None or not str(opened.get("body") or "").strip()):
        raise Stop(f"the opened release PR for {tag} is not the checked one (state, title, base, head, empty body or auto-merge); "
                   "it is preserved for inspection")
    if pending.get("version") == version:
        remember(release_pr_opened={"pr": opened.get("number"), "head": head, "at": now_iso()})
    hzlib.say(f"OPENED release PR #{opened.get('number')} \"{title}\" at {head}; not merged: `sm release resume {version} --intro ...` merges it")
    return 0


def run_release_script(work: str, bump: str, intro: str):
    gate = edge.gate()  # moved: the run lock is `sm gate run --`, no script is started
    argv = [*gate, "bash", "scripts/release.sh", bump]
    return edge.run(argv, cwd=work, env=child_environment(INTRO=intro), capture_output=True, text=True, check=False)


def waiver_path() -> Path:
    """Sidecar, never a receipt field (pause, prepare-only and rejection require exact receipt key sets)."""
    return global_receipt().with_name("release-waiver.json")


def record_waiver(version: str, spec: str, quote: str) -> None:
    """Owner waiver request for one KNOWN-flake candidate job; release_candidate_policy decides against live evidence."""
    job, sep, bead = spec.rpartition("=")
    if not sep or not job.strip() or not re.fullmatch(r"[a-z0-9]{3}", bead.strip()) or len(quote.strip()) < 10:
        raise Stop('--waive-job needs "JOB NAME=bead" and --owner-quote with the owner\'s own words')
    data = {"version": version, "job": job.strip(), "bead": bead.strip(), "owner_quote": quote.strip(), "at": now_iso()}
    waiver_path().write_text(json.dumps(data))
    hzlib.say(f"owner waiver requested for {version}: {data['job']}={data['bead']} (checked against live evidence at merge)")


def pending_waiver(version: str) -> dict | None:
    try:
        data = json.loads(waiver_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != version:
        return None
    return {key: data.get(key) for key in ("job", "bead", "owner_quote")}


def main_moved_refusal(number: int, tag: str) -> str | None:
    """The release PR is one commit on the main base the candidate ran on; its squash merge carries everything that
    reached main since (0.9.53 shipped #886 unproven). Returns the refusal text when main's tip is not that base, or
    when either side cannot be read exactly (fail closed); None when main has not moved."""
    pr_head = hzlib.gh("api", f"repos/{CORE}/pulls/{number}", "--jq", ".head.sha")[1].strip()
    code, parents = hzlib.gh("api", f"repos/{CORE}/commits/{pr_head}", "--jq", ".parents[].sha") if pr_head else (1, "")
    base = parents.split()
    tip_code, tip = hzlib.gh("api", f"repos/{CORE}/commits/main", "--jq", ".sha")
    tip = tip.strip()
    sha = re.compile(r"[0-9a-f]{40}")
    if code != 0 or tip_code != 0 or len(base) != 1 or not sha.fullmatch(base[0]) or not sha.fullmatch(tip):
        return f"main moved check refused: cannot read the candidate base of PR #{number} or main's tip exactly; nothing was merged"
    if base[0] == tip:
        return None
    return (f"main moved since the candidate base: PR #{number} ({pr_head[:9]}) was proven on {base[0][:9]}, main is now {tip[:9]}; "
            "its squash merge would carry unproven commits into the release (0.9.53 shipped #886 this way). Nothing was merged. "
            f"Recovery: `sm release resume {tag.removeprefix('v')} --drop-candidate --reason TEXT --close-pr`, then cut again so the "
            "candidate runs on the new base (keep main frozen until the merge)")


def merge_release_pr(tag: str, composite_run: int | None = None) -> None:
    pr = release_pr(tag)
    if not pr:
        raise Stop(f"no release PR for {tag}")
    waiver = None if composite_run else pending_waiver(tag.removeprefix("v"))
    def full_candidate_guard() -> bool:
        if (moved := main_moved_refusal(pr["number"], tag)):
            hzlib.say(moved)
            return False
        try:
            evidence = (release_candidate_capacity.consume(ReleaseAdapter(), composite_run, pr["number"])
                        if composite_run else release_candidate_policy.verify_pr(pr["number"], waiver))
        except release_candidate_capacity.InvalidCapacity as exc:
            hzlib.say(f"full candidate refused: {release_candidate_capacity.safe_refusal(exc)}")
            return False
        except release_candidate_policy.release_candidate.CandidateError as exc:
            hzlib.say(f"full candidate refused: {exc}")
            return False
        except Exception as exc:  # Fail-closed boundary: malformed remote evidence can never authorize merging.
            hzlib.say(f"full candidate refused: invalid evidence ({type(exc).__name__})")
            return False
        if composite_run:
            release_candidate_capacity.persist(ReleaseAdapter(), candidate_composite=evidence)
        else:
            remember(candidate_ci=evidence)
            if evidence.get("owner_waiver"):
                w = evidence["owner_waiver"]
                hzlib.say(f"owner waiver applied: {w['job']}=KNOWN({w['bead']}), attempt-2 rerun green (run {w['run_id']})")
        return True

    merged, detail = hzlib.load("merge_train").train(
        CORE, str(pr["number"]), ignore_freeze=True, pre_merge_guard=full_candidate_guard,
    )
    if not merged and (release_pr(tag) or {}).get("state") != "MERGED":
        raise Stop(f"release PR #{pr['number']} not merged: {detail}")


def main_tip_green() -> bool:
    tip = hzlib.gh("api", f"repos/{CORE}/commits/main", "--jq", ".sha")[1].strip()
    latest = (hzlib.resolve_main_runs(runs(CORE, core_facts.ci_file(), branch="main")) or [])[:3]
    if any(r["status"] != "completed" for r in latest):
        return False
    if latest and latest[0]["headSha"] == tip and latest[0]["conclusion"] == "failure":
        if hzlib.only_never_started_macos_jobs_failed(CORE, latest[0]["databaseId"]):
            return True  # macOS cells GitHub never gave a runner: nothing ran, nothing hidden
        raise Stop(f"main tip {tip[:8]} is red (run {latest[0]['databaseId']}); fix main first")
    if latest and latest[0]["headSha"] == tip and latest[0]["conclusion"] == "success" and latest[0].get("attempt") != 1:
        # hz-queue's restored-by-rerun lets merges proceed after a KNOWN flake rerun; a release never counts it (2026-11-07).
        raise Stop(f"main tip {tip[:8]} is green only on rerun attempt {latest[0].get('attempt')} (run {latest[0]['databaseId']}); "
                   "a release needs a first-attempt full green main run")
    return bool(latest) and latest[0]["headSha"] == tip and latest[0]["conclusion"] == "success"


def main_candidate_gate(version: str) -> bool:
    """TRAIN PATH ONLY (`--candidate-gate`, passed by hz-train): the release candidate, a full first-attempt CI run plus
    acceptance on both OSes, is the gate, so main's tip need not be finished or green. Refuses an UNKNOWN failing job on
    main's newest completed run (hzlib.main_release_gate). A pass is remembered per version, so a resume after hz-train
    cancelled the redundant unstarted main tip run does not re-judge. The memo is a sidecar file, never a receipt field
    (pause, prepare-only and rejection require exact receipt key sets). The default path keeps main_tip_green()."""
    memo = global_receipt().with_name("release-main-gate.json")
    try:
        passed = json.loads(memo.read_text())
    except (OSError, ValueError):
        passed = {}
    if isinstance(passed, dict) and passed.get("version") == version:
        return True
    gate = hzlib.main_release_gate(hzlib.gh("api", f"repos/{CORE}/commits/main", "--jq", ".sha")[1].strip())
    if gate["verdict"] != "ok":
        # A completed, exact-source candidate is stronger evidence than an older main failure.
        # Do not memoize this route: every resume must reverify the current PR head, and the
        # merge guard independently verifies it again immediately before merging.
        pr = release_pr(f"v{version}")
        if pr and pr.get("state") == "OPEN":
            try:
                evidence = release_candidate_policy.verify_pr(pr["number"])
            except Exception:  # Unavailable or malformed evidence must never open the gate.
                pass
            else:
                hzlib.say(f"main gate (train path): exact candidate verified (run {evidence['run_id']})")
                return True
        raise Stop(f"main gate (train path) refused: {gate['why']}")
    memo.write_text(json.dumps({"version": version, "kind": gate["kind"], "why": gate["why"], "run": gate["run"], "at": now_iso()}))
    hzlib.say(f"main gate (train path, candidate is the gate): {gate['why']}")
    return True


def dispatch(repo: str, workflow: str, ref: str, key: str, **inputs) -> None:
    if state().get(f"{key}_marker"):  # a marked dispatch (acceptance) is bound by its marker and commit
        remember(**{key: now_iso(-5)})
    else:
        # An unmarked dispatch is bound to exactly what was asked: the head SHA of the ref, the workflow, the event and
        # the inputs (as data: the run list cannot show them, so two runs that could be this one are refused, never
        # guessed). The adopted run's ID is kept; a new dispatch drops the previous adoption.
        code, sha = hzlib.gh("api", f"repos/{repo}/commits/{ref}", "--jq", ".sha")
        sha = sha.strip()
        if code != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise Stop(f"cannot resolve {ref} of {workflow} to an exact commit before dispatching it")
        remember(**{key: now_iso(-5), f"{key}_run_id": None, f"{key}_run_attempt": None,
                    f"{key}_dispatch": {"repo": repo, "workflow": workflow, "ref": ref, "sha": sha, "event": "workflow_dispatch",
                                        "inputs": {name: str(value) for name, value in inputs.items()}}})
    args = ["workflow", "run", workflow, "-R", repo, "--ref", ref]
    for name, value in inputs.items():
        args += ["-f", f"{name}={value}"]
    if hzlib.gh(*args)[0] != 0:
        raise Stop(f"dispatching {workflow} failed")


def pypi_step(name: str, version: str, tag: str, timeout: float) -> None:
    label = f"PyPI {name} {version}"
    if pypi_has(name, version):
        hzlib.say(f"ok   {label} (already)")
        return
    deadline = time.time() + timeout
    if not ours(CORE, "pypi.yml", "pypi"):
        wait("release producer", lambda: release_producer_ready(tag), max(0, deadline - time.time()))
        if time.time() > deadline:
            raise Stop(f"{label}: timed out before dispatch")
        dispatch(CORE, "pypi.yml", tag, "pypi")

    def check() -> None:
        run = ours(CORE, "pypi.yml", "pypi")
        if run and run["status"] == "completed" and run["conclusion"] != "success" and not pypi_has(name, version):
            raise Stop(f"the PyPI run {run['databaseId']} ended {run['conclusion']}: sm ci watch {run['databaseId']}")

    wait(label, lambda: pypi_has(name, version), max(0, deadline - time.time()), check)
    hzlib.say(f"ok   {label}")


def compose_busy() -> bool:
    return any(r["status"] != "completed" for r in runs(PLUGINS, "compose.yml"))


def published_release(repo: str, tag: str) -> dict | None:
    """The exact published (non-draft) release `tag` in `repo`, else None."""
    release = hzlib.ghj("api", f"repos/{repo}/releases/tags/{tag}", default=None)
    if not isinstance(release, dict) or release.get("tag_name") != tag or release.get("draft") is not False:
        return None
    return release


def compose_step(version: str, timeout: float) -> None:
    """Wait for the FINAL app-vX: acceptance checks the collection's latest app, which a held app is not.

    A held core (core ADR 0114) is composed only by an input-less compose run (plugins `select_held`, the app is
    published as a pre-release) and finalized by the next input-less run (`held_to_finalize`, once PyPI has the core);
    an explicit version would compose nothing for a held core. A final core keeps the explicit dispatch.
    """
    app = f"app-v{version}"
    def released() -> bool:
        guard = state().get("compose_replacement_guard")
        if guard:
            run = ours(PLUGINS, "compose.yml", "compose")
            if (not run or run.get("headSha") != guard.get("reviewed_head") or type(run.get("databaseId")) is not int
                    or run["databaseId"] <= 0 or run["databaseId"] == guard.get("failed_run") or run.get("attempt") != 1
                    or (guard.get("replacement_run") is not None and run["databaseId"] != guard["replacement_run"])):
                raise Stop("reviewed compose replacement is pending or its identity changed; reconcile before continuing")
            if guard.get("replacement_run") is None or (state().get("compose_adopted") or {}).get("run") != run["databaseId"]:
                remember(compose_replacement_guard=guard | {"replacement_run": run["databaseId"]},
                         compose_adopted={"run": run["databaseId"], "sha": run["headSha"],
                                          "created": run.get("createdAt"), "at": now_iso()})
            if run.get("status") != "completed" or run.get("conclusion") != "success":
                return False
        return bool((r := published_release(PLUGINS, app)) and r.get("prerelease") is False)

    def held() -> bool:
        core = published_release(CORE, f"v{version}")
        if core is None or not isinstance(core.get("prerelease"), bool):
            raise Stop(f"v{version} is no published core release")
        return core["prerelease"]

    def dispatch_compose() -> None:
        if held():
            dispatch(PLUGINS, "compose.yml", "main", "compose")
            return
        _, out = hzlib.gh("release", "view", f"v{version}", "-R", CORE, "--json", "assets", "--jq", ".assets[].url")
        wheel = next((u for u in out.split() if u.endswith(".whl")), "")
        if not wheel:
            raise Stop(f"v{version} has no wheel asset")
        dispatch(PLUGINS, "compose.yml", "main", "compose", version=version, core_wheel=wheel)

    def act() -> None:
        if state().get("compose_replacement_guard"):
            return  # released() verified or refused the one bound replacement; never dispatch it again.
        if ours(PLUGINS, "compose.yml", "compose"):
            return
        wait("compose queue", lambda: not compose_busy(), timeout)
        dispatch_compose()

    def check() -> None:
        for key in ("compose", "compose_finalize"):
            run = ours(PLUGINS, "compose.yml", key) if state().get(key) else None
            if run and run["status"] == "completed" and run["conclusion"] != "success" and not released():
                if key == "compose" and state().get("compose_replacement_guard"):
                    raise Stop(f"reviewed compose replacement {run['databaseId']} ended {run['conclusion']}; no further dispatch")
                if key == "compose" and (fix := compose_fix_landed(run)):
                    if compose_busy():
                        return  # the fresh compose waits for the queue, like the first one
                    # The failed run stays the record of the failure; the fresh run is bound by the new dispatch time.
                    remember(compose_replaced=[*state().get("compose_replaced", []), fix | {"at": now_iso()}])
                    hzlib.say(f"compose run {run['databaseId']} ended {run['conclusion']}; plugins main moved and published "
                              f"{', '.join(f'{d} {o} -> {n}' for d, (o, n) in sorted(fix['plugins'].items()))}: dispatching a fresh compose")
                    dispatch_compose()
                    return
                raise Stop(f"compose run {run['databaseId']} ended {run['conclusion']}: sm ci watch {run['databaseId']} -R {PLUGINS}")
        run = ours(PLUGINS, "compose.yml", "compose")
        if (run and run["status"] == "completed" and not state().get("compose_finalize") and not compose_busy()
                and (r := published_release(PLUGINS, app)) and r.get("prerelease") is True):
            dispatch(PLUGINS, "compose.yml", "main", "compose_finalize")  # once; the schedule also finalizes

    step(f"compose {app}", released, act, timeout, check)


PLUGIN_PYPROJECT = re.compile(r"plugins/(heinzel-[a-z0-9-]+)/pyproject\.toml")


def plugin_version(dist: str, ref: str) -> str | None:
    """The version of plugin `dist` at `ref` of the plugins repository, else None (absent or unreadable)."""
    code, out = hzlib.gh("api", f"repos/{PLUGINS}/contents/plugins/{dist}/pyproject.toml?ref={ref}", "--jq", ".content")
    if code != 0:
        return None
    try:
        version = tomllib.loads(base64.b64decode(out).decode())["project"]["version"]
    except (ValueError, KeyError, TypeError):
        return None
    return version if isinstance(version, str) else None


def compose_fix_landed(run: dict) -> dict | None:
    """The checked route past a failed compose run: plugins main moved past the run's head AND at least one plugin
    whose version changed on main since then is published on PyPI (the fix the failed compose lacked, as in 0.9.40:
    relock #88 plus plugin releases; manual run 43443233990). Returns the receipt entry, else None (stop as before).
    One fresh compose per failed run: a failed run that was already replaced is never replaced again."""
    run_id, head = run.get("databaseId"), run.get("headSha") or ""
    if not release_snapshot.COMMIT.fullmatch(head) or any(e.get("failed_run") == run_id for e in state().get("compose_replaced", [])):
        return None
    code, main_head = hzlib.gh("api", f"repos/{PLUGINS}/commits/main", "--jq", ".sha")
    main_head = main_head.strip()
    if code != 0 or not release_snapshot.COMMIT.fullmatch(main_head) or main_head == head:
        return None
    compare = hzlib.ghj("api", f"repos/{PLUGINS}/compare/{head}...{main_head}", default=None)
    if not isinstance(compare, dict) or compare.get("status") != "ahead":
        return None
    changed = {}
    for item in compare.get("files") or []:
        found = PLUGIN_PYPROJECT.fullmatch(str(item.get("filename", "")) if isinstance(item, dict) else "")
        if not found:
            continue
        dist = found[1]
        old, new = plugin_version(dist, head), plugin_version(dist, main_head)
        if new and new != old and pypi_has(dist, new):
            changed[dist] = [old, new]
    if not changed:
        return None
    return {"failed_run": run_id, "failed_conclusion": run.get("conclusion"), "failed_head": head, "main": main_head, "plugins": changed}


def acceptance_identity(version: str, snapshot: dict | None, ref: str | None = None) -> dict:
    """Separate immutable release bytes from the explicitly selected harness commit."""
    captured = snapshot if snapshot is not None else release_snapshot.capture(version)
    previous = state().get("snapshot")
    if previous is not None and previous != captured:
        raise Stop("release snapshot changed; original artifacts must be preserved")
    if ref is not None and ref != "main":
        raise Stop("corrected acceptance harness must resolve protected main")
    selected = ref or (captured["tag"] if snapshot is not None else "main")
    code, commit = hzlib.gh("api", f"repos/{CORE}/commits/{selected}", "--jq", ".sha")
    commit = commit.strip()
    if code or not release_snapshot.COMMIT.fullmatch(commit):
        raise Stop("cannot resolve acceptance harness commit")
    if not release_snapshot.workflow_supports_snapshot(commit):
        raise Stop("acceptance harness cannot enforce the release snapshot")
    return {"version": version, "app_version": version, "snapshot": captured,
            "ref": selected, "commit": commit}


def rerun_rejection(replace_run: int, identity: dict, reason: str | None) -> dict | None:
    """Return the exact completed rejection of the bound attempt N when the same run was later rerun, else None.

    Only an explicit --replace-acceptance-run reaches this. None (latest attempt is not higher than N, or the receipt
    does not bind exactly this run) leaves every existing guard in force; any other mismatch stops. A later attempt
    of the same run is never adopted or credited: the replacement verdict must come from a fresh dispatch.
    """
    data = state()
    attempt, marker, commit = data.get("acceptance_run_attempt"), data.get("acceptance_marker"), data.get("acceptance_commit")
    if (data.get("acceptance_run_id") != replace_run or type(attempt) is not int or attempt < 1
            or not marker or commit != identity.get("commit")):
        return None
    latest = hzlib.ghj("api", f"repos/{CORE}/actions/runs/{replace_run}", default=None)
    if not isinstance(latest, dict) or type(latest.get("run_attempt")) is not int or latest["run_attempt"] <= attempt:
        return None

    def same_run(run) -> bool:
        return (isinstance(run, dict) and run.get("id") == replace_run
                and run.get("path") == ".github/workflows/acceptance.yml" and run.get("head_sha") == commit
                and str(run.get("display_title", "")).split()[-1:] == [marker])

    if not same_run(latest):
        raise Stop("persisted workflow run identity changed")
    original = hzlib.ghj("api", f"repos/{CORE}/actions/runs/{replace_run}/attempts/{attempt}", default=None)
    if not same_run(original) or original.get("run_attempt") != attempt:
        raise Stop("rerun replacement requires the exact bound original attempt")
    if original.get("status") != "completed" or original.get("conclusion") in (None, "", "success"):
        raise Stop("rerun replacement requires a completed non-success original attempt")
    if not isinstance(reason, str) or not reason.strip():
        raise Stop("rerun replacement requires --replace-reason")
    listed = hzlib.ghj("api", f"repos/{CORE}/actions/runs/{replace_run}/attempts/{attempt}/jobs?per_page=100", default=None)
    jobs = listed.get("jobs") if isinstance(listed, dict) else None
    if (not isinstance(jobs, list) or not jobs
            or any(not isinstance(job, dict) or job.get("run_attempt") != attempt for job in jobs)):
        raise Stop("rerun replacement cannot read the original attempt's jobs")
    return {
        "run_id": replace_run, "run_attempt": attempt, "head_sha": original["head_sha"],
        "display_title": original["display_title"], "status": original["status"],
        "conclusion": original["conclusion"], "html_url": original.get("html_url"),
        "jobs": [{key: job.get(key) for key in ("id", "name", "status", "conclusion", "html_url")} for job in jobs],
        "latest_attempt_seen": latest["run_attempt"], "later_attempts_credited": False,
        "note": "later attempts of this run are not adopted; the verdict must come from a fresh dispatch",
        "reason": reason.strip(),
    }


def acceptance_step(version: str, timeout: float, snapshot: dict | None = None,
                    harness_ref: str | None = None, replace_run: int | None = None,
                    replace_reason: str | None = None) -> int:
    """Every path uses the same persisted marker and resolved harness identity."""
    data = state()
    identity = data.get("acceptance_identity")
    if identity is not None:
        if identity.get("version") != version or (snapshot is not None and identity.get("snapshot") != snapshot):
            raise Stop("acceptance release identity changed")
        if harness_ref is not None and harness_ref != identity.get("ref") and replace_run is None:
            raise Stop("changing harness requires explicit rejected-run replacement")
    if replace_run is not None and any(
        attempt.get("acceptance_run_id") == replace_run for attempt in data.get("acceptance_attempts", [])
    ):
        if not identity or harness_ref != identity.get("ref"):
            raise Stop("replacement resume identity changed")
        replace_run = None
    if replace_run is not None and identity is None:
        # Upgrade only an already marked, pinned tagged attempt, never the unsafe timestamp-only path.
        original = data.get("snapshot")
        if (not isinstance(original, dict) or not data.get("acceptance_marker")
                or data.get("acceptance_run_id") != replace_run
                or data.get("acceptance_commit") != original.get("commit")
                or original.get("version") != version):
            raise Stop("legacy replacement requires the original marked snapshot and exact recorded run")
        identity = {"version": version, "app_version": version, "snapshot": original,
                    "ref": original["tag"], "commit": original["commit"]}
        # Persist a structural migration without altering or discarding any original attempt fields.
        original_run = hzlib.ghj("api", f"repos/{CORE}/actions/runs/{replace_run}", default=None)
        if not isinstance(original_run, dict) or original_run.get("run_attempt") != 1:
            raise Stop("legacy replacement requires the original run attempt")
        remember(acceptance_identity=identity, acceptance_run_attempt=1)
    if replace_run is not None:
        # The same run was rerun after attempt N: archive N's exact rejection, credit no later attempt, dispatch fresh.
        rerun = rerun_rejection(replace_run, identity, replace_reason) if identity else None
        if rerun is None:
            old = ours(CORE, "acceptance.yml", "acceptance") if identity else None
            if not old or old["databaseId"] != replace_run or old["status"] != "completed":
                raise Stop("replacement requires the exact completed original run")
            good, lines = verdict(replace_run)
            if good:
                raise Stop("an accepted run cannot be replaced")
        else:
            lines = [f"attempt {rerun['run_attempt']}: {rerun['conclusion']}"] + [
                f"  job: {job['name']}: {job['conclusion']}" for job in rerun["jobs"]]
        if harness_ref is None:
            raise Stop("replacement requires an explicit corrected harness ref")
        replacement = acceptance_identity(version, identity["snapshot"], harness_ref)
        if replacement["commit"] == identity["commit"]:
            raise Stop("replacement requires a different corrected harness commit")
        history = data.get("acceptance_attempts", [])
        archive = {key: value for key, value in state().items() if key.startswith("acceptance") and key != "acceptance_attempts"}
        archive["verdict"] = lines
        if rerun is not None:
            archive["rerun_rejection"] = rerun
        remember(finished=False, acceptance_run=None, acceptance_attempts=history + [archive], acceptance_identity=replacement,
                 acceptance=None, acceptance_marker=None, acceptance_commit=None,
                 acceptance_run_id=None, acceptance_run_attempt=None, acceptance_requested=False)
        identity = replacement
    if identity is None:
        # Never silently adopt the legacy timestamp-only attempt or overwrite its rejection.
        if any(data.get(key) for key in ("acceptance", "acceptance_requested", "acceptance_run")):
            raise Stop("legacy acceptance has no bound identity; preserve it for explicit recovery")
        identity = acceptance_identity(version, snapshot, harness_ref)
        remember(acceptance_identity=identity, snapshot=identity["snapshot"], acceptance_snapshot_mode=snapshot is not None)
    if state().get("acceptance_commit") not in (None, identity["commit"]):
        raise Stop("acceptance marker belongs to a different harness commit")

    def act() -> None:
        if ours(CORE, "acceptance.yml", "acceptance") or state().get("acceptance_requested"):
            return
        marker = "release-" + uuid.uuid4().hex
        remember(acceptance_marker=marker, acceptance_commit=identity["commit"], acceptance_requested=True)
        pinned = identity["snapshot"]
        dispatch(CORE, "acceptance.yml", identity["ref"], "acceptance", expected_version=version,
                 expected_app_version=identity["app_version"], expected_wheel_sha256=pinned["wheel_sha256"],
                 expected_profile_sha256=pinned["profile_sha256"], release_attempt=marker)

    def done() -> bool:
        run = ours(CORE, "acceptance.yml", "acceptance")
        if run and snapshot is not None:
            release_snapshot_freeze()
        return bool(run and run["status"] == "completed")

    step("acceptance", done, act, timeout)
    return ours(CORE, "acceptance.yml", "acceptance")["databaseId"]


def acceptance_receipt_matches(result: dict[str, Any], run_id: int, attempt: int | None, identity: dict[str, Any], os_name: str) -> bool:
    """Require an accepted public result bound to the specific release, platform and workflow attempt."""
    ci, published = result["ci"], result["published"]
    return (result["verdict"] in ACCEPTED and result.get("public_path_proof") is True
            and ci.get("repository") == CORE and ci.get("sha") == identity["commit"]
            and ci.get("run_id") == str(run_id) and ci.get("runner_os") == os_name
            and ci.get("run_attempt") == str(attempt) and ci.get("event") == "workflow_dispatch"
            and published.get("expect_version") == identity["version"]
            and published.get("expect_app_version") == identity["app_version"]
            and published.get("core_wheel_sha256") == identity["snapshot"]["wheel_sha256"]
            and published.get("profile_sha256") == identity["snapshot"]["profile_sha256"])


def acceptance_receipts(run_id: int, identity: dict) -> bool:
    """Read both uploaded receipts from this run, never a local or different run's result."""
    with tempfile.TemporaryDirectory(prefix="sm-acceptance-receipts-") as directory:
        code, _ = hzlib.gh("run", "download", str(run_id), "-R", CORE,
                           "--pattern", "acceptance-result-*", "--dir", directory)
        if code:
            return False
        files = list(Path(directory).glob("*/result.json"))
        if {path.parent.name for path in files} != {"acceptance-result-Linux", "acceptance-result-macOS"} or len(files) != 2:
            return False
        for path in files:
            try:
                result = json.loads(path.read_text())
                os_name = "Linux" if path.parent.name.endswith("Linux") else "macOS"
                if not acceptance_receipt_matches(result, run_id, state().get("acceptance_run_attempt"), identity, os_name):
                    return False
            except (OSError, ValueError, KeyError, TypeError):
                return False
        return True


def pinned_snapshot(version: str) -> dict:
    """Fail closed on a changed resume identity or an older workflow that cannot enforce the snapshot."""
    try:
        snapshot = release_snapshot.capture(version)
    except (release_snapshot.InvalidSnapshot, OSError, ValueError) as exc:
        raise Stop(f"release snapshot: {exc}") from exc
    if not release_snapshot.workflow_supports_snapshot(snapshot["commit"]):
        raise Stop("tagged acceptance workflow cannot enforce a release snapshot; resume without --snapshot-acceptance")
    previous = state().get("snapshot")
    if previous is not None and previous != snapshot:
        raise Stop("release snapshot changed since the previous attempt; operator review required")
    remember(snapshot=snapshot)
    return snapshot


def verdict(run_id: int) -> tuple[bool, list[str]]:
    """Acceptance requires a successful workflow and exactly one passing verdict from each required OS."""
    identity = state().get("acceptance_identity")
    if not identity or state().get("acceptance_run_id") != run_id or not state().get("acceptance_marker"):
        return False, ["acceptance has no exact bound run identity"]
    bound = ours(CORE, "acceptance.yml", "acceptance")
    if not bound or bound["databaseId"] != run_id:
        return False, ["acceptance run identity cannot be verified"]
    expected = {"Acceptance (ubuntu-24.04)", "Acceptance (macos-latest)"}
    info = hzlib.ghj("run", "view", str(run_id), "-R", CORE, "--json", "status,conclusion,jobs", default={}) or {}
    jobs = info.get("jobs", [])
    required_jobs = [job for job in jobs if job.get("name") in expected]
    successful_jobs = (len(required_jobs) == len(expected)
                       and {job.get("name") for job in required_jobs} == expected
                       and all(job.get("conclusion") == "success" for job in required_jobs))
    good = info.get("status") == "completed" and info.get("conclusion") == "success" and successful_jobs
    _, log = hzlib.gh("run", "view", str(run_id), "-R", CORE, "--log")
    lines = []
    counts = {name: 0 for name in expected}
    for raw in log.splitlines():
        match = VERDICT.match(raw)
        if not match or match["job"] not in expected:
            continue
        try:
            result = json.loads(match["json"])
        except ValueError:
            continue
        counts[match["job"]] += 1
        ok = result.get("verdict") in ACCEPTED
        good &= ok
        lines.append(f"{match['job'].replace('Acceptance ', '')}: {result.get('verdict')}" + ("" if ok else f" ({result.get('why', '')})"))
        for key in ("failed_cases", "failures", "failed"):
            lines += [f"  case: {c}" for c in result.get(key, []) if isinstance(c, str)]
    if any(count != 1 for count in counts.values()):
        good = False
        lines.append("each required OS must provide exactly one acceptance verdict")
    if info.get("status") != "completed" or info.get("conclusion") != "success" or not successful_jobs:
        lines.append("workflow and both required OS jobs must complete successfully")
    if good and not acceptance_receipts(run_id, identity):
        good = False
        lines.append("both acceptance receipts must prove the exact release and harness identity")
    if not good:
        lines += [f"  test: {t}" for t in hzlib.failing_tests(CORE, run_id)]
    return good, lines


def snapshot_hold_path() -> Path:
    token = state().get("freeze_token")
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
        raise Stop("release hold identity is missing or invalid")
    return hzlib.release_holds() / token


def owned_hold_exists(version: str) -> bool:
    """Reject substituted directories/files or a hold belonging to a different release."""
    root, hold = hzlib.release_holds(), snapshot_hold_path()
    if root.is_symlink():
        raise Stop("release hold directory must not be a symlink")
    try:
        metadata = hold.lstat()
        if not stat.S_ISREG(metadata.st_mode) or hold.read_text() != version:
            raise Stop("release hold is not a regular file owned by this version")
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise Stop("cannot verify release hold ownership") from exc
    return True


def acquire_snapshot_freeze(version: str) -> None:
    """Use a unique owned hold; incident and other release holds have different names."""
    root = hzlib.release_holds()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise Stop("release hold directory must not be a symlink")
    if not state().get("freeze_token"):
        remember(freeze_token=uuid.uuid4().hex)
    hold = snapshot_hold_path()
    if not owned_hold_exists(version):
        try:
            with hold.open("x") as stream:
                stream.write(version)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise Stop("cannot exclusively create the release hold") from exc
    # Persist directory entries as well as file contents. A retry must repeat this barrier even when a
    # previous failed fsync left the hold visible; readiness may only be restored after durable acquisition.
    try:
        for parent in (root, root.parent):
            directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError as exc:
        raise Stop("cannot durably acquire the release hold; readiness remains unchanged") from exc


def hold_events(data: dict) -> list[dict]:
    events = data.get("hold_events", [])
    if not isinstance(events, list) or any(
        not isinstance(event, dict) or event.get("action") not in ("pause-request", "pause", "resume")
        or event.get("version") != data.get("version") or event.get("token") != data.get("freeze_token")
        or not isinstance(event.get("at"), str) for event in events
    ):
        raise Stop("release hold history is invalid")
    return events


def pretag_identity(version: str, *, allow_missing_pr: bool = False) -> dict:
    """A successful typed response must prove absence; transport failures are never absence."""
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/heinzel/{version}/json", timeout=20):
            raise Stop("pause requires the version to be absent from PyPI")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise Stop("cannot prove the version is absent from PyPI") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise Stop("cannot prove the version is absent from PyPI") from exc
    owner, name = CORE.split("/", 1)
    tag, head = f"v{version}", f"release/v{version}"
    query = """query($owner:String!,$name:String!,$tag:String!,$head:String!,$ref:String!,$branch:String!){
      repository(owner:$owner,name:$name){ref(qualifiedName:$ref){name}
      branch:ref(qualifiedName:$branch){name}
      release(tagName:$tag){tagName}
      pullRequests(first:2,headRefName:$head,states:[OPEN,CLOSED,MERGED]){
        nodes{number state headRefName baseRefName headRefOid isDraft autoMergeRequest{enabledAt}}}}}"""
    code, output = hzlib.gh("api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}",
                             "-f", f"name={name}", "-f", f"tag={tag}", "-f", f"head={head}",
                             "-f", f"ref=refs/tags/{tag}", "-f", f"branch=refs/heads/{head}")
    try:
        result = json.loads(output)
        repo = result["data"]["repository"]
        if code or result.get("errors") or not isinstance(repo, dict):
            raise ValueError
        if "ref" not in repo or "release" not in repo or repo["ref"] is not None or repo["release"] is not None:
            raise Stop("pause requires an absent release tag and GitHub release")
        prs = repo["pullRequests"]["nodes"]
        if allow_missing_pr and prs == []:
            if "branch" not in repo or repo["branch"] is not None:
                raise Stop("cannot pause before a release PR while the release branch exists or is unknown")
            return {"pr": None, "head": None, "draft": True}
        if not isinstance(prs, list) or len(prs) != 1:
            raise ValueError
        pr = prs[0]
        if (pr["state"] != "OPEN" or pr["headRefName"] != head or pr["baseRefName"] != "main"
                or pr["autoMergeRequest"] is not None):
            raise Stop("pause requires one open release PR into main without auto-merge")
        if (type(pr["number"]) is not int or pr["number"] <= 0 or type(pr["isDraft"]) is not bool
                or not re.fullmatch(r"[a-f0-9]{40}", pr["headRefOid"])):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise Stop("cannot prove the release is safely before publication") from exc
    return {"pr": pr["number"], "head": pr["headRefOid"], "draft": pr["isDraft"]}


def pause_request(events: list[dict]) -> dict | None:
    """Keep the original readiness through partial pause attempts and ambiguous API failures."""
    for event in reversed(events):
        if event["action"] == "resume":
            return None
        if event["action"] == "pause-request":
            if {"pr", "head", "was_ready"} <= event.keys() and event["pr"] is None and event["head"] is None:
                if event["was_ready"] is False:
                    return event
                raise Stop("pause request identity is invalid")
            if (type(event.get("was_ready")) is not bool or type(event.get("pr")) is not int
                    or event["pr"] <= 0 or not isinstance(event.get("head"), str)
                    or not re.fullmatch(r"[a-f0-9]{40}", event["head"])):
                raise Stop("pause request identity is invalid")
            return event
    return None


def same_pause_pr(identity: dict, request: dict) -> bool:
    return identity["pr"] == request["pr"] and identity["head"] == request["head"]


def pin_owned_hold(version: str) -> tuple[int, tuple[int, int]]:
    """Pin the directory and inode; never follow a replacement directory or hold symlink."""
    directory = None
    try:
        directory = os.open(hzlib.release_holds(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptor = os.open(snapshot_hold_path().name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor) as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or stream.read() != version:
                raise Stop("release hold ownership changed before pause")
        return directory, (metadata.st_dev, metadata.st_ino)
    except (OSError, Stop) as exc:
        if directory is not None:
            os.close(directory)
        raise Stop("cannot pin owned release hold for pause") from exc


def unlink_pinned_hold(directory: int, identity: tuple[int, int]) -> None:
    """Delete from the pinned directory only after rejecting observed inode substitution."""
    try:
        root = hzlib.release_holds().lstat()
        pinned = os.fstat(directory)
        held = os.stat(snapshot_hold_path().name, dir_fd=directory, follow_symlinks=False)
        if ((root.st_dev, root.st_ino) != (pinned.st_dev, pinned.st_ino)
                or not stat.S_ISDIR(root.st_mode) or (held.st_dev, held.st_ino) != identity):
            raise Stop("release hold directory or file was replaced; hold retained")
        os.unlink(snapshot_hold_path().name, dir_fd=directory)
        os.fsync(directory)
    except OSError as exc:
        raise Stop("cannot remove pinned release hold; pause evidence retained for retry") from exc


def refuse_tag_on_main(version: str, action: str) -> None:
    if release_flow.is_tag_on_main(version):
        raise Stop(f"{action} is for the release-PR flow; {version} is tag-on-green-main and holds no merge freeze. "
                   "Before its bump PR merges: close the bump PR and clear the train; after: fix forward on main, then "
                   f"sm release resume {version} --refresh")


def has_only_pretag_core_state(data: dict) -> bool:
    """Plugin dispatch timestamps share this receipt without advancing the core release."""
    for key in data.keys() - {"version", "freeze_token", "finished", "hold_events"}:
        value = data[key]
        if not re.fullmatch(r"plugin-heinzel-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9]+\.[0-9]+\.[0-9]+", key):
            return False
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value):
            return False
        try:
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return False
    return True


def pause_release(version: str) -> int:
    """Remove only the owned hold once the release is absent or its PR is draft."""
    refuse_tag_on_main(version, "--pause")
    data = state()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or data.get("version") != version:
        raise Stop("pause requires the exact unfinished receipt version X.Y.Z")
    if data.get("finished") or not has_only_pretag_core_state(data):
        raise Stop("pause is restricted to a pre-publication release receipt")
    events = hold_events(data)
    exists = owned_hold_exists(version)
    if not exists and (not events or events[-1]["action"] != "pause"):
        raise Stop("owned release hold is missing without a recorded pause")
    if exists:
        directory, identity = pin_owned_hold(version)
        try:
            return complete_pause(version, data, events, exists, directory, identity)
        finally:
            os.close(directory)
    return complete_pause(version, data, events, exists)


def complete_pause(version: str, data: dict, events: list[dict], exists: bool,
                   directory: int | None = None, held_identity: tuple[int, int] | None = None) -> int:
    identity = pretag_identity(version, allow_missing_pr=True)
    request = pause_request(events)
    if request is None:
        request = {"action": "pause-request", "version": version, "token": data["freeze_token"],
                   "at": now_iso(), "pr": identity["pr"], "head": identity["head"], "was_ready": not identity["draft"]}
        remember(hold_events=events + [request])
    elif not same_pause_pr(identity, request):
        raise Stop("release PR identity changed since pause was requested; hold retained")
    if not exists:
        if not identity["draft"]:
            raise Stop("paused release PR is no longer draft; coordinator ownership was violated")
        hzlib.say(f"PAUSED {version} (already); owned hold remains removed")
        return 0
    if not identity["draft"]:
        if hzlib.gh("pr", "ready", str(identity["pr"]), "-R", CORE, "--undo")[0] != 0:
            raise Stop("cannot confirm release PR was made draft; hold retained, retry to reconcile")
    verified = pretag_identity(version, allow_missing_pr=request["pr"] is None)
    if not same_pause_pr(verified, request) or not verified["draft"]:
        raise Stop("release PR was not held draft at the requested identity; hold retained")
    # With a PR, draft is the merge interlock. Without one, both its branch and PR must be absent.
    event = {"action": "pause", "version": version, "token": data["freeze_token"], "at": now_iso(), **verified}
    remember(hold_events=hold_events(state()) + [event])
    if not owned_hold_exists(version):
        raise Stop("release hold disappeared while pausing")
    unlink_pinned_hold(directory, held_identity)
    detail = "release branch and PR are absent" if verified["pr"] is None else "release PR is draft"
    hzlib.say(f"PAUSED {version}; {detail} and only its owned hold was removed. Resume with the original release command.")
    return 0


def resume_paused_hold() -> None:
    data = state()
    events = hold_events(data)
    if events and events[-1]["action"] in ("pause-request", "pause"):
        # Reacquire before any remote read or readiness mutation, even after a partially completed pause.
        acquire_snapshot_freeze(data["version"])
        request = pause_request(events)
        if request is None:
            raise Stop("paused release has no durable pause request")
        identity = pretag_identity(data["version"], allow_missing_pr=request["pr"] is None)
        if not same_pause_pr(identity, request):
            raise Stop("release PR identity changed since pause; hold reacquired")
        if request["was_ready"] and identity["draft"]:
            if hzlib.gh("pr", "ready", str(identity["pr"]), "-R", CORE)[0] != 0:
                raise Stop("cannot confirm release PR readiness; hold retained, retry to reconcile")
            verified = pretag_identity(data["version"])
            if not same_pause_pr(verified, request) or verified["draft"]:
                raise Stop("release PR readiness or identity changed; hold retained")
        elif not request["was_ready"] and not identity["draft"]:
            raise Stop("originally draft release PR was made ready externally; hold retained")
        event = {"action": "resume", "version": data["version"], "token": data["freeze_token"], "at": now_iso()}
        write_state(data | {"hold_events": events + [event]})


def dropped_dir() -> Path:
    return statedir.path("release-dropped")


DROP_QUERY = """query($owner:String!,$name:String!,$tag:String!,$head:String!,$ref:String!,$branch:String!){
  repository(owner:$owner,name:$name){ref(qualifiedName:$ref){name}
  branch:ref(qualifiedName:$branch){name}
  release(tagName:$tag){tagName}
  pullRequests(first:20,headRefName:$head,states:[OPEN,CLOSED,MERGED]){
    nodes{number state headRefName isCrossRepository}}}}"""


def drop_active_runs(head: str) -> list[int]:
    """Workflow runs on the release branch that have not completed. A candidate that still runs is never dropped:
    its remaining jobs can still fail (first-attempt rule) and its runs would outlive the hold. Unknown refuses."""
    found = []
    for status in ("queued", "in_progress", "waiting", "requested", "pending"):
        code, output = hzlib.gh("api", f"repos/{CORE}/actions/runs?branch={head}&status={status}&per_page=100")
        try:
            page = json.loads(output)
            runs = page["workflow_runs"]
            if code or type(page["total_count"]) is not int or not isinstance(runs, list) or page["total_count"] != len(runs) or any(
                    type(run["id"]) is not int or run["head_branch"] != head or run["status"] == "completed" for run in runs):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise Stop(f"drop refused: cannot prove that no candidate run on {head} is still active") from exc
        found += [run["id"] for run in runs]
    return sorted(set(found))


def drop_identity(version: str) -> dict:
    """Prove nothing of the version is published; report the release PRs and branch. Unknown is never absent."""
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/heinzel/{version}/json", timeout=20):
            raise Stop(f"drop refused: heinzel {version} is on PyPI")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise Stop(f"drop refused: cannot prove {version} is absent from PyPI (HTTP {exc.code})") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise Stop(f"drop refused: cannot prove {version} is absent from PyPI") from exc
    owner, name = CORE.split("/", 1)
    tag, head = f"v{version}", f"release/v{version}"
    code, output = hzlib.gh("api", "graphql", "-f", f"query={DROP_QUERY}", "-f", f"owner={owner}",
                             "-f", f"name={name}", "-f", f"tag={tag}", "-f", f"head={head}",
                             "-f", f"ref=refs/tags/{tag}", "-f", f"branch=refs/heads/{head}")
    try:
        result = json.loads(output)
        repo = result["data"]["repository"]
        if code or result.get("errors") or not isinstance(repo, dict) or not {"ref", "branch", "release"} <= repo.keys():
            raise ValueError
        prs = repo["pullRequests"]["nodes"]
        if not isinstance(prs, list) or len(prs) >= 20 or any(
                type(pr["number"]) is not int or pr["number"] <= 0 or pr["headRefName"] != head
                or pr["state"] not in ("OPEN", "CLOSED", "MERGED") for pr in prs):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise Stop("drop refused: cannot prove the release state on GitHub (tag, release, PR, branch)") from exc
    prs = [pr for pr in prs if not pr.get("isCrossRepository")]  # a fork's PR of the same branch name is no release PR of ours
    if repo["ref"] is not None:
        raise Stop(f"drop refused: tag {tag} exists on the remote")
    if repo["release"] is not None:
        raise Stop(f"drop refused: GitHub release {tag} exists")
    merged = [pr["number"] for pr in prs if pr["state"] == "MERGED"]
    if merged:
        raise Stop(f"drop refused: release PR #{merged[0]} is merged")
    running = drop_active_runs(head)
    if running:
        raise Stop(f"drop refused: candidate run {running[0]} on {head} is still active ({len(running)} in all); read every "
                   f"failure first, then wait for it or cancel it (gh run cancel {running[0]} -R {CORE})")
    return {"open": sorted(pr["number"] for pr in prs if pr["state"] == "OPEN"),
            "closed": sorted(pr["number"] for pr in prs if pr["state"] == "CLOSED"),
            "branch": repo["branch"] is not None}


def recorded_release_pr(data: dict[str, Any]) -> int | None:
    """The PR number `sm release pr` recorded (release_pr_opened), or None. A malformed record refuses: it is never
    read as no PR. Whether the PR is unmerged, and that no tag or PyPI release exists, drop_identity proves on GitHub."""
    opened = data.get("release_pr_opened")
    if opened is None:
        return None
    if (not isinstance(opened, dict) or set(opened) != {"pr", "head", "at"} or type(opened["pr"]) is not int
            or opened["pr"] <= 0 or not isinstance(opened["head"], str) or not re.fullmatch(r"[0-9a-f]{40}", opened["head"])
            or not isinstance(opened["at"], str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", opened["at"])):
        raise Stop("drop refused: the receipt's release_pr_opened record is malformed; preserving it for review")
    return opened["pr"]


def drop_unmet(version: str, identity: dict) -> str | None:
    if identity["open"]:
        return f"release PR #{identity['open'][0]} is open"
    if identity["branch"]:
        return f"remote branch release/v{version} exists"
    return None


def archive_dropped(version: str, stamp: str) -> Path:
    """Move the receipt (never copy and delete by content) into the dropped archive under a new name."""
    receipt, root = global_receipt(), dropped_dir()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = root / f"v{version}-{stamp}.json"
        try:
            os.link(receipt, target)
        except FileExistsError:
            target = root / f"v{version}-{stamp}-{uuid.uuid4().hex[:8]}.json"
            os.link(receipt, target)
        fsync_dir(root)
        waiver = waiver_path()
        if pending_waiver(version) is not None:
            os.replace(waiver, target.with_suffix(".waiver.json"))
        receipt.unlink()
        fsync_dir(receipt.parent)
    except OSError as exc:
        raise Stop("cannot archive the dropped receipt; run the same drop command again") from exc
    return target


def drop_candidate(version: str, reason: str, close_pr: bool) -> int:
    """Drop a provably unpublished candidate: release only its own hold and archive its receipt."""
    reason = (reason or "").strip()
    if not EXACT_VERSION.fullmatch(version or "") or len(reason) < 10:
        raise Stop("drop refused: it needs an exact version X.Y.Z and --reason with at least 10 characters")
    refuse_tag_on_main(version, "--drop-candidate")
    if RECEIPT_OVERRIDE is not None or tail_receipt(version).exists():
        raise Stop(f"drop refused: {version} is past its tag (it has a tail receipt); reject or absorb it instead")
    data = state()
    if data.get("version") != version:
        raise Stop(f"drop refused: the release receipt is for {data.get('version') or 'no release'}, not {version}")
    if data.get("finished"):
        raise Stop(f"drop refused: release {version} is finished")
    recorded = data.get("drop")
    if recorded is not None and (not isinstance(recorded, dict) or recorded.get("version") != version
                                 or recorded.get("token") != data.get("freeze_token")):
        raise Stop("drop refused: the receipt holds a drop record of another identity; preserving it for review")
    if not has_only_pretag_core_state({key: value for key, value in data.items() if key not in ("drop", "release_pr_opened")}):
        raise Stop("drop refused: the receipt records steps after the tag (PyPI, compose or acceptance)")
    opened = recorded_release_pr(data)  # `sm release pr` records the PR it opened: pre-tag, if GitHub agrees below
    events = hold_events(data)
    owned = data.get("freeze_token") is not None
    exists = owned and owned_hold_exists(version)
    if owned and not exists and recorded is None and (not events or events[-1]["action"] != "pause"):
        raise Stop("drop refused: the owned release hold is missing without a recorded pause")
    directory = held = None
    if exists:
        directory, held = pin_owned_hold(version)
    try:
        identity = drop_identity(version)  # proves: no tag, no GitHub release, PyPI 404, no merged release PR
        if opened is not None and opened not in identity["open"] + identity["closed"]:
            raise Stop(f"drop refused: the receipt names release PR #{opened}, which GitHub does not show as an "
                       f"unmerged PR of release/v{version}; nothing was dropped")
        unmet = drop_unmet(version, identity)
        if unmet and not close_pr:
            raise Stop(f"drop refused: {unmet}; close the PR and delete the branch, or pass --close-pr")
        closed_now, branch_deleted = [], False
        if unmet:
            for number in identity["open"]:
                if hzlib.gh("pr", "close", str(number), "-R", CORE)[0] != 0:
                    raise Stop(f"drop refused: cannot close release PR #{number}; nothing was dropped")
                closed_now.append(number)
            if identity["branch"]:
                # A failed delete is judged by the proof below: closing a PR may already have removed the branch.
                hzlib.gh("api", "--method", "DELETE", f"repos/{CORE}/git/refs/heads/release/v{version}")
                branch_deleted = True
            identity = drop_identity(version)
            if unmet := drop_unmet(version, identity):
                raise Stop(f"drop refused: {unmet} after --close-pr; nothing was dropped")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        record = {"version": version, "token": data.get("freeze_token"), "reason": reason, "at": now_iso(),
                  "hold": "owned" if exists else "paused" if owned else "none",
                  "evidence": {"tag": False, "github_release": False, "pypi": 404, "merged_pr": False,
                               "closed_prs": identity["closed"], "closed_by_drop": closed_now,
                               "branch": False, "branch_deleted_by_drop": branch_deleted,
                               **({"recorded_pr": opened} if opened is not None else {})}}
        if recorded is None:
            remember(drop=record)  # durable intent before the hold goes: a retry may then find the hold absent
        else:
            record = recorded
        if exists:
            if not owned_hold_exists(version):
                raise Stop("drop refused: the release hold disappeared while dropping")
            unlink_pinned_hold(directory, held)
    finally:
        if directory is not None:
            os.close(directory)
    target = archive_dropped(version, stamp)
    hold = {"owned": "its owned hold was removed", "paused": "its owned hold was already removed by --pause",
            "none": "it owned no hold"}.get(record.get("hold"), "its owned hold is removed")
    hzlib.say(f"DROPPED {version}: {hold}; receipt archived at {target}. No workflow run was cancelled. "
              f"A fresh `sm release resume {version}` starts clean.")
    if hzlib.merge_frozen():
        hzlib.say("note: other merge holds remain (incident sentinel or another release's hold); they were not touched")
    return 0


def release_snapshot_freeze() -> None:
    """Remove this release's hold; a verified tagged adoption may never have owned one."""
    data = state()
    if "freeze_token" not in data:
        version = data.get("version")
        if data.get("hold_events", []) != []:
            raise Stop("release hold history is invalid without an owned hold")
        if not isinstance(version, str) or EXACT_VERSION.fullmatch(version) is None or not has_tag(CORE, f"v{version}"):
            raise Stop("release hold identity is missing or invalid")
        hzlib.say("ok   tagged adoption has no release-owned hold; other holds remain untouched")
        return
    snapshot_hold_path().unlink(missing_ok=True)


def prepare_release(version: str) -> int:
    """Resume only the paused, owned pre-tag hold and exact PR readiness; perform no release step."""
    refuse_tag_on_main(version, "--prepare-only")
    data = state()
    if (not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or data.get("version") != version
            or data.get("finished") or not has_only_pretag_core_state(data)):
        raise Stop("prepare-only requires the exact unfinished pre-publication receipt version")
    events = hold_events(data)
    request = next((event for event in reversed(events) if event["action"] == "pause-request"), None)
    if (request is None or request.get("was_ready") is not True or type(request.get("pr")) is not int
            or request["pr"] <= 0 or not isinstance(request.get("head"), str)
            or not re.fullmatch(r"[a-f0-9]{40}", request["head"])):
        raise Stop("prepare-only requires a recorded pause of the previously ready exact release PR")
    # The existing resume operation reacquires the same owned hold before its first remote read.
    resume_paused_hold()
    if not owned_hold_exists(version):
        raise Stop("prepare-only requires the release-owned hold to remain present")
    identity = pretag_identity(version)
    if not same_pause_pr(identity, request) or identity["draft"]:
        raise Stop("prepare-only release PR identity or readiness changed; hold retained")
    events = hold_events(state())
    audit = {"action": "resume", "mode": "prepare-only", "version": version, "token": data["freeze_token"],
             "at": now_iso(), "pr": identity["pr"], "head": identity["head"]}
    if not events or any(events[-1].get(key) != audit[key] for key in audit if key != "at"):
        remember(hold_events=events + [audit])
    hzlib.say(f"PREPARED {version}; owned hold retained, PR #{identity['pr']} ready at {identity['head'][:8]}. "
              "No main check, merge, dispatch or publication performed.")
    return 0


def release_core(request: str, intro: str, timeout: float, snapshot_acceptance: bool = False, harness_ref: str | None = None, replace_run: int | None = None, composite_run: int | None = None, candidate_gate: bool = False, wait_previous: float | None = None, refresh: bool = False, replace_reason: str | None = None) -> int:
    flow = pick_flow(request, composite_run)
    if flow == release_flow.TAG_ON_MAIN:
        return release_core_on_main(request, intro, timeout, snapshot_acceptance, harness_ref, replace_run,
                                    candidate_gate, wait_previous, refresh, replace_reason)
    if refresh:
        raise Stop("--refresh is only for a tag-on-green-main release (flag HZ_RELEASE_TAG_ON_MAIN at its start)")
    freeze, _ = paths()
    previous = state()
    events = hold_events(previous)
    if (events and events[-1]["action"] in ("pause-request", "pause")
            and request not in ("patch", previous.get("version"))):
        raise Stop("resume requires the paused receipt version or patch")
    resume_paused_hold()
    version, bump = next_version(request)
    rejection_guard(version)
    if RECEIPT_OVERRIDE is None and state().get("finished") and state().get("version") != version:
        migrate_legacy_receipt()  # never overwrite a finished legacy receipt: it is N's verdict for N+1's wait
    guard_tails(version)
    tag = f"v{version}"
    previous = state()
    if previous.get("version") and previous["version"] != version and not previous.get("finished"):
        raise Stop(f"release {previous['version']} is unfinished; finish it before starting {version}")
    if (previous.get("version") == version and previous.get("acceptance_identity")
            and previous.get("acceptance_snapshot_mode", True) != snapshot_acceptance):
        raise Stop("acceptance resume must preserve the original snapshot hold mode")
    if previous.get("version") == version and previous.get("snapshot") and previous.get("acceptance_snapshot_mode", True) and not snapshot_acceptance:
        raise Stop("this release uses a snapshot; resume with --snapshot-acceptance")
    if previous.get("version") != version:
        write_state({"version": version})
    hzlib.say(f"release {tag}")
    # An unsupported tagged workflow can resume on the default path. Keep its existing owned hold until default
    # acceptance finishes; do not switch to or remove a shared incident sentinel while that hold is active.
    owned_hold = snapshot_acceptance or bool(state().get("freeze_token"))
    # The freeze covers only cut -> tag (research/pipelined-release-train-2026-11-07.md): a resume past the tag never
    # freezes main again, because PyPI, compose and acceptance are pinned to the tag, not to main.
    if has_tag(CORE, tag):
        hzlib.say("ok   freeze (not needed: tag exists, main stays open)")
    elif owned_hold:
        acquire_snapshot_freeze(version)
        hzlib.say("ok   freeze (release-owned)")
    else:
        step("freeze", freeze.exists, lambda: freeze.touch(), 60)
    if candidate_gate:
        step("main gate (train path)", lambda: has_tag(CORE, tag) or main_candidate_gate(version), None, timeout)
    else:
        step("main tip green", lambda: has_tag(CORE, tag) or main_tip_green(), None, timeout)
    # Open first so the candidate CI runs while the previous release's tail is judged; the merge publishes.
    step("release PR open", lambda: has_tag(CORE, tag) or bool(release_pr(tag)), lambda: open_release_pr(tag, bump, intro), timeout)
    step("previous release accepted", lambda: has_tag(CORE, tag) or previous_accepted(version), None,
         timeout if wait_previous is None else wait_previous)
    step("release PR merged", lambda: has_tag(CORE, tag) or bool((p := release_pr(tag)) and p["state"] == "MERGED"), lambda: merge_release_pr(tag, composite_run) if composite_run else merge_release_pr(tag), timeout)
    step("tag", lambda: has_tag(CORE, tag), None, timeout)
    lift_freeze_at_tag(owned_hold, freeze)
    # The tag exists before its release does; pypi.yml reads the published release, so dispatching on the tag alone races.
    step("release published", lambda: release_published(tag), None, timeout)
    pypi_step("heinzel", version, tag, timeout)
    compose_step(version, timeout)
    snapshot = pinned_snapshot(version) if snapshot_acceptance else None
    run_id = acceptance_step(version, timeout, snapshot, harness_ref, replace_run, replace_reason)
    # Past the handoff the shared sentinel may belong to the next candidate (N+1): the tail never touches it.
    if owned_hold:
        release_snapshot_freeze()
    elif RECEIPT_OVERRIDE is None:
        freeze.unlink(missing_ok=True)
    hzlib.say("ok   release hold removed; other merge holds remain" if hzlib.merge_frozen() else "ok   unfreeze")
    good, lines = verdict(run_id)
    publish_acceptance(version, run_id, good)  # before `finished`, so a failed upload is retried on resume
    if good:
        promote_release(version)
    remember(finished=True, acceptance_run=run_id, accepted=good)
    hzlib.say(f"{'ACCEPTED' if good else 'NOT ACCEPTED'} {version} (run {run_id})")
    for line in lines:
        hzlib.say("  " + line)
    return 0 if good else 1

# ---------------------------------------------------------------- tag-on-green-main (core ADR 0144, flag)

BUMP_RUN_LISTING = "actions/workflows/{workflow}/runs?head_sha={sha}&event=push&per_page=100"  # workflow: Core's policy


def pick_flow(request: str, composite_run: int | None) -> str:
    """The flow is pinned per version at its first start (release_flow); a version already in a receipt, past its tag
    or resumed with candidate composite evidence stays on the release-PR flow."""
    data = state()
    try:
        # The pin wins over everything (review 2026-11-08 CHANGE 2): read it before the hold/pause shortcut.
        early = request if request != "patch" else data.get("version")
        pinned = release_flow.flow_of(early) if isinstance(early, str) and EXACT_VERSION.fullmatch(early) else None
    except release_flow.FlowError as exc:
        raise Stop(str(exc)) from exc
    if pinned:
        return pinned
    if data.get("freeze_token") or data.get("hold_events"):
        return release_flow.RELEASE_PR  # an owned hold or a pause exists only in the release-PR flow; no network first
    version = next_version(request)[0] if request == "patch" else request
    if not EXACT_VERSION.fullmatch(version or ""):
        raise Stop("an exact version X.Y.Z is required")
    try:
        pinned = release_flow.flow_of(version)
        if pinned:
            return pinned
        started = (data.get("version") == version or RECEIPT_OVERRIDE is not None or composite_run is not None
                   or has_tag(CORE, f"v{version}"))
        return release_flow.choose(version, started)
    except release_flow.FlowError as exc:
        raise Stop(str(exc)) from exc


def flow_data(version: str) -> dict:
    try:
        data = release_flow.read(version)
    except release_flow.FlowError as exc:
        raise Stop(str(exc)) from exc
    if not data or data["flow"] != release_flow.TAG_ON_MAIN:
        raise Stop(f"release {version} is not a tag-on-green-main release")
    return data


def save_bump(version: str, bump: dict) -> None:
    data = flow_data(version)
    bumps = list(data.get("bumps") or [])
    if bumps and bumps[-1].get("branch") == bump.get("branch"):
        bumps[-1] = bump
    else:
        bumps.append(bump)
    release_flow.update(version, bumps=bumps)


def bump_prs(tag: str) -> list[dict]:
    """bump/vX and its refresh branches bump/vX-refresh-<sha>; nothing else."""
    rows = hzlib.ghj("pr", "list", "-R", CORE, "--state", "all", "-L", "50", "--search", f"head:bump/{tag}",
                     "--json", "number,state,headRefName,title,mergeCommit", default=None)
    if not isinstance(rows, list):
        raise Stop("cannot list bump PRs")
    pattern = re.compile(rf"bump/{re.escape(tag)}(-refresh-[0-9a-f]{{12}})?")
    return [r for r in rows if isinstance(r, dict) and pattern.fullmatch(str(r.get("headRefName", "")))]


def bump_pr(tag: str, branch: str) -> dict | None:
    return next((r for r in bump_prs(tag) if r["headRefName"] == branch), None)


def refused_stop(version: str, bump: dict) -> Stop:
    return Stop(f"candidate of bump commit {str(bump.get('sha'))[:9]} refused: {bump.get('why')}; "
                f"fix forward on main, then sm release resume {version} --refresh")


def open_bump_pr(version: str, bump_kind: str, intro: str, refresh: bool) -> None:
    tag = f"v{version}"
    data = flow_data(version)
    current = release_flow.current_bump(data)
    if current and current.get("verdict") != "refused":
        return
    if current and not refresh:
        raise refused_stop(version, current)
    if refresh and not current:
        raise Stop(f"--refresh needs a refused candidate of {version} first")
    known = {b.get("branch") for b in data.get("bumps") or []}
    pending = [r for r in bump_prs(tag) if r["headRefName"] not in known and r.get("state") != "CLOSED"]
    if (refresh and pending and pending[0]["headRefName"].startswith(f"bump/{tag}-refresh-")) or (not refresh and pending):
        save_bump(version, {"branch": pending[0]["headRefName"], "pr": pending[0]["number"]})  # resume after release.sh
        return
    raise Stop("tag-on-green-main bump PR creation is blocked until its --open-pr push is release-scoped")


def checkout_branch(work: str) -> str:
    """release.sh leaves the release checkout on the branch it pushed."""
    return edge.run(["git", "-C", work, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True,
                          check=False).stdout.strip()


def bump_runs(sha: str) -> list[dict] | None:
    listing = hzlib.ghj("api", f"repos/{CORE}/" + BUMP_RUN_LISTING.format(workflow=core_facts.ci_file(), sha=sha), default=None)
    rows = listing.get("workflow_runs") if isinstance(listing, dict) else None
    return rows if isinstance(rows, list) else None


def dispatch_tag_workflow(sha: str) -> bool:
    return hzlib.gh("workflow", "run", "release-tag.yml", "-R", CORE, "--ref", "main", "-f", f"sha={sha}")[0] == 0


def ship(pr: int) -> tuple[int, str]:
    """hz-ship: diff-guard, then the queue merges it like any lean PR (it respects incident holds)."""
    done = edge.run(edge.ship(pr), capture_output=True,  # moved: `sm head ship PR`, no script is started
                          text=True, check=False)
    return done.returncode, (done.stdout + done.stderr).strip()


def bump_merged(version: str) -> bool:
    current = release_flow.current_bump(flow_data(version)) or {}
    if current.get("sha"):
        return True
    found = bump_pr(f"v{version}", current.get("branch", ""))
    if not found:
        raise Stop(f"bump PR {current.get('branch')} disappeared")
    if found.get("state") == "CLOSED":
        raise Stop(f"bump PR #{found['number']} was closed unmerged: reopen it or clear the release")
    sha = (found.get("mergeCommit") or {}).get("oid", "")
    if found.get("state") != "MERGED" or not release_candidate_policy.release_candidate.SHA.fullmatch(sha or ""):
        return False
    save_bump(version, current | {"sha": sha})
    hzlib.say(f"     bump commit {sha[:9]} on main (#{found['number']})")
    return True


def queue_bump(version: str) -> None:
    current = release_flow.current_bump(flow_data(version)) or {}
    if current.get("queued"):
        return
    code, out = ship(current["pr"])
    if code != 0:
        raise Stop(f"sm head ship {current['pr']} did not queue the bump PR (exit {code}): {out.splitlines()[-1] if out else '?'}")
    save_bump(version, current | {"queued": True})


def candidate_verified(version: str) -> bool:
    """Wait until the bump commit's main push run is complete, then judge it once with the reviewed policy."""
    current = release_flow.current_bump(flow_data(version)) or {}
    if current.get("verdict") == "ok":
        return True
    if current.get("verdict") == "refused":
        raise refused_stop(version, current)
    sha = current.get("sha", "")
    rows = bump_runs(sha)
    if not rows or any(r.get("status") != "completed" for r in rows):
        return False  # not created yet, queued or running: the main run of the bump commit is the candidate
    try:
        evidence = release_candidate_policy.verify_commit(sha, pending_waiver(version))
    except Exception as exc:  # Fail-closed boundary: malformed or red evidence never tags.
        why = str(exc) if isinstance(exc, release_candidate_policy.release_candidate.CandidateError) else f"invalid evidence ({type(exc).__name__})"
        save_bump(version, current | {"verdict": "refused", "why": why})
        raise refused_stop(version, current | {"why": why}) from exc
    remember(candidate_ci=evidence)
    save_bump(version, current | {"verdict": "ok", "run": evidence.get("run_id")})
    if evidence.get("owner_waiver"):
        w = evidence["owner_waiver"]
        hzlib.say(f"owner waiver applied: {w['job']}=KNOWN({w['bead']}), attempt-2 rerun green (run {w['run_id']})")
    hzlib.say(f"     candidate run {evidence.get('run_id')} of {sha[:9]}: first attempt, {evidence.get('job_count')} jobs green")
    return True


def tag_commit(tag: str) -> str:
    code, out = hzlib.gh("api", f"repos/{CORE}/commits/{tag}", "--jq", ".sha")
    return out.strip() if code == 0 else ""


def tagged_at_bump(version: str) -> bool:
    tag = f"v{version}"
    if not has_tag(CORE, tag):
        return False
    current = release_flow.current_bump(flow_data(version)) or {}
    if current.get("verdict") != "ok" or not current.get("sha"):
        raise Stop(f"{tag} exists without a verified bump commit of this release; preserve it for review")
    if tag_commit(tag) != current["sha"]:
        raise Stop(f"{tag} exists but does not point at the verified bump commit {current['sha'][:9]}; preserve it for review")
    return True


def dispatch_tag(version: str) -> None:
    data = flow_data(version)
    current = release_flow.current_bump(data) or {}
    if current.get("verdict") != "ok" or not current.get("sha"):
        raise Stop("the tag needs a verified candidate")  # never reached: the step order guarantees it
    if data.get("tag_dispatched_at") and data.get("tag_sha") == current["sha"]:
        return
    stamp = now_iso(-5)
    if not dispatch_tag_workflow(current["sha"]):
        raise Stop("dispatching release-tag.yml failed")
    release_flow.update(version, tag_dispatched_at=stamp, tag_sha=current["sha"])


def tag_check(version: str) -> None:
    since = flow_data(version).get("tag_dispatched_at")
    if not since:
        return
    for run in runs(CORE, "release-tag.yml", event="workflow_dispatch"):
        if run["createdAt"] >= since and run["status"] == "completed" and run["conclusion"] != "success" \
                and not has_tag(CORE, f"v{version}"):
            raise Stop(f"release-tag run {run['databaseId']} ended {run['conclusion']}: sm ci watch {run['databaseId']}")


def release_core_on_main(request: str, intro: str, timeout: float, snapshot_acceptance: bool, harness_ref: str | None,
                         replace_run: int | None, candidate_gate: bool, wait_previous: float | None,
                         refresh: bool, replace_reason: str | None = None) -> int:
    version, bump_kind = next_version(request)
    rejection_guard(version)
    if RECEIPT_OVERRIDE is None and state().get("finished") and state().get("version") != version:
        migrate_legacy_receipt()
    guard_tails(version)
    tag = f"v{version}"
    previous = state()
    if previous.get("version") and previous["version"] != version and not previous.get("finished"):
        raise Stop(f"release {previous['version']} is unfinished; finish it before starting {version}")
    if previous.get("version") == version and previous.get("snapshot") and previous.get("acceptance_snapshot_mode", True) and not snapshot_acceptance:
        raise Stop("this release uses a snapshot; resume with --snapshot-acceptance")
    if previous.get("version") != version:
        write_state({"version": version})
    hzlib.say(f"release {tag} (tag-on-green-main)")
    hzlib.say("ok   no freeze (tag-on-green-main: main stays open)")
    tagged = lambda: tagged_at_bump(version)  # noqa: E731
    if candidate_gate:
        step("main gate (train path)", lambda: tagged() or main_candidate_gate(version), None, timeout)
    else:
        step("main tip green", lambda: tagged() or main_tip_green(), None, timeout)
    step("bump PR open", lambda: tagged() or ((b := release_flow.current_bump(flow_data(version))) is not None
                                             and b.get("verdict") != "refused"),
         lambda: open_bump_pr(version, bump_kind, intro, refresh), timeout)
    step("bump PR merged", lambda: tagged() or bump_merged(version), lambda: queue_bump(version), timeout)
    step("candidate verified", lambda: tagged() or candidate_verified(version), None, timeout)
    # Before the tag (review 2026-11-08 CHANGE 1): publish.yml holds the tag as a pre-release, promoted after ACCEPTED
    # (core ADR 0114); N+1 is tagged only after N is ACCEPTED or absorbed. Main is never held by it.
    step("previous release accepted", lambda: tagged() or previous_accepted(version), None,
         timeout if wait_previous is None else wait_previous)
    step("tag", tagged, lambda: dispatch_tag(version), timeout, lambda: tag_check(version))
    hand_off_tail()
    step("release published", lambda: release_published(tag), None, timeout)
    pypi_step("heinzel", version, tag, timeout)
    compose_step(version, timeout)
    snapshot = pinned_snapshot(version) if snapshot_acceptance else None
    run_id = acceptance_step(version, timeout, snapshot, harness_ref, replace_run, replace_reason)
    good, lines = verdict(run_id)
    publish_acceptance(version, run_id, good)
    if good:
        promote_release(version)
    remember(finished=True, acceptance_run=run_id, accepted=good)
    hzlib.say(f"{'ACCEPTED' if good else 'NOT ACCEPTED'} {version} (run {run_id})")
    for line in lines:
        hzlib.say("  " + line)
    return 0 if good else 1



ACCEPTANCE_ASSET = "acceptance.json"
"""The public verdict marker on the GitHub release; core auto-update installs only a release whose marker says accepted."""


def newest_final_tag() -> str | None:
    """The newest published FINAL vX.Y.Z (held pre-releases, drafts, edge-v/app-v excluded); Stop when unreadable."""
    code, out = hzlib.gh("api", f"repos/{CORE}/releases?per_page=30", "--jq",
                         "[.[] | select((.draft | not) and (.prerelease | not)) | .tag_name] | .[]")
    if code != 0:
        raise Stop("promote: cannot read the published final releases")
    tags = [t for t in out.split() if t.startswith("v") and EXACT_VERSION.fullmatch(t[1:])]
    return max(tags, key=lambda t: vkey(t[1:])) if tags else None


def promote_release(version: str) -> None:
    """Make an ACCEPTED vX.Y.Z final and latest (core ADR 0114: Publish holds it as a pre-release); idempotent.

    Called only after this version's own ACCEPTED verdict. Promotes exactly the bytes acceptance pinned, never an
    absorbed release (it stays a pre-release; the absorbing release is promoted on its own verdict) and never
    behind a newer final release, so `latest` cannot move backwards."""
    tag = f"v{version}"
    if not EXACT_VERSION.fullmatch(version or ""):
        raise Stop(f"promote: {version!r} is no exact version")
    data = state()
    if data.get("version") != version:
        raise Stop(f"promote: the release receipt is not {version}'s")
    if data.get("absorbed_by"):
        raise Stop(f"promote: {version} is absorbed by {data['absorbed_by']}; it stays held")
    hold = lambda: hzlib.ghj("api", f"repos/{CORE}/releases/tags/{tag}", default=None)  # noqa: E731
    before = hold()
    if not isinstance(before, dict) or before.get("tag_name") != tag or before.get("draft") is not False:
        raise Stop(f"promote: {tag} is no published release; the stable channel keeps it held")
    if before.get("prerelease") is False:
        # Core's Publish creates a final release that is not Latest (the updater offers releases/latest): the
        # accepted release becomes Latest here, never behind a newer one.
        def latest_tag() -> str:
            got, out = hzlib.gh("api", f"repos/{CORE}/releases/latest", "--jq", ".tag_name")
            return out.strip() if got == 0 else ""

        latest = latest_tag()
        if latest == tag:
            hzlib.say(f"ok   promote {tag}: already final and Latest")
            return
        if latest.startswith("v") and EXACT_VERSION.fullmatch(latest[1:]) and vkey(latest[1:]) > vkey(version):
            hzlib.say(f"ok   promote {tag}: final; {latest} is newer and stays Latest")
            return
        code, _ = hzlib.gh("release", "edit", tag, "--latest", "-R", CORE)
        if code != 0 or latest_tag() != tag:
            raise Stop(f"promote: {tag} is final but not Latest (gh exit {code}); rerun sm release resume {version} to retry")
        remember(promoted_at=now_iso())
        hzlib.say(f"ok   promote {tag}: final and Latest; the stable channel offers it now")
        return
    pinned = (data.get("acceptance_identity") or {}).get("snapshot") or data.get("snapshot")
    if not isinstance(pinned, dict) or pinned.get("tag") != tag or pinned.get("version") != version:
        raise Stop(f"promote: {tag} has no pinned acceptance snapshot")
    try:
        wheel = release_snapshot.core_wheel_digest(before, version)
    except release_snapshot.InvalidSnapshot as exc:
        raise Stop(f"promote: {tag}: {exc}") from exc
    if wheel != pinned.get("wheel_sha256"):
        raise Stop(f"promote: {tag} wheel differs from the accepted snapshot")
    newest = newest_final_tag()
    if newest is not None and vkey(newest[1:]) >= vkey(version):
        raise Stop(f"promote: {newest} is already final; {tag} stays held so latest never moves backwards")
    hzlib.say(f"run  promote {tag}: gh release edit --prerelease=false --latest")
    code, _ = hzlib.gh("release", "edit", tag, "--prerelease=false", "--latest", "-R", CORE)
    after = hold()
    if (code != 0 or not isinstance(after, dict) or after.get("tag_name") != tag or after.get("draft") is not False
            or after.get("prerelease") is not False):
        raise Stop(f"promote: {tag} is still a pre-release (gh exit {code}); rerun sm release resume {version} to retry")
    remember(promoted_at=now_iso())
    hzlib.say(f"ok   promote {tag}: final and latest; the stable channel offers it now")


def publish_acceptance(version: str, run_id: int, good: bool) -> None:
    """Upload the verdict as release asset `acceptance.json` (replacing any earlier one) and read it back."""
    tag = f"v{version}"
    marker = {"schema": 1, "project": "heinzel", "version": version, "accepted": bool(good),
              "acceptance_run": run_id, "at": now_iso()}
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / ACCEPTANCE_ASSET
        path.write_text(json.dumps(marker) + "\n")
        code, _ = hzlib.gh("release", "upload", tag, str(path), "--clobber", "-R", CORE)
    names = hzlib.ghj("api", f"repos/{CORE}/releases/tags/{tag}", "--jq", "[.assets[].name]", default=None)
    if code != 0 or not isinstance(names, list) or ACCEPTANCE_ASSET not in names:
        raise Stop(f"cannot publish the acceptance marker on {tag}; auto-update holds {version} until it exists")
    hzlib.say(f"ok   acceptance marker on {tag}: {'accepted' if good else 'not accepted'}")


def lift_freeze_at_tag(owned_hold: bool, freeze: Path) -> None:
    """Right after the tag: remove only this release's own hold (or the release sentinel), so main merges resume
    while PyPI, compose and acceptance run against the tag. Incident holds stay; idempotent on resume. A tail resume
    (already handed off) touches no freeze: the shared sentinel may then belong to the next candidate."""
    if RECEIPT_OVERRIDE is not None:
        hzlib.say("ok   merge freeze lifted at tag (already; tail receipt)")
        return
    if owned_hold:
        release_snapshot_freeze()
    else:
        freeze.unlink(missing_ok=True)
    if not state().get("freeze_lifted_at"):
        remember(freeze_lifted_at=now_iso())
    hzlib.say("ok   merge freeze lifted at tag; other merge holds remain" if hzlib.merge_frozen()
              else "ok   merge freeze lifted at tag (main open for the tail)")
    hand_off_tail()


PLUGIN_TAG = re.compile(r"\sRELEASE_TAG: (\S+)\s*$", re.MULTILINE)
UNBOUND = ("the plugin release was dispatched but GitHub named no run ID for it; no run is adopted. Look at the release "
           "runs; when none releases this plugin version and none is active: sm release plugin NAME --redispatch")


def plugin_dispatch(key: str, dist: str) -> None:
    """Dispatch one plugin release and bind it to exactly the run GitHub created for it (bead heinzel-management-ovvg).

    The dispatch asks GitHub for the new run's ID (`return_run_details`); a resume follows only that ID. The run list
    cannot show a run's inputs, and every plugin release runs on the same main head, so a reply without an ID stops:
    no run is guessed (not the newest of any plugin, not one by time and head)."""
    code, sha = hzlib.gh("api", f"repos/{PLUGINS}/commits/main", "--jq", ".sha")
    sha = sha.strip()
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise Stop("cannot resolve main of release.yml to an exact commit before dispatching it")
    inputs = {"plugin": dist, "publish": "true", "registry": "pypi"}
    remember(**{key: now_iso(-5), f"{key}_run_id": None, f"{key}_run_attempt": None,
                f"{key}_dispatch": {"repo": PLUGINS, "workflow": "release.yml", "ref": "main", "sha": sha, "event": "workflow_dispatch",
                                    "inputs": inputs}})
    code, out = hzlib.gh("api", "-X", "POST", f"repos/{PLUGINS}/actions/workflows/release.yml/dispatches", "-f", "ref=main",
                         *(arg for name, value in inputs.items() for arg in ("-f", f"inputs[{name}]={value}")), "-F", "return_run_details=true")
    if code != 0:
        raise Stop("dispatching release.yml failed")
    try:
        reply = json.loads(out) if out.strip() else {}
    except ValueError:
        reply = {}
    run_id = reply.get("workflow_run_id") if isinstance(reply, dict) else None
    if type(run_id) is not int or run_id <= 0:
        raise Stop(UNBOUND)
    # GitHub named the run it created: bind to it, at the head it really took
    asked = state()[f"{key}_dispatch"]
    run = hzlib.ghj("api", f"repos/{PLUGINS}/actions/runs/{run_id}", default=None)
    if isinstance(run, dict) and run.get("id") == run_id and isinstance(run.get("head_sha"), str):
        asked = asked | {"sha": run["head_sha"]}  # main may have moved between resolving it and the dispatch
    remember(**{f"{key}_run_id": run_id, f"{key}_dispatch": asked})


def plugin_run(key: str) -> dict | None:
    """The run of this plugin release's own dispatch, by its recorded ID; never a run of another dispatch."""
    data = state()
    asked = data.get(f"{key}_dispatch")
    if not isinstance(asked, dict):
        return None
    if data.get(f"{key}_run_id") is None:
        raise Stop(UNBOUND)
    return bound_run(PLUGINS, "release.yml", key, data.get(key), asked, data)


def plugin_run_tag(run_id: int) -> str | None:
    """The RELEASE_TAG the run's publish job released (its step environment in the job log), else None."""
    jobs = hzlib.ghj("api", f"repos/{PLUGINS}/actions/runs/{run_id}/jobs", default=None)
    tags = set()
    for job in (jobs or {}).get("jobs", []) if isinstance(jobs, dict) else []:
        if isinstance(job, dict) and job.get("name") in ("publish", "github-publish") and job.get("conclusion") == "success":
            code, log = hzlib.gh("run", "view", "-R", PLUGINS, "--job", str(job.get("id")), "--log")
            tags |= set(PLUGIN_TAG.findall(log)) if code == 0 else set()
    return tags.pop() if len(tags) == 1 else None


def release_plugin(name: str, timeout: float, redispatch: bool = False) -> int:
    """Plugin stamps live in their own receipt: the core receipt (and its handoff, verify and the next cut) never sees them.
    The receipt binds the release to its dispatched run's ID; `--redispatch` is the only way to a second dispatch."""
    global RECEIPT_OVERRIDE
    if plugin_keys(read_json_object(global_receipt()) or {}):
        raise Stop("the release receipt still holds plugin stamps from an older sm; run `sm release repair-receipt --apply` first")
    RECEIPT_OVERRIDE = plugin_receipt()
    dist = name if name.startswith("heinzel-") else f"heinzel-{name}"
    code, out = hzlib.gh("api", f"repos/{PLUGINS}/contents/plugins/{dist}/pyproject.toml?ref=main", "--jq", ".content")
    if code != 0:
        raise Stop(f"{dist} not found in heinzel-plugins main")
    version = tomllib.loads(base64.b64decode(out).decode())["project"]["version"]
    # The dispatch key names the version: a receipt of an earlier release of this plugin must not count as this one.
    key, tag = f"plugin-{dist}-{version}", f"{dist}/v{version}"
    again = f"sm release plugin {name} --redispatch"

    def act() -> None:
        data = state()
        if isinstance(data.get(f"{key}_dispatch"), dict):
            if not redispatch:
                return  # follow the recorded run
            run = plugin_run(key) if data.get(f"{key}_run_id") is not None else {"databaseId": None, "status": "completed"}
            if not run or run["status"] != "completed":
                raise Stop(f"--redispatch refused: the recorded release run of {dist} {version} is not completed"
                           + (f": sm ci watch {run['databaseId']} -R {PLUGINS}" if run else ""))
            remember(**{f"{key}_replaced": [*(data.get(f"{key}_replaced") or []), {"run": run["databaseId"], "conclusion": run.get("conclusion"), "at": now_iso()}]})
        elif data.get(key) and not redispatch:
            raise Stop(f"the plugin receipt holds only a dispatch time for {dist} {version} (an older sm) and no run of its own; "
                       f"no other run is adopted. Look at the release runs; if none releases {tag}: {again}")
        plugin_dispatch(key, dist)

    succeeded_at: list[float] = []  # when the run was first seen ended success without the version on PyPI

    def check() -> None:
        run = plugin_run(key)
        if run and run["status"] == "completed" and not pypi_has(dist, version):
            if run["conclusion"] == "success":
                succeeded_at[:1] = succeeded_at[:1] or [time.time()]
                if time.time() - succeeded_at[0] < PYPI_PROPAGATION:
                    return  # PyPI propagation: keep polling within the bounded window
                raise Stop(f"the release run {run['databaseId']} ended success but {dist} {version} is not on PyPI after "
                           f"{PYPI_PROPAGATION // 60} minutes: sm ci watch {run['databaseId']} -R {PLUGINS}")
            raise Stop(f"the release run {run['databaseId']} ended {run['conclusion']} without {dist} {version} on PyPI: "
                       f"sm ci watch {run['databaseId']} -R {PLUGINS}; after the fix: {again}")

    step(f"PyPI {dist} {version}", lambda: pypi_has(dist, version), act, timeout, check)
    run_id = state().get(f"{key}_run_id")
    if run_id is not None:  # the version is on PyPI; success names this run only when it released exactly this tag
        released = plugin_run_tag(run_id) if type(run_id) is int and run_id > 0 else None
        if released != tag:
            raise Stop(f"{dist} {version} is on PyPI but the recorded release run {run_id} released {released or 'no readable RELEASE_TAG'}, "
                       f"not {tag}: sm ci watch {run_id} -R {PLUGINS}")
    hzlib.say(f"RELEASED {dist} {version}, verified on PyPI" + (f" (run {run_id}, {tag})" if run_id is not None else ""))
    return 0


class ReleaseAdapter:
    """Use the existing coordinator's receipt writer and pinned hold operations."""

    def __getattr__(self, name):
        return globals()[name]


def rejection_guard(version: str | None) -> None:
    try:
        release_rejection.guard(version)
    except (release_rejection.InvalidRejection, OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise Stop(f"release rejection history blocks dispatch: {exc}") from exc


def reject_release(args) -> int:
    try:
        return release_rejection.reject(ReleaseAdapter(), args)
    except (release_rejection.InvalidRejection, OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise Stop(f"release rejection not finalized: {exc}") from exc


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="The release steps of AGENTS.md section 6, resumable.")
    parser.add_argument("version", nargs="?", help="X.Y.Z or patch")
    parser.add_argument("--plugin")
    parser.add_argument("--redispatch", action="store_true", help="with --plugin: dispatch again after the recorded run completed without the release")
    parser.add_argument("--repair-receipt", action="store_true", help="move plugin stamps out of the core release receipt (dry run unless --apply)")
    parser.add_argument("--apply", action="store_true", help="with --repair-receipt: write the repair (a backup copy first)")
    parser.add_argument("--candidate-capacity", choices=("prepare", "request"), help="explicit infrastructure-only candidate audit/request; no release actions")
    parser.add_argument("--candidate-run", type=int)
    parser.add_argument("--expected-audit-sha256")
    parser.add_argument("--use-candidate-composite", type=int, help="explicitly verify audited original+replacement execution; never first-attempt proof")
    parser.add_argument("--reject-release", action="store_true", help="close the exact reviewed published failure as REJECTED, never accepted")
    parser.add_argument("--failed-compose-run", type=int)
    parser.add_argument("--failed-compose-attempt", type=int)
    parser.add_argument("--expected-receipt-sha256")
    parser.add_argument("--expected-freeze-token")
    parser.add_argument("--expected-compose-head")
    parser.add_argument("--expected-core-source")
    parser.add_argument("--reason")
    parser.add_argument("--absorb", metavar="X.Y.Z", help="record that the next release absorbs this finished NOT ACCEPTED "
                        "release (fix forward); needs --reason naming the fix PR or an independent review line")
    parser.add_argument("--wait-previous", type=int, help="minutes to wait for the previous release's verdict (default --timeout)")
    parser.add_argument("--pause", action="store_true", help="pause an exact pre-tag core release and remove only its owned hold")
    parser.add_argument("--prepare-only", action="store_true", help="resume the paused exact release hold/readiness only, then exit")
    parser.add_argument("--drop-candidate", action="store_true", help="drop an exact candidate that is provably before publication "
                        "(no tag, no GitHub release, PyPI 404, release PR closed or absent, branch absent, no active run): remove only its "
                        "owned hold and archive the receipt under release-dropped/ in the state directory; needs --reason; cancels no run")
    parser.add_argument("--close-pr", action="store_true", help="with --drop-candidate: close the open release PR and delete "
                        "release/vX.Y.Z first, then prove the state again")
    parser.add_argument("--snapshot-acceptance", action="store_true", help="allow core merges during acceptance of pinned release bytes")
    parser.add_argument("--acceptance-ref", help="explicit corrected acceptance harness ref")
    parser.add_argument("--replace-acceptance-run", type=int, help="preserve and replace this exact completed rejected attempt")
    parser.add_argument("--replace-reason", help="written infra-only reason; required when the rejected run was rerun after "
                        "its bound attempt (that attempt is archived, later attempts are never credited)")
    parser.add_argument("--candidate-gate", action="store_true", help="train path only (sm release train): replace the main-tip-green precondition by "
                        "hzlib.main_release_gate: main's newest completed run may be unfinished or red by KNOWN failures, never UNKNOWN; "
                        "the candidate's first-attempt full CI and acceptance stay the gate")
    parser.add_argument("--waive-job", help='owner waiver: "JOB NAME=bead" for one in-date KNOWN flaky candidate job that '
                        "passed on the attempt-2 rerun of only failed jobs; needs --owner-quote; refused otherwise")
    parser.add_argument("--owner-quote", help="the owner's own words authorizing --waive-job")
    parser.add_argument("--refresh", action="store_true", help="tag-on-green-main only: after a refused candidate and its fix "
                        "forward, cut `chore(release): vX (refresh)` at the same version; its main run is the next candidate")
    parser.add_argument("--open-pr", action="store_true", help="push the reviewed prepared release branch and open its PR; never merges")
    parser.add_argument("--reviewed-head", help="with --open-pr: the full SHA of the reviewed release commit")
    parser.add_argument("--dry-run", action="store_true", help="with --open-pr: check everything, push nothing")
    parser.add_argument("--intro", default="")
    parser.add_argument("--timeout", type=int, default=120, help="minutes per step")
    args = parser.parse_args(argv)
    if args.open_pr:
        allowed = {"--open-pr", "--reviewed-head", "--dry-run"}
        if (any(item.startswith("--") and item.split("=", 1)[0] not in allowed for item in argv)
                or not args.version or not EXACT_VERSION.fullmatch(args.version) or not args.reviewed_head):
            parser.error("--open-pr needs an exact core version X.Y.Z and --reviewed-head SHA, and takes only --dry-run besides")
    elif args.reviewed_head or args.dry_run:
        parser.error("--reviewed-head and --dry-run require --open-pr")
    if args.version == "migrate-legacy" and len(argv) != 1:
        parser.error("migrate-legacy takes no options")
    if args.absorb:
        if any(item.startswith("--") and item.split("=", 1)[0] not in ("--absorb", "--reason") for item in argv) or args.version:
            parser.error("--absorb X.Y.Z takes only --reason")
        if not args.reason:
            parser.error("--absorb needs --reason")
    elif args.repair_receipt and (args.version or args.plugin):
        parser.error("--repair-receipt takes only --apply")
    elif args.apply and not args.repair_receipt:
        parser.error("--apply goes with --repair-receipt")
    elif not args.repair_receipt and bool(args.version) == bool(args.plugin):
        parser.error("give a version or --plugin NAME")
    if args.redispatch and not args.plugin:
        parser.error("--redispatch goes with --plugin")
    if args.plugin and args.snapshot_acceptance:
        parser.error("--snapshot-acceptance is only for core releases")
    if args.pause and (args.plugin or args.snapshot_acceptance or args.intro or args.version == "patch"):
        parser.error("--pause requires an exact core version and no release action options")
    if args.prepare_only and (args.plugin or args.pause or args.snapshot_acceptance or args.intro or args.version == "patch"):
        parser.error("--prepare-only requires an exact core version and no release action options")
    if (args.acceptance_ref or args.replace_acceptance_run) and (args.plugin or args.pause or args.prepare_only):
        parser.error("acceptance identity options require a core release")
    if args.replace_reason is not None and not args.replace_acceptance_run:
        parser.error("--replace-reason requires --replace-acceptance-run")
    rejection_fields = (args.failed_compose_run, args.failed_compose_attempt, args.expected_receipt_sha256,
                        args.expected_freeze_token, args.expected_compose_head, args.expected_core_source, args.reason)
    if args.reject_release:
        allowed = {"--reject-release", "--failed-compose-run", "--failed-compose-attempt", "--expected-receipt-sha256",
                   "--expected-freeze-token", "--expected-compose-head", "--expected-core-source", "--reason"}
        if any(item.startswith("--") and item.split("=", 1)[0] not in allowed for item in argv):
            parser.error("--reject-release accepts only its exact identity options")
        if (any(field is None for field in rejection_fields) or args.plugin or args.pause or args.prepare_only or args.snapshot_acceptance
                or args.acceptance_ref or args.replace_acceptance_run or args.intro or args.version == "patch"
                or "--timeout" in argv or any(item.startswith("--timeout=") for item in argv)):
            parser.error("--reject-release requires exact identities and no other release action options")
    elif (any(field is not None for field in rejection_fields) and not args.candidate_capacity and not args.absorb
          and not args.drop_candidate):
        parser.error("rejection identity options require --reject-release")
    if args.drop_candidate:
        allowed = {"--drop-candidate", "--reason", "--close-pr"}
        if (any(item.startswith("--") and item.split("=", 1)[0] not in allowed for item in argv)
                or not args.version or not EXACT_VERSION.fullmatch(args.version) or not args.reason):
            parser.error("--drop-candidate needs an exact core version X.Y.Z and --reason, and takes only --close-pr besides")
    elif args.close_pr:
        parser.error("--close-pr requires --drop-candidate")
    if args.candidate_capacity:
        allowed = {"--candidate-capacity", "--candidate-run", "--expected-receipt-sha256", "--expected-freeze-token", "--expected-audit-sha256"}
        if (any(item.startswith("--") and item.split("=", 1)[0] not in allowed for item in argv)
                or args.plugin or args.version == "patch" or not args.candidate_run
                or not args.expected_receipt_sha256 or not args.expected_freeze_token
                or (args.candidate_capacity == "request" and not args.expected_audit_sha256)):
            parser.error("candidate capacity requires exact version/run/receipt/hold identities and no release action flags")
    elif args.candidate_run is not None or args.expected_audit_sha256 is not None:
        parser.error("candidate audit identity requires --candidate-capacity")
    if args.candidate_gate and (args.plugin or args.pause or args.prepare_only or args.reject_release
                                or args.candidate_capacity or args.version == "patch"):
        parser.error("--candidate-gate is only for an exact core release run by sm release train")
    if args.refresh and (args.plugin or args.version in (None, "patch") or args.pause or args.prepare_only
                         or args.reject_release or args.candidate_capacity or args.use_candidate_composite is not None):
        parser.error("--refresh needs an exact core version and no other release action")
    if args.use_candidate_composite is not None and (args.use_candidate_composite <= 0 or args.version == "patch"
            or args.plugin or args.pause or args.prepare_only or args.reject_release or args.candidate_capacity):
        parser.error("composite evidence requires an exact normal core release resume")
    global RECEIPT_OVERRIDE
    try:
        # Core and plugin commands share the receipt. They must share the lock before reading or writing any state.
        # A tail command routes BEFORE the global lock: N+1 holds that lock pre-tag (incl. its wait for N's verdict),
        # so N's tail must never wait on it. Race-safe: hand_off_tail takes vN.lock before os.replace.
        named = args.absorb or args.version
        try:
            if args.repair_receipt:
                acquire_global_lock()
                return repair_receipt(args.apply)
            if args.version == "migrate-legacy":
                acquire_global_lock()
                moved = migrate_legacy_receipt()
                hzlib.say(f"MIGRATED {moved}" if moved else "nothing to migrate: no finished global receipt")
                return 0
            if named and EXACT_VERSION.fullmatch(named) and tail_receipt(named).exists():
                route_to_tail(named)  # past its tag: its own receipt and lock; the next candidate may start
            else:
                acquire_global_lock()
                if args.absorb and state().get("version") == args.absorb and state().get("finished"):
                    migrate_legacy_receipt()  # absorbing a finished legacy receipt: hand it off first
                    route_to_tail(args.absorb)
            state()  # reject corruption before any network request or publication action
            if args.drop_candidate:
                return drop_candidate(args.version, args.reason, args.close_pr)
            if state().get("drop") is not None:
                dropping = state().get("version")
                raise Stop(f"a drop of {dropping} is recorded but unfinished; finish it first with "
                           f"`sm release drop {dropping} --reason ...`")
            if args.open_pr:  # a rejected release is never re-opened as a PR: the rejection history refuses first
                rejection_guard(args.version)
                return open_reviewed_release_pr(args.version, args.reviewed_head, args.dry_run)
            if args.absorb:
                return absorb_release(args.absorb, args.reason)
            if args.reject_release:
                return reject_release(args)
            rejection_guard(args.version)
            if args.candidate_capacity:
                try:
                    return release_candidate_capacity.action(ReleaseAdapter(), args)
                except (release_candidate_capacity.InvalidCapacity, KeyError, TypeError, ValueError, OSError) as exc:
                    raise Stop(f"candidate capacity refused: {exc}") from exc
            if args.prepare_only:
                return prepare_release(args.version)
            if args.pause:
                return pause_release(args.version)
            if args.plugin:
                return release_plugin(args.plugin, args.timeout * 60, args.redispatch)
            if args.waive_job or args.owner_quote:
                if not (args.waive_job and args.owner_quote) or args.version in (None, "patch") or args.use_candidate_composite:
                    raise Stop("--waive-job and --owner-quote go together, with an exact version and no candidate composite")
                record_waiver(args.version, args.waive_job, args.owner_quote)
            wait_previous = None if args.wait_previous is None else args.wait_previous * 60
            if args.use_candidate_composite is not None:
                return release_core(args.version, args.intro, args.timeout * 60, args.snapshot_acceptance, args.acceptance_ref, args.replace_acceptance_run, args.use_candidate_composite, candidate_gate=args.candidate_gate, wait_previous=wait_previous, replace_reason=args.replace_reason)
            return release_core(args.version, args.intro, args.timeout * 60, args.snapshot_acceptance, args.acceptance_ref, args.replace_acceptance_run, candidate_gate=args.candidate_gate, wait_previous=wait_previous, refresh=args.refresh, replace_reason=args.replace_reason)
        finally:
            free_global_lock()
            free_tail_lock()
            RECEIPT_OVERRIDE = None
    except Stop as stop:
        if args.reject_release:
            print(f"STOPPED: {stop}. Retry the identical rejection command to reconcile; combined app not accepted.", file=sys.stderr)
        elif args.drop_candidate:
            print(f"STOPPED: {stop}. Resolve exactly that and run the same drop command again; "
                  "no other hold or receipt was touched.", file=sys.stderr)
        else:
            print(f"STOPPED: {stop}. Run the same command again to continue; freeze state is preserved.", file=sys.stderr)
        return 1

