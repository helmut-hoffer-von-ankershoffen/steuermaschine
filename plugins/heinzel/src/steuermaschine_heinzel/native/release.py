"""Releases, the write commands: the resumable step runner, dropping a failed candidate, releasing a plugin.

The logic is the moved step runner in `release_lib/runner.py` (see `port/move.py verify`): its argument checks, its
locks, its receipts and its steps are unchanged. This module turns typed options into the runner's argument list,
runs it in process, shows its progress and returns a result model. Nothing here starts an old script.

Review unit of every command here: this file, `release_lib/` with the reviewed source files the candidate pins are
computed from, and `queue_lib/` (the runner merges through the queue's merge train and reads GitHub through the
queue's shared library).
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

import typer
from pydantic import Field

from steuermaschine import WAITING
from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime
from steuermaschine.protocols import Busy

from .queue_lib import hzlib
from .release_lib.binding import moved
from .release_lib.names import translate

# The review unit: the release library, EVERY reviewed source file the pins are computed from (they are data, not
# `.py`, so they are named one by one: replacing one of them is a re-pin and must void the verdict), the queue library.
UNIT = ("release_lib", "queue_lib")
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
    ("PyPI", ("pypi_has",), ("pypi_step",)),
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


def runner():
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

    def render(self) -> list[str]:
        return closing(self.lines, self.stopped)

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


@native("release resume", writes=True, replaces=("hz-release",), files=UNIT, help=H(
    "Take one core release through its steps; every step is checked first, so the same command continues where it stopped.",
    "Use when a release is due, stopped or paused; the release train calls it for every cut.",
    "freezes merges, opens and merges the release PR, dispatches PyPI, compose and acceptance, writes receipts, holds and the public verdict.",
    "sm release resume 0.9.40 --intro \"what changed for users\" --snapshot-acceptance",
    "sm release status   (after ACCEPTED: sm release verify VERSION, then sm release after VERSION)",
    "Steps: freeze, main gate, release PR open, previous release accepted, release PR merged, tag, freeze lifted, release\n"
    "published, PyPI, compose, acceptance, verdict. Exit 0: ACCEPTED. Exit 1: stopped (the line says why) or NOT ACCEPTED.\n"
    "VERSION is X.Y.Z, `patch` (the unfinished release, else the next patch) or `migrate-legacy`.\n"
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
    module = runner()
    code, lines, errors = run(module, argv)
    return outcome(module, action, version, code, lines, errors)


@native("release plugin", writes=True, replaces=("hz-release",), files=UNIT, review="release-resume.md", help=H(
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


@native("release pr", writes=True, files=UNIT, help=H(
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


@native("release repair-receipt", writes=True, replaces=("hz-release",), files=UNIT, help=H(
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


@native("release drop", writes=True, replaces=("hz-release",), files=UNIT, help=H(
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
