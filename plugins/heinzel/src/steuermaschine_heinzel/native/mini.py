"""mini verify: prove the Mini runs a healthy, accepted Core release (LIVE-OK). Read-only: it asks the installed
product for its version and status, reads two URLs and the release receipts; it never restarts or signals anything.
mini update: when that proof fails, the supported update, a restart of the web and daemon jobs, then the same proof.

`verify(world, minimum)` and `update(world, minimum)` are the importable functions (go --live, release verify --mini
and release resume --live use them).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine.config import child_environment, path_with
from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native

from . import updater_hold
from .go import world
from .go_lib import probes

VERSION = r"[0-9]+\.[0-9]+\.[0-9]+"
PLUGINS_SHOWN = ("heinzel-google", "heinzel-github", "heinzel-slack")


class MiniSettings(BaseModel):
    mini_port: int = Field(8737, description="The port of the live web service on this machine.")
    mini_remote_url: str | None = Field(None, description="The URL users reach the live service at (over the tailnet); none: LIVE-OK cannot be proven.")
    mini_bin_dir: Path | None = Field(None, description="Where the product's command is installed; put first on PATH for the status reads. Unset: PATH as it is.")
    mini_jobs: list[str] = Field(["io.github.helmut-hoffer-von-ankershoffen.heinzel.web", "io.github.helmut-hoffer-von-ankershoffen.heinzel.daemon"],
                                 description="The launchd jobs `sm mini update` restarts after the update (Core's ui and daemon unit labels).")
    mini_update_wait_seconds: int = Field(180, description="How long `sm mini update` waits after the restart for the served version to equal the installed one.")


class Unverified(Exception):
    """Required evidence was unavailable or negative."""


class MiniVerify(Model):
    """The proof, as far as it got. `live` only when every line is positive."""

    live: bool = False
    version: str = ""  # the verified version (LIVE-OK X.Y.Z)
    reason: str = ""  # why not, when not live
    target: str = ""
    target_kind: str = ""  # minimum | latest
    cli: str = ""
    web_version: str = ""
    web_ok: bool | None = None
    daemon_version: str = ""
    daemon_ok: bool | None = None
    acceptance_run: int | None = None
    local: str = ""  # 200 | unverified
    remote: str = ""  # 200 | unverified
    doctor: str = ""  # healthy | unverified
    plugins: str = ""
    held: str = ""  # the version the Mini's updater holds back for want of an acceptance marker
    updated: list[str] = []  # what `sm mini update` did before the proof (empty: nothing was changed)

    def render(self) -> list[str]:
        out = [f"update: {step}" for step in self.updated]
        if self.cli:
            out.append(f"{self.target_kind}={self.target} cli={self.cli}")
        if self.web_ok is not None:
            out.append(f"web={self.web_version} web_ok={self.web_ok} daemon={self.daemon_version} daemon_ok={self.daemon_ok} acceptance_run={self.acceptance_run}")
        if self.doctor:
            out.append(f"local={self.local} tail={self.remote} doctor={self.doctor}")
        if self.live:
            out.append(f"plugins: {self.plugins}")
        out.append(f"LIVE-OK {self.version}" if self.live else f"NOT-LIVE: {self.reason}")
        return out

    @property
    def exit_code(self) -> int:
        return 0 if self.live else 1


def version_key(version: str) -> tuple[int, ...]:
    return tuple(map(int, version.split(".")))


def object_data(raw: str, label: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except ValueError:
        raise Unverified(f"{label} returned malformed JSON") from None
    if not isinstance(data, dict):
        raise Unverified(f"{label} did not contain a JSON object")
    return data


def receipt(path: Path) -> dict[str, Any] | None:
    """One release receipt of the scripted era (adopted read-only); a link or an unreadable file is not evidence."""
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise Unverified(f"{path.name} is not a regular release receipt")
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise Unverified(f"{path.name} is unreadable") from None
    return object_data(raw, path.name)


PLUGIN_STAMP = re.compile(r"plugin-heinzel-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9]+\.[0-9]+\.[0-9]+(?:_[a-z_]+)?")


def core_receipt(data: dict[str, Any]) -> dict[str, Any]:
    """The core part of the global release receipt. Plugin dispatch stamps (written there by an older `sm release
    plugin`) are not core evidence: a receipt of only those counts as absent, so the proof comes from the tail receipts
    alone. Any other key without an exact version is a broken core receipt and stays NOT-LIVE."""
    core = {key: value for key, value in data.items() if not PLUGIN_STAMP.fullmatch(key)}
    if core and not re.fullmatch(VERSION, str(core.get("version"))):
        raise Unverified("global release receipt has no exact version")
    return core


def acceptance_run(data: dict[str, Any]) -> int | None:
    run = data.get("acceptance_run")
    run_id = data.get("acceptance_run_id")
    if (data.get("accepted") is True and data.get("finished") is True and data.get("absorbed_by") is None
            and type(run) is int and run > 0 and (run_id is None or (type(run_id) is int and run_id == run))):
        return run
    return None


def exact_acceptance(version: str, tail_dir: Path, legacy_receipt: dict[str, Any]) -> int | None:
    tail = receipt(tail_dir / f"v{version}.json")
    if tail is not None and tail.get("version") != version:
        raise Unverified(f"v{version}.json does not own v{version}")
    old = legacy_receipt if legacy_receipt.get("version") == version else None
    if tail is not None and old is not None and acceptance_run(tail) != acceptance_run(old):
        raise Unverified(f"conflicting release receipts for v{version}")
    return acceptance_run(tail if tail is not None else old or {})


def latest_acceptance(tail_dir: Path, legacy_receipt: dict[str, Any]) -> str:
    try:
        versions = {path.stem[1:] for path in tail_dir.iterdir() if re.fullmatch(f"v{VERSION}\\.json", path.name)}
    except FileNotFoundError:
        versions = set()
    except OSError:
        raise Unverified("release tail receipts are unreadable") from None
    old = legacy_receipt.get("version")
    if isinstance(old, str) and re.fullmatch(VERSION, old):
        versions.add(old)
    for version in sorted(versions, key=version_key, reverse=True):
        if exact_acceptance(version, tail_dir, legacy_receipt) is not None:
            return version
    raise Unverified("no finished accepted Core release receipt")


def doctor_ok(report: dict[str, Any]) -> bool:
    checks = report.get("checks")
    if not isinstance(checks, dict) or report.get("status") not in ("up", "degraded"):
        return False
    for group in checks.values():
        if not isinstance(group, list) or any(
            not isinstance(check, dict) or not isinstance(check.get("name"), str)
            or check.get("status") not in ("up", "degraded") for check in group
        ):
            return False
    freshness = [check["status"] for check in checks.get("system.daemon", []) if check["name"] == "last tick"]
    return freshness == ["up"]


def fresh_tick(value: object, interval: object, now: float) -> bool:
    if not isinstance(value, str) or type(interval) is not int or interval <= 0:
        return False
    try:
        tick = datetime.datetime.fromisoformat(value)
        if tick.tzinfo is None:
            return False
        age = datetime.datetime.fromtimestamp(now, datetime.UTC) - tick
        # The daemon's own freshness window, and a real observed tick.
        return datetime.timedelta(0) <= age <= datetime.timedelta(minutes=3 * interval)
    except (ValueError, OverflowError):
        return False


def observed_version(status: dict[str, Any]) -> str:
    version = status.get("version")
    return version if isinstance(version, str) and re.fullmatch(VERSION, version) else "?"


def product(s: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """The product's command name and the environment it runs in."""
    bin_dir = s.get('mini_bin_dir')
    env = child_environment(PATH=path_with(str(Path(str(bin_dir)).expanduser()))) if bin_dir else child_environment()
    return str(s.get("mini_cli") or "hz"), env


def verify(w: probes.World, minimum: str | None = None) -> MiniVerify:
    """The whole proof. Never raises for missing evidence: the result says how far it got and why it stopped."""
    s = w.settings or {}
    cli_name, env = product(s)
    port = int(s.get("mini_port") or 8737)
    remote = s.get("mini_remote_url")
    out = MiniVerify(target_kind="minimum" if minimum else "latest")

    def command(*argv: str) -> str:
        res = w.commands.run(list(argv), timeout=12, env=env)
        if res.rc in (124, 127):
            raise Unverified(f"{' '.join(argv)} unavailable")
        if res.rc:
            raise Unverified(f"{' '.join(argv)} exited {res.rc}")
        return res.out

    def http_code(url: str) -> str:
        return command("curl", "--disable", "--silent", "--noproxy", "*", "--max-time", "8", "--output", os.devnull, "--write-out", "%{http_code}", url).strip()

    try:
        versions = re.findall(rf"^\S+ ({VERSION})$", command(cli_name, "--version"), re.MULTILINE)
        if len(versions) != 1:
            raise Unverified(f"{cli_name} --version did not report one exact Core version")
        cli = versions[0]
        legacy_receipt = core_receipt(receipt(w.state_dir / "release.json") or {})
        tails = w.state_dir / "release"
        out.target = minimum or latest_acceptance(tails, legacy_receipt)
        out.cli = cli
        if version_key(cli) < version_key(out.target) or (minimum is None and cli != out.target):
            held = updater_hold.held_version(s)
            if held and version_key(held) > version_key(cli) and version_key(held) >= version_key(out.target):
                out.held = held
                raise Unverified(updater_hold.held_reason(held, cli))
            raise Unverified(f"installed v{cli} does not satisfy {out.target_kind} v{out.target}")
        run = exact_acceptance(cli, tails, legacy_receipt)
        if run is None:
            raise Unverified(f"v{cli} has no finished positive acceptance verdict")

        web = object_data(command(cli_name, "ui", "status", "--json"), "web status")
        dae = object_data(command(cli_name, "system", "daemon", "status", "--json"), "daemon status")
        out.web_ok = (web.get("installed") is True and web.get("loaded") is True and web.get("ready") is True
                      and type(web.get("port")) is int and web["port"] == port
                      and isinstance(web.get("instance"), str) and bool(re.fullmatch(r"[0-9a-f]{32}", web["instance"]))
                      and web.get("version") == cli)
        out.daemon_ok = (dae.get("installed") is True and dae.get("owned") is True and dae.get("loaded") is True
                         and dae.get("paused") is False and dae.get("version_mismatch") is False
                         and dae.get("version") == cli and dae.get("installed_version") == cli
                         and fresh_tick(dae.get("last_tick"), dae.get("interval_minutes"), w.now()))
        # Only validated versions are shown, never raw status output.
        out.web_version, out.daemon_version, out.acceptance_run = observed_version(web), observed_version(dae), run
        if not out.web_ok or not out.daemon_ok:
            raise Unverified("web or daemon ownership, readiness, or running version is unverified")
        local = http_code(f"http://127.0.0.1:{port}/")
        tail = http_code(str(remote)) if remote else ""
        healthy = doctor_ok(object_data(command(cli_name, "system", "doctor", "--json"), "doctor"))
        out.local, out.remote, out.doctor = ("200" if local == "200" else "unverified"), ("200" if tail == "200" else "unverified"), ("healthy" if healthy else "unverified")
        if not remote:
            raise Unverified("the remote URL is not configured (setting mini_remote_url), so reachability for users is unverified")
        if local != "200" or tail != "200" or not healthy:
            raise Unverified("HTTP reachability or doctor health is unverified")
        try:
            plugins = json.loads(command(cli_name, "system", "plugin", "list", "--json"))
            out.plugins = " ".join(f"{p['distribution'].removeprefix('heinzel-')}={p['version']}/{p.get('source_kind') or '?'}"
                                   for p in plugins if p["distribution"] in PLUGINS_SHOWN) or "none-of-google/github/slack"
        except (Unverified, ValueError, TypeError, KeyError):
            out.plugins = "unreadable"
        out.live, out.version = True, cli
    except Unverified as exc:
        out.reason = str(exc)
    return out


def update(w: probes.World, minimum: str | None = None) -> MiniVerify:
    """Make the proof hold: nothing when it holds already. Else the supported `hz system update --channel stable --yes`,
    then, when the proof still fails, `launchctl kickstart -k` of both jobs, then the proof again until the version
    served on the port equals the installed one and the rest holds (or the wait ends). An installed version older than
    --minimum after the update (the stable channel does not offer it yet) restarts nothing."""
    before = verify(w, minimum)
    if before.live:
        return before.model_copy(update={"updated": ["nothing to do: already live"]})
    s = w.settings or {}
    cli_name, env = product(s)
    commands: Any = w.commands
    done = commands.run([cli_name, "system", "update", "--channel", "stable", "--yes"], timeout=900, env=env)
    if done.rc:
        tail = (done.err or done.out).strip().splitlines()[-1:] or [""]
        return before.model_copy(update={"reason": f"{cli_name} system update exited {done.rc}: {tail[0][:160]}", "updated": []})
    steps = [f"{cli_name} system update --channel stable --yes: ok"]
    after = verify(w, minimum)
    if after.live:
        return after.model_copy(update={"updated": steps})
    if after.cli and minimum and version_key(after.cli) < version_key(minimum):
        return after.model_copy(update={"updated": [*steps, f"the stable channel does not offer v{minimum} yet; nothing restarted"]})
    for job in s.get("mini_jobs") or MiniSettings.model_fields["mini_jobs"].default:
        kicked = commands.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{job}"], timeout=60)
        if kicked.rc:
            return after.model_copy(update={"reason": f"launchctl kickstart -k {job} exited {kicked.rc}", "updated": steps})
        steps.append(f"kickstarted {job}")
    deadline = w.now() + int(s.get("mini_update_wait_seconds") or 180)
    while not (after := verify(w, minimum)).live and w.now() < deadline:
        clock: Any = w.clock
        clock.sleep(10)
    if not after.live and after.web_version != after.cli and after.cli:
        after.reason = f"the version served on port {int(s.get('mini_port') or 8737)} is {after.web_version or '?'}, the installed one {after.cli}"
    return after.model_copy(update={"updated": steps})


@native("mini verify", writes=False, replaces=("hz-verify-mini",), help=H(
    "Prove the Mini runs a healthy, accepted Core release (LIVE-OK).",
    "Use after a release is published and before telling the owner it is live.",
    "nothing (read-only; it reads the live service's status and two URLs, never restarts or signals it).",
    "sm mini verify --minimum 0.9.39", "sm beads sweep   (after LIVE-OK)",
    "Without --minimum the installed version must be the latest one with a finished positive acceptance receipt; with it,\n"
    "that version or a newer accepted one. The last line is `LIVE-OK X.Y.Z` (exit 0) or `NOT-LIVE: reason` (exit 1).\n"
    "Proof: the installed version, its acceptance receipt, web and daemon owned, loaded, ready and on that version with a\n"
    "fresh daemon tick, HTTP 200 locally and at the remote URL, and a healthy doctor."))
def mini_verify(minimum: str | None = typer.Option(None, "--minimum", metavar="X.Y.Z", help="Accept this version or a newer accepted one.")) -> MiniVerify:
    if minimum is not None and not re.fullmatch(VERSION, minimum):
        raise Refuse("--minimum requires an exact X.Y.Z", "sm mini verify --minimum 0.9.39", code=2)
    return verify(world(), minimum)


@native("mini update", writes=True, help=H(
    "Bring the Mini to a healthy, accepted Core release and prove it (LIVE-OK).",
    "Use after a release is accepted, when `sm mini verify` says NOT-LIVE; `sm release resume VERSION --live` runs it itself.",
    "runs `hz system update --channel stable --yes` and restarts the web and daemon launchd jobs (`launchctl kickstart -k`); nothing when the proof holds already.",
    "sm mini update --minimum 0.9.53", "sm beads sweep   (after LIVE-OK)",
    "The restart happens only when the proof still fails after the update and the installed version is not older than\n"
    "--minimum. Then it waits (setting mini_update_wait_seconds) until the version served on the port equals the\n"
    "installed one and the whole proof of `sm mini verify` holds. The last line is `LIVE-OK X.Y.Z` (exit 0) or `NOT-LIVE: reason` (exit 1)."))
def mini_update(minimum: str | None = typer.Option(None, "--minimum", metavar="X.Y.Z", help="Accept this version or a newer accepted one.")) -> MiniVerify:
    if minimum is not None and not re.fullmatch(VERSION, minimum):
        raise Refuse("--minimum requires an exact X.Y.Z", "sm mini update --minimum 0.9.53", code=2)
    return update(world(), minimum)
