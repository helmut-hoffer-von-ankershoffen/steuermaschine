#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""hz-train [tick] [--dry-run] [--now]   |   hz-train next [--start] [X.Y.Z]   |   hz-train run X.Y.Z   |   hz-train status   |   hz-train clear [X.Y.Z]   |   hz-train plan [PR...]   |   hz-train --self-test

The automated core release train (bead ao9, research/lead-time-2026-11-07.md: waiting for the next release was the
largest stage, 87 min median). launchd runs `hz-train tick` every 10 minutes; a tick starts a train only when every
condition holds, otherwise it says why and exits:

  no train is before its tag and none is blocked; the hz-release receipt has no release that is unfinished BEFORE its
  tag and that this train did not start (a release another agent owns is never touched; one past its tag is in its
  tail and does not block the cut); the newest release tail (~/.cache/hz-release/vN.json) is finished or has its
  acceptance dispatched (the merge window: N's 2 acceptance jobs queue before N+1's candidate); no release/* PR is
  open; no merge freeze (incident or release hold) is on; main has releasable commits since the latest tag (anything
  but chore(release), docs, test, ci, style); HZ_TRAIN_EVERY minutes (default 0: self-pacing) passed since the latest
  tag and since the last train start; the main gate below says ok.

`hz-train next [--start] [X.Y.Z]` (tools/hz_next.py, research/continuous-release-2026-11-08.md): cut(N) ends nothing. Run it in the
same tick as the cut: it prints the N+1 batch (merged since the cut, queued PRs green with CI on current main, blockers, builders to
nudge); --start also sends the GO lines, queues the green PRs and records ~/.cache/hz-train/next-vN+1.json. Never touches v*.json.

Pipelined (research/pipelined-release-train-2026-11-08.md): at most two trains, N in its tail and N+1 as candidate.
The tick is a dispatcher under ~/.cache/hz-train/train.lock: it starts `hz-train run X.Y.Z` (detached, its own
session, so a 60-minute tail never stops the next tick under launchd) for every active train whose
~/.cache/hz-train/vX.Y.Z.lock is free, oldest first, then makes the cut decision. N+1's hz-release opens its release
PR and waits at `previous release accepted` (N ACCEPTED or `hz-release --absorb N`) before merging, so PyPI order
stays monotonic; that wait is a `wait:` line here, never an attempt. One Mini owner: the mini phase holds
~/.cache/hz-train/mini.lock through proof and finish; a healthy, accepted N or newer satisfies N.
The shared incident hold pauses the dispatcher and detached runners; an owned release hold still blocks a new cut.

Main gate (coordinator decision 2026-11-07: the CANDIDATE is the gate). The release candidate (release/vX.Y.Z: full
suite on both OSes, acceptance on both OSes, snapshot acceptance, first attempt, hz-release's rules unchanged) is what
gates a release, so the train does NOT wait for main's newest run to finish and does NOT refuse a red main whose
failures are all KNOWN. hzlib.main_release_gate reads main's newest COMPLETED run that is evidence (runs where no job
failed, only cancelled or never-started jobs, are skipped): first-attempt green, restored-by-rerun (attempt-1
failures all KNOWN) or red-by-KNOWN (every hard-failed job KNOWN in date in research/known-failures.toml, by job name,
or by its failing tests when it is the only failed job) -> ok; an UNKNOWN failing job (a regression candidate), a
green rerun of an UNKNOWN failure, or unreadable/no evidence -> refused (exit 3, noted once per tip and run). The
decision is written to the train state (`main_gate`) and hz-note: `started with main tip X unfinished/red-by-KNOWN:
candidate is the gate (...)`. hz-release gets `--candidate-gate` (train path only; a manual hz-release keeps its
main-tip-green step).

A train runs `hz-release <next patch> --intro <from the commit subjects> --snapshot-acceptance --candidate-gate` end
to end with HZ_GATE_PRIORITY=2 and HZ_GATE_HOLD_EXEMPT=1: the release-owned merge freeze, candidate CI, tag, PyPI,
compose, acceptance on both OSes. hz-release lifts the merge freeze right after the tag (the freeze covers cut -> tag
only, research/pipelined-release-train-2026-11-07.md); the tail runs with main open. Once the candidate's release/vX.Y.Z run is active, the train frees runner
slots once: `hz-ci-defer --cancel` (PR CI only, nothing kept; never main or release/*) and cancels main's CI run for
the tip ONLY when the candidate's base is that tip and the run has not started (status queued, no job running or
done, no runner); a main run in progress is never cancelled. Both go to hz-note and the state (`freed`). During the
candidate window (until hz-release prints its `tag` step, or the tag exists) it keeps ~/.cache/hz-gate.hold fresh so builders' heavy gates wait; it writes
and removes only a hold file it created (content `hz-train <token>`), and never touches a hold file somebody else
made. The release runs with `--live`: after ACCEPTED it updates the Mini and proves it (`sm mini update --minimum
X.Y.Z`); the mini phase runs the same command every tick until LIVE-OK (it changes the Mini only while the proof
fails), then notifies the owner once and resumes deferred CI. An accepted release that is not live yet waits in the
mini phase without consuming attempts.

Auto-cut is off by default (setting release_train_auto_cut): a tick then drives the active trains only, and
`sm daemon train --now` is the one command that cuts (every other condition still holds).

State: one file per version, ~/.cache/hz-train/vX.Y.Z.json (atomic; phase release, mini, finish, blocked, then done or
cleared), plus ~/.cache/hz-train/train.json for the cadence (last_start, last_version, red_noted); one log per release,
~/.cache/hz-train/vX.Y.Z.log. A legacy ~/.cache/hz-train/state.json is migrated under the train lock (so never while
the train that wrote it runs): its train is copied unchanged to vX.Y.Z.json, the cadence to train.json, and the old
file is kept as state.json.migrated. Every phase is persisted, so a tick after a restart resumes where the last one
stopped (hz-release itself is resumable); active trains are the versions not done or cleared. A train stops as
`blocked` after 3 failed attempts or a NOT ACCEPTED verdict; `hz-train clear [X.Y.Z]` (default: the newest active
train) resets it after the cause is fixed. Dropping a candidate: `hz-release X --pause`, close its release PR, clear.
Every start, stop and finish goes to hz-note. The owner is notified only when a release is published, accepted and
verified live on the Mini, or for a blocker only the owner can clear (credentials, trusted publisher, permissions).
Notification: the notifier provider (moved; the setting release_train_notify is the alarm's fallback command).

Tag-on-green-main (flag HZ_RELEASE_TAG_ON_MAIN, default OFF; tools/release_flow.py, core ADR 0144): hz-release then
cuts a lean bump PR (bump/vX) that the queue merges at once, verifies the first-attempt full main run of that commit
and tags it; main never freezes. With the flag on, the cut no longer waits for N's acceptance dispatch (there is no
merge window to protect): N tagged and releasable commits suffice. bump/* PRs count as open release PRs. A refused
candidate blocks the train after its attempts: fix forward on main, `hz-release X --refresh`, then `hz-train clear X`.

Exit codes: 0 done or nothing to do, 1 a step failed (next tick resumes), 2 usage, 3 blocked or refused."""

import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import edge  # moved: siblings are imported relatively, no sys.path entry
from ..queue_lib import hzlib, release_flow  # noqa: E402
from steuermaschine.config import child_environment
from ... import state as statedir  # the plugin's state paths
from ..queue_lib import core_facts

HOME = Path.home()
CORE = "helmut-hoffer-von-ankershoffen/heinzel"
STATE_DIR = Path(edge.setting("release_train_state_dir") or statedir.path("train"))
GATE_HOLD = statedir.path("gate.hold")
RECEIPT = statedir.path("release.json")
TAIL_DIR = statedir.path("release")  # hz-release hands the receipt off here at the tag (vX.Y.Z.json)
CHECKOUT = Path(edge.setting("release_train_checkout") or HOME / "Code/heinzel-release-train")
EVERY = int(edge.setting("release_train_every_minutes") or 0)  # 0: self-pacing (candidate ~40 min); opt-in floor in minutes
AUTO_CUT = bool(edge.setting("release_train_auto_cut"))  # off: only `--now` cuts; ticks drive the active trains
VERSION = re.compile(r"\d+\.\d+\.\d+")
PREVIOUS_NOT_ACCEPTED = re.compile(r"previous release (\d+\.\d+\.\d+) NOT ACCEPTED")
MAX_ATTEMPTS = 3
TAG_STEP = re.compile(r"^ok\s+tag\b")
ENDED = ("done", "cleared")
INDEX_KEYS = ("last_start", "last_version", "last_gate", "last_finished", "last_minutes", "red_noted")
NOT_RELEASABLE = re.compile(r"^(chore\(release\)|docs|test|ci|style)(\([^)]*\))?!?:")
USER_FACING = re.compile(r"^(feat|fix|perf|revert)(\([^)]*\))?!?:\s*")
OWNER_ONLY = re.compile(r"trusted publisher|Non-user identities|HTTP 40[13]|permission denied|not authori[sz]ed|"
                        r"authentication|gh auth login|token (expired|revoked)|2FA|billing|spending limit", re.I)


# ---------------------------------------------------------------- state

def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except ValueError as exc:
        raise SystemExit(f"sm release train: {path} is malformed ({exc}); inspect it, then `sm release train clear`")


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".state-", delete=False) as stream:
        json.dump(data, stream, indent=1, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, path)


def version_path(version: str) -> Path:
    return STATE_DIR / f"v{version}.json"


def _vkey(path: Path) -> tuple:
    try:
        return tuple(int(p) for p in path.stem.lstrip("v").split("."))
    except ValueError:
        return (1 << 30,)


def trains() -> list[dict]:
    """Per-version train states, oldest version first (vX.Y.Z.json; logs and other files are ignored)."""
    out = []
    for path in sorted(STATE_DIR.glob("v*.json"), key=_vkey):
        if re.fullmatch(r"v\d+\.\d+\.\d+\.json", path.name) and (data := _read(path)) is not None:
            out.append(data)
    return out


def vkey(version: str) -> tuple:
    try:
        return tuple(int(p) for p in version.split("."))
    except (AttributeError, ValueError):
        return (1 << 30,)


def active_trains() -> list[dict]:
    """Trains not done or cleared, oldest first; an unmigrated legacy state.json counts as one (read-only)."""
    legacy = legacy_state()
    if legacy is not None:
        return [legacy] if legacy.get("version") and legacy.get("phase", "idle") not in ENDED + ("idle",) else []
    return [t for t in trains() if t.get("phase") not in ENDED]


def index() -> dict:
    return _read(STATE_DIR / "train.json") or {}


def legacy_state() -> dict | None:
    return _read(STATE_DIR / "state.json")


def load_state() -> dict:
    """The active train (lowest version not done or cleared), else the cadence index with phase idle. Read-only: an
    unmigrated legacy state.json is shown as it is."""
    legacy = legacy_state()
    if legacy is not None:
        return legacy
    active = next((t for t in trains() if t.get("phase") not in ENDED), None)
    return active or ((_read(STATE_DIR / "train.json") or {}) | {"phase": "idle"})


def save_state(data: dict) -> None:
    """A train's own state goes to its version file; an idle record (cadence, refusal notes) to train.json."""
    if data.get("version") and data.get("phase", "idle") != "idle":
        _write(version_path(data["version"]), data)
        return
    index = _read(STATE_DIR / "train.json") or {}
    _write(STATE_DIR / "train.json", index | {k: data[k] for k in INDEX_KEYS if k in data})


def migrate_legacy() -> str:
    """Call only under the train lock: the train that wrote state.json has exited, so its state is final here.
    Copies the in-flight train unchanged to vX.Y.Z.json (an existing version file wins), the cadence to train.json,
    and keeps the old file as state.json.migrated. Nothing about the train itself changes."""
    legacy_path = STATE_DIR / "state.json"
    legacy = legacy_state()
    if legacy is None:
        return ""
    moved = ""
    if legacy.get("version") and legacy.get("phase", "idle") != "idle":
        if not version_path(legacy["version"]).exists():
            _write(version_path(legacy["version"]), legacy)
        moved = f"train v{legacy['version']} ({legacy['phase']}) -> {version_path(legacy['version']).name}; "
    index = _read(STATE_DIR / "train.json") or {}
    _write(STATE_DIR / "train.json", {k: legacy[k] for k in INDEX_KEYS if k in legacy} | index)
    os.replace(legacy_path, legacy_path.with_name("state.json.migrated"))
    return f"migrated state.json: {moved}cadence -> train.json"


# ---------------------------------------------------------------- gate hold (only ever our own file)

class GateHold:
    # Owner rule 2026-11-07 (playbook 6.6b): builders never idle while a candidate runs, so the local gate hold is OFF
    # unless the setting release_train_gate_hold is on (hosted candidate CI does not compete with local heavy gates).
    ENABLED = bool(edge.setting("release_train_gate_hold"))

    def __init__(self, path: Path, token: str):
        self.path, self.mark = path, f"sm release train {token}\n"

    def ours(self) -> bool:
        try:
            return self.path.read_text() == self.mark
        except OSError:
            return False

    def acquire(self) -> str:
        if not self.ENABLED:
            return "gate hold disabled (builders keep working during the candidate)"
        if self.ours():
            os.utime(self.path)
            return "own hold refreshed"
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return "a hold file somebody else made is present; left as it is"
        with os.fdopen(fd, "w") as stream:
            stream.write(self.mark)
        return "hold taken"

    def refresh(self) -> None:
        if self.ENABLED and self.ours():
            os.utime(self.path)

    def release(self) -> bool:
        if self.ours():
            self.path.unlink(missing_ok=True)
            return True
        return False


# ---------------------------------------------------------------- locks

class file_lock:
    """Non-blocking flock as a context manager: `with file_lock(p) as got:`; got is False when another holds it."""

    def __init__(self, path: Path):
        self.path, self.stream = path, None

    def __enter__(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a")
        try:
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.stream.close()
            self.stream = None
            return False
        return True

    def __exit__(self, *exc) -> None:
        if self.stream is not None:
            fcntl.flock(self.stream, fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None


def accepted(w, version: str) -> bool:
    """The release has its finished ACCEPTED verdict (its tail receipt, else the legacy global one): what is left is the Mini."""
    try:
        data = dict(w.tail_receipts()).get(version) or w.receipt()
    except (OSError, ValueError):
        return False  # unreadable is not accepted: the failure path below decides
    return data.get("version") == version and data.get("finished") is True and data.get("accepted") is True


def lock_busy(w, name: str) -> bool:
    with w.lock(name) as got:
        return not got


def live_ok(line: str, version: str) -> bool:
    """A successful verifier must report one exact, sufficiently recent version token."""
    match = re.fullmatch(r"LIVE-OK ([0-9]+\.[0-9]+\.[0-9]+)", line)
    return bool(match) and vkey(match[1]) >= vkey(version)


def wait_for_incident(w) -> bool:
    if not w.incident_active():
        return False
    w.say("wait: incident merge freeze is on (event=train_incident_hold result=wait)")
    return True


# ---------------------------------------------------------------- the real world

class Real:
    def __init__(self, dry: bool = False):
        self.dry = dry

    def say(self, text: str) -> None:
        print(text, flush=True)

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def _gh(self, *args: str):
        limit = hzlib.gh_timeout(args)
        try:
            done = edge.run(["gh", *args], capture_output=True, text=True, check=False, timeout=limit)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"gh {' '.join(args[:3])}: timed out after {limit}s; refused") from None
        if done.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args[:3])}: {done.stderr.strip()[-200:]}")
        return json.loads(done.stdout) if done.stdout.strip() else None

    def receipt(self) -> dict:
        try:
            return json.loads(RECEIPT.read_text())
        except FileNotFoundError:
            return {}

    def tail_receipts(self) -> list[tuple[str, dict]]:
        """hz-release tail receipts (version, data), oldest first. Unreadable ones raise (the cut fails closed)."""
        out = []
        for path in TAIL_DIR.glob("v*.json") if TAIL_DIR.is_dir() else []:
            if VERSION.fullmatch(path.stem[1:]):
                out.append((path.stem[1:], json.loads(path.read_text())))
        return sorted(out, key=lambda item: vkey(item[0]))

    def tail_receipt(self, version: str) -> dict:
        try:
            return json.loads((TAIL_DIR / f"v{version}.json").read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def lock(self, name: str):
        return file_lock(STATE_DIR / name)

    def drive(self, version: str) -> int:
        """Start `hz-train run X` in its own session (launchd never runs a second tick while one runs, and kills
        only the tick's process group); its output goes to the tick's log. The runner takes vX.lock itself."""
        subprocess.Popen(edge.train_run(version), stdin=subprocess.DEVNULL,  # moved: `sm daemon train --run X`, no script
                         start_new_session=True, env=child_environment())
        self.say(f"started: sm daemon train {version} (detached)")
        return 0

    def release_pr_open(self) -> int:
        """Open release/* or bump/* PRs: a candidate between cut and merge. Runs after the merge are N's tail."""
        prs = self._gh("pr", "list", "-R", CORE, "--state", "open", "--json", "headRefName") or []
        return sum(1 for p in prs if p["headRefName"].startswith(("release/", "bump/")))

    def latest_tag(self) -> tuple[str, float]:
        # Held pre-releases count (core ADR 0114 holds every vX.Y.Z until acceptance); edge-v/app-v and drafts never.
        found = [r for r in self._gh("api", f"repos/{CORE}/releases?per_page=30") or []
                 if not r.get("draft") and re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", str(r.get("tag_name")))]
        rel = (max(found, key=lambda r: vkey(r["tag_name"][1:])) if found
               else self._gh("api", f"repos/{CORE}/releases/latest"))
        stamp = datetime.fromisoformat(rel["published_at"].replace("Z", "+00:00")).timestamp()
        return rel["tag_name"], stamp

    def unreleased(self, tag: str) -> tuple[str, list[str]]:
        cmp = self._gh("api", f"repos/{CORE}/compare/{tag}...main")
        subjects = [s for c in cmp.get("commits", []) for s in commit_subjects(c["commit"]["message"])]
        tip = cmp["commits"][-1]["sha"] if cmp.get("commits") else ""
        return tip, subjects

    def _hzlib(self):
        from . import edge  # moved: siblings are imported relatively, no sys.path entry
        from ..queue_lib import hzlib
        return hzlib

    def main_gate(self, tip: str) -> dict:
        """hzlib.main_release_gate: ok unless main's newest completed evidence run has an UNKNOWN failure."""
        return self._hzlib().main_release_gate(tip)

    def freeze_active(self) -> bool:
        return self._hzlib().merge_frozen()

    def incident_active(self) -> bool:
        return self._hzlib().incident_frozen()

    def tag_on_main(self) -> bool:
        return release_flow.flag_on()

    def candidate_active(self, version: str) -> bool:
        rows = self._gh("run", "list", "-R", CORE, "--branch", f"release/v{version}", "-L", "20", "--json", "status") or []
        return any(r.get("status") != "completed" for r in rows)

    def defer_cancel(self) -> str:
        done = edge.run([*edge.sm(), "ci", "defer", "--cancel", "--force-release"], capture_output=True, text=True, check=False)  # moved: no script
        lines = done.stdout.splitlines()
        return f"sm ci defer --cancel --force-release exit {done.returncode}: {sum(l.startswith('cancelled ') for l in lines)} PR run(s) cancelled and recorded"

    def cancel_unstarted_main_run(self, version: str) -> str:
        """Cancel main's CI run for the tip only when the candidate covers that tip and no job of the run has started."""
        tip = self._gh("api", f"repos/{CORE}/commits/main")["sha"]
        base = self._gh("api", f"repos/{CORE}/compare/main...release/v{version}")["merge_base_commit"]["sha"]
        if base != tip:
            return f"kept: the candidate's base {base[:9]} is not main's tip {tip[:9]}"
        rows = self._gh("run", "list", "-R", CORE, "--workflow", core_facts.ci_file(), "--branch", "main", "-L", "20", "--json", "databaseId,headSha,status") or []
        out = []
        for row in (r for r in rows if r["headSha"] == tip and r["status"] != "completed"):
            rid = row["databaseId"]
            run = self._gh("api", f"repos/{CORE}/actions/runs/{rid}")
            jobs = (self._gh("api", f"repos/{CORE}/actions/runs/{rid}/jobs?per_page=100") or {}).get("jobs", [])
            if not unstarted(run, jobs):
                out.append(f"{rid} kept ({run.get('status')}: started; a main run in progress is never cancelled)")
                continue
            try:
                done = edge.run(["gh", "run", "cancel", str(rid), "-R", CORE], capture_output=True, text=True, check=False,
                                timeout=hzlib.GH_WRITE_TIMEOUT)
            except subprocess.TimeoutExpired:
                out.append(f"{rid} cancel timed out after {hzlib.GH_WRITE_TIMEOUT}s; its state is unknown")
                continue
            out.append(f"{rid} cancelled (queued, no job started)" if done.returncode == 0 else f"{rid} cancel failed: {done.stderr.strip()[-120:]}")
        return "; ".join(out) or f"no unfinished main run for the tip {tip[:9]}"

    def has_tag(self, tag: str) -> bool:
        try:
            done = edge.run(["gh", "api", f"repos/{CORE}/git/ref/tags/{tag}"], capture_output=True, check=False, timeout=hzlib.GH_READ_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"gh api tag {tag}: timed out after {hzlib.GH_READ_TIMEOUT}s; whether it exists is unknown") from None
        return done.returncode == 0

    def prepare_checkout(self, previous: str, version: str) -> None:
        """AGENTS.md 'Preparing the Core release checkout': a clean dedicated checkout on main (never /private/tmp)."""
        if not (CHECKOUT / ".git").exists():
            edge.run(["git", "clone", "-q", f"https://github.com/{CORE}.git", str(CHECKOUT)], check=True)
        for cmd in (["fetch", "-q", "origin"], ["switch", "-q", "main"], ["merge", "-q", "--ff-only", "origin/main"]):
            edge.run(["git", "-C", str(CHECKOUT), *cmd], check=True)
        dirty = edge.run(["git", "-C", str(CHECKOUT), "status", "--porcelain"], capture_output=True, text=True).stdout
        if dirty.strip():
            raise RuntimeError(f"release checkout {CHECKOUT} is not clean")
        shown = edge.run([*edge.gate(), "uv", "run", "--frozen",  # moved: `sm gate run --priority 2 --hold-exempt --`
                                "bump-my-version", "show", "current_version"], cwd=CHECKOUT, capture_output=True,
                               text=True, env=self.release_env())
        current = (shown.stdout.strip().splitlines() or ["?"])[-1]
        if current not in (previous, version):
            raise RuntimeError(f"release checkout reports version {current}, expected {previous}")

    def release_env(self) -> dict:
        return child_environment(HZ_RELEASE_DIR=str(CHECKOUT), SM_HEINZEL__RELEASE_DIR=str(CHECKOUT))  # moved: the run lock reads no environment

    def run_release(self, version: str, intro: str, hold: GateHold, on_candidate=None) -> tuple[int, str]:
        log = STATE_DIR / f"v{version}.log"
        cmd = [*edge.sm(), "release", "resume", version, "--intro", intro, "--snapshot-acceptance", "--candidate-gate", "--live"]  # moved: no script
        stop = threading.Event()

        def keep_hold() -> None:  # candidate window: hold until the tag exists, refresh well inside hz-gate's 900 s
            fired = on_candidate is None
            while not stop.wait(60):
                if not fired:
                    try:
                        if self.candidate_active(version):
                            fired = True
                            on_candidate()
                    except Exception as exc:  # noqa: BLE001 - freeing slots is best effort; the release goes on
                        self.say(f"freeing runner slots failed: {exc}")
                try:
                    tagged = self.has_tag(f"v{version}")
                except Exception as exc:  # noqa: BLE001 - unknown tag state: keep holding, ask again next loop
                    self.say(f"tag check failed, gate hold kept: {exc}")
                    tagged = False
                if tagged:
                    if hold.release():
                        self.say("gate hold released: tag exists")
                    return
                hold.refresh()

        self.say(hold.acquire())
        watcher = threading.Thread(target=keep_hold, daemon=True)
        watcher.start()
        tail: list[str] = []
        try:
            with log.open("a") as out:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=self.release_env())
                for line in proc.stdout:
                    out.write(line)
                    out.flush()
                    tail = (tail + [line.rstrip()])[-40:]
                    self.say("  " + line.rstrip())
                    if not stop.is_set() and tag_step_done(line):
                        stop.set()  # the tag exists: builders' gates run again at once, not up to 60 s later
                        if hold.release():
                            self.say("gate hold released: tag step done")
                code = proc.wait()
        finally:
            stop.set()
            hold.release()  # a stopped release never keeps builders waiting; the resume takes the hold again
        return code, "\n".join(tail)

    def acceptance_run(self, version: str) -> str:
        """After the handoff the verdict lives in the tail receipt; a legacy receipt is the global one."""
        data = self.tail_receipt(version) or self.receipt()
        return str(data.get("acceptance_run", "?")) if data.get("version") == version else "?"

    def verify_mini(self, version: str) -> str:
        verifier = [*edge.sm(), "mini", "update"]  # the proof; it updates and restarts the Mini only while the proof fails
        try:
            done = edge.run([*verifier, "--minimum", version], capture_output=True, text=True,
                                  timeout=1500, check=False)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return "NOT-LIVE: verifier unavailable"
        line = (done.stdout.strip().splitlines() or [""])[-1]
        if done.returncode:
            if line.startswith("NOT-LIVE: "):
                return line
            return f"NOT-LIVE: verifier exited {done.returncode}"
        return line if live_ok(line, version) else "NOT-LIVE: verifier did not report exact live proof"

    def defer_resume(self) -> str | None:
        found = edge.run([*edge.sm(), "ci", "deferred"], capture_output=True, text=True, check=False)  # moved: `sm ci deferred`, no script
        listed = found.stdout if found.returncode == 0 else None  # moved: an unreadable list is not an empty one
        if wait_for_incident(self):
            return None
        if listed is None:  # moved: say so instead of noting a false "nothing deferred"
            return f"the list of deferred runs is unreadable (exit {found.returncode}); resume by hand: sm ci rerun"
        if not listed.strip():
            return "nothing deferred"
        if edge.setting("release_train_resume_all"):  # opt-out of the selective resume
            done = edge.run([*edge.sm(), "ci", "rerun", "--all"], capture_output=True, text=True, check=False)  # moved: no script
        else:  # only open queued/combine PRs; closed PRs dropped, the rest stays for sm ci rerun
            done = edge.run([*edge.sm(), "release", "train", "resume-ci"], capture_output=True, text=True, check=False)  # moved: no script
        return (done.stdout.strip().splitlines() or ["?"])[-1]

    def note(self, text: str) -> None:
        edge.note(f"release train: {text}")  # moved: the same line in the same document, written here

    def notify(self, text: str) -> None:
        sent = edge.notify_owner(text)  # moved: the owner is reached through the notifier provider, no script is searched or started
        self.note(f"owner {'notified' if sent else 'NOT notified (no working notifier)'}: {text}")


# ---------------------------------------------------------------- the train

def next_patch(tag: str) -> str:
    major, minor, patch = (int(p) for p in tag.lstrip("v").split("."))
    return f"{major}.{minor}.{patch + 1}"


def tag_step_done(line: str) -> bool:
    """hz-release prints `ok   tag` (or `ok   tag (already)`) once the tag exists."""
    return bool(TAG_STEP.match(line.strip()))


def receipt_blocks(w, receipt: dict) -> bool:
    """A receipt blocks the cut only while its release is unfinished BEFORE its tag (cut -> tag is the only window
    that needs main == the candidate). Unreadable tag state fails closed."""
    if not receipt.get("version") or receipt.get("finished"):
        return False
    try:
        return not w.has_tag(f"v{receipt['version']}")
    except Exception:  # noqa: BLE001 - fail closed
        return True


def unstarted(run: dict, jobs: list[dict]) -> bool:
    """A main run nothing of which has started: run queued/waiting, no job running, done (other than skipped) or on a runner."""
    if run.get("status") not in ("queued", "waiting", "pending", "requested"):
        return False
    return not any(j.get("status") == "in_progress" or j.get("runner_name")
                   or (j.get("status") == "completed" and j.get("conclusion") != "skipped") for j in jobs)


def gate_label(gate: dict, tip: str) -> str:
    if gate["kind"] == "green" and gate["sha"] == tip:
        return f"started with main tip {tip[:9]} first-attempt green ({gate['why']})"
    state = "unfinished" if not gate.get("tip_finished") else "finished"
    kind = {"known": "red-by-KNOWN", "restored": "restored-by-rerun", "green": "last evidence green"}.get(gate["kind"], gate["kind"])
    return f"started with main tip {tip[:9]} {state}/{kind}: candidate is the gate ({gate['why']})"


def free_runners(w, st: dict) -> dict:
    """Once the candidate runs: PR CI deferred (keep none), main's redundant tip run cancelled only if unstarted."""
    if wait_for_incident(w):
        return {}
    freed = {"deferred": w.defer_cancel(), "main": w.cancel_unstarted_main_run(st["version"]), "at": w.now()}
    text = f"train v{st['version']}: candidate running, runner slots freed: {freed['deferred']}; main tip run: {freed['main']}"
    w.say(text)
    w.note(text)
    return freed


COMBINES = re.compile(r"^Combines #\d+ (?P<subject>[a-z]+(?:\([^)]+\))?!?: \S.*)$")


def commit_subjects(message: str) -> list[str]:
    """A commit's subject, or for a combine squash commit its members' `Combines #N type(scope): title` subjects
    (wrapped lines joined), so the intro names what was released, never "combine of N changes"."""
    members, current = [], None
    for line in message.splitlines()[1:]:
        line = line.strip()
        if m := COMBINES.match(line):
            current = len(members)
            members.append(m["subject"])
        elif current is not None and line and not line.startswith(("Combines", "<!--")):
            members[current] += " " + line
        else:
            current = None
    return members or message.splitlines()[:1]


INTRO_ITEMS = 4


def intro_from(subjects: list[str]) -> str:
    """The first INTRO_ITEMS user-facing changes in the order given (the combine's member order, first = most
    important), plus how many more there are. Never sorted."""
    ranked = [s for s in subjects if USER_FACING.match(s)]
    items = [re.sub(r"\s*\(#\d+\)$", "", USER_FACING.sub("", s)) for s in ranked[:INTRO_ITEMS]]
    items = [i[:1].upper() + i[1:] for i in items]
    if not items:
        return "Reliability and maintenance fixes."
    more = len(ranked) - len(items)
    text = "; ".join(items) + (f"; and {more} more" if more else "")
    return (text[:597] + "...") if len(text) > 600 else text + "."


def tick(w, dry: bool = False, now_flag: bool = False) -> int:
    """Dispatcher: drive every active train whose version lock is free (oldest first), then decide the cut."""
    if wait_for_incident(w):
        return 0
    codes = []
    active = active_trains()
    for st in active:
        version = st["version"]
        if st.get("phase") == "blocked":
            w.say(f"blocked: v{version}: {st.get('reason')} (fix the cause, then `sm release train clear {version}`)")
            codes.append(3)
            continue
        if dry:
            w.say(f"dry-run: would resume train v{version} at phase {st['phase']} (attempt {st.get('attempts', 0) + 1})")
            continue
        if lock_busy(w, f"v{version}.lock"):
            w.say(f"skip: train v{version} is running")
            continue
        if wait_for_incident(w):
            return 0
        codes.append(w.drive(version))
    if wait_for_incident(w):
        return 0
    return max(codes + [cut(w, dry, now_flag)])


def cut(w, dry: bool, now_flag: bool) -> int:
    """Start N+1 when N is past its tag and N's acceptance is dispatched (serial rules 1 and 2 of the design)."""
    active = active_trains()
    if any(t.get("phase") == "blocked" for t in active):
        return 3
    for t in active:
        try:
            before_tag = t.get("phase") == "release" and not w.has_tag(f"v{t['version']}")
        except Exception:  # noqa: BLE001 - fail closed
            before_tag = True
        if before_tag:
            if not dry:
                w.say(f"skip: train v{t['version']} is the candidate (before its tag); one candidate at a time")
            return 0
    st = index()
    receipt = w.receipt()  # a release another agent owns is never touched
    if receipt_blocks(w, receipt):
        w.say(f"skip: release {receipt['version']} is unfinished before its tag and not this train's; it is never touched")
        return 0
    try:
        tails = w.tail_receipts()
    except (OSError, ValueError) as exc:
        w.say(f"skip: a release tail receipt is unreadable ({exc}); inspect the tail receipts directory")
        return 0
    if receipt.get("version") and receipt["version"] not in {v for v, _ in tails}:  # legacy: before the handoff
        tails = sorted(tails + [(receipt["version"], receipt)], key=lambda item: vkey(item[0]))
    if tails and not w.tag_on_main():  # tag-on-green-main: no merge window, N tagged is enough
        newest, data = tails[-1]
        if not data.get("finished") and not data.get("acceptance_requested"):
            w.say(f"wait: merge window, v{newest} acceptance not dispatched yet")
            return 0
    if w.release_pr_open():
        w.say("skip: a release/* PR is open")
        return 0
    if w.freeze_active():
        w.say("skip: a merge freeze (incident or release hold) is on; a train never starts under it")
        return 0
    tag, tag_time = w.latest_tag()
    version = next_patch(tag)
    # A tagged N whose GitHub release is still a draft leaves releases/latest at N-1: never cut a version again that a
    # train ran (done) or runs, or that a tail owns. A cleared version may be cut again (fix-forward re-cut).
    in_flight = ([t["version"] for t in trains() if t.get("version") and t.get("phase") != "cleared"]
                 + [v for v, _ in tails if not _.get("finished")])
    if any(vkey(v) >= vkey(version) for v in in_flight):
        w.say(f"wait: latest release is {tag}; v{max(in_flight, key=vkey)} was cut already or is in flight")
        return 0
    tip, subjects = w.unreleased(tag)
    releasable = [s for s in subjects if not NOT_RELEASABLE.match(s)]
    if not releasable:
        w.say(f"skip: nothing releasable on main since {tag} ({len(subjects)} commits)")
        return 0
    if not AUTO_CUT and not now_flag:
        w.say(f"skip: auto-cut is off ({len(releasable)} releasable commits since {tag}); cut with `sm daemon train --now`")
        return 0
    since = max(tag_time, st.get("last_start", 0))
    wait_min = EVERY - (w.now() - since) / 60
    if wait_min > 0 and not now_flag:
        w.say(f"skip: next train in {wait_min:.0f} min ({len(releasable)} releasable commits since {tag})")
        return 0
    gate = w.main_gate(tip)
    if gate["verdict"] != "ok":
        key = f"{tip}:{gate.get('run')}"
        if st.get("red_noted") != key and not dry:
            w.note(f"refused to start a release: main tip {tip[:9]}: {gate['why']}; fix main first (6.2a)")
            save_state({"phase": "idle", "red_noted": key})
        w.say(f"refused: main tip {tip[:9]}: {gate['why']}")
        return 3
    intro = intro_from(releasable)
    decision = gate_label(gate, tip)
    if dry:
        w.say(f"dry-run: would start train v{version} from {tip[:9]} ({len(releasable)} commits)\n  main gate: {decision}\n"
              f"  intro: {intro}\n"
              f"  then: sm release resume {version} --intro ... --snapshot-acceptance --candidate-gate (priority 2, gate hold until tag); "
              "when the candidate runs: sm ci defer --cancel (keep none) and cancel main's tip run only if unstarted; "
              "Mini proof + notify owner, sm release train resume-ci (open queued/combine PRs only; setting release_train_resume_all: sm ci rerun --all)")
        return 0
    if wait_for_incident(w):
        return 0
    new = {"phase": "release", "version": version, "previous": tag.lstrip("v"), "intro": intro, "tip": tip,
           "start": w.now(), "last_start": w.now(), "attempts": 0, "token": os.urandom(8).hex(),
           "main_gate": decision}
    save_state(new)
    save_state({"phase": "idle", "last_start": new["last_start"]})
    prs = " ".join(re.findall(r"#\d+", " ".join(releasable)))
    w.note(f"train v{version} {decision}: {len(releasable)} commits {prs}")
    return w.drive(version)


def run_version(w, version: str) -> int:
    """`hz-train run X`: one train under its own vX.lock (never the train lock), from its persisted phase."""
    with w.lock(f"v{version}.lock") as got:
        if not got:
            w.say(f"skip: train v{version} is running")
            return 0
        if wait_for_incident(w):
            return 0
        legacy = legacy_state()
        st = legacy if legacy is not None and legacy.get("version") == version else _read(version_path(version))
        if not st or st.get("phase") in ENDED + ("idle", "blocked"):
            w.say(f"nothing to run: train v{version} is {(st or {}).get('phase', 'absent')}")
            return 3 if st and st.get("phase") == "blocked" else 0
        return resume(w, st, False)


def resume(w, st: dict, dry: bool) -> int:
    version = st["version"]
    if dry:
        w.say(f"dry-run: would resume train v{version} at phase {st['phase']} (attempt {st.get('attempts', 0) + 1})")
        return 0
    if st["phase"] == "release":
        receipt = w.receipt()
        if (receipt.get("version") not in (None, version) and not receipt.get("finished")
                and receipt_blocks(w, receipt) and not w.has_tag(f"v{version}")):
            return block(w, st, f"the release receipt now belongs to {receipt['version']}", owner=False)
        try:
            if not w.has_tag(f"v{version}"):
                w.prepare_checkout(st["previous"], version)
            if wait_for_incident(w):
                return 0
            freed: dict = {}
            code, tail = w.run_release(version, st["intro"], GateHold(GATE_HOLD, st["token"]),
                                       on_candidate=lambda: freed.update(free_runners(w, st)))
            if wait_for_incident(w):
                return 0
            if freed:
                st["freed"] = freed
                save_state(st)
        except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
            if wait_for_incident(w):
                return 0
            code, tail = 1, f"STOPPED: {exc}"
        if code != 0 and not accepted(w, version):
            if "another release command is running" in tail or re.search(r"tail of v\S+ is running", tail):
                w.say("skip: another release command is running")
                return 0
            if (previous := PREVIOUS_NOT_ACCEPTED.search(tail)):
                # N+1 waits for N's fix-forward or absorb; never an attempt, never a block of N+1 (noted once).
                if st.get("previous_noted") != previous[1]:
                    w.note(f"train v{version} waits: previous release {previous[1]} NOT ACCEPTED; fix forward, "
                           f"then sm release resume {previous[1]} --absorb --reason ... (or sm release train clear {version})")
                    save_state(st | {"previous_noted": previous[1]})
                w.say(f"wait: previous release {previous[1]} NOT ACCEPTED; v{version} merges only after its absorb")
                return 0
            if re.search(r"release \S+ is unfinished; finish it before starting", tail):
                w.say("wait: the previous release's tail is unfinished (one release receipt); the next tick retries")
                return 0
            if "NOT ACCEPTED" in tail:
                return block(w, st, f"published but NOT ACCEPTED (run {w.acceptance_run(version)}); see the train's v{version}.log", owner=False)
            reason = next((l for l in reversed(tail.splitlines()) if "STOPPED" in l), tail.splitlines()[-1] if tail else "?")
            if OWNER_ONLY.search(tail):
                return block(w, st, reason, owner=True)
            st["attempts"] = st.get("attempts", 0) + 1
            if st["attempts"] >= MAX_ATTEMPTS:
                return block(w, st, f"{MAX_ATTEMPTS} attempts: {reason}", owner=False)
            save_state(st | {"last_error": reason})
            w.say(f"release step failed (attempt {st['attempts']}): {reason}; the next tick resumes")
            return 1
        st |= {"phase": "mini", "attempts": 0, "acceptance": w.acceptance_run(version), "published": w.now()}
        save_state(st)
        w.note(f"v{version} published and ACCEPTED (acceptance run {st['acceptance']}); merge freeze was lifted at the tag")
    if st["phase"] in ("mini", "finish"):
        return finish_live(w, st)
    return 0


def finish_live(w, st: dict) -> int:
    version = st["version"]
    with w.lock("mini.lock") as got:
        if not got:
            w.say(f"wait: another train is verifying the Mini; v{version} retries next tick")
            return 0
        if wait_for_incident(w):
            return 0

        # A legacy finish also needs fresh proof. Keep the same Mini lock through notification and CI resumption.
        w.say(f"event=train_mini_verify stage=start version={version}")
        line = w.verify_mini(version)
        if not live_ok(line, version):
            w.say(f"wait: v{version} needs an accepted live build ({line}); "
                  "event=train_mini_verify result=wait")
            return 0
        shown = line.split()[1]
        w.say(f"event=train_mini_verify result=live version={version} live_version={shown}")
        if wait_for_incident(w):
            return 0

        if st["phase"] == "mini":
            minutes = (w.now() - st["start"]) / 60
            superseded = f"; supersedes train v{version}" if shown != version else ""
            w.notify(f"Heinzelmännchen {shown} is accepted and live on the Mini ({line}){superseded}; "
                     f"train took {minutes:.0f} min.")
            st |= {"phase": "finish", "live": w.now(), "live_version": shown}
            save_state(st)

        # PR CI deferred for a running candidate (N+1) stays deferred until that candidate's own finish.
        candidate = next((t["version"] for t in active_trains() if t.get("version") != version
                          and t.get("phase") == "release" and not w.has_tag(f"v{t['version']}")), None)
        if wait_for_incident(w):
            return 0
        resumed = f"left for train v{candidate} (its candidate runs)" if candidate else w.defer_resume()
        if resumed is None or wait_for_incident(w):
            return 0
        w.note(f"train v{version} done: v{shown} verified live on the Mini; deferred CI: {resumed}")
        summary = {"last_start": max(st["last_start"], index().get("last_start", 0)), "last_version": version,
                   "last_gate": st.get("main_gate"), "last_finished": w.now(),
                   "last_minutes": round((w.now() - st["start"]) / 60)}
        save_state(st | {"phase": "done", "finished": w.now(), "live_version": shown})
        save_state({"phase": "idle"} | summary)
    return 0


def block(w, st: dict, reason: str, owner: bool) -> int:
    save_state(st | {"phase": "blocked", "reason": reason, "blocked_at": w.now()})
    if owner:
        w.notify(f"Release train v{st['version']} needs you: {reason}")
    else:
        w.note(f"TODO: train v{st['version']} blocked: {reason} (agents fix; then sm release train clear {st['version']})")  # moved: clear takes the version
    w.say(f"blocked: {reason}")
    return 3


# ---------------------------------------------------------------- self-test

class Fake:
    def __init__(self, **kw):
        self.t = 10_000_000.0
        self.held = set()  # lock names another process holds (vX.lock, mini.lock)
        self.tails = []
        self.live_line = None
        self.rcpt = {"version": "0.9.27", "finished": True, "acceptance_run": 42}
        self.active = 0
        self.tags = set()
        self.tag, self.tag_time = "v0.9.27", self.t - 2 * 3600
        self.subjects = ["fix(web): thin scrollbars (#566)", "docs: x", "feat(web): clear button (#561)"]
        self.gate = gate("green", "abc123def456", "run 1 at abc123def first-attempt green")
        self.frozen = False
        self.incident = False
        self.flag = False
        self.candidate = True
        self.release_result = (0, "ACCEPTED 0.9.28 (run 42)")
        self.live = True
        self.calls, self.notes, self.notified, self.out = [], [], [], []
        self.__dict__.update(kw)

    def say(self, text): self.out.append(text)
    def now(self): return self.t
    def sleep(self, s): self.t += s
    def receipt(self): return self.rcpt
    def tail_receipts(self): return list(self.tails)

    def lock(self, name):
        fake = self

        class _Lock:
            def __enter__(self):
                return name not in fake.held
        _Lock.__exit__ = lambda self, *exc: None
        return _Lock()

    def drive(self, version):  # inline: the detached runner of the real world
        self.calls.append(("drive", version))
        return run_version(self, version)
    def release_pr_open(self): return self.active
    def latest_tag(self): return self.tag, self.tag_time
    def unreleased(self, tag): return "abc123def456", self.subjects
    def main_gate(self, tip): return self.gate
    def freeze_active(self): return self.frozen or self.incident
    def incident_active(self): return self.incident
    def tag_on_main(self): return self.flag
    def defer_cancel(self):
        self.calls.append(("defer_cancel",))
        return "2 PR run(s) cancelled and recorded"
    def cancel_unstarted_main_run(self, version):
        self.calls.append(("cancel_main", version))
        return "77 cancelled (queued, no job started)"
    def has_tag(self, tag): return tag in self.tags
    def prepare_checkout(self, previous, version): self.calls.append(("checkout", previous, version))
    def acceptance_run(self, version=None): return "42"
    def verify_mini(self, version): return (self.live_line or f"LIVE-OK {version}") if self.live else "NOT-LIVE"
    def defer_resume(self):
        self.calls.append(("defer_resume",))
        return "nothing deferred"
    def note(self, text): self.notes.append(text)
    def notify(self, text): self.notified.append(text)

    def run_release(self, version, intro, hold, on_candidate=None):
        self.calls.append(("release", version, intro))
        if on_candidate and self.candidate:
            on_candidate()
        return self.release_result


def gate(kind, sha, why, verdict="ok", finished=True):
    return {"verdict": verdict, "kind": kind, "sha": sha, "why": why, "run": 7, "tip_finished": finished}


def self_test() -> int:
    global STATE_DIR, AUTO_CUT
    AUTO_CUT = True  # the cut rules; auto-cut off is tested by the plugin's tests
    failures = []

    def check(name, cond):
        print(f"{'ok  ' if cond else 'FAIL'} {name}")
        if not cond:
            failures.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        def fresh(**kw):
            global STATE_DIR
            STATE_DIR = Path(tmp) / f"s{len(os.listdir(tmp))}"
            return Fake(**kw)

        w = fresh(rcpt={"version": "0.9.28", "freeze_token": "x"})
        check("an unfinished release owned by someone else is never touched", tick(w) == 0 and not w.calls and "never touched" in w.out[-1])
        w = fresh(tails=[("0.9.27", {"version": "0.9.27", "freeze_lifted_at": "x"})])
        check("a tail without acceptance dispatched waits (merge window)", tick(w) == 0 and not w.calls and "merge window" in w.out[-1])
        w = fresh(tails=[("0.9.27", {"version": "0.9.27", "freeze_lifted_at": "x"})])
        w.flag = True
        check("tag-on-green-main: a tagged N without acceptance dispatched does not hold the cut", tick(w) == 0
              and any(c[0] == "release" for c in w.calls) and not any("merge window" in line for line in w.out))
        w = fresh(active=1)
        w.flag = True
        check("tag-on-green-main: an open bump/release PR still skips the cut", tick(w) == 0 and not w.calls)
        w = fresh(active=1)
        check("an active release/* run skips", tick(w) == 0 and not w.calls)
        w = fresh(subjects=["chore(release): v0.9.27 (#570)", "docs: a", "test(heads): b (#564)"])
        check("nothing releasable skips", tick(w) == 0 and "nothing releasable" in w.out[-1])
        global EVERY
        EVERY = 60
        w = fresh(tag_time=10_000_000.0 - 20 * 60)
        check("HZ_TRAIN_EVERY=60: interval not elapsed skips", tick(w) == 0 and "next train in 40 min" in w.out[-1])
        check("--now overrides the interval", tick(w, now_flag=True) == 0 and w.calls[2][0] == "release")
        EVERY = 0
        unknown = gate("unknown", "0ld", "run 7 at 0ld failure: UNKNOWN failing job(s) Lint: a regression candidate", verdict="refuse")
        w = fresh(gate=unknown)
        check("an UNKNOWN failing job on main's newest completed run refuses", tick(w) == 3 and len(w.notes) == 1
              and "UNKNOWN failing job" in w.notes[0] and not w.calls and "refused" in w.out[-1])
        tick(w)
        check("a refusal is noted once per tip and run", len(w.notes) == 1)
        w = fresh(gate=unknown, frozen=True)
        check("a freeze skips before the main gate", tick(w) == 0 and "freeze" in w.out[-1] and not w.notes)
        w = fresh(gate=gate("unknown", "", "main CI evidence is unreadable", verdict="refuse"))
        check("unreadable main evidence refuses (fail closed)", tick(w) == 3 and not w.calls)
        w = fresh(gate=gate("green", "abc123def456", "x green only on rerun of an UNKNOWN failure", verdict="refuse"))
        check("a green rerun of an UNKNOWN failure refuses", tick(w) == 3 and not w.calls)
        w = fresh(active=1, gate=gate("known", "0ld", "red-by-KNOWN: web-1=KNOWN(wzd)", finished=False))
        check("an active release/* run still skips with a KNOWN-red main", tick(w) == 0 and not w.calls)
        for kind, why, label in (("known", "run 7 at 0ld red-by-KNOWN: End-to-end web-1=KNOWN(wzd)", "unfinished/red-by-KNOWN"),
                                 ("restored", "run 7 at 0ld restored-by-rerun (attempt 2 green)", "unfinished/restored-by-rerun"),
                                 ("green", "run 7 at 0ld first-attempt green", "unfinished/last evidence green")):
            w = fresh(gate=gate(kind, "0ld", why, finished=False))
            rc = tick(w)
            st = load_state()
            check(f"main tip unfinished, {kind}: the train starts, candidate is the gate", rc == 0 and w.calls[2][0] == "release"
                  and label in st.get("last_gate", "") + w.notes[0] and "candidate is the gate" in w.notes[0])
        w = fresh(gate=gate("known", "0ld", "red-by-KNOWN: web-1=KNOWN(wzd)", finished=False))
        tick(w, dry=True)
        check("dry-run names the gate decision and the slot freeing", not w.calls and "red-by-KNOWN" in w.out[-1]
              and "sm ci defer --cancel" in w.out[-1] and "--candidate-gate" in w.out[-1])
        w = fresh()
        check("dry-run plans and changes nothing", tick(w, dry=True) == 0 and not w.calls and not STATE_DIR.exists()
              and "would start train v0.9.28" in w.out[-1])
        w = fresh()
        rc = tick(w)
        check("happy path exits 0", rc == 0)
        check("release runs the next patch with an intro from user-facing commits",
              ("release", "0.9.28", "Thin scrollbars; Clear button.") in w.calls)
        check("Mini proof precedes one notification", len(w.notified) == 1 and "0.9.28" in w.notified[0])
        check("deferred CI resumed and train idle", ("defer_resume",) in w.calls and load_state()["phase"] == "idle"
              and load_state()["last_version"] == "0.9.28")
        check("candidate running: PR CI deferred (keep none) and the unstarted main tip run cancelled, both noted",
              ("defer_cancel",) in w.calls and ("cancel_main", "0.9.28") in w.calls
              and any("runner slots freed" in n and "77 cancelled" in n for n in w.notes))
        check("a first-attempt green tip is recorded as such", "first-attempt green" in w.notes[0])
        w.tag = "v0.9.28"
        check("a finished train: the next cut follows at once (EVERY=0)", tick(w) == 0 and ("release", "0.9.29", "Thin scrollbars; Clear button.") in w.calls)
        w = fresh(live=True)
        tick(w)
        check("an already-live Mini is reported", len(w.notified) == 1)
        w = fresh(release_result=(1, "STOPPED: main tip green: timed out. Run the same command again"))
        check("a failed step exits 1 and keeps the phase", tick(w) == 1 and load_state()["phase"] == "release" and not w.notified)
        w.rcpt = {"version": "0.9.28", "freeze_token": "t"}  # the train's own unfinished receipt
        check("restart resumes the same train", tick(w) == 1 and [c[0] for c in w.calls].count("release") == 2
              and "before its tag" in w.out[-1])
        check("third failure blocks without bothering the owner", tick(w) == 3 and load_state()["phase"] == "blocked"
              and not w.notified and "TODO" in w.notes[-1])
        check("blocked stays blocked", tick(w) == 3 and [c[0] for c in w.calls].count("release") == 3)
        w = fresh(release_result=(1, "HTTP400 Non-user identities cannot create new projects\nSTOPPED: pypi"))
        check("owner-only blocker notifies the owner", tick(w) == 3 and len(w.notified) == 1)
        w = fresh(release_result=(1, "NOT ACCEPTED 0.9.28 (run 42)"))
        check("NOT ACCEPTED blocks for agents, no Mini update", tick(w) == 3 and not w.notified)
        w = fresh(live=False)
        check("Mini proof miss waits without attempts", tick(w) == 0 and load_state()["phase"] == "mini"
              and load_state()["attempts"] == 0 and not w.notified)
        w.live = True
        check("Mini phase resumes without re-releasing", tick(w) == 0 and [c[0] for c in w.calls].count("release") == 1
              and len(w.notified) == 1)
        STATE_DIR = Path(tmp) / "resume"
        save_state({"phase": "finish", "version": "0.9.28", "start": 1, "last_start": 1})
        w = Fake()
        check("finish phase after a restart resumes deferred CI only", tick(w) == 0 and w.calls == [("drive", "0.9.28"), ("defer_resume",)])

        hold_path = Path(tmp) / "gate.hold"
        hold_path.write_text("someone else\n")
        GateHold.ENABLED = True  # the self-test covers the opt-in behaviour
        mine = GateHold(hold_path, "abc")
        check("a foreign gate hold is left in place", "somebody else" in mine.acquire() and not mine.release() and hold_path.exists())
        hold_path.unlink()
        check("own gate hold is taken", mine.acquire() == "hold taken" and mine.ours())
        check("own gate hold is re-adopted after restart", GateHold(hold_path, "abc").acquire() == "own hold refreshed")
        check("own gate hold is removed", mine.release() and not hold_path.exists())
        check("an unstarted queued run may be cancelled", unstarted({"status": "queued"}, [{"status": "queued"}, {"status": "completed", "conclusion": "skipped"}]))
        check("a run with a job in progress is never cancelled", not unstarted({"status": "queued"}, [{"status": "in_progress"}]))
        check("a run with a job on a runner is never cancelled", not unstarted({"status": "queued"}, [{"status": "queued", "runner_name": "GitHub Actions 3"}]))
        check("a run in progress is never cancelled", not unstarted({"status": "in_progress"}, []))
        check("a run with a finished job is never cancelled", not unstarted({"status": "queued"}, [{"status": "completed", "conclusion": "success"}]))
        combine = ("feat(web): combine of 3 web changes (#679)\n\nCombine of 3.\n\nCombines #659 feat(web): links standard, no\n"
                   "text links in chrome\nCombines #673 fix(web): sort the sidebar\nCombines #659 / #673\n")
        check("a combine's members are its subjects", commit_subjects(combine) == [
            "feat(web): links standard, no text links in chrome", "fix(web): sort the sidebar"])
        check("intro names at most four changes and counts the rest",
              intro_from([f"fix(web): f{n}" for n in range(3)] + [f"feat(web): n{n}" for n in range(3)])
              == "F0; F1; F2; N0; and 2 more.")
        check("intro falls back for maintenance-only trains", intro_from(["refactor: x", "chore(deps): y"]) == "Reliability and maintenance fixes.")
    print("SELF-TEST", "PASSED" if not failures else f"FAILED: {failures}")
    return 0 if not failures else 1


# ---------------------------------------------------------------- main

def main(argv: list[str]) -> int:
    flags = {a for a in argv if a.startswith("-")}
    words = [a for a in argv if not a.startswith("-")]
    unknown = flags - {"--dry-run", "--now", "--self-test", "-h", "--help"}
    command = words[0] if words else "tick"
    if command == "next" and not unknown - {"--start"} and len(words) <= 2 and (len(words) == 1 or VERSION.fullmatch(words[1])):
        from . import batch_next as hz_next  # N+1's batch at the cut; read-only apart from GO/queue commands and its own next-vX.json record
        if flags & {"-h", "--help"}:
            print(hz_next.__doc__)
            return 0
        return hz_next.run(STATE_DIR, trains(), start="--start" in flags, version=words[1] if len(words) == 2 else None)
    one_version = command in ("run", "clear") and len(words) == 2 and VERSION.fullmatch(words[1])
    if (unknown or command not in ("tick", "status", "clear", "plan", "run") or (command == "run" and not one_version)
            or (len(words) > 1 and command not in ("plan",) and not one_version)):
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        return 2
    if flags & {"-h", "--help"}:
        print(__doc__)
        return 0
    if "--self-test" in flags:
        return self_test()
    if command == "plan":  # release candidate list as one table; read-only (bead: velocity)
        from . import plan as hz_plan
        return hz_plan.plan(words[1:])
    if command == "status":
        print(status(Real()))
        return 0
    if command == "run":  # one train under its own version lock, started detached by the tick
        print(f"sm daemon train {words[1]} {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}", flush=True)
        return run_version(Real(), words[1])
    return locked(command, flags, words[1] if len(words) > 1 else None)


def status(w) -> str:
    """One line per active train and per hz-release tail, then the oldest active train (or the index) as JSON."""
    lines = []
    for t in active_trains():
        busy = "running" if lock_busy(w, f"v{t['version']}.lock") else "idle"
        lines.append(f"train v{t['version']}: {t.get('phase')} ({busy}, attempts {t.get('attempts', 0)})"
                     + (f": {t['reason']}" if t.get("reason") else ""))
    try:
        tails = w.tail_receipts()
    except (OSError, ValueError) as exc:
        tails, _ = [], lines.append(f"tail receipts unreadable: {exc}")
    for version, data in tails:
        verdict = ("ACCEPTED" if data.get("accepted") else f"NOT ACCEPTED{' (absorbed by ' + data['absorbed_by'] + ')' if data.get('absorbed_by') else ''}") if data.get("finished") else (
            "acceptance dispatched" if data.get("acceptance_requested") else "tail running")
        lines.append(f"{version} tail: {verdict}, acceptance run {data.get('acceptance_run_id') or data.get('acceptance_run') or '-'}")
    receipt = w.receipt()
    if receipt.get("version") and not receipt.get("finished"):
        lines.append(f"{receipt['version']} candidate: release receipt before its handoff")
    return "\n".join(lines + [json.dumps(load_state(), indent=1, sort_keys=True)])


def clear(version: str | None = None, w=None) -> str:
    """Mark one train cleared: the named version, else the newest active one. Refused while its runner holds vX.lock:
    the runner's next save_state would silently overwrite `cleared`."""
    active = active_trains()
    st = next((t for t in active if t.get("version") == version), None) if version else (active[-1] if active else load_state())
    if st is None:
        return f"refused: no active train v{version}"
    if st.get("version") and lock_busy(w or Real(), f"v{st['version']}.lock"):
        return f"refused: train v{st['version']} is running: sm release resume {st['version']} --pause first, then clear"
    if st.get("version") and st.get("phase") not in ENDED + ("idle",):
        save_state(st | {"phase": "cleared", "cleared_at": time.time()})
    return f"train state cleared (was {st.get('phase')} v{st.get('version')}: {st.get('reason', '')})"


def locked(command: str, flags: set, version: str | None = None) -> int:
    """Under the train lock: the legacy migration, clear, the tick's decisions. Trains run under their own vX.lock."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "train.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("skip: another sm release train is running")
            return 0
        w = Real()
        if command == "tick" and wait_for_incident(w):
            return 0
        if "--dry-run" not in flags and (moved := migrate_legacy()):
            print(moved, flush=True)
            Real().note(moved)
        if command == "clear":
            text = clear(version)
            print(text)
            if text.startswith("refused:"):
                return 1
            Real().note(text)
            return 0
        print(f"sm release train {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}", flush=True)
        return tick(w, dry="--dry-run" in flags, now_flag="--now" in flags)

