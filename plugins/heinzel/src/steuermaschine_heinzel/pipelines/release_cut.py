"""`sm release cut VERSION`: one core release from the release commit to verified surfaces, as one resumable pipeline.

    ci change -> notes -> publish review (STOP) -> resume* -> verify

Nothing is admitted first (Core's CI runs the full matrix on every PR). Each step runs the existing native command
(`release notes-audit`, `release resume`, `release verify`) through its review gate. The release commit itself
(release.sh in a hooked clone on main's tip) is made before the first run and named with --set; this pipeline never makes it.
The state is kept per version.

The publish review is the stop before the step that can publish: approving it lets `release resume` open the
release PR, wait for the candidate's first attempt, merge (publish), and run PyPI, compose and acceptance. A stop
between a green candidate and the merge needs the runner split into steps (release.CUT_STEPS); until then the
runner's own guard (the first-attempt candidate policy) is the gate at the merge.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from steuermaschine import USAGE, verdicts
from steuermaschine.engine import Ctx, Pipeline, Refuse, ReviewStop, Step, Subject, Wait
from steuermaschine.hashing import canonical_sha256
from steuermaschine.native import unit

from .common import command, core_repo, ok, path_input

VERB = "cut"
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
SHA = re.compile(r"[0-9a-f]{40}")
BINDS = ("main",)
INPUTS = {
    "clone": "the hooked release clone at the release commit (required)",
    "review_root": "the directory the publishing plan file is written under (required)",
    "intro": "what changed for users; the SAME text as at the cut (required)",
    "snapshot": "yes (default): acceptance pinned to the tag's public snapshot; no: the default path",
    "candidate_gate": "yes: the train path (main may be red by KNOWN failures; the candidate is the gate); default no",
}
TRAPS = {
    "ci change": "A release whose CI paths changed since the last published tag needs a ci_change verdict on its exact commit: "
                 "read the changed paths, then record it; no checksum stands in for that review.",
    "notes": "Release notes that drop a combine member are refused by the runner after the push: fix them before the cut.",
    "publish review": "Approving lets resume merge the release PR once the candidate's first attempt is green: that publishes. Read the plan file.",
    "resume": "First-attempt rule: a failed candidate job is never rerun. A failed compose is re-adopted by every resume until a fresh compose "
              "runs: fix the cause, publish the fix, then resume. The SAME intro; a release started with a snapshot resumes with it.",
    "verify": "Published is not live and not accepted: only sm mini verify proves the Mini runs it.",
}


def anchor() -> None:
    """Anchor of the review unit of `sm release cut`: this module and every first-party file it imports."""


def identify(providers: Any, settings: dict[str, Any], version: str) -> Subject:
    if not VERSION.fullmatch(version):
        raise Refuse(f"{version!r} is not a version X.Y.Z", code=USAGE)
    core = str(core_repo(settings.get("core_repo_dir"), "set core_repo_dir"))
    try:
        main = providers.vcs.remote_head(core, "main") or ""
    except RuntimeError as exc:
        raise Refuse(f"cannot read main on the remote: {exc}") from None
    if len(main) != 40:
        raise Refuse("cannot read main on the remote: no exact commit (a cut binds one main)")
    return Subject(key=version, label=f"v{version}", fingerprint={"main": main}, facts={"version": version, "main": main, "core": core})


def _yes(ctx: Ctx, name: str, default: bool) -> bool:
    value = str(ctx.inputs.get(name) or "").strip().lower()
    return default if not value else value in ("yes", "true", "1", "on")


# ----------------------------------------------------------------------------- checks before the admission
def _version(tag: str) -> tuple[int, ...] | None:
    plain = tag.removeprefix("v")
    return tuple(int(part) for part in plain.split(".")) if VERSION.fullmatch(plain) else None


def _last_tag(core: str, head: str) -> str:
    """The commit of the newest PUBLISHED release (the highest vX.Y.Z tag on the Core remote, peeled), which must be an
    ancestor of `head`. A local tag never counts. Unreadable, none, or not an ancestor: a Refuse (fail closed)."""
    from ..native.queue_lib import core_policy

    errors = (core_policy.CandidateError, OSError, ValueError, core_policy.subprocess.SubprocessError)
    try:
        listing = core_policy.git_read(core, "ls-remote", "--tags", "origin", "refs/tags/v*").decode()
    except errors as exc:
        raise Refuse(f"cannot read the published release tags of the Core remote: {exc}") from None
    commits: dict[str, str] = {}
    for line in listing.splitlines():
        sha, _, ref = line.partition("\t")
        name = ref.removeprefix("refs/tags/")
        peeled = name.endswith("^{}")
        name = name.removesuffix("^{}")
        if SHA.fullmatch(sha) and _version(name) and (peeled or name not in commits):
            commits[name] = sha  # an annotated tag's peeled line names its commit
    if not commits:
        raise Refuse("no published release tag (vX.Y.Z) on the Core remote: the CI change cannot be judged")
    newest = max(commits, key=lambda name: _version(name) or ())
    try:
        core_policy.git_read(core, "merge-base", "--is-ancestor", commits[newest], head)
    except errors:
        raise Refuse(f"the newest published release {newest} ({commits[newest][:12]}) is not an ancestor of the release "
                     f"commit {head[:12]} in the Core clone (fetch it, or the release does not descend from it)") from None
    return commits[newest]


def changes_since_release(core: str, head: str) -> tuple[str, tuple[str, ...]]:
    """(the newest published release's commit, the CI changes from it to `head`)."""
    from ..native.queue_lib import core_policy

    base = _last_tag(core, head)
    return base, core_policy.ci_changes(core, base, head)


def judged(ctx: Ctx, head: str) -> str:
    """The CI-change judgement of exactly `head`: no CI change since the newest published release, or an independent
    ci_change verdict on that commit. Every step that acts on the release commit calls this, so a remade commit is
    judged again; the run's data keeps only the commit judged last."""
    if not SHA.fullmatch(head or ""):
        raise Refuse("no exact release commit to judge for CI changes")
    if ctx.data.get("ci_change_head") == head:
        return str(ctx.data.get("ci_change_detail") or "")
    core = str(ctx.subject.facts["core"])
    base, changes = changes_since_release(core, head)
    if changes:
        store = ctx.providers.store
        for author in dict.fromkeys((ctx.author, verdicts.current_agent())):  # the run's author and the driving agent
            if author:
                verdicts.register(store, head, "release ci_change", author=author)
        shown = "; ".join(changes[:20]) + (f"; and {len(changes) - 20} more" if len(changes) > 20 else "")
        command_line = (f'sm review verdict {head} --approve --kind {verdicts.CI_CHANGE} --reviewer NAME --model M '
                        f'--notes "the changed CI paths read: {shown}"')
        verdicts.require_independent(store, head, [], command_line, kind=verdicts.CI_CHANGE)  # authors: registered above
        detail = f"{len(changes)} CI change(s) since {base[:12]}: ci_change verdict for {head[:12]} recorded"
    else:
        detail = f"no CI path changed since {base[:12]}: no ci_change verdict needed"
    ctx.data["ci_change_head"], ctx.data["ci_change_detail"] = head, detail
    return detail


def _clone_head(ctx: Ctx) -> str:
    head = ctx.providers.vcs.head(str(path_input(ctx, "clone", "the hooked release clone")))
    if not SHA.fullmatch(head or ""):
        raise Refuse("the release clone has no exact head commit")
    return head


def _same_commit(ctx: Ctx) -> str:
    """The release clone's head, judged for CI changes (a remade commit is judged again)."""
    head = _clone_head(ctx)
    judged(ctx, head)
    return head


def ci_change(ctx: Ctx) -> str:
    """The release commit's CI paths against the newest published release, by the not-CI paths of that release's policy.
    A change needs an independent `ci_change` verdict on the exact release commit; none, or another kind, refuses."""
    return judged(ctx, _clone_head(ctx))


def notes(ctx: Ctx) -> str:
    done = ok("release notes-audit", base="origin/main", checkout=path_input(ctx, "clone", "the hooked release clone"))
    return done.render()[0] if done.render() else "notes audit ok"


# ----------------------------------------------------------------------------- publish
def plan_file(ctx: Ctx) -> Path:
    return path_input(ctx, "review_root", "the review root") / f"release-v{ctx.subject.facts['version']}" / "cut-plan.json"


def publish_review(ctx: Ctx) -> tuple[str, str, str]:
    """The plan of the publishing run: what resume will run, with which inputs, on which admission. Its canonical
    sha256 is what the reviewer approves. The plan is kept in the run's state (the file is a copy for the reviewer).
    Before a verdict, changed inputs make a new plan and a new digest. Once a plan has an approving verdict it is
    the plan: inputs that differ from it refuse, and the file is restored from it. This stop does not rely on
    the engine: past the admission (an external step) the engine keeps a done stop even when its digest changes."""
    plan = {"version": ctx.subject.facts["version"], "main_at_admission": ctx.subject.facts["main"], "intro": str(ctx.inputs.get("intro") or ""),
            "snapshot_acceptance": _yes(ctx, "snapshot", True), "candidate_gate": _yes(ctx, "candidate_gate", False),
            "command": _resume_command(ctx)}
    prior, prior_sha = ctx.data.get("publish_plan"), str(ctx.data.get("publish_sha256") or "")
    if isinstance(prior, dict) and prior != plan and _approved(ctx, prior_sha):
        changed = sorted(k for k in set(prior) | set(plan) if prior.get(k) != plan.get(k))
        raise Refuse(f"the publishing plan was approved as {prior_sha[:12]}; the inputs now differ in {', '.join(changed)}. An approved plan is "
                     "not changed: give the approved values again (sm release status shows them in the plan file), or drop this cut and start "
                     "a new one for a new review", f"sm release cut {ctx.subject.facts['version']}")
    if isinstance(prior, dict) and _approved(ctx, prior_sha):
        plan = prior
    if not plan["intro"]:
        raise Refuse("the input 'intro' is missing: what changed for users, the same text as at the cut. --set intro=\"...\"", code=USAGE)
    text = json.dumps(plan, indent=1, sort_keys=True) + "\n"
    sha = canonical_sha256(text)
    ctx.data.update(publish_plan=plan, publish_sha256=sha)
    path = plan_file(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    brief = (f"Review the publishing run of v{plan['version']}: {path}.\n"
             "Approving lets `sm release resume` open the release PR, wait for the previous release's verdict and the candidate's FIRST "
             "attempt, merge the release PR (this publishes), then PyPI, compose and acceptance.\n"
             "Check: the version, the intro (user-facing, the same as at the cut) and the snapshot mode. The digest is canonical JSON of the plan. Resume runs with exactly the values of this plan.")
    return sha, brief, str(path)


def _approved(ctx: Ctx, digest: str) -> bool:
    verdict = ctx.providers.store.get_receipt("verdicts", digest) if digest else None
    return isinstance(verdict, dict) and verdict.get("verdict") == "approve"


def approved_plan(ctx: Ctx) -> dict[str, Any]:
    """The plan the publish review approved: the only source of resume's inputs."""
    plan, sha = ctx.data.get("publish_plan"), str(ctx.data.get("publish_sha256") or "")
    if not isinstance(plan, dict) or not _approved(ctx, sha):
        raise Refuse("no approved publishing plan: the publish review must approve the plan before resume runs",
                     f"sm release cut {ctx.subject.facts['version']}")
    current = publish_review(ctx)[0]  # the same check as the stop: refuses when the inputs differ from the approved plan
    if current != sha:
        raise Refuse(f"the publishing plan changed after its approval ({sha[:12]} -> {current[:12]})", f"sm release cut {ctx.subject.facts['version']}")
    return plan


def _resume_command(ctx: Ctx) -> str:
    flags = " --snapshot-acceptance" if _yes(ctx, "snapshot", True) else ""
    flags += " --candidate-gate" if _yes(ctx, "candidate_gate", False) else ""
    return f"sm release resume {ctx.subject.facts['version']} --intro INTRO{flags}"


def resume_probe(ctx: Ctx) -> str | None:
    import sys

    # The receipt is read with the code of `release resume`, reached through the registry like every step's command:
    # that module is the review unit of `release resume`, not of this pipeline (an `import` put it into this digest).
    release = sys.modules[command("release resume").fn.__module__]
    _where, receipt = release.receipt_of(release.runner(), ctx.subject.facts["version"])
    if receipt.get("finished") and receipt.get("accepted") is True:
        return "ACCEPTED (finished receipt)"
    return None


def resume(ctx: Ctx) -> str:
    _same_commit(ctx)
    result = ok_or_stopped(ctx)
    return f"ACCEPTED (acceptance run {result.acceptance_run})" if result.acceptance_run else "ACCEPTED"


def ok_or_stopped(ctx: Ctx) -> Any:
    from .common import run

    plan = approved_plan(ctx)  # never the --set values of this call: only what the reviewer approved
    result = run("release resume", version=plan["version"], intro=plan["intro"], snapshot_acceptance=bool(plan["snapshot_acceptance"]),
                 candidate_gate=bool(plan["candidate_gate"]))
    if result.exit_code == 0 and result.finished and result.accepted is True:
        return result  # the release's own verdict, read from its receipt; an exit code alone is no verdict
    if result.finished and result.accepted is False:
        raise Refuse(f"v{ctx.subject.facts['version']} finished NOT ACCEPTED: absorb it in the next release or reject it",
                     f"sm release resume {ctx.subject.facts['version']} --absorb --reason \"fixed in N+1 by #PR\"")
    if result.exit_code == 0:
        raise Refuse(f"resume exited 0 but v{ctx.subject.facts['version']} has no ACCEPTED verdict (stands: {result.stands}, finished: {result.finished}, "
                     f"accepted: {result.accepted}): {'; '.join(result.render())[:300]}", f"sm release status   (then: sm release cut {ctx.subject.facts['version']} --live)")
    stopped = result.stopped or "; ".join(result.render())[:400] or f"exit {result.exit_code}"
    ctx.data["stopped"] = stopped
    raise Refuse(f"resume stopped: {stopped}", f"sm release status   (then: sm release cut {ctx.subject.facts['version']} --live)")


def verify(ctx: Ctx) -> str:
    try:
        done = ok("release verify", version=ctx.subject.facts["version"], surface=["pypi", "release"])
    except Refuse as no:
        raise Wait(f"published surfaces not all serving yet: {no.message}", "PyPI and the GitHub release") from None
    return f"{len(done.checks)} surface checks pass"


def pipeline() -> Pipeline:
    steps = [
        Step("ci change", ci_change, binds=BINDS, trap=TRAPS["ci change"],
             changes="nothing but the reviewable-digest record of the release commit (git reads)"),
        Step("notes", notes, binds=BINDS, trap=TRAPS["notes"], changes="nothing (git reads of the release clone)"),
        ReviewStop("publish review", publish_review, trap=TRAPS["publish review"]),
        Step("resume", resume, probe=resume_probe, external=True, trap=TRAPS["resume"],
             changes="the release: hold, release PR, candidate wait, merge (publishes), tag, PyPI, compose, acceptance, verdict (sm release resume)",
             plan=_resume_command),
        Step("verify", verify, trap=TRAPS["verify"], changes="nothing (reads PyPI and the GitHub release)"),
    ]
    return Pipeline("release", VERB, "Cut one release: CI-change review, notes, a review before publishing, resume, verify; resumable.",
                    steps, identify, inputs=INPUTS, when="Use when a release is due and its release commit exists (release.sh in a hooked clone).",
                    example_arg="0.9.41", done_next="sm release after VERSION, then sm mini verify --minimum VERSION",
                    unit=unit("release cut", anchor))
