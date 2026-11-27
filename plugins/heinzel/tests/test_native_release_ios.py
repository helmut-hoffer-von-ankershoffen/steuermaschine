"""release ios, native: the scripted Mini (git, xcodebuild, the tag's testflight and verifier) in a temporary state
directory. No test starts a process, reads the network or sleeps."""

import hashlib
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
        c.on(["uv", "run", "--script"], lambda argv: Result(0, json.dumps({"app": "test-app", "builds": {
            r["id"]: dict(r, short=None if r["short"] == "None" else r["short"]) for r in ios.parse_status("\n".join(self.asc))}})))
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
        ext = app / "PlugIns/HeinzelWidgets.appex"
        ext.mkdir(parents=True)
        for bundle in (app, ext):
            (bundle / "fixture").write_bytes(b"signed executable fixture")
            (bundle / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": self.archive_version or self.version,
                                                                "CFBundleExecutable": "fixture", "CFBundleVersion": build}))
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
    m.processing = 1  # the first confirmation read still says PROCESSING
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


def official_release(m, fault=""):
    from steuermaschine_heinzel.native.queue_lib import hzlib

    tag = f"v{m.version}"
    root = f"repos/{hzlib.CORE}/"
    get = ["gh", "api", "--hostname", "github.com"]
    marker = {"schema": 1, "project": "heinzel", "version": m.version, "accepted": True, "acceptance_run": 17}
    manifest = {"schema": 1, "project": "heinzel", "version": m.version, "commit": SHA}
    ref = {"ref": f"refs/tags/{tag}", "object": {"type": "commit", "sha": SHA}}
    release = {"tag_name": tag, "draft": False, "prerelease": False, "assets": []}
    if fault == "rejected":
        marker["accepted"] = False
    if fault == "wrong-version":
        marker["version"] = "0.9.56"
    if fault == "wrong-project":
        marker["project"] = "unrelated"
    if fault == "wrong-commit":
        manifest["commit"] = "b" * 40
    if fault == "wrong-ref":
        ref["ref"] = "refs/heads/" + tag
    if fault == "wrong-remote-sha":
        ref["object"]["sha"] = "c" * 40
    if fault == "unpublished":
        release["draft"] = True
    if fault in ("annotated", "wrong-peel"):
        ref["object"] = {"type": "tag", "sha": "f" * 40}
        annotated = {"sha": "f" * 40 if fault == "annotated" else "d" * 40, "object": {"type": "commit", "sha": SHA}}
        m.commands.on([*get, root + "git/tags/" + "f" * 40], Result(0, json.dumps(annotated)))
    if fault.startswith("hosted"):
        wheel, profile = "1" * 64, "2" * 64
        marker.update(wheel_sha256=wheel, profile_sha256=profile)
        release["assets"].append({"name": f"heinzel-{m.version}-py3-none-any.whl", "digest": "sha256:" + wheel})
        app = {"tag_name": f"app-{tag}", "draft": False, "prerelease": False, "assets": [
            {"name": "standard.json", "digest": "sha256:" + (profile if fault != "hosted-profile" else "3" * 64)}]}
        m.commands.on([*get, f"repos/{hzlib.PLUGINS}/releases/tags/app-{tag}"], Result(0, json.dumps(app)))
    for i, (name, doc) in enumerate([("acceptance.json", marker), ("artifact-manifest.json", manifest)], start=1):
        raw = json.dumps(doc) + "\n"
        digest = hashlib.sha256(raw.encode()).hexdigest()
        if name == "acceptance.json" and fault == "missing":
            continue
        asset = {"name": name, "id": i, "digest": "sha256:" + ("0" * 64 if name == "acceptance.json" and fault == "wrong-digest" else digest)}
        release["assets"].append(asset)
        if name == "acceptance.json" and fault == "duplicate":
            release["assets"].append(asset)
        m.commands.on([*get, root + f"releases/assets/{i}", "-H", "Accept: application/octet-stream"], Result(0, raw))
    m.commands.on([*get, root + f"releases/tags/{tag}"], Result(0, json.dumps(release)))
    m.commands.on([*get, root + f"git/ref/tags/{tag}"], Result(0, json.dumps(ref)))


@pytest.mark.parametrize("fault", ["missing", "rejected", "wrong-version", "wrong-project", "wrong-digest", "duplicate",
                                   "wrong-commit", "wrong-ref", "wrong-remote-sha", "wrong-peel", "unpublished", "hosted-profile"])
def test_replacement_refuses_before_asc_when_official_acceptance_is_unbound(tmp_path, fault):
    m = Mini(tmp_path, version="0.9.57")
    m.asc = [row(m.version, 1)]
    official_release(m, fault)
    out = invoke(m.rt, ["release", "ios", m.version, "--supersede-build-id", "x1", "--expected-revision", SHA])
    assert out.exit_code == 1 and not m.receipt()["clean"]
    assert not any(cmd[0] in {"uv", "xcodebuild"} or "add" in cmd for cmd in m.commands.calls)
    assert not (m.state / "ios/submissions").exists() and not m.notes


@pytest.mark.parametrize("evidence", ["annotated", "hosted-valid"])
def test_accepted_canonical_replacement_reads_proof_before_first_apple_command(tmp_path, evidence):
    m = Mini(tmp_path, version="0.9.57")
    official_release(m, evidence)
    m.asc = [row(m.version, 1)]
    out = invoke(m.rt, ["release", "ios", m.version, "--supersede-build-id", "x1", "--expected-revision", SHA])
    assert out.exit_code == 0, out.stdout
    calls = m.commands.calls
    first_apple = next(i for i, cmd in enumerate(calls) if cmd[0] in {"uv", "xcodebuild"})
    assert all(i < first_apple for i, cmd in enumerate(calls) if cmd[:2] == ["gh", "api"])
    assert len(m.uploads) == len(m.notes) == 1


def test_existing_unbound_rehearsal_requires_explicit_exact_replacement(tmp_path):
    m = Mini(tmp_path, version="0.9.57")
    m.asc = [row("0.9.56", 35), row(m.version, 1)]
    official_release(m)
    for flags in ([], ["--supersede-build-id", "x2", "--expected-revision", SHA],
                  ["--supersede-build-id", "x1"], ["--supersede-build-id", "x1", "--expected-revision", "b" * 40]):
        out = invoke(m.rt, ["release", "ios", m.version, *flags])
        assert out.exit_code == 1 and not m.receipt()["clean"]
        assert not m.notes and not m.uploads and not m.archived
    out = invoke(m.rt, ["release", "ios", m.version, "--supersede-build-id", "x1", "--expected-revision", SHA])
    assert out.exit_code == 0, out.stdout
    assert m.receipt()["build"] == "36"
    assert m.run().exit_code == 0
    assert len(m.uploads) == 1 and len(m.archived) == 1 and not m.receipt()["uploaded"]


def test_default_resume_of_replacement_rechecks_authority_before_apple(tmp_path):
    m = Mini(tmp_path, version="0.9.57", ios_wait_minutes=1)
    m.asc = [row(m.version, 1)]
    m.beta = ["READY_FOR_BETA_SUBMISSION"]
    official_release(m)
    flags = ["--supersede-build-id", "x1", "--expected-revision", SHA]
    assert invoke(m.rt, ["release", "ios", m.version, *flags]).exit_code == 1
    assert len(m.uploads) == 1 and not m.notes
    official_release(m, "rejected")
    m.commands.calls.clear()
    m.beta = ["IN_BETA_TESTING"]
    assert m.run().exit_code == 1 and not m.receipt()["clean"]
    assert not any(cmd[0] in {"uv", "xcodebuild"} for cmd in m.commands.calls)
    assert len(m.uploads) == 1 and not m.notes


def test_ambiguous_upload_never_submits_again_or_adopts_a_visible_build(tmp_path):
    m = Mini(tmp_path)
    m.fail["upload"] = Result(1, "", "timeout")
    assert m.run().exit_code == 1
    m.fail.clear()
    m.asc.insert(0, row(m.version, 32))
    assert m.run().exit_code == 1 and "uncertain upload" in m.receipt()["reason"]
    assert not m.notes and len(m.archived) == 1


def test_successful_upload_with_temporary_processing_delay_resumes_same_build(tmp_path):
    m = Mini(tmp_path, ios_wait_minutes=1)
    m.beta = ["READY_FOR_BETA_SUBMISSION"]
    assert m.run().exit_code == 1 and len(m.uploads) == 1
    m.beta = ["IN_BETA_TESTING"]
    assert m.run().exit_code == 0 and m.receipt()["build"] == "32" and len(m.uploads) == 1


def test_assigns_a_valid_build_outside_the_group(tmp_path):
    m = Mini(tmp_path)
    m.auto_group = False
    out = m.run()
    assert out.exit_code == 0 and "ios: assigned build 32 to the internal group" in out.stdout
    assert m.rt.providers.clock.slept == [30] * 4


def test_automatic_group_membership_catches_up_after_valid(tmp_path):
    m = Mini(tmp_path)
    m.auto_group = False
    m.beta = ["READY_FOR_BETA_SUBMISSION", "IN_BETA_TESTING"]
    verbs = []
    stale_reads = 0

    def propagate(argv):
        nonlocal stale_reads
        verb = argv[5]
        verbs.append(verb)
        result = m.testflight(argv)
        if verb == "status" and f"{m.version} (32) VALID expired=False NOT-in-group" in result.out:
            stale_reads += 1
            m.auto_group = stale_reads == 4
        return result

    m.commands.on(["uv", "run", "--quiet", "--script"], propagate)
    out = m.run()
    assert out.exit_code == 0
    assert "assign" not in verbs
    assert verbs.count("upload") == 1 and verbs.count("beta") == 2
    assert m.rt.providers.clock.slept == [30] * 5
    receipt = m.receipt()
    assert receipt["clean"] and receipt["in_group"] and receipt["beta"] == "IN_BETA_TESTING" and receipt["notes"]
    assert m.notes == ["Heinzel 0.9.53\n\n- Faster chats\n"]


def test_the_assignment_grace_does_not_extend_the_confirmation_deadline(tmp_path):
    m = Mini(tmp_path, ios_wait_minutes=1)
    m.auto_group = False
    clock = m.rt.providers.clock

    def late_status(argv):
        result = m.testflight(argv)
        if argv[5] == "status" and not m.rt.providers.clock.slept and "(32) VALID" in result.out:
            clock.sleep(55)  # first VALID row arrives five seconds before confirmation ends
        return result

    m.commands.on(["uv", "run", "--quiet", "--script"], late_status)
    out = m.run()
    assert out.exit_code == 1 and "not in the internal group" in out.stdout
    assert "assigned build" not in out.stdout
    assert not m.notes and clock.slept == [55, 5]


def test_unrelated_assignment_errors_still_fail_after_the_grace(tmp_path):
    m = Mini(tmp_path)
    m.auto_group = False
    m.fail["assign"] = Result(1, "", "GET /v1/apps/demo/betaGroups: HTTP 403 forbidden")
    out = m.run()
    assert out.exit_code == 1 and "HTTP 403 forbidden" in out.stdout
    assert not m.notes and not m.receipt()["clean"]
    assert m.rt.providers.clock.slept == [30] * 4


GROUP_POST = "POST /v1/betaGroups/example/relationships/builds: HTTP 422 "
GROUP_REFUSAL = {
    "code": "ENTITY_UNPROCESSABLE",
    "title": "Builds cannot be assigned to this internal group.",
    "detail": "Cannot add internal group to a build.",
}


@pytest.mark.parametrize("membership_appears", [True, False])
def test_known_assignment_refusal_requires_positive_membership_before_success(tmp_path, membership_appears):
    m = Mini(tmp_path, ios_wait_minutes=3)
    m.auto_group = False
    verbs = []

    def assign_race(argv):
        verb = argv[5]
        verbs.append(verb)
        if verb == "assign":
            m.auto_group = membership_appears
            return Result(1, "", GROUP_POST + json.dumps({"errors": [GROUP_REFUSAL]}, indent=2))
        return m.testflight(argv)

    m.commands.on(["uv", "run", "--quiet", "--script"], assign_race)
    out = m.run()
    assert verbs.count("assign") == verbs.count("upload") == 1
    assert m.receipt()["clean"] is membership_appears
    assert bool(m.notes) is membership_appears
    if membership_appears:
        assert out.exit_code == 0 and m.receipt()["in_group"] and m.receipt()["beta"] == "IN_BETA_TESTING"
        assert "assigned build" not in out.stdout
    else:
        assert out.exit_code == 1 and "did not appear in the group before the deadline" in out.stdout
        assert "beta" not in verbs


@pytest.mark.parametrize("other", [
    GROUP_POST + json.dumps({"errors": [{**GROUP_REFUSAL, "detail": "different rejection"}]}),
    GROUP_POST + json.dumps({"errors": [GROUP_REFUSAL, {"code": "FORBIDDEN"}]}),
    "GET /v1/betaGroups/example/relationships/builds: HTTP 422 " + json.dumps({"errors": [GROUP_REFUSAL]}),
    GROUP_POST + '{"errors":',
])
def test_other_assignment_errors_are_never_treated_as_propagation(other):
    assert not ios.internal_group_refusal(other)


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


def test_without_a_core_repository_it_is_not_clean_with_a_receipt(tmp_path):
    from steuermaschine.native import NativeRuntime, using
    from steuermaschine_heinzel.native.go import world
    m = Mini(tmp_path)
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {"legacy_state_dir": str(m.state)}}, Path("."))
    rt = Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, settings, _providers=fake_providers(commands=m.commands))
    out = invoke(rt, ["release", "ios", "0.9.53"])
    assert out.exit_code == 1 and "set ios_core_repo (or queue_core_repo)" in out.stdout
    with using(NativeRuntime(rt.providers, rt.settings, rt.project)):
        lines = release_ios.after_accepted(world(), "0.9.53")
    assert lines[-1].startswith("IOS NOT-CLEAN 0.9.53: `sm release ios` checks out the release tag")
    assert rules.ios_alarm(ios.latest_receipt(m.state), 0)["key"] == "ios:0.9.53"


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
    assert ios.parse_status("garbage\n" + row("None", 3)) == [{"short": "None", "build": "3", "state": "VALID", "expired": False, "in_group": True, "id": "x3"}]
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
        tools.append(argv[-1] if argv[5] in {"beta", "notes", "assign"} else argv[4])
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


def test_the_hook_never_raises_and_records_any_error(tmp_path, monkeypatch):
    from steuermaschine.native import NativeRuntime, using
    from steuermaschine_heinzel.native.go import world
    m = Mini(tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("xcodebuild vanished")
    monkeypatch.setattr(ios, "publish", broken)
    with using(NativeRuntime(m.rt.providers, m.rt.settings, m.rt.project)):
        lines = release_ios.after_accepted(world(), "0.9.53")
    assert lines == ["IOS NOT-CLEAN 0.9.53: RuntimeError: xcodebuild vanished"]
    assert m.receipt()["clean"] is False and "xcodebuild vanished" in m.receipt()["reason"]
    assert rules.ios_alarm(ios.latest_receipt(m.state), 0)["cls"] == "ALERT"


# ----------------------------------------------------------------------------- the verbs sm calls against the recorded tool help
def test_every_testflight_verb_sm_calls_is_in_the_recorded_help():
    """tests/fixtures/testflight-help.txt is `testflight -h` of the Core tag v0.9.54; no Core checkout is read."""
    import inspect
    import re

    helptext = (Path(__file__).parent / "fixtures" / "testflight-help.txt").read_text()
    offered = set(re.search(r"\{([a-z,]+)\}", helptext)[1].split(","))
    source = inspect.getsource(ios)
    called = set(re.findall(r"testflight\(\s*\"([a-z]+)\"", source)) | set(re.findall(r"\.testflight\(\s*\"([a-z]+)\"", source))
    assert {"status", "assign", "beta", "notes", "upload"} <= called  # the scan sees every verb the step uses
    assert called <= offered, f"sm calls testflight verbs the tool does not have: {sorted(called - offered)}"


def test_an_older_not_clean_is_superseded_once_a_newer_build_is_assigned(tmp_path):
    state = tmp_path / "state"
    ios.write_receipt(state, ios.IosResult(version="0.9.54", reason="stuck"), 1.0)
    assert ios.superseded_line(state) == ""
    ios.write_receipt(state, ios.IosResult(version="0.9.55", build="33", clean=True, beta="IN_BETA_TESTING"), 2.0)
    assert ios.superseded_line(state) == "ios: v0.9.54 NOT-CLEAN superseded by v0.9.55 build 33"
    assert ios.superseded_by(state, ios.read_receipt(state, "0.9.55")) is None


def test_after_accepted_ignores_old_verify_only_success_and_leaves_rehearsal_untouched(tmp_path):
    from steuermaschine.native import NativeRuntime, using
    from steuermaschine_heinzel.native.go import world

    m = Mini(tmp_path, version="0.9.57")
    ios.write_receipt(m.state, ios.IosResult(version=m.version, build="1", clean=True, revision=SHA), 0)
    m.asc.insert(0, row(m.version, 1))
    with using(NativeRuntime(m.rt.providers, m.rt.settings, m.rt.project)):
        output = release_ios.after_accepted(world(), m.version)
    assert output[-1].startswith("IOS NOT-CLEAN") and not m.receipt()["clean"]
    assert not m.uploads and not m.notes and not m.archived


def test_submitted_asc_id_drift_refuses_a_successful_old_number(tmp_path):
    m = Mini(tmp_path)
    assert m.run().exit_code == 0
    m.asc[0] = m.asc[0].replace("id=x32", "id=unrelated")
    assert m.run().exit_code == 1 and "build ID" in m.receipt()["reason"]
    assert len(m.notes) == len(m.uploads) == 1


def test_unknown_marketing_version_blocks_allocation(tmp_path):
    m = Mini(tmp_path)
    m.asc.append(row("None", 500))
    assert m.run().exit_code == 1 and "without a marketing version" in m.receipt()["reason"]
    assert not m.uploads and not m.notes


def asc_page(identifier, number, *, total=2, link=None):
    return {"meta": {"paging": {"total": total}}, "links": {"next": link},
            "included": [{"type": "preReleaseVersions", "id": "v", "attributes": {"version": "0.9.56"}}],
            "data": [{"type": "builds", "id": identifier, "attributes": {"version": number, "expired": False, "processingState": "VALID"},
                      "relationships": {"preReleaseVersion": {"data": {"type": "preReleaseVersions", "id": "v"}}}}]}


@pytest.mark.parametrize("fault", ["", "truncated", "repeat-id", "changed-total", "different-app", "external-host"])
def test_inventory_follows_only_complete_app_scoped_pages(fault):
    from steuermaschine_heinzel.native.release_lib.ios_inventory import inventory

    api = "https://asc.invalid"
    next_page = api + "/v1/builds?filter%5Bapp%5D=fixture&include=preReleaseVersion&cursor=123"
    if fault == "different-app":
        next_page = next_page.replace("fixture", "elsewhere")
    if fault == "external-host":
        next_page = next_page.replace("asc.invalid", "remote.invalid")
    pages = [asc_page("record-1", "2", link=None if fault == "truncated" else next_page),
             asc_page("record-1" if fault == "repeat-id" else "record-2", "400", total=3 if fault == "changed-total" else 2)]
    if fault:
        with pytest.raises(ValueError):
            inventory(lambda path: pages.pop(0), "fixture", api)
    else:
        result = inventory(lambda path: pages.pop(0), "fixture", api)
        assert result["builds"]["record-2"]["build"] == "400" and result["builds"]["record-1"]["short"] == "0.9.56"
        assert not pages


@pytest.mark.parametrize("verb", ["assign", "beta", "notes"])
def test_each_tagged_lookup_requires_the_pinned_build_at_operation_time(verb):
    from types import SimpleNamespace

    from steuermaschine_heinzel.native.release_lib.ios_inventory import scoped_command

    row = {"id": "submitted", "build": "36", "short": "0.9.57", "expired": False, "state": "VALID"}
    rows = [row]
    seen = []
    tool = SimpleNamespace(builds=lambda env: rows)

    def command(env, args):
        seen.extend(tool.builds(env))
    tool.cmd_assign = tool.cmd_beta = tool.cmd_notes = command
    for changed in ([row, dict(row, id="duplicate")], [dict(row, short="other")], [dict(row, id="unbound")], [dict(row, expired=True)], [dict(row, state="FAILED")]):
        rows = changed
        with pytest.raises(ValueError, match="submitted build identity"):
            scoped_command(tool, {}, verb, ["36", "fixture"], "0.9.57", "submitted")
    assert not seen
    rows = [row]
    scoped_command(tool, {}, verb, ["36", "fixture"], "0.9.57", "submitted")
    assert seen == [row]


def test_tag_helper_adapter_runs_the_extensionless_fixture_without_external_calls(tmp_path, monkeypatch, capsys):
    import runpy
    import sys

    from steuermaschine_heinzel.native.release_lib import ios_inventory

    script = tmp_path / "testflight"
    script.write_text("""
APP_ID = 'a'
API = 'https://fixture.invalid'
def load_env():
    return {}
def call(env, method, path):
    assert method == 'GET'
    return {'meta': {'paging': {'total': 0}}, 'data': [], 'links': {}}
def builds(env):
    return [{'id': 'owned', 'build': '42', 'short': '0.9.57', 'expired': False, 'state': 'VALID'}]
def cmd_notes(env, args):
    print(args.file, builds(env)[0]['id'])
cmd_assign = cmd_beta = cmd_notes
""")
    bridge = str(ios_inventory.__file__)
    monkeypatch.setattr(sys, "argv", [bridge, str(script)])
    runpy.run_path(bridge, run_name="__main__")
    assert json.loads(capsys.readouterr().out) == {"app": "a", "builds": {}}
    monkeypatch.setattr(sys, "argv", [bridge, "notes", "42", "fixture.txt", "0.9.57", "owned", str(script)])
    runpy.run_path(bridge, run_name="__main__")
    assert capsys.readouterr().out.strip() == "fixture.txt owned"


def test_failed_verification_can_retry_without_overwriting_the_old_archive(tmp_path):
    m = Mini(tmp_path)
    m.fail["verify"] = Result(1, "", "signed extension has wrong revision")
    assert m.run().exit_code == 1 and not m.uploads and not m.notes
    m.fail.clear()
    assert m.run().exit_code == 0 and len(m.archived) == 2 and len(m.uploads) == 1
