"""The real state `go` decides on, read-only: git and gh reads, the process list, the adopted state files.
Every probe degrades: one that fails or runs out of time becomes {"unknown": why}.

A `World` carries the providers (the runtime of a native call does not cross into the probe threads).
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from steuermaschine.github.client import Gh
from steuermaschine.github.forge import GitHub
from steuermaschine.hashing import canonical_sha256

from .. import updater_hold
from ..queue_lib import core_facts
from . import legacy
from .decide import unknown

SLOT_VARS = ("FOCUSED_CI_PLANS", *(f"FOCUSED_CI_PLANS_V2_{i}" for i in range(4)))
VALUE_OPTIONS = ("--out", "--core-repo", "--core-repos", "--review-root", "--nonconsumers", "--prepared", "--max-wait", "--approved-sha256",
                 "--pr", "--expected-remote-head", "--packet-dir", "--sel", "--junit", "--title", "--reason", "--approved", "--format")
SM_HEAD = re.compile(r"(?:^|[/\s])(?:sm|steuermaschine|sm-dev)\s+(?:--format[= ]\S+\s+)?head\s+(prepare|admit)\s+(.*)$")
OLD_ADMIT = re.compile(r"\S+ \S*/[a-z]+-focused-admit (prepare|apply) (/\S+)")
OLD_RELEASE = re.compile(r"/[a-z]+-release \d|/[a-z]+-train run ")


@dataclass
class World:
    commands: object
    clock: object
    state_dir: Path  # the adopted state files
    core: str  # the bare Core repository on this machine
    repo: str  # OWNER/NAME
    settings: dict[str, Any] = field(default_factory=dict)
    focus_list: dict[str, Any] | None = None  # the focus list of `sm focus` (None: never written, the old focus file is adopted)
    acks: dict[str, Any] = field(default_factory=dict)  # alarm keys acknowledged with `sm alarm ack`
    rehearsal: dict[str, Any] | None = None  # the rehearsal document of the state store (None: never written, the old state file is adopted)
    runs: list[Any] = field(default_factory=list)  # the run states of the engine pipelines (sm head admit, sm release cut)
    _ps: list[Any] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def gh(self) -> Gh:
        return Gh(self.commands, repo=self.repo, timeout=12.0, clock=self.clock)

    def now(self) -> float:
        return self.clock.now()

    def sh(self, argv: list[str], timeout: float = 12, cwd: str | None = None) -> str:
        """Run a read-only command; RuntimeError with a short reason on any failure."""
        name = os.path.basename(argv[0])
        res = self.commands.run(argv, cwd=cwd, timeout=timeout)
        if res.rc:
            tail = (res.err or res.out).strip().splitlines()[-1:] or [""]
            raise RuntimeError(f"{name} timed out after {timeout:.0f}s" if res.rc == 124 else f"{name} exited {res.rc}: {tail[0][:120]}")
        return res.out

    def gh_json(self, args: list[str], timeout: float = 12):
        data, why = self.gh.try_json(args, timeout=timeout, retry=False)
        if why:
            raise RuntimeError(f"gh: {why}"[:160])
        return data

    def processes(self) -> list[tuple[int, str, str]]:
        """(pid, elapsed, command line) of every process; read once per call."""
        with self._lock:
            if self._ps is None:
                rows = []
                for line in self.sh(["ps", "-ww", "-axo", "pid=,etime=,command="], timeout=5).splitlines():
                    parts = line.split(None, 2)
                    if len(parts) == 3 and parts[0].isdigit():
                        rows.append((int(parts[0]), parts[1], parts[2]))
                self._ps = rows
            return self._ps


def setting(w: World, name: str, default):
    value = (w.settings or {}).get(name)
    return default if value in (None, "") else value


# ---------------------------------------------------------------- git and GitHub
def core_of(w: World) -> str:
    """The Core checkout git reads run in; a probe fails with the setting's name when it is not set (never the cwd)."""
    if not w.core:
        raise RuntimeError("core_repo_dir is not set")
    return w.core


def probe_main(w: World) -> dict[str, Any]:
    out = w.sh(["git", "-C", core_of(w), "ls-remote", "origin", "refs/heads/main"], timeout=10)
    sha = out.split()[0] if out.split() else ""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("git ls-remote returned no main")
    return {"sha": sha}


def probe_remote_heads(w: World) -> dict[str, Any]:
    out = w.sh(["git", "-C", core_of(w), "ls-remote", "--heads", "origin"], timeout=10)
    return {ref.removeprefix("refs/heads/"): sha for sha, ref in (line.split("\t") for line in out.splitlines() if "\t" in line)}


def probe_runs(w: World) -> list[Any]:
    return w.gh_json(["run", "list", "-R", w.repo, "-L", "60", "--json", "databaseId,headBranch,headSha,status,conclusion,workflowName,event,createdAt"])


def probe_active_runs(w: World) -> dict[str, Any]:
    """Separate bounded active inventory; incomplete reads never establish idle."""
    result = GitHub(w.commands, repo=w.repo, timeout=12, clock=w.clock).active(limit=300)
    return {"complete": result.state == "ok" and not result.truncated,
            "reason": result.state if result.state != "ok" else "truncated" if result.truncated else "",
            "runs": [{"databaseId": run.id, "headBranch": run.branch, "status": run.status,
                      "workflowName": run.workflow} for run in result.runs]}


def probe_prs(w: World) -> list[Any]:
    return w.gh_json(["pr", "list", "-R", w.repo, "--state", "open", "-L", "60", "--json",
                      "number,title,headRefName,headRefOid,isDraft,mergeStateStatus,statusCheckRollup"])


def probe_releases(w: World) -> dict[str, Any]:
    rows = w.gh_json(["release", "list", "-R", w.repo, "-L", "5", "--json", "tagName,isLatest,publishedAt"]) or []
    latest = next((r for r in rows if r.get("isLatest")), rows[0] if rows else {})
    return {"latest": str(latest.get("tagName", "")).removeprefix("v"), "published": latest.get("publishedAt", "")}


def probe_slots(w: World) -> dict[str, Any]:
    """Bytes used per focused-plan variable (values are never printed). One read."""
    rows = (w.gh_json(["api", f"repos/{w.repo}/actions/variables?per_page=100"]) or {})["variables"]
    return {r["name"]: len(str(r.get("value", "")).encode()) for r in rows if r["name"] in SLOT_VARS}


def probe_since_tag(w: World, main_sha: str, latest: str) -> dict[str, Any]:
    if not (main_sha and latest):
        raise RuntimeError("needs main and the latest release")
    return {"count": int(w.sh(["git", "-C", core_of(w), "rev-list", "--count", f"v{latest}..{main_sha}"], timeout=8).strip())}


# ---------------------------------------------------------------- the merge queue, the train, releases
def queue_waits(items: list[dict], now: float) -> list[str]:
    """`item 790: waiting 277 min: BLOCKED with green checks ...` for every queued PR, from what the last tick recorded."""
    out = []
    for item in items:
        if item.get("status") != "queued":
            continue
        if item.get("waiting_since"):
            out.append(f"item {item['key']}: waiting {int((now - item['waiting_since']) / 60)} min: {item.get('wait_reason') or 'reason not recorded'}")
        elif item.get("ready_since"):
            age = int((now - item["ready_since"]) / 60)
            if age > 15:
                out.append(f"item {item['key']}: ready and unmerged for {age} min; a gate above holds it, or the runner is not ticking (sm mini loops)")
    return out


def queue_verdict(data: dict, frozen: bool, stalls: list[str]) -> str:
    queued = [i["key"] for i in data["items"] if i.get("status") == "queued"]
    if not queued:
        return "nothing queued"
    if frozen:
        return "merge freeze holds all core PRs"
    if data.get("blocked") == "merge freeze is on":
        return "the freeze was lifted after the last tick; the next tick (within 5 minutes) gates again"
    if data.get("blocked"):
        return f"last tick: {data['blocked']}"
    if stalls:
        return "no queue-level block recorded, but " + "; ".join(line.removeprefix("item ") for line in stalls)
    return "no block recorded; the next tick should merge"


def main_runs(w: World, limit: int = 5) -> list[dict]:
    rows, _ = w.gh.try_json(["run", "list", "-R", w.repo, "--workflow", core_facts.ci_file(), "--branch", "main", "-L", str(limit),
                             "--json", "status,conclusion,databaseId,headSha,attempt,createdAt"], timeout=12, retry=False)
    return rows if isinstance(rows, list) else []


def probe_queue(w: World) -> dict[str, Any]:
    data = legacy.queue_state(w.state_dir)
    frozen = legacy.merge_frozen(w.state_dir)
    queued = [str(i.get("key")) for i in data["items"] if i.get("status") == "queued"]
    added = {str(i.get("key")): i.get("added") for i in data["items"] if i.get("status") == "queued" and i.get("added")}
    return {"queued": queued, "freeze": frozen, "verdict": queue_verdict(data, frozen, queue_waits(data["items"], w.now())),
            "main_runs": [{"databaseId": r.get("databaseId"), "headSha": r.get("headSha"), "status": r.get("status"), "conclusion": r.get("conclusion") or ""}
                          for r in main_runs(w)], "added": added}


def probe_train(w: World) -> dict[str, Any]:
    """One line per active train and per release tail, from their state files; `running` from the process list."""
    running = {m[1] for _, _, cmd in _processes_or_none(w) if (m := re.search(r"-train run (\d+\.\d+\.\d+)", cmd))}
    trains = [f"train v{t['version']}: {t.get('phase')} ({'running' if t.get('version') in running else 'idle'}, attempts {t.get('attempts', 0)})"
              + (f": {t['reason']}" if t.get("reason") else "") for t in legacy.active_trains(w.state_dir)]
    tails = []
    for version, data in legacy.release_tails(w.state_dir):
        absorbed = f" (absorbed by {data['absorbed_by']})" if data.get("absorbed_by") else ""
        verdict = ("ACCEPTED" if data.get("accepted") else f"NOT ACCEPTED{absorbed}") if data.get("finished") else (
            "acceptance dispatched" if data.get("acceptance_requested") else "tail running")
        tails.append(f"{version} tail: {verdict}, acceptance run {data.get('acceptance_run_id') or data.get('acceptance_run') or '-'}")
    return {"trains": trains, "tails": tails[-2:]}


def _processes_or_none(w: World) -> list[Any]:
    try:
        return w.processes()
    except RuntimeError:
        return []


def probe_release(w: World) -> dict[str, Any]:
    accepted = legacy.accepted_versions(w.state_dir)
    return {"active": legacy.release_receipt(w.state_dir), "latest_accepted": max(accepted, key=legacy.version_key) if accepted else None,
            "dropped": legacy.dropped_candidates(w.state_dir)}


def probe_holds(w: World) -> dict[str, Any]:
    files = legacy.hold_files(w.state_dir)
    prs = {}
    actions = setting(w, "go_pr_actions_file", None)
    try:
        rows = legacy.pr_actions(Path(str(actions)).expanduser() if actions else None)
        prs = {k: str(v.get("action", "")) for k, v in rows.items() if isinstance(v, dict) and v.get("hold") is True}
    except (OSError, ValueError) as exc:
        files.append(f"PR action table unreadable: {exc}")
    return {"files": files, "prs": prs}


def probe_alarms(w: World) -> dict[str, Any]:
    return legacy.alarm_state(w.state_dir)


REHEARSAL_KIND, REHEARSAL_KEY = "heinzel.rehearse.state", "rehearsal"  # where the native rehearsal keeps `current`, `last`, `last_green`


def rehearsal_state(w: World) -> dict | None:
    """The rehearsal state: the store document the native rehearsal writes; the state file of the scripted era only
    while no store document exists. One reader for `go` and for the alarm, so both always judge the same verdict."""
    if w.rehearsal is not None:
        if not isinstance(w.rehearsal, dict):
            raise ValueError("the rehearsal state in the store is not an object")
        return w.rehearsal
    return legacy.rehearsal_state(w.state_dir)


def probe_rehearse(w: World) -> dict[str, Any]:
    return {}  # `sm release rehearse` is gone: a rehearsal state left on disk by an older sm is not read


def probe_receipts(w: World) -> list[Any]:
    return legacy.admission_receipt_heads(w.state_dir)


PIPELINE_KINDS = ("runs.head.admit", "runs.release.cut")
OPEN = ("finished", "superseded")
ADMIT_STEPS = ("check", "pr", "queue")  # the steps of `sm head admit` today; a run that names any other is state of the deleted admission


def from_deleted_admission(state: dict) -> bool:
    """A head.admit run saved by the old admission (packet, prepare, plan steps): not work, so never a NEXT."""
    named = [*(state.get("steps") or {}), *([state["running"]] if state.get("running") else [])]
    return state.get("pipeline") == "head.admit" and any(name not in ADMIT_STEPS for name in named)


def probe_pipelines(w: World) -> list[Any]:
    """The unfinished pipeline runs, each with what `go` needs to judge it: is the process of a running step alive,
    and where is the worktree's head now (a head run whose worktree moved on is superseded by the next run)."""
    out = []
    for state in w.runs:
        if not isinstance(state, dict) or state.get("status") in OPEN or from_deleted_admission(state):
            continue
        row = {k: state.get(k) for k in ("pipeline", "key", "label", "arg", "status", "phase", "next", "waiting_on", "trap", "running", "pid", "review", "fingerprint")}
        row["external_done"] = any(v.get("status") == "done" and k in ("apply", "pr", "queue", "admit apply", "resume") for k, v in (state.get("steps") or {}).items())
        if state.get("status") == "running" and isinstance(state.get("pid"), int):
            row["alive"] = _alive(state["pid"])
        if state.get("pipeline") == "head.admit" and state.get("arg"):
            try:
                row["worktree_head"] = w.sh(["git", "-C", str(state["arg"]), "rev-parse", "HEAD"], timeout=5).strip()
            except RuntimeError:
                row["worktree_head"] = ""
        out.append(row)
    return out


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def probe_tools(w: World) -> dict[str, Any]:
    """What the command tree offers: dropping a candidate (rehearsal is gone)."""
    return {"rehearse": False, "drop_candidate": True}


def probe_mini(w: World, live: bool = False) -> dict[str, Any]:
    out = {"cli": None, "live": None, "live_ok": None}
    binary = str(setting(w, "mini_cli", "hz"))
    found = re.findall(r"^\S+ (\d+\.\d+\.\d+)$", w.sh([binary, "--version"], timeout=10), re.MULTILINE)
    out["cli"] = found[0] if found else None
    out["held"] = updater_hold.held_version(w.settings)
    return out


# ---------------------------------------------------------------- admission: processes and packets
def packet_argument(words: list[str]) -> str | None:
    """The first positional argument of a command line tail (option values skipped)."""
    skip = False
    for word in words:
        if skip:
            skip = False
        elif word in VALUE_OPTIONS:
            skip = True
        elif not word.startswith("-"):
            return word
    return None


def option_value(words: list[str], name: str) -> str | None:
    for i, word in enumerate(words):
        if word == name and i + 1 < len(words):
            return words[i + 1]
        if word.startswith(name + "="):
            return word.split("=", 1)[1]
    return None


def parse_process(pid: int, etime: str, cmd: str) -> dict | None:
    """One admission or release process, or None. Known shapes: the admission tool of the scripted era
    (prepare or apply with an absolute packet), `sm head prepare ...`, `sm head admit WORKTREE`, a release runner."""
    if m := OLD_ADMIT.match(cmd):
        return {"pid": pid, "etime": etime, "kind": m[1], "packet": m[2]}
    if m := SM_HEAD.search(cmd):
        words = m[2].split()
        first = packet_argument(words)
        if not first:
            return None
        row = {"pid": pid, "etime": etime, "kind": m[1], "waits": "--wait" in words}
        if m[1] == "prepare":
            core = option_value(words, "--core-repo")
            row.update(packet=os.path.expanduser(first), core_repo=os.path.expanduser(core) if core else None)
        else:
            row.update(worktree=os.path.expanduser(first), packet=None)
        return row
    if OLD_RELEASE.search(cmd):
        return {"pid": pid, "etime": etime, "kind": "release", "packet": cmd.split("/")[-1][:60]}
    return None


def probe_procs(w: World) -> list[Any]:
    mine = os.getpid()
    found = []
    for pid, etime, cmd in w.processes():
        if pid == mine or re.search(r"(?:^|[/\s])(?:sm|steuermaschine|sm-dev)\s+(?:--format[= ]\S+\s+)?go\b", cmd):
            continue
        if row := parse_process(pid, etime, cmd):
            found.append(row)
    return found


def packet_globs(w: World) -> list[str]:
    return [os.path.expanduser(str(g)) for g in setting(w, "go_packet_globs", DEFAULT_PACKET_GLOBS)]


DEFAULT_PACKET_GLOBS: list[str] = []  # a machine's packet directories are its setting go_packet_globs


def probe_packets(w: World, extra: list[str] | None = None) -> list[Any]:
    """No admission, no packets: `sm head admit` builds none any more, so there is none to list."""
    return []


def read_packet(w: World, wave: Path) -> dict[str, Any]:
    import json

    d = wave.parent
    data = json.loads(wave.read_text())
    p = {"dir": str(d), "packet": str(d / "packet.json"), "branch": data["branch"], "head": data["head"], "main": data["main"],
         "core_repo": data.get("core_repo", ""), "worktree": data.get("worktree") or data.get("core_repo", ""), "review_root": data.get("review_root", ""),
         "prepared": str(d / "prepare.out"), "prepared_sha256": None, "approved_sha256": None, "apply_refused": None,
         "nonconsumers": None, "pr": None, "expected_remote_head": None, "resulting_main_only": False, "protected_producer": False,
         "unknown_args": [], "body": str(d / "PR-BODY.md") if (d / "PR-BODY.md").exists() else None, "subject": None, "bead": None,
         "route": data.get("kind") or "preopen"}
    prepared = d / "prepare.out"
    if prepared.exists() and prepared.stat().st_size:
        try:
            p["prepared_sha256"] = canonical_sha256(prepared.read_text())
        except ValueError:
            p["prepared_sha256"] = None
    review = d / "APPLY-REVIEW.md"
    if review.exists():
        verdicts = re.findall(r"^VERDICT: APPROVE sha256=([0-9a-f]{64})\s*$", review.read_text(errors="replace"), re.MULTILINE)
        p["approved_sha256"] = verdicts[-1] if verdicts else None
    err = d / "apply.err"
    if err.exists():
        for line in err.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and str(event.get("event", "")).endswith("outcome") and event.get("operation") == "apply":
                p["apply_refused"] = None if event.get("result") == "success" else str(event.get("reason") or event.get("result"))
    p.update(prepare_arguments(d, p))
    if p["worktree"]:
        try:
            p["subject"] = w.sh(["git", "-C", p["worktree"], "log", "-1", "--format=%s", p["head"]], timeout=5).strip()[:72] or None
        except RuntimeError:
            pass
    return p


def _receipts(path: Path) -> object:
    import json

    try:
        if path.stat().st_size > 4_000_000:
            return None
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data.get("receipts") if isinstance(data, dict) else None


def _loop_scripts(d: Path, review_root: str) -> list[Path]:
    places = [d, d.parent, d.parent / "bin", d.parent.parent / "bin"] + ([Path(review_root) / "bin"] if review_root else [])
    return [f for f in dict.fromkeys(place / "prepare-loop.sh" for place in places) if f.is_file()]


def _named_nonconsumers(text: str, d: Path) -> list[Path]:
    """Every file a text names after --nonconsumers. Shell variables are resolved from plain NAME=value lines of the
    same text; a path that still holds a variable is tried by its file name next to the packet."""
    env = {m[1]: m[2].strip("'\"") for m in re.finditer(r"^\s*(?:export\s+)?([A-Za-z_]\w*)=([^\s;()]+)", text, re.MULTILINE)}
    out = []
    for m in re.finditer(r"--nonconsumers[ =]+([^\s;|>&)`'\"]+)", text):
        word = m[1]
        for _ in range(4):
            word = re.sub(r"\$\{?([A-Za-z_]\w*)\}?", lambda v: env.get(v[1], v[0]), word)
        if "$" in word:
            out += [d / Path(word).name, d.parent / Path(word).name]
        else:
            out.append(Path(os.path.expanduser(word)))
    return out


def prepare_arguments(d: Path, p: dict) -> dict[str, Any]:
    """The arguments `prepare` ran with, without a store. The prepared output itself records --pr, the update and
    resulting-main modes, the protected-producer flag and the nonconsumer receipts; only the FILE that held the
    receipts is not recorded. It is taken from prepare.args (when one was written), else from a sibling
    prepare-loop.sh, prepare.err or a note, else from any small JSON near the packet, and it counts only when its
    receipts equal the prepared ones (apply re-prepares and refuses any other bytes). What stays open is named."""
    import json

    out: dict = {"unknown_args": []}
    recorded: dict = {}
    args_file = d / "prepare.args"
    if args_file.is_file():
        try:
            recorded = json.loads(args_file.read_text())
            argv = [str(a) for a in recorded["argv"]]
        except (OSError, ValueError, KeyError, TypeError):
            argv, recorded = [], {}
            out["unknown_args"].append("prepare.args: unreadable, so it was ignored")
        base = Path(str(recorded.get("cwd") or d))
        for flag, key in (("--core-repo", "core_repo"), ("--review-root", "review_root")):
            if value := next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == flag), None):
                out[key] = str(base / value)
    else:
        argv = []
    prepared = None
    try:
        prepared = json.loads((d / "prepare.out").read_text())
    except (OSError, ValueError):
        pass
    if isinstance(prepared, dict) and "members" in prepared:
        out["unknown_args"].append("--packets/--core-repos/--prs: prepare.out is a batch transaction, which cannot be rebuilt from one packet")
        return out
    if isinstance(prepared, dict):
        out["pr"] = prepared.get("pr") if isinstance(prepared.get("pr"), int) else None
        out["protected_producer"] = prepared.get("protected_producer") is True
        out["resulting_main_only"] = "resulting_main" in prepared
        update = prepared.get("pr_update")
        if isinstance(update, dict):
            if re.fullmatch(r"[0-9a-f]{40}", str(update.get("old"))):
                out["expected_remote_head"] = update["old"]
            else:
                out["unknown_args"].append("--expected-remote-head: prepare.out has a pr_update without its old SHA")
    named = [Path(str(recorded.get("cwd") or d)) / argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--nonconsumers"]
    texts = _loop_scripts(d, p.get("review_root") or "") + [d / "prepare.err"] + sorted(
        list(d.glob("*.md")) + list(d.glob("*.log")) + list(d.glob("*.sh")) + list(d.glob("*.cmd")))
    for extra in texts:
        try:
            named += _named_nonconsumers(extra.read_text(errors="replace"), d)
        except OSError:
            continue
    want = prepared.get("nonconsumers") if isinstance(prepared, dict) and "nonconsumers" in prepared else None
    if want is None:  # an older or unreadable prepare output: the named file is the best there is
        out["nonconsumers"] = str(named[0]) if named else None
    elif want:
        from .words import q

        near = [f for place in dict.fromkeys([d, d.parent, d.parent / "inputs", d.parent.parent, Path(p.get("review_root") or d),
                                              Path(p.get("review_root") or d).parent]) for f in sorted(place.glob("*.json"))]
        out["nonconsumers"] = next((str(f) for f in dict.fromkeys(named + near) if _receipts(f) == want), None)
        if not out["nonconsumers"]:
            out["unknown_args"].append(
                f"--nonconsumers: prepare used {len(want)} receipts and no file near the packet holds exactly these (prepare.args, "
                f"prepare-loop.sh, notes and *.json were read); the receipts are in prepare.out, so this writes the file: "
                f"jq '{{receipts: .nonconsumers}}' {q(d / 'prepare.out')} > {q(d / 'nonconsumers.json')}")
    return out


# ---------------------------------------------------------------- declared focus
def focus_head(w: World, worktree: str) -> dict[str, Any]:
    """Branch, head, subject and dirtiness of a worktree, read live (the head may move after it was declared)."""
    git = ["git", "-C", worktree]
    head = w.sh([*git, "rev-parse", "HEAD"], timeout=5).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise RuntimeError("no commit checked out")
    return {"head": head, "branch": w.sh([*git, "rev-parse", "--abbrev-ref", "HEAD"], timeout=5).strip(),
            "subject": w.sh([*git, "log", "-1", "--format=%s"], timeout=5).strip()[:72],
            "dirty": bool(w.sh([*git, "status", "--porcelain", "--untracked-files=no"], timeout=8).strip())}


def declared_focus(w: World) -> list[dict]:
    """The declared focus worktrees: the focus list of `sm focus` once it exists (entries that are paths), else the
    old focus file, adopted read-only."""
    if w.focus_list is not None:
        return [{"worktree": key, "for": value.get("for") or "", "note": value.get("note") or "", "source": "focus-list"}
                for key, value in w.focus_list.items() if isinstance(value, dict) and str(key).startswith(("/", "~"))]
    return [{**entry, "source": "old-file"} for entry in legacy.focus_entries(w.state_dir)]


def probe_focus(w: World) -> list[Any]:
    """The declared focus heads with their live state; one read per entry tells whether the head is merged."""
    out = []
    for entry in declared_focus(w):
        row = {**entry, "head": None, "branch": entry.get("branch"), "prs": None}
        try:
            row.update(focus_head(w, str(entry["worktree"])))
        except RuntimeError as exc:
            row["error"] = str(exc)
        if row["head"] and row.get("branch") not in (None, "HEAD"):
            try:
                row["prs"] = w.gh_json(["pr", "list", "-R", w.repo, "--head", row["branch"], "--state", "all", "-L", "10", "--json", "number,state,headRefOid"], timeout=10)
            except RuntimeError:
                row["prs"] = None  # unknown is never merged
        out.append(row)
    return out


# ---------------------------------------------------------------- all of them, under one deadline
def collect(w: World, live: bool = False, budget: float | None = None) -> dict[str, Any]:
    """Run every probe in parallel under one deadline; a late or failing probe becomes {'unknown': why}."""
    budget = float(budget or setting(w, "go_budget_seconds", 18))
    probes = {"main": probe_main, "remote_heads": probe_remote_heads, "runs": probe_runs, "active_runs": probe_active_runs, "prs": probe_prs, "releases": probe_releases,
              "queue": probe_queue, "train": probe_train, "release": probe_release, "holds": probe_holds, "procs": probe_procs,
              "receipts": probe_receipts, "slots": probe_slots, "mini": lambda world: probe_mini(world, live),
              "tools": probe_tools, "alarms": probe_alarms, "rehearse": probe_rehearse, "focus": probe_focus,
              "pipelines": probe_pipelines}
    state: dict = {}
    done: dict = {}

    def work(name: str, fn) -> None:
        try:
            done[name] = fn(w)
        except Exception as exc:
            done[name] = unknown(exc if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}")

    threads = {name: threading.Thread(target=work, args=(name, fn), daemon=True, name=f"go-{name}") for name, fn in probes.items()}
    for thread in threads.values():
        thread.start()
    deadline = _Deadline(budget)
    for name, thread in threads.items():
        thread.join(deadline.left())
        state[name] = done[name] if name in done else unknown(f"no answer within {budget:.0f}s")
    procs = state["procs"] if isinstance(state["procs"], list) else []
    try:
        state["packets"] = probe_packets(w, [p.get("packet") for p in procs if p.get("kind") in ("prepare", "apply")])
    except Exception as exc:
        state["packets"] = unknown(f"{type(exc).__name__}: {exc}")
    try:
        state["since_tag"] = probe_since_tag(w, state["main"].get("sha", ""), state["releases"].get("latest", ""))
    except Exception as exc:
        state["since_tag"] = unknown(exc)
    try:
        state["main_ci"] = probe_exact_main_ci(w, state)
    except Exception as exc:
        state["main_ci"] = unknown(exc)
    try:
        state["main_workload"] = probe_main_workload(w, state)
    except Exception as exc:
        state["main_workload"] = unknown(exc)
    return state


def full_candidate_jobs(w: World, sha: str) -> frozenset[str] | None:
    """The success jobs of the full candidate workload, from Core's reviewed release policy at `sha` (None: unknown)."""
    from ..queue_lib import core_policy, release_candidate

    try:
        policy = core_policy.at_commit_api(lambda path: w.gh_json(["api", f"repos/{w.repo}/{path}"], timeout=10), sha)
    except release_candidate.CandidateError:
        return None
    return frozenset(name for name, conclusion in policy.expected_jobs("workflow_dispatch").items() if conclusion == "success")


def probe_exact_main_ci(w: World, state: dict) -> dict[str, Any]:
    """Read the CI workflow for the exact main SHA; incomplete inventories are unknown."""
    from . import decide

    sha = state.get("main", {}).get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("main SHA unavailable")
    rows = w.gh_json(["run", "list", "-R", w.repo, "--workflow", core_facts.ci_file(), "--branch", "main", "--commit", sha,
                      "-L", "100", "--json", "databaseId,headBranch,headSha,status,conclusion,workflowName,event,createdAt"], timeout=12)
    if not isinstance(rows, list) or len(rows) >= 100:
        raise RuntimeError("main CI inventory unavailable or incomplete")
    fields = ("databaseId", "headBranch", "headSha", "status", "conclusion", "workflowName", "event")
    if any(not isinstance(row, dict) or any(field not in row for field in fields) or type(row["databaseId"]) is not int for row in rows):
        raise RuntimeError("main CI inventory malformed")
    result = decide.main_ci(rows, sha, exact=True)
    return {**result, "sha": sha}


def probe_main_workload(w: World, state: dict) -> dict[str, Any]:
    """What main's CI run actually ran (focused, full or unknown), from its job list: one read of that run's jobs."""
    from . import decide

    sha = state["main"].get("sha", "") if isinstance(state.get("main"), dict) else ""
    ci = state.get("main_ci") if isinstance(state.get("main_ci"), dict) else {}
    if ci.get("sha") != sha:
        return {"run": None, "workload": "unknown"}
    if not ci.get("run"):
        return {"run": None, "workload": "unknown"}
    data = w.gh_json(["api", f"repos/{w.repo}/actions/runs/{ci['run']}/jobs?per_page=100"], timeout=10)
    jobs = data.get("jobs") if isinstance(data, dict) else None
    full = full_candidate_jobs(w, sha)
    if full is None:
        return {"run": ci["run"], "workload": "unknown"}
    return {"run": ci["run"], "workload": decide.workload(jobs, full)}


class _Deadline:
    """Wall time left of a budget. Real time on purpose: it bounds real threads (the clock provider may be a fake)."""

    def __init__(self, seconds: float):
        import time as _time

        self._now, self.end = _time.monotonic, _time.monotonic() + seconds

    def left(self) -> float:
        return max(0.1, self.end - self._now())
