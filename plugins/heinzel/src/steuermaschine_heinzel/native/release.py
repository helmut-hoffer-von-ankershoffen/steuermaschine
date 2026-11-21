"""Releases, the write commands: the resumable step runner, dropping a failed candidate, releasing a plugin.

The logic is the moved step runner in `release_lib/runner.py` (see `port/move.py verify`): its argument checks, its
locks, its receipts and its steps are unchanged. This module turns typed options into the runner's argument list,
runs it in process, shows its progress and returns a result model. Nothing here starts an old script.

Review unit of every command here: this file, `release_lib/`, and `queue_lib/` (the runner merges through the queue's
merge train, reads GitHub through the queue's shared library and Core's release policy through core_policy).
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from pydantic import Field

from steuermaschine import WAITING
from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime
from steuermaschine.protocols import Busy

from . import mini, release_ios
from .go import world
from .queue_lib import hzlib
from .release_lib.binding import moved
from .release_lib.names import translate

EXACT = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
STEP = re.compile(r"^ok {3}(?P<name>.+?)(?P<already> \(already\))?$")


# For a pipeline that drives one release step at a time (`sm release cut`): the steps of a core release in the order
# the runner takes them, as the runner prints them, each with the functions of `runner()` that answer "is it done" and
# "do it". The pipeline holds the runner's lock itself: `acquire_global_lock` before the tag, `route_to_tail(version)`
# past it (`tail_receipt(version).exists()`), `free_global_lock` / `free_tail_lock` afterwards; `state()` and
# `remember(**fields)` read and write the receipt. `run(runner(), [...])` stays the way to take all steps at once.
CUT_STEPS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("freeze", ("has_tag", "owned_hold_exists"), ("acquire_snapshot_freeze",)),
    ("main gate (train path)", ("has_tag", "main_candidate_gate"), ()),
    ("main tip green", ("has_tag", "main_tip_green"), ()),
    ("release PR open", ("has_tag", "release_pr"), ("open_release_pr",)),
    ("previous release accepted", ("has_tag", "previous_accepted"), ()),
    ("release PR merged", ("has_tag", "release_pr"), ("merge_release_pr",)),
    ("tag", ("has_tag",), ()),
    ("merge freeze lifted at tag", (), ("lift_freeze_at_tag",)),
    ("release published", ("release_published",), ()),
    ("PyPI", ("pypi_has",), ("compose_beside_pypi", "pypi_step")),
    ("compose", ("published_release",), ("compose_step",)),
    ("acceptance", (), ("pinned_snapshot", "acceptance_step")),
    ("verdict", ("verdict",), ("publish_acceptance", "promote_release", "remember")),
)


class Progress(io.TextIOBase):
    """Standard output of the moved runner: every finished line is shown at once (a release runs for hours) and kept."""

    def __init__(self, show):
        self.show, self.lines, self.rest = show, [], ""

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        *done, self.rest = (self.rest + text).split("\n")
        for line in done:
            self.lines.append(translate(line))
            self.show(self.lines[-1])
        return len(text)

    def finish(self) -> list[str]:
        if self.rest:
            self.write("\n")
        return self.lines


def runner() -> Any:
    """A fresh copy of the moved step runner. Its waits sleep on the clock provider; everything else is unchanged."""
    module = moved("release_lib.runner")
    bound = types.ModuleType(hzlib.__name__)
    bound.__dict__.update(vars(hzlib))
    bound.sleep = runtime().providers.clock.sleep  # type: ignore[attr-defined]
    module.hzlib = bound
    return module


def run(module, argv: list[str]) -> tuple[int, list[str], list[str]]:
    """Run the moved command line in process: (exit code, stdout lines as shown, stderr lines). A refused argument
    combination (the moved parser's own check) is a refusal with exit 2."""
    say, shown_on = runtime().say, sys.stderr

    def show(text: str) -> None:  # progress goes where it went before the runner's own stderr was captured
        with contextlib.redirect_stderr(shown_on):
            say(text)

    out, err = Progress(show), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = int(module.main(argv) or 0)
        except SystemExit as stop:
            code = stop.code if isinstance(stop.code, int) else 1
    lines, errors = out.finish(), [translate(line) for line in err.getvalue().splitlines()]
    if code == 2 and any(": error: " in line for line in errors):
        raise Refuse(next(line.split(": error: ", 1)[1] for line in errors if ": error: " in line), "sm release resume --help", code=2)
    return code, lines, errors


def receipt_of(module, version: str) -> tuple[str, dict]:
    """(where the release stands, its receipt): `tail` past its tag, `candidate` before it, `none`."""
    for where, path in (("tail", module.tail_receipt(version)), ("candidate", module.global_receipt())):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("version") == version:
            return where, data
    return "none", {}


# ----------------------------------------------------------------------------- release resume
class ReleaseStep(Model):
    name: str
    already: bool = Field(False, description="The step was found done before this call.")


class ReleaseRun(Model):
    """What one call of the step runner did."""

    action: str = Field(description="resume, pause, prepare-only, absorb, reject, candidate-capacity, migrate-legacy, plugin or pr.")
    version: str = ""
    exit: int
    stopped: str = Field("", description="Why the runner stopped; the same command continues after that is resolved.")
    steps: list[ReleaseStep] = []
    stands: str = Field("none", description="candidate: before its tag (one at a time); tail: past its tag; none: no receipt.")
    finished: bool = False
    accepted: bool | None = Field(None, description="The verdict of a finished release; None while it is unfinished.")
    acceptance_run: int | None = None
    lines: list[str] = []
    live: list[str] = Field([], description="With --live: the lines of `sm mini update` after ACCEPTED; the last is LIVE-OK X.Y.Z or NOT-LIVE: reason.")
    ios: list[str] = Field([], description="With --live: the lines of `sm release ios` after ACCEPTED; NOT-CLEAN is an alarm and leaves the exit code alone.")

    def render(self) -> list[str]:
        return closing(self.lines, self.stopped) + self.live + self.ios

    @property
    def exit_code(self) -> int:
        return self.exit


def closing(lines: list[str], stopped: str) -> list[str]:
    """The result in text form: why the runner stopped, else its last statement with the detail lines below it.
    The steps before it were shown as they happened (progress)."""
    if stopped:
        return [f"STOPPED: {stopped}"]
    last = max((i for i, line in enumerate(lines) if line and not line.startswith("  ")), default=None)
    return [] if last is None else lines[last:]


def outcome(module, action: str, version: str, code: int, lines: list[str], errors: list[str]) -> ReleaseRun:
    for line in lines:
        if not EXACT.fullmatch(version) and (found := re.fullmatch(r"release v([0-9.]+)", line)):
            version = found[1]
    stands, receipt = receipt_of(module, version) if EXACT.fullmatch(version) else ("none", {})
    stopped = next((line.removeprefix("STOPPED: ") for line in errors if line.startswith("STOPPED: ")), "")
    steps = [ReleaseStep(name=found["name"], already=bool(found["already"])) for line in lines if (found := STEP.match(line))]
    accepted = receipt.get("accepted") if receipt.get("finished") else None
    run_id = receipt.get("acceptance_run")
    return ReleaseRun(action=action, version=version, exit=code, stopped=stopped, steps=steps, stands=stands, finished=bool(receipt.get("finished")),
                      accepted=accepted if isinstance(accepted, bool) else None, acceptance_run=run_id if isinstance(run_id, int) else None,
                      lines=lines + [line for line in errors if not line.startswith("STOPPED: ")])


@native("release resume", writes=True, replaces=("hz-release",), help=H(
    "Take one core release through its steps; every step is checked first, so the same command continues where it stopped.",
    "Use when a release is due, stopped or paused; the release train calls it for every cut.",
    "freezes merges, opens and merges the release PR, dispatches PyPI, compose and acceptance, writes receipts, holds and the public verdict.",
    "sm release resume 0.9.40 --intro \"what changed for users\" --snapshot-acceptance",
    "sm release status   (after ACCEPTED: sm release verify VERSION, then sm release after VERSION)",
    "Steps: freeze, main gate, release PR open, previous release accepted, release PR merged, tag, freeze lifted, release\n"
    "published, PyPI, compose, acceptance, verdict. Exit 0: ACCEPTED. Exit 1: stopped (the line says why) or NOT ACCEPTED.\n"
    "VERSION is X.Y.Z, `patch` (the unfinished release, else the next patch) or `migrate-legacy`.\n"
    "--live: after ACCEPTED, `sm mini update --minimum VERSION` too; exit 0 only at LIVE-OK (the release train's runs use it).\n"
    "It also runs `sm release ios VERSION`: a TestFlight failure is IOS NOT-CLEAN and an alarm, never an un-publish.\n"
    "One action besides resuming at a time: --pause, --prepare-only, --absorb, --reject-release, --candidate-capacity.\n"
    "The candidate gate needs the candidate's first attempt green: never rerun a failed candidate job to pass it.\n"
    "A failed compose run is replaced once by a fresh compose (same inputs) when plugins main moved past it and a plugin\n"
    "version changed since is on PyPI; the receipt keeps the failed run under compose_replaced. Otherwise it stops."))
def release_resume(
    version: str = typer.Argument(..., metavar="VERSION", help="X.Y.Z, `patch`, or `migrate-legacy`."),
    intro: str = typer.Option("", "--intro", help="What changed for users; needed only to open the release PR."),
    snapshot_acceptance: bool = typer.Option(False, "--snapshot-acceptance", help="Pin acceptance to the tag-pinned public snapshot; the release owns its hold."),
    timeout: int | None = typer.Option(None, "--timeout", metavar="MINUTES", help="Minutes per step (default 120)."),
    wait_previous: int | None = typer.Option(None, "--wait-previous", metavar="MINUTES", help="Minutes to wait for the previous release's verdict (default --timeout)."),
    candidate_gate: bool = typer.Option(False, "--candidate-gate", help="Train path only: main may be unfinished or red by KNOWN failures; the candidate stays the gate."),
    refresh: bool = typer.Option(False, "--refresh", help="Tag-on-green-main only: after a refused candidate and its fix, cut the refresh commit at the same version."),
    pause: bool = typer.Option(False, "--pause", help="Pause an exact pre-tag release and remove only the hold it owns."),
    prepare_only: bool = typer.Option(False, "--prepare-only", help="Resume the paused hold and PR readiness only, then exit."),
    absorb: bool = typer.Option(False, "--absorb", help="Record that the next release absorbs this finished NOT ACCEPTED release; needs --reason."),
    reason: str | None = typer.Option(None, "--reason", help="With --absorb: the fix PR (\"fixed in N+1 by #PR\") or `review: ...`. With --reject-release: the written reason."),
    acceptance_ref: str | None = typer.Option(None, "--acceptance-ref", metavar="REF", help="An explicit corrected acceptance harness ref (main)."),
    replace_acceptance_run: int | None = typer.Option(None, "--replace-acceptance-run", metavar="RUN", help="Preserve and replace this exact completed rejected acceptance attempt."),
    replace_reason: str | None = typer.Option(None, "--replace-reason", help="Written infra-only reason; needed when the rejected run was rerun after its bound attempt."),
    waive_job: str | None = typer.Option(None, "--waive-job", metavar="JOB=BEAD", help="Owner waiver for one in-date KNOWN flaky candidate job; needs --owner-quote."),
    owner_quote: str | None = typer.Option(None, "--owner-quote", help="The owner's own words that authorize --waive-job."),
    use_candidate_composite: int | None = typer.Option(None, "--use-candidate-composite", metavar="RUN", help="Verify audited original plus replacement execution; never first-attempt proof."),
    candidate_capacity: str | None = typer.Option(None, "--candidate-capacity", metavar="prepare|request", help="Infrastructure-only candidate audit or request; no release action."),
    candidate_run: int | None = typer.Option(None, "--candidate-run", metavar="RUN", help="With --candidate-capacity: the candidate run."),
    expected_audit_sha256: str | None = typer.Option(None, "--expected-audit-sha256", help="With --candidate-capacity request: the digest of the prepared audit."),
    expected_receipt_sha256: str | None = typer.Option(None, "--expected-receipt-sha256", help="With --candidate-capacity or --reject-release: the digest of the receipt."),
    expected_freeze_token: str | None = typer.Option(None, "--expected-freeze-token", help="With --candidate-capacity or --reject-release: the token of the owned hold."),
    reject_release: bool = typer.Option(False, "--reject-release", help="Close the exact reviewed published failure as REJECTED, never accepted."),
    failed_compose_run: int | None = typer.Option(None, "--failed-compose-run", metavar="RUN", help="With --reject-release: the failed compose run."),
    failed_compose_attempt: int | None = typer.Option(None, "--failed-compose-attempt", metavar="N", help="With --reject-release: its attempt."),
    expected_compose_head: str | None = typer.Option(None, "--expected-compose-head", metavar="SHA", help="With --reject-release: the compose head."),
    expected_core_source: str | None = typer.Option(None, "--expected-core-source", metavar="SHA", help="With --reject-release: the core source commit."),
    live: bool = typer.Option(False, "--live", help="After ACCEPTED: update the Mini and prove it (sm mini update); exit 0 only at LIVE-OK."),
) -> ReleaseRun:
    if absorb:  # the runner takes the version as the value of --absorb, and nothing but --reason besides
        argv, action = ["--absorb", version], "absorb"
    else:
        argv = [version]
        action = next((name for name, on in (("pause", pause), ("prepare-only", prepare_only), ("reject", reject_release), ("candidate-capacity", bool(candidate_capacity)),
                                             ("migrate-legacy", version == "migrate-legacy")) if on), "resume")
    argv += [flag for flag, on in (("--snapshot-acceptance", snapshot_acceptance), ("--candidate-gate", candidate_gate), ("--refresh", refresh), ("--pause", pause),
                                   ("--prepare-only", prepare_only), ("--reject-release", reject_release)) if on]
    for flag, value in (("--intro", intro or None), ("--timeout", timeout), ("--wait-previous", wait_previous), ("--reason", reason), ("--acceptance-ref", acceptance_ref),
                        ("--replace-acceptance-run", replace_acceptance_run), ("--replace-reason", replace_reason), ("--waive-job", waive_job), ("--owner-quote", owner_quote),
                        ("--use-candidate-composite", use_candidate_composite), ("--candidate-capacity", candidate_capacity), ("--candidate-run", candidate_run),
                        ("--expected-audit-sha256", expected_audit_sha256), ("--expected-receipt-sha256", expected_receipt_sha256),
                        ("--expected-freeze-token", expected_freeze_token), ("--failed-compose-run", failed_compose_run), ("--failed-compose-attempt", failed_compose_attempt),
                        ("--expected-compose-head", expected_compose_head), ("--expected-core-source", expected_core_source)):
        if value is not None:
            argv += [flag, str(value)]
    if live and (action != "resume" or not EXACT.fullmatch(version)):
        raise Refuse("--live goes with resuming an exact X.Y.Z only", "sm release resume 0.9.53 --live", code=2)
    module = runner()
    code, lines, errors = run(module, argv)
    done = outcome(module, action, version, code, lines, errors)
    if live and done.exit == 0 and done.finished and done.accepted:
        proof = mini.update(world(), done.version)
        done.live, done.exit = proof.render(), proof.exit_code
        done.ios = release_ios.after_accepted(world(), done.version)
    return done


class AcceptanceCapacity(Model):
    version: str
    failed_run: int
    audit_sha256: str
    replacement_marker: str | None
    head: str
    mac_job: int
    linux_artifact: int

    def render(self) -> list[str]:
        if self.replacement_marker is None:
            return [f"Prepared capacity audit {self.audit_sha256} for {self.version} run {self.failed_run}",
                    f"Tagged head {self.head}. Unacquired Mac job {self.mac_job}. Linux receipt {self.linux_artifact}."]
        return [f"Requested fresh acceptance for {self.version} with marker {self.replacement_marker}",
                f"Original rejection {self.failed_run} preserved. Audit {self.audit_sha256}."]


@native("release acceptance-capacity", writes=True, help=H(
    "Audit or request one fresh tagged public acceptance run after an unacquired Mac runner.",
    "Use when the exact published release was rejected and its Mac job never received a runner.",
    "prepare reads evidence. request archives the rejection and dispatches one fresh run on the same tagged snapshot.",
    "sm release acceptance-capacity 0.9.40 --mode prepare --failed-run 123 --failed-attempt 1 --receipt-sha256 SHA",
    "Review the audit digest, then use --mode request --audit-sha256 SHA with the same receipt. Resume normally for both OS verdicts.",
))
def release_acceptance_capacity(
    version: str = typer.Argument(..., metavar="VERSION", help="Exact Core release version X.Y.Z."),
    mode: str = typer.Option(..., "--mode", metavar="prepare|request", help="Audit only or request after review."),
    failed_run: int = typer.Option(..., "--failed-run", min=1, help="Original rejected acceptance run ID."),
    failed_attempt: int = typer.Option(..., "--failed-attempt", min=1, help="The bound original attempt. Must be 1."),
    receipt_sha256: str = typer.Option(..., "--receipt-sha256", help="SHA-256 of the unchanged release tail receipt."),
    audit_sha256: str | None = typer.Option(None, "--audit-sha256", help="Reviewed audit digest. Required for request."),
) -> AcceptanceCapacity:
    from .release_lib import acceptance_capacity
    from .release_lib.candidate_capacity import InvalidCapacity

    module = runner()
    if not EXACT.fullmatch(version) or mode not in ("prepare", "request") or (
        (mode == "request") != bool(audit_sha256)
    ) or (audit_sha256 is not None and not acceptance_capacity.DIGEST.fullmatch(audit_sha256)):
        raise Refuse("Use an exact release version and request only with the reviewed audit digest.",
                     "sm release acceptance-capacity --help")
    try:
        if not module.tail_receipt(version).is_file():
            raise module.Stop("the exact release tail receipt is absent")
        module.route_to_tail(version)
        module.rejection_guard(version)
        result = acceptance_capacity.recover(module, version, failed_run, failed_attempt, receipt_sha256, audit_sha256)
    except (module.Stop, InvalidCapacity) as exc:
        raise Refuse(f"acceptance capacity refused: {exc}", "sm release acceptance-capacity --help") from None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
        raise Refuse("Acceptance capacity evidence could not be authenticated.", "sm release acceptance-capacity --help") from exc
    finally:
        module.free_tail_lock()
        module.RECEIPT_OVERRIDE = None
    return AcceptanceCapacity(version=version, failed_run=failed_run, **result)


class ComposeReplacement(Model):
    version: str
    failed_run: int
    reviewed_head: str

    def render(self) -> list[str]:
        return [f"compose replacement requested for {self.version}; preserved failed run {self.failed_run}"]


@native("release replace-compose", writes=True, help=H(
    "Request one fresh compose after an exact failed run and independently reviewed test-only fix.",
    "Use when a completed compose failed because of a reviewed acceptance-test defect.",
    "records the failed compose and dispatches one fresh compose; does not accept a release.",
    "sm release replace-compose 0.9.40 --failed-run 123 --failed-attempt 1 --reviewed-head SHA --core-source SHA --receipt-sha256 DIGEST --reason 'review: test fixture fix'",
    "sm release resume VERSION --snapshot-acceptance",
    "Requires an exact unfinished tail, unchanged receipt, Core source and current reviewed Plugins main.\n"
    "Only acceptance-test changes are permitted; the old run and full receipt are retained."))
def release_replace_compose(
    version: str = typer.Argument(..., metavar="VERSION", help="Exact Core release version X.Y.Z."),
    failed_run: int = typer.Option(..., "--failed-run", min=1, help="Exact failed compose run ID."),
    failed_attempt: int = typer.Option(..., "--failed-attempt", min=1, help="Exact failed compose attempt."),
    reviewed_head: str = typer.Option(..., "--reviewed-head", help="Independently reviewed Plugins main SHA."),
    core_source: str = typer.Option(..., "--core-source", help="Exact Core release tag commit SHA."),
    receipt_sha256: str = typer.Option(..., "--receipt-sha256", help="SHA-256 of the unchanged tail receipt."),
    reason: str = typer.Option(..., "--reason", help="Written reason identifying the independent review."),
) -> ComposeReplacement:
    from .release_lib import compose_harness

    module = runner()
    try:
        if not EXACT.fullmatch(version):
            raise module.Stop("an exact release version is required")
        if not module.tail_receipt(version).is_file():
            raise module.Stop("the exact release tail receipt is absent")
        module.route_to_tail(version)
        module.rejection_guard(version)
        compose_harness.replace(module, version, failed_run, failed_attempt, reviewed_head, core_source, receipt_sha256, reason)
    except (module.Stop, module.release_rejection.InvalidRejection, OSError, ValueError, TypeError, KeyError) as exc:
        raise Refuse(f"compose replacement refused: {exc}", "sm release replace-compose --help") from exc
    finally:
        module.free_tail_lock()
        module.RECEIPT_OVERRIDE = None
    return ComposeReplacement(version=version, failed_run=failed_run, reviewed_head=reviewed_head)


@native("release plugin", writes=True, replaces=("hz-release",), help=H(
    "Release a plugin whose version on the plugins repository's main is not on PyPI yet, and verify PyPI.",
    "Use after a plugin PR with a version bump merged.",
    "dispatches the plugins repository's release workflow (publish to PyPI) and writes the dispatch and its run ID into the\n"
    "plugin release receipt.",
    "sm release plugin audio", "sm release verify --package heinzel-audio VERSION --surface pypi",
    "A rerun follows only the run of its own dispatch (by its recorded ID), never the newest release run of another plugin.\n"
    "Success needs the version on PyPI AND that run's RELEASE_TAG equal to heinzel-NAME/vVERSION.\n"
    "A failed run is never dispatched again by itself (a failure can repeat; a second dispatch costs a CI run): after the fix,\n"
    "--redispatch dispatches once more; it refuses while the recorded run has not completed. Exit 0: released, 1: stopped."))
def release_plugin(name: str = typer.Argument(..., metavar="NAME", help="The plugin, with or without the `heinzel-` prefix."),
                   timeout: int | None = typer.Option(None, "--timeout", metavar="MINUTES", help="Minutes to wait for PyPI (default 120)."),
                   redispatch: bool = typer.Option(False, "--redispatch", help="Dispatch again after the recorded run completed without the release "
                                                   "(or an older sm left only a dispatch time).")) -> ReleaseRun:
    module = runner()
    code, lines, errors = run(module, ["--plugin", name, *(["--timeout", str(timeout)] if timeout is not None else []), *(["--redispatch"] if redispatch else [])])
    return outcome(module, "plugin", "", code, lines, errors)


@native("release pr", writes=True, help=H(
    "Push the reviewed, prepared release branch and open its release PR; never merges it.",
    "Use when the release branch release/vX.Y.Z was prepared in the release checkout and its exact head was reviewed\n"
    "(resume refuses to touch an existing branch and preserves it).",
    "pushes release/vX.Y.Z under the run lock with the release-only hook selectors, opens \"chore(release): vX.Y.Z\" with the\n"
    "release notes as body and checks the opened PR; writes the PR into this release's receipt.",
    "sm release pr 0.9.41 --head FULL_SHA --dry-run",
    "sm release resume VERSION --intro \"what changed for users\" --snapshot-acceptance   (it merges the PR)",
    "Checks before the push: no tag, no release PR in any state, the receipt is this release or none, the release checkout\n"
    "(setting release_dir) is on release/vX.Y.Z, clean, one commit on origin/main, only the reviewed release files and the\n"
    "version changed, its head is exactly --head, the release notes name every combined PR and are not empty, the\n"
    "remote branch is absent. After: the remote branch is exactly --head; the PR is open against main, titled\n"
    "\"chore(release): vX.Y.Z\", has a body and no auto-merge. Branches are never deleted. Exit 0: opened (or dry run passed)."))
def release_pr(version: str = typer.Argument(..., metavar="VERSION", help="The exact version X.Y.Z of the prepared release."),
               head: str = typer.Option(..., "--head", metavar="SHA", help="The full SHA of the reviewed release commit."),
               dry_run: bool = typer.Option(False, "--dry-run", help="Check everything, push nothing, open nothing.")) -> ReleaseRun:
    if not EXACT.fullmatch(version):
        raise Refuse(f"VERSION must be X.Y.Z, not {version!r}", "sm release pr --help", code=2)
    module = runner()
    code, lines, errors = run(module, [version, "--open-pr", "--reviewed-head", head, *(["--dry-run"] if dry_run else [])])
    return outcome(module, "pr", version, code, lines, errors)


@native("release repair-receipt", writes=True, replaces=("hz-release",), help=H(
    "Move plugin release stamps out of the core release receipt into their own file (one-time repair).",
    "Use when `sm release verify --mini` says the global release receipt has no exact version.",
    "with --apply: copies the core receipt to a backup, writes the stamps to the plugin receipt and removes them from the core\n"
    "receipt (which is then absent when nothing else was left, as after a tag handoff). Without --apply it writes nothing.",
    "sm release repair-receipt --dry-run", "sm release repair-receipt --apply",
    "The core release proof stays the tail receipt of each release (its version, finished, accepted, acceptance run): the\n"
    "repair never invents a core version and never touches a tail receipt."))
def release_repair_receipt(apply: bool = typer.Option(False, "--apply/--dry-run", help="Write the repair (default: print the plan only).")) -> ReleaseRun:
    module = runner()
    code, lines, errors = run(module, ["--repair-receipt", *(["--apply"] if apply else [])])
    return outcome(module, "repair-receipt", "", code, lines, errors)


# ----------------------------------------------------------------------------- release drop
CUT_KIND, CUT_ARCHIVE_KIND = "runs.release.cut", "runs.release.cut.dropped"


class ReleaseReplan(Model):
    """Read-only proof, or archived local cut replaced with a fresh full-route generation."""

    proof: dict[str, Any]

    def render(self) -> list[str]:
        action = "REPLANNED" if self.proof["live"] else "READY to replan"
        return [f"{action} {self.proof['version']}: {self.proof['head']} on {self.proof['base']}; main_ci=full",
                f"state {self.proof['state_sha256']}; archive {self.proof['archive'] or '(dry run)'}",
                "Next: sm release cut VERSION; fresh source, admission and publish reviews are required."]


@native("release replan", writes=True, help=H(
    "Replan a stopped local focused cut to full CI before any external release work.",
    "Use after a pre-external input/evidence refusal, never after a candidate or admission attempt.",
    "with --live: archives the exact local cut state and replaces it with a fresh full-route generation; no remote writes, artifact or receipt edits.",
    "sm release replan 0.9.51 --head SHA --packet /new/packet.json --main-ci full --reason 'full candidate requested'",
    "Repeat with --live after inspecting the proof; then sm release cut VERSION for fresh reviews.",
    "Requires an identical pair packet in a separate artifact-free output directory. Proves no admission receipt/journal, release receipt, tag, publication, PR, branch or candidate run exists for this release. Unknown refuses. --lane never creates a fresh run for an existing version."))
def release_replan(version: str = typer.Argument(..., metavar="VERSION", help="Exact version of the stopped pre-external cut."),
                   head: str = typer.Option(..., "--head", help="Exact reviewed release commit."),
                   packet: Path = typer.Option(..., "--packet", help="Identical pair packet in a separate fresh output directory."),
                   main_ci: str = typer.Option(..., "--main-ci", help="Only full is supported."),
                   reason: str = typer.Option(..., "--reason", help="Why the pre-external cut must be replanned."),
                   live: bool = typer.Option(False, "--live/--dry-run", help="Archive and replace local cut state (default: prove only).")) -> ReleaseReplan:
    from .admission_lib.focused_admit import AdmissionError
    from .release_lib import replan

    if not EXACT.fullmatch(version) or main_ci != "full":
        raise Refuse("replan needs an exact release version and --main-ci full")
    module = runner()
    try:
        with runtime().providers.store.lock(f"release.cut.{version}"):
            proof = replan.execute(runtime(), module, version, head, packet, reason, live)
        return ReleaseReplan(proof=proof)
    except Busy:
        raise Refuse("release cut is advancing; replan changed nothing", code=WAITING) from None
    except (module.Stop, AdmissionError, OSError, ValueError, KeyError, TypeError) as exc:
        raise Refuse(f"release replan refused: {exc}") from None


def reset_cut_state(version: str) -> str:
    """After a drop: the saved state of `sm release cut VERSION` (steps done, the adopted prepare, the inputs) belongs to
    the dropped candidate. Copy it to the archive kind first, then delete it, so a later cut starts fresh and the old
    run stays readable. Returns the archive key, or "" when there was no cut state."""
    store = runtime().providers.store
    state = store.load(CUT_KIND, version)
    if state is None:
        return ""
    key = f"{version}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    store.save(CUT_ARCHIVE_KIND, key, state)
    store.delete(CUT_KIND, version)
    return f"{CUT_ARCHIVE_KIND}/{key}"


class ReleaseDrop(Model):
    """A candidate that was provably before publication, dropped so that a fresh cut starts clean."""

    version: str
    dropped: bool
    exit: int
    stopped: str = Field("", description="The exact unmet condition; nothing was dropped.")
    archive: str = Field("", description="Where the receipt went, with the reason and the evidence.")
    hold: str = Field("", description="owned: the release's own hold was removed; paused: a pause had removed it; none: it owned no hold.")
    closed_pull_requests: list[int] = Field([], description="Release PRs this drop closed (--close-pr).")
    branch_deleted: bool = False
    other_holds_remain: bool = False
    cut_state_archived: str = Field("", description="Where the state of `sm release cut VERSION` went (kept, not deleted); a later cut starts fresh.")
    lines: list[str] = []

    def render(self) -> list[str]:
        return [f"STOPPED: {self.stopped}"] if self.stopped else [line for line in self.lines if line.startswith(("DROPPED ", "note: "))]

    @property
    def exit_code(self) -> int:
        return self.exit


@native("release drop", writes=True, replaces=("hz-release",), help=H(
    "Drop a candidate that is provably before publication, so that a fresh cut of the same version starts clean.",
    "Use when a candidate failed and will be cut again (first-attempt rule: a failed candidate is never rerun green).",
    "removes ONLY the hold this release owns and archives its receipt; with --close-pr it closes the release PR and deletes its branch.",
    "sm release drop 0.9.40 --reason \"candidate red: Coverage, fix in #812\" --close-pr",
    "sm release resume VERSION --intro ...   (the fresh cut)",
    "Proves first, from typed replies: no tag, no GitHub release, PyPI answers 404, no merged release PR, the release PR\n"
    "closed or absent, the branch absent, no workflow run on that branch still active. A transport failure is never read as\n"
    "absence. It cancels no run: wait for a running candidate or cancel it yourself. Never touches the incident hold or\n"
    "another release's hold. Not for a release past its tag (reject or absorb), a finished one, or tag-on-green-main."))
def release_drop(version: str = typer.Argument(..., metavar="VERSION", help="The exact version X.Y.Z of the candidate."),
                 reason: str = typer.Option(..., "--reason", help="Why the candidate is dropped; kept with the archived receipt."),
                 close_pr: bool = typer.Option(False, "--close-pr", help="Close the open release PR and delete its branch first, then prove everything again.")) -> ReleaseDrop:
    # The cut's own engine lock, held for the whole drop: a cut that is advancing saves its state after every step and would
    # bring the dropped run back. Busy: nothing is dropped, wait and run the same command again.
    try:
        with runtime().providers.store.lock(f"release.cut.{version}"):
            return _drop(version, reason, close_pr)
    except Busy:
        raise Refuse(f"`sm release cut {version}` is advancing and holds its lock: nothing was dropped. Run the same command again when it has stopped",
                     f"sm release drop {version} --reason ...", code=WAITING) from None


def _drop(version: str, reason: str, close_pr: bool) -> ReleaseDrop:
    module = runner()
    code, lines, errors = run(module, [version, "--drop-candidate", "--reason", reason, *(["--close-pr"] if close_pr else [])])
    stopped = next((line.removeprefix("STOPPED: ") for line in errors if line.startswith("STOPPED: ")), "")
    record: dict = {}
    done = next((line for line in lines if line.startswith(f"DROPPED {version}:")), "")
    archive = found[1] if (found := re.search(r"receipt archived at (\S+?)\. ", done)) else ""
    if archive:
        with contextlib.suppress(OSError, ValueError):
            record = json.loads(Path(archive).read_text()).get("drop") or {}
    evidence = record.get("evidence") or {}
    archived_cut = ""
    if done:
        try:
            archived_cut = reset_cut_state(version)
        except OSError as exc:  # the drop itself is done: say so, and that the cut state must be cleared by hand
            lines = [*lines, f"note: the drop is done, but the state of `sm release cut {version}` could not be archived ({exc}); delete it before a new cut"]
    if archived_cut:
        lines = [*lines, f"note: the state of `sm release cut {version}` was archived at {archived_cut}; a later cut starts fresh"]
    return ReleaseDrop(version=version, dropped=bool(done), exit=code, stopped=stopped, archive=archive, hold=str(record.get("hold") or ""),
                       closed_pull_requests=list(evidence.get("closed_by_drop") or []), branch_deleted=bool(evidence.get("branch_deleted_by_drop")),
                       cut_state_archived=archived_cut, other_holds_remain=any(line.startswith("note: other merge holds remain") for line in lines),
                       lines=lines + [e for e in errors if not e.startswith("STOPPED: ")])
