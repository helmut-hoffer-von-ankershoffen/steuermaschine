"""The iOS app of a Core release on TestFlight: build, verify, upload and confirm one build per release version.

`publish(world, version)` is the importable step (`sm release ios` and `sm release resume X --live` use it). It reads
every Heinzel fact from the reviewed source at the release tag, never from a copy here:
- the marketing version is the `version` in the tag's pyproject.toml (it must equal VERSION);
- HEINZEL_SOURCE_REVISION is the tag's commit;
- the signing team and the provisioning profile of each bundle come from the tag's
  apple/distribution/ExportOptions-testflight.plist;
- App Store Connect is reached only through the tag's apple/scripts/testflight (its app ID, its internal group, the
  API key env file it reads; the key is never read here).
The build number is the next one above the highest build App Store Connect holds for the app (monotonic, from ASC).

Idempotent: when App Store Connect already holds a build of VERSION, nothing is built or uploaded; the build is only
waited for and confirmed. Every call writes the receipt ios/vX.Y.Z.json in the state directory: `clean` true only
when the build is VALID, unexpired and in the internal group. A false `clean` is an alarm (alarm_lib.rules.ios_alarm);
it never touches the published release.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import plistlib
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from steuermaschine.config import child_environment

VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
ROW = re.compile(r"^(?P<short>\S+) \((?P<build>[0-9]+)\) (?P<state>[A-Z_]+) expired=(?P<expired>True|False) (?P<group>in-group|NOT-in-group) id=\S+$")
TESTFLIGHT = "apple/scripts/testflight"
VERIFIER = "apple/scripts/widget_configuration.py"
EXPORT_OPTIONS = "apple/distribution/ExportOptions-testflight.plist"
PROJECT = "apple/Heinzel.xcodeproj"
DONE_STATES = ("VALID",)
BAD_STATES = ("FAILED", "INVALID")
BETA_OK = "IN_BETA_TESTING"
BETA_BAD = ("MISSING_EXPORT_COMPLIANCE", "PROCESSING_EXCEPTION", "EXPIRED", "INVALID")
STUCK_SECONDS = 3600  # a receipt still `running` this long after its start: the step died or hangs


class IosFailed(Exception):
    """The step could not finish; the message is the reason in the receipt."""


@dataclass
class IosResult:
    version: str
    clean: bool = False
    build: str = ""
    revision: str = ""
    uploaded: bool = False  # this call built and uploaded the build (False: it was there already)
    state: str = ""  # the App Store Connect processing state last seen
    in_group: bool = False
    beta: str = ""  # the internal beta state last seen (IN_BETA_TESTING: testers can install it)
    notes: bool = False  # the What to Test text was set from the release notes
    reason: str = ""
    steps: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"ios: {step}" for step in self.steps]
        if self.clean:
            out.append(f"IOS-OK {self.version} build {self.build} {self.beta} in the internal TestFlight group")
        else:
            out.append(f"IOS NOT-CLEAN {self.version}: {self.reason}")
        return out

    def receipt(self, started: float) -> dict[str, Any]:
        stamp = datetime.datetime.fromtimestamp(started, datetime.UTC).isoformat(timespec="seconds")
        return {"version": self.version, "clean": self.clean, "build": self.build, "revision": self.revision, "uploaded": self.uploaded,
                "state": self.state, "in_group": self.in_group, "beta": self.beta, "notes": self.notes, "reason": self.reason,
                "at": stamp, "started": started}


# ----------------------------------------------------------------------------- receipts
def receipt_dir(state_dir: Path) -> Path:
    return state_dir / "ios"


def read_receipt(state_dir: Path, version: str) -> dict[str, Any] | None:
    try:
        data = json.loads((receipt_dir(state_dir) / f"v{version}.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("version") == version else None


def latest_receipt(state_dir: Path) -> dict[str, Any] | None:
    """The receipt of the highest version; None when there is none."""
    try:
        names = [p.name[1:-5] for p in receipt_dir(state_dir).iterdir() if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+\.json", p.name)]
    except OSError:
        return None
    for version in sorted(names, key=lambda v: tuple(map(int, v.split("."))), reverse=True):
        if (data := read_receipt(state_dir, version)) is not None:
            return data
    return None


def summary(data: dict[str, Any] | None) -> str:
    """One line for `sm release status` and `sm why`; empty before the first run."""
    if not data:
        return ""
    if data.get("running"):
        return f"ios: v{data.get('version')} TestFlight step running since {data.get('at')}"
    if data.get("clean"):
        return f"ios: v{data.get('version')} TestFlight build {data.get('build')} {data.get('beta') or data.get('state')}, in the internal group"
    return f"ios: v{data.get('version')} NOT-CLEAN: {str(data.get('reason') or '?')[:140]}; sm release ios {data.get('version')}"


def write_receipt(state_dir: Path, result: IosResult, started: float, running: bool = False) -> None:
    folder = receipt_dir(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"v{result.version}.json"
    partial = target.with_suffix(".json.partial")
    partial.write_text(json.dumps({**result.receipt(started), "running": running}, indent=2) + "\n")
    partial.replace(target)


# ----------------------------------------------------------------------------- App Store Connect, through the tag's tool
def parse_status(text: str) -> list[dict[str, Any]]:
    rows = []
    for line in text.splitlines():
        if found := ROW.match(line.strip()):
            rows.append({"short": found["short"], "build": found["build"], "state": found["state"], "expired": found["expired"] == "True",
                         "in_group": found["group"] == "in-group"})
    return rows


def next_build(rows: list[dict[str, Any]]) -> str:
    return str(max((int(r["build"]) for r in rows), default=0) + 1)


@dataclass
class Context:
    w: Any
    version: str
    checkout: Path  # the clean release worktree
    work: Path  # archive and export, outside the worktree
    env: dict[str, str]
    result: IosResult
    tool: Path | None = None  # the testflight tool; None: the tag's


    def run(self, argv: list[str], timeout: float, what: str, cwd: Path | None = None) -> str:
        res = self.w.commands.run(argv, cwd=str(cwd or self.checkout), timeout=timeout, env=self.env)
        if res.rc:
            tail = [line for line in (res.err or res.out or "").strip().splitlines() if line.strip()][-1:] or [""]
            raise IosFailed(f"{what} exited {res.rc}: {tail[0][:200]}")
        return res.out or ""

    def testflight(self, *args: str, timeout: float = 120) -> str:
        tool = self.tool or self.checkout / TESTFLIGHT
        return self.run(["uv", "run", "--quiet", "--script", str(tool), *args], timeout, f"testflight {args[0]}")

    def rows(self) -> list[dict[str, Any]]:
        return parse_status(self.testflight("status"))


def builds_of(rows: list[dict[str, Any]], version: str) -> list[dict[str, Any]]:
    return [r for r in rows if r["short"] == version and not r["expired"]]


# ----------------------------------------------------------------------------- the step
def tag_facts(ctx: Context) -> tuple[str, dict[str, Any]]:
    """The marketing version of the tag's pyproject and the tag's export options; refuses a mismatch."""
    project = tomllib.loads((ctx.checkout / "pyproject.toml").read_text())
    marketing = str(project.get("project", {}).get("version") or "")
    if marketing != ctx.version:
        raise IosFailed(f"pyproject.toml at v{ctx.version} says version {marketing or '?'}, not {ctx.version}")
    options = plistlib.loads((ctx.checkout / EXPORT_OPTIONS).read_bytes())
    if not isinstance(options, dict) or not isinstance(options.get("provisioningProfiles"), dict) or not options.get("teamID"):
        raise IosFailed(f"{EXPORT_OPTIONS} at v{ctx.version} names no team or provisioning profiles")
    return marketing, options


def signing_xcconfig(options: dict[str, Any], marketing: str, build: str, revision: str) -> str:
    """Manual App Store signing as in the tag's export options: one profile per bundle identifier."""
    lines = ["CODE_SIGN_STYLE = Manual", f"DEVELOPMENT_TEAM = {options['teamID']}", "CODE_SIGN_IDENTITY = Apple Distribution"]
    for bundle, profile in sorted(options["provisioningProfiles"].items()):
        lines.append(f"HZ_PROFILE_{re.sub(r'[^A-Za-z0-9]', '_', str(bundle))} = {profile}")
    lines += ["PROVISIONING_PROFILE_SPECIFIER = $(HZ_PROFILE_$(PRODUCT_BUNDLE_IDENTIFIER:c99extidentifier))",
              f"MARKETING_VERSION = {marketing}", f"CURRENT_PROJECT_VERSION = {build}", f"HEINZEL_SOURCE_REVISION = {revision}"]
    return "\n".join(lines) + "\n"


def build_and_upload(ctx: Context, marketing: str, options: dict[str, Any], build: str) -> None:
    r = ctx.result
    archive = ctx.work / f"Heinzel-{ctx.version}-{build}.xcarchive"
    config = ctx.work / "signing.xcconfig"
    config.write_text(signing_xcconfig(options, marketing, build, r.revision))
    ctx.run(["xcodebuild", "archive", "-project", str(ctx.checkout / PROJECT), "-scheme", "Heinzel", "-configuration", "Release",
             "-destination", "generic/platform=iOS", "-archivePath", str(archive), "-derivedDataPath", str(ctx.work / "derived"),
             "-xcconfig", str(config), "-quiet"], 3600, "xcodebuild archive")
    r.steps.append(f"archived {marketing} ({build}) at {r.revision[:12]}")
    info = plistlib.loads((archive / "Products/Applications/Heinzel.app/Info.plist").read_bytes())
    if info.get("CFBundleShortVersionString") != marketing:
        raise IosFailed(f"the archive says version {info.get('CFBundleShortVersionString')}, not {marketing}")
    ctx.run(["uv", "run", "--quiet", "--no-project", "--python", "3.12", "python", str(ctx.checkout / VERIFIER), "archive", str(archive),
             "--build", build, "--revision", r.revision], 300, "widget_configuration.py archive")
    r.steps.append("archive verified (widget_configuration.py archive)")
    upload = dict(options, manageAppVersionAndBuildNumber=False)  # the verified archive's numbers are the uploaded ones
    plist = ctx.work / "ExportOptions-upload.plist"
    plist.write_bytes(plistlib.dumps(upload))
    ctx.testflight("upload", str(archive), "--plist", str(plist), "--export-path", str(ctx.work / "export"), timeout=1800)
    r.uploaded = True
    r.steps.append(f"uploaded build {build}")


def confirm(ctx: Context, build: str, wait_seconds: int) -> None:
    """Wait until App Store Connect lists the build VALID, add it to the group when automatic distribution did not, then
    read it once more."""
    r = ctx.result
    clock: Any = ctx.w.clock
    deadline = ctx.w.now() + wait_seconds
    assigned = False
    while True:
        row = next((x for x in ctx.rows() if x["build"] == build and x["short"] == ctx.version), None)
        r.state = row["state"] if row else "NOT-LISTED"
        r.in_group = bool(row and row["in_group"])
        if row and row["state"] in BAD_STATES:
            raise IosFailed(f"App Store Connect processing of build {build} ended {row['state']}")
        if row and row["expired"]:
            raise IosFailed(f"build {build} is expired")
        if row and row["state"] in DONE_STATES:
            if row["in_group"] and beta_ready(ctx, build):
                return
            if not row["in_group"] and not assigned:
                ctx.testflight("assign", build)
                assigned = True
                r.steps.append(f"assigned build {build} to the internal group")
                continue
        if ctx.w.now() >= deadline:
            where = f"{r.state}, beta {r.beta or '?'}" if r.in_group else f"{r.state}{', not in the internal group' if r.state == 'VALID' else ''}"
            raise IosFailed(f"build {build} is {where} after {wait_seconds // 60} min; run the same command again")
        clock.sleep(30)


def beta_ready(ctx: Context, build: str) -> bool:
    """Testers can install it: IN_BETA_TESTING. A state that never becomes it fails at once."""
    r = ctx.result
    found = re.search(r"internalBuildState=(\S+)", ctx.testflight("beta", build))
    r.beta = found[1] if found else "?"
    if r.beta in BETA_BAD:
        raise IosFailed(f"build {build} beta state is {r.beta}")
    return r.beta == BETA_OK


def set_notes(ctx: Context, build: str) -> None:
    """The release notes of the tag's GitHub release as the build's What to Test text."""
    res = ctx.w.commands.run(["gh", "release", "view", f"v{ctx.version}", "-R", ctx.w.repo, "--json", "body", "-q", ".body"], timeout=60)
    if res.rc or not (res.out or "").strip():
        raise IosFailed(f"the release notes of v{ctx.version} are unreadable (gh release view exited {res.rc})")
    notes = ctx.work / "what-to-test.txt"
    notes.write_text(f"Heinzel {ctx.version}\n\n{res.out.strip()}\n")
    ctx.testflight("notes", build, str(notes))
    ctx.result.notes = True
    ctx.result.steps.append(f"What to Test set from the v{ctx.version} release notes")


def tool_of(w: Any, core_repo: Path, ref: str | None, work: Path) -> Path | None:
    """The testflight tool at REF of the core repository (setting ios_tool_ref); None: the tag's own."""
    if not ref:
        return None
    res = w.commands.run(["git", "-C", str(core_repo), "show", f"{ref}:{TESTFLIGHT}"], timeout=30)
    if res.rc or not res.out:
        raise IosFailed(f"{TESTFLIGHT} is not at {ref} in {core_repo}")
    tool = work / "testflight"
    tool.write_text(res.out)
    return tool


def publish(w: Any, version: str, core_repo: Path, work_root: Path, wait_seconds: int = 1800) -> IosResult:
    """The whole step; never raises for a failure: the result (and its receipt) says why."""
    result = IosResult(version=version)
    if not VERSION.fullmatch(version):
        result.reason = "VERSION must be X.Y.Z"
        return result
    work_root.mkdir(parents=True, exist_ok=True)
    lock = (work_root / "ios.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        result.reason = "another `sm release ios` holds the lock; run the same command again when it is done"
        return result
    base = work_root / f"v{version}"
    checkout, work = base / "src", base / "work"
    started = w.now()
    write_receipt(w.state_dir, result, started, running=True)
    try:
        result.revision = commit_of(w, core_repo, version)
        fresh_checkout(w, core_repo, checkout, result.revision)
        work.mkdir(parents=True, exist_ok=True)
        developer = w.settings.get("ios_developer_dir")
        env = child_environment(DEVELOPER_DIR=str(Path(str(developer)).expanduser())) if developer else child_environment()
        ctx = Context(w=w, version=version, checkout=checkout, work=work, env=env, result=result,
                      tool=tool_of(w, core_repo, w.settings.get("ios_tool_ref"), work))
        marketing, options = tag_facts(ctx)
        rows = ctx.rows()
        mine = builds_of(rows, version)
        usable = [x for x in mine if x["state"] not in BAD_STATES]
        if usable:
            build = max(usable, key=lambda x: int(x["build"]))["build"]
            result.steps.append(f"App Store Connect holds {version} ({build}) already: verify only")
        elif mine:
            raise IosFailed(f"every build of {version} in App Store Connect failed processing ({', '.join(x['build'] for x in mine)}); a person decides")
        else:
            build = next_build(rows)
            build_and_upload(ctx, marketing, options, build)
        result.build = build
        confirm(ctx, build, wait_seconds)
        set_notes(ctx, build)
        result.clean = True
    except IosFailed as exc:
        result.reason = str(exc)
    except (OSError, ValueError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        result.reason = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        drop_checkout(w, core_repo, checkout)
        if result.clean:
            shutil.rmtree(work, ignore_errors=True)  # a failed run keeps its archive and logs for the person who looks
        write_receipt(w.state_dir, result, started)
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    return result


def commit_of(w: Any, core_repo: Path, version: str) -> str:
    res = w.commands.run(["git", "-C", str(core_repo), "rev-parse", "--verify", f"refs/tags/v{version}^{{commit}}"], timeout=30)
    sha = (res.out or "").strip()
    if res.rc or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise IosFailed(f"tag v{version} is not in {core_repo} (git fetch --tags)")
    return sha


def fresh_checkout(w: Any, core_repo: Path, checkout: Path, sha: str) -> None:
    drop_checkout(w, core_repo, checkout)
    res = w.commands.run(["git", "-C", str(core_repo), "worktree", "add", "--detach", "-f", str(checkout), sha], timeout=300)
    if res.rc:
        raise IosFailed(f"git worktree add of {sha[:12]} exited {res.rc}")


def drop_checkout(w: Any, core_repo: Path, checkout: Path) -> None:
    if checkout.exists():
        w.commands.run(["git", "-C", str(core_repo), "worktree", "remove", "--force", str(checkout)], timeout=120)
    shutil.rmtree(checkout, ignore_errors=True)
    w.commands.run(["git", "-C", str(core_repo), "worktree", "prune"], timeout=60)
