"""release ios, native: the scripted Mini (git, xcodebuild, the tag's testflight and verifier) in a temporary state
directory. No test starts a process, reads the network or sleeps."""

import json
import plistlib
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, fake_providers, invoke
from steuermaschine_heinzel.native import release_ios
from steuermaschine_heinzel.native.alarm_lib import rules
from steuermaschine_heinzel.native.release_lib import ios
from steuermaschine_heinzel.plugin import plugin

SHA = "a" * 40
OPTIONS = {"method": "app-store-connect", "destination": "upload", "teamID": "ABCDE12345", "signingStyle": "manual",
           "provisioningProfiles": {"team.example.app": "App Profile", "team.example.app.widgets": "Widget Profile"},
           "manageAppVersionAndBuildNumber": True}


def row(short, build, state="VALID", group="in-group", expired=False):
    return f"{short} ({build}) {state} expired={expired} {group} id=x{build}"


class Mini:
    def __init__(self, tmp_path, version="0.9.53", **table):
        self.state = tmp_path / "state"
        self.version = version
        self.pyproject = version
        self.asc: list[str] = [row("0.1.0", 31), row("0.1.0", 30)]
        self.processing = 0  # status reads that still say PROCESSING after the upload
        self.auto_group = True
        self.archived: list[list[str]] = []
        self.uploads: list[list[str]] = []
        self.verified = 0
        self.fail: dict[str, Result] = {}
        self.archive_version: str | None = None
        self.beta = ["IN_BETA_TESTING"]  # the internal beta states read in turn (the last one stays)
        self.notes: list[str] = []
        self.release_notes = Result(0, "- Faster chats\n")
        c = self.commands = FakeCommands()
        c.on(["git", "-C", str(tmp_path / "core"), "rev-parse"], lambda argv: self.fail.get("tag") or Result(0, SHA + "\n"))
        c.on(["git", "-C", str(tmp_path / "core"), "worktree"], self.worktree)
        c.on(["uv", "run", "--quiet", "--script"], self.testflight)
        c.on(["uv", "run", "--quiet", "--no-project"], self.verify)
        c.on(["xcodebuild", "archive"], self.archive)
        c.on(["gh", "release", "view"], lambda argv: self.release_notes)
        c.on(["git", "-C", str(tmp_path / "core"), "show"], lambda argv: self.fail.get("show") or Result(0, "# the tool at the ref\n"))
        stack = stack_of(plugin)
        settings, _ = stack.validate({"heinzel": {"legacy_state_dir": str(self.state), "queue_core_repo": str(tmp_path / "core"), **table}}, Path("."))
        self.rt = Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, settings, _providers=fake_providers(commands=c))

    def worktree(self, argv):
        if argv[4] == "add":
            src = Path(argv[-2])
            (src / "apple/distribution").mkdir(parents=True)
            (src / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{self.pyproject}"\n')
            (src / ios.EXPORT_OPTIONS).write_bytes(plistlib.dumps(OPTIONS))
        return Result(0)

    def testflight(self, argv):
        verb = argv[5]
        if verb in self.fail:
            return self.fail[verb]
        if verb == "upload":
            self.uploads.append(argv)
            build = self.archived[-1][-1]
            self.asc.insert(0, row(self.version, build, "PROCESSING", "NOT-in-group"))
            return Result(0)
        if verb == "beta":
            state = self.beta.pop(0) if len(self.beta) > 1 else self.beta[0]
            return Result(0, f"build {argv[6]} internalBuildState={state}\n")
        if verb == "notes":
            self.notes.append(Path(argv[7]).read_text())
            return Result(0, "notes set\n")
        if verb == "assign":
            self.asc = [line.replace("NOT-in-group", "in-group") if f"({argv[6]})" in line else line for line in self.asc]
            return Result(0, f"assigned build {argv[6]}\n")
        if self.processing:
            self.processing -= 1
        else:
            self.asc = [line.replace("PROCESSING", "VALID").replace("NOT-in-group", "in-group" if self.auto_group else "NOT-in-group") for line in self.asc]
        return Result(0, "\n".join(self.asc) + "\n")

    def archive(self, argv):
        if "archive" in self.fail:
            return self.fail["archive"]
        archive = Path(argv[argv.index("-archivePath") + 1])
        config = Path(argv[argv.index("-xcconfig") + 1]).read_text()
        build = next(line.split(" = ")[1] for line in config.splitlines() if line.startswith("CURRENT_PROJECT_VERSION"))
        self.archived.append([config, build])
        app = archive / "Products/Applications/Heinzel.app"
        app.mkdir(parents=True)
        (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": self.archive_version or self.version, "CFBundleVersion": build}))
        return Result(0)

    def verify(self, argv):
        self.verified += 1
        return self.fail.get("verify") or Result(0, "Widget configuration archive passed\n")

    def run(self, version=None):
        return invoke(self.rt, ["release", "ios", version or self.version])

    def receipt(self):
        return json.loads((self.state / "ios" / f"v{self.version}.json").read_text())


def test_builds_the_next_number_verifies_uploads_and_confirms_the_group(tmp_path):
    m = Mini(tmp_path)
    m.processing = 2  # the first read (before the build) and one after the upload still say PROCESSING
    out = m.run()
    assert out.exit_code == 0, out
    assert out.stdout.splitlines()[-1] == "IOS-OK 0.9.53 build 32 IN_BETA_TESTING in the internal TestFlight group"
    assert m.notes == ["Heinzel 0.9.53\n\n- Faster chats\n"] and "ios: What to Test set from the v0.9.53 release notes" in out.stdout
    config, build = m.archived[0]
    assert build == "32" and "MARKETING_VERSION = 0.9.53" in config and f"HEINZEL_SOURCE_REVISION = {SHA}" in config
    assert "DEVELOPMENT_TEAM = ABCDE12345" in config and "HZ_PROFILE_team_example_app_widgets = Widget Profile" in config
    assert m.verified == 1 and len(m.uploads) == 1
    assert not Path(m.uploads[0][m.uploads[0].index("--plist") + 1]).exists()  # the work directory of a clean run is removed
    receipt = m.receipt()
    assert receipt["clean"] and receipt["build"] == "32" and receipt["uploaded"] and receipt["revision"] == SHA and receipt["in_group"]
    assert receipt["running"] is False and receipt["notes"] and receipt["beta"] == "IN_BETA_TESTING"
    assert m.rt.providers.clock.slept == [30]
    assert not (m.state / "ios" / "work" / "v0.9.53" / "src").exists()


def test_the_upload_keeps_the_archive_numbers(tmp_path):
    m = Mini(tmp_path)
    seen = {}

    def keep(argv):
        if argv[5] == "upload":
            seen.update(plistlib.loads(Path(argv[argv.index("--plist") + 1]).read_bytes()))
        return Mini.testflight(m, argv)
    m.commands.on(["uv", "run", "--quiet", "--script"], keep)
    assert m.run().exit_code == 0
    assert seen["manageAppVersionAndBuildNumber"] is False and seen["teamID"] == "ABCDE12345"


def test_a_version_already_in_app_store_connect_is_only_verified(tmp_path):
    m = Mini(tmp_path)
    m.asc.insert(0, row("0.9.53", 32))
    out = m.run()
    assert out.exit_code == 0 and "ios: App Store Connect holds 0.9.53 (32) already: verify only" in out.stdout
    assert not m.archived and not m.uploads and m.receipt()["uploaded"] is False


def test_assigns_a_valid_build_outside_the_group(tmp_path):
    m = Mini(tmp_path)
    m.auto_group = False
    out = m.run()
    assert out.exit_code == 0 and "ios: assigned build 32 to the internal group" in out.stdout


@pytest.mark.parametrize(("change", "reason"), [
    (lambda m: setattr(m, "pyproject", "0.9.52"), "pyproject.toml at v0.9.53 says version 0.9.52, not 0.9.53"),
    (lambda m: m.fail.update(tag=Result(128, "", "bad")), "tag v0.9.53 is not in"),
    (lambda m: m.fail.update(archive=Result(65, "", "error: signing\n")), "xcodebuild archive exited 65: error: signing"),
    (lambda m: setattr(m, "archive_version", "0.1.0"), "the archive says version 0.1.0, not 0.9.53"),
    (lambda m: m.fail.update(verify=Result(1, "", "Widget configuration check failed: x")), "widget_configuration.py archive exited 1"),
    (lambda m: m.fail.update(upload=Result(70, "", "denied")), "testflight upload exited 70: denied"),
    (lambda m: m.asc.insert(0, row("0.9.53", 32, "FAILED", "NOT-in-group")), "every build of 0.9.53 in App Store Connect failed processing (32)"),
    (lambda m: setattr(m, "processing", 10_000), "build 32 is PROCESSING after 30 min; run the same command again"),
    (lambda m: setattr(m, "beta", ["READY_FOR_BETA_SUBMISSION"]), "build 32 is VALID, beta READY_FOR_BETA_SUBMISSION after 30 min"),
    (lambda m: setattr(m, "beta", ["MISSING_EXPORT_COMPLIANCE"]), "build 32 beta state is MISSING_EXPORT_COMPLIANCE"),
    (lambda m: setattr(m, "release_notes", Result(1, "", "not found")), "the release notes of v0.9.53 are unreadable (gh release view exited 1)"),
    (lambda m: m.fail.update(notes=Result(2, "", "invalid choice: 'notes'")), "testflight notes exited 2"),
    (lambda m: m.fail.update(notes=Result(1, "", 'PATCH x: HTTP 409 {\n  "detail" : "invalid text"\n}\n')), 'testflight notes exited 1: PATCH x: HTTP 409 { "detail" : "invalid text" }'),
])
def test_a_failure_is_not_clean_with_its_reason_and_an_alarm(tmp_path, change, reason):
    m = Mini(tmp_path)
    change(m)
    out = m.run()
    assert out.exit_code == 1 and out.stdout.splitlines()[-1].startswith("IOS NOT-CLEAN 0.9.53: ") and reason in out.stdout, out
    data = m.receipt()
    assert data["clean"] is False and reason in data["reason"]
    alarm = rules.ios_alarm(ios.latest_receipt(m.state), m.rt.providers.clock.now())
    assert alarm and alarm["cls"] == "ALERT" and alarm["key"] == "ios:0.9.53"
    assert not (m.state / "ios" / "work" / "v0.9.53" / "src").exists()


def test_processing_that_ends_badly_stops_at_once(tmp_path):
    m = Mini(tmp_path)
    real = m.testflight

    def bad(argv):
        out = real(argv)
        if argv[5] == "status":
            out = Result(0, out.out.replace("(32) VALID", "(32) INVALID"))
        return out
    m.commands.on(["uv", "run", "--quiet", "--script"], bad)
    out = m.run()
    assert out.exit_code == 1 and "processing of build 32 ended INVALID" in out.stdout


def test_refusals_and_the_lock(tmp_path):
    m = Mini(tmp_path)
    assert m.run("0.9").exit_code == 2
    lock = (m.state / "ios" / "work")
    lock.mkdir(parents=True)
    import fcntl
    with (lock / "ios.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        out = m.run()
    assert out.exit_code == 1 and "holds the lock" in out.stdout
    assert ios.publish(m.rt.providers, "bad", tmp_path, tmp_path).reason == "VERSION must be X.Y.Z"


def test_without_a_core_repository_it_refuses_and_the_hook_says_not_clean(tmp_path):
    from steuermaschine.native import NativeRuntime, using
    from steuermaschine_heinzel.native.go import world
    m = Mini(tmp_path)
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {"legacy_state_dir": str(m.state)}}, Path("."))
    rt = Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, settings, _providers=fake_providers(commands=m.commands))
    assert invoke(rt, ["release", "ios", "0.9.53"]).exit_code == 2
    with using(NativeRuntime(rt.providers, rt.settings, rt.project)):
        lines = release_ios.after_accepted(world(), "0.9.53")
    assert lines[0].startswith("IOS NOT-CLEAN 0.9.53: `sm release ios` checks out the release tag")


def test_summary_and_latest_receipt(tmp_path):
    assert ios.summary(None) == ""
    assert ios.latest_receipt(tmp_path / "none") is None
    (tmp_path / "ios").mkdir()
    (tmp_path / "ios" / "v0.9.9.json").write_text(json.dumps({"version": "0.9.9", "clean": True, "build": "5", "state": "VALID"}))
    (tmp_path / "ios" / "v0.9.10.json").write_text(json.dumps({"version": "0.9.10", "clean": False, "reason": "x"}))
    (tmp_path / "ios" / "v0.9.11.json").write_text("broken")
    (tmp_path / "ios" / "notes.txt").write_text("")
    assert ios.latest_receipt(tmp_path)["version"] == "0.9.10"
    assert ios.summary(ios.latest_receipt(tmp_path)) == "ios: v0.9.10 NOT-CLEAN: x; sm release ios 0.9.10"
    assert ios.summary({"version": "0.9.9", "clean": True, "build": "5", "state": "VALID"}) == "ios: v0.9.9 TestFlight build 5 VALID, in the internal group"
    assert ios.summary({"version": "0.9.9", "clean": True, "build": "5", "beta": "IN_BETA_TESTING"}).endswith("build 5 IN_BETA_TESTING, in the internal group")
    assert rules.ios_alarm({"version": "0.9.9", "clean": True}, 0) is None and rules.ios_alarm(None, 0) is None
    running = {"version": "0.9.9", "clean": False, "running": True, "at": "2026-11-21T10:00:00+00:00"}
    start = 1_795_255_200.0  # 2026-11-21T10:00:00Z
    assert rules.ios_alarm(running, start + 3599) is None
    assert rules.ios_alarm(running, start + 3600)["key"] == "ios:0.9.9:stuck"
    assert ios.summary(running) == "ios: v0.9.9 TestFlight step running since 2026-11-21T10:00:00+00:00"
    assert ios.parse_status("garbage\n" + row("None", 3)) == [{"short": "None", "build": "3", "state": "VALID", "expired": False, "in_group": True}]
    assert ios.next_build([]) == "1"


def test_beta_waits_for_in_beta_testing(tmp_path):
    m = Mini(tmp_path)
    m.beta = ["PROCESSING", "IN_BETA_TESTING"]
    out = m.run()
    assert out.exit_code == 0 and m.rt.providers.clock.slept == [30]


def test_the_tool_can_come_from_a_ref(tmp_path):
    m = Mini(tmp_path, ios_tool_ref="origin/main")
    tools = []

    def record(argv):
        tools.append(argv[4])
        return Mini.testflight(m, argv)
    m.commands.on(["uv", "run", "--quiet", "--script"], record)
    assert m.run().exit_code == 0
    assert tools and all(tool.endswith("/work/testflight") for tool in tools)
    m.fail["show"] = Result(128, "", "bad ref")
    out = m.run()
    assert out.exit_code == 1 and "apple/scripts/testflight is not at origin/main" in out.stdout


def test_a_running_receipt_is_written_first(tmp_path):
    m = Mini(tmp_path)
    seen = []
    m.commands.on(["git", "-C", str(tmp_path / "core"), "rev-parse"], lambda argv: seen.append(m.receipt()) or Result(0, SHA + "\n"))
    assert m.run().exit_code == 0
    assert seen[0]["running"] is True and seen[0]["clean"] is False
