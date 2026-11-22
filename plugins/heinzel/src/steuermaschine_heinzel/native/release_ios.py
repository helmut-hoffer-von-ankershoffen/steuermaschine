"""release ios: the iOS app of a Core release on TestFlight, one build per release version (logic: release_lib/ios.py).

`run(world, version)` is the importable step: `sm release resume X --live` calls it after ACCEPTED, beside the Mini
update. A failure is NOT-CLEAN and an alarm; it never touches the published release.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import typer
from pydantic import BaseModel, Field

from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native

from .go import world
from .go_lib import probes
from .release_lib import ios


class IosSettings(BaseModel):
    ios_core_repo: Path | None = Field(None, description="The Core git repository the release tag is checked out from (a worktree per run). None: the setting queue_core_repo.")
    ios_developer_dir: Path | None = Field(None, description="DEVELOPER_DIR for xcodebuild. None: the Xcode that xcode-select names.")
    ios_wait_minutes: int = Field(30, description="How long `sm release ios` waits for App Store Connect to process the build until testers can install it; longer is NOT-CLEAN (stuck).")
    ios_tool_ref: str | None = Field("origin/main", description="A git ref of the Core repository for apple/scripts/testflight; default origin/main: an older tag lacks the notes and beta verbs. Empty: the tag's own.")


class ReleaseIos(Model):
    """One release version on TestFlight. Exit 0 only when the build is VALID and in the internal group."""

    version: str
    clean: bool
    build: str = ""
    revision: str = ""
    uploaded: bool = Field(False, description="This call built and uploaded the build; False: App Store Connect held it already (verify only).")
    state: str = Field("", description="The App Store Connect processing state last seen.")
    in_group: bool = False
    reason: str = ""
    lines: list[str] = []

    def render(self) -> list[str]:
        return self.lines

    @property
    def exit_code(self) -> int:
        return 0 if self.clean else 1


def run(w: probes.World, version: str) -> ReleaseIos:
    s = w.settings or {}
    repo = s.get("ios_core_repo") or s.get("queue_core_repo") or w.core
    if not repo:  # NOT-CLEAN with a receipt, not a refusal: the release driver and the follower call this and must go on
        done = ios.IosResult(version=version, reason="`sm release ios` checks out the release tag: set ios_core_repo (or queue_core_repo) in [heinzel]")
        ios.write_receipt(w.state_dir, done, w.now())
        return ReleaseIos(version=version, clean=False, reason=done.reason, lines=done.lines())
    done = ios.publish(w, version, Path(str(repo)).expanduser(), ios.receipt_dir(w.state_dir) / "work", int(s.get("ios_wait_minutes") or 30) * 60)
    return ReleaseIos(version=done.version, clean=done.clean, build=done.build, revision=done.revision, uploaded=done.uploaded, state=done.state,
                      in_group=done.in_group, reason=done.reason, lines=done.lines())


@native("release ios", writes=True, help=H(
    "Put the iOS app of one Core release on TestFlight and confirm it reached the internal group.",
    "Use after a release is ACCEPTED when `sm release status` shows ios NOT-CLEAN; `sm release resume X --live` runs it itself.",
    "checks the tag out in a worktree, archives with xcodebuild, verifies, uploads to App Store Connect, assigns to the internal group, sets What to Test; writes ios/vX.Y.Z.json.",
    "sm release ios 0.9.54", "sm release status",
    "Marketing version: the tag's pyproject version (must equal VERSION). Build number: one above the highest build App Store\n"
    "Connect holds. HEINZEL_SOURCE_REVISION: the tag's commit. Signing: the tag's ExportOptions-testflight.plist.\n"
    "Done means VALID, in the internal group, internal beta state IN_BETA_TESTING, and What to Test set from the tag's GitHub\n"
    "release notes. Processing longer than ios_wait_minutes (30), an invalid binary or a bad beta state is NOT-CLEAN and an alarm.\n"
    "Idempotent: when App Store Connect holds a build of VERSION, it only waits for it and confirms it.\n"
    "The last line is `IOS-OK X.Y.Z build N ...` (exit 0) or `IOS NOT-CLEAN X.Y.Z: reason` (exit 1)."))
def release_ios(version: str = typer.Argument(..., metavar="VERSION", help="The exact released Core version X.Y.Z; its tag vX.Y.Z must exist.")) -> ReleaseIos:
    if not ios.VERSION.fullmatch(version):
        raise Refuse("VERSION must be an exact X.Y.Z", "sm release ios 0.9.54", code=2)
    return run(world(), version)


def after_accepted(w: probes.World, version: str) -> list[str]:
    """The hook of `sm release resume X --live`: the lines of this step. It never raises: a refusal (no repository set)
    or any other error is NOT-CLEAN with its reason, written to the receipt so that the alarm `ios:VERSION` holds."""
    try:
        return run(w, version).lines
    except Exception as exc:  # the published release and the Mini proof must survive any failure here
        failed = ios.IosResult(version=version, reason=f"{type(exc).__name__}: {str(exc)[:200]}")
        with contextlib.suppress(OSError):
            ios.write_receipt(w.state_dir, failed, w.now())
        return failed.lines()
