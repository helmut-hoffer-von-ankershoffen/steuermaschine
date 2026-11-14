"""`sm release cut VERSION`: one core release from the release commit to verified surfaces, as one resumable pipeline.

    pins -> notes -> packet -> admit prepare -> admission review (STOP) -> admit apply* -> publish review (STOP) -> resume* -> verify

The `packet` step builds the release-main-only/v1 reservation packet from the pair packet (`sm head manifest`), the
independent review file and the normal commit-hook transcript; no command has to be run by hand.
Each step runs the existing native command (`release pins`, `release notes-audit`, `release admit prepare|apply`,
`release resume`, `release verify`) through its review gate. The release commit itself (release.sh in a hooked clone
on main's tip) and the pair packet are made before the first run and named with --set; this pipeline never makes them.
The state is kept per version. Main moving before the admission is applied voids prepare and its review.

The publish review is the stop before the step that can publish: approving it lets `release resume` open the
release PR, wait for the candidate's first attempt, merge (publish), and run PyPI, compose and acceptance. A stop
between a green candidate and the merge needs the runner split into steps (release.CUT_STEPS); until then the
runner's own guard (the first-attempt candidate policy) is the gate at the merge.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from steuermaschine import USAGE
from steuermaschine.engine import Ctx, Pipeline, Refuse, ReviewStop, Step, Subject, Wait
from steuermaschine.hashing import canonical_sha256
from steuermaschine.native import unit

from .common import command, core_repo, need, ok, path_input, prepared_digest, receipt_heads

VERB = "cut"
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
BINDS = ("main",)
INPUTS = {
    "packet": "the pair packet.json of the release commit (sm head manifest in the hooked clone) (required); the reservation packet is built next to it",
    "commit_log": "the transcript of the release commit's normal hook run, relative to review_root; its last line is vX.Y.Z (required)",
    "release_review": "the independent review of the release commit, relative to review_root; default RELEASE-REVIEW.md",
    "clone": "the hooked release clone at the release commit (required)",
    "review_root": "the review root the packet's review paths are relative to (required)",
    "intro": "what changed for users; the SAME text as at the cut (required)",
    "snapshot": "yes (default): acceptance pinned to the tag's public snapshot; no: the default path",
    "candidate_gate": "yes: the train path (main may be red by KNOWN failures; the candidate is the gate); default no",
    "nonconsumers": "the reviewed stuck-queued receipts file, the same for prepare and apply",
}
TRAPS = {
    "pins": "A re-pin is a review: never cut while the executed release files differ from the reviewed ones.",
    "notes": "Release notes that drop a combine member are refused by the runner after the push: fix them before the cut.",
    "packet": "A packet is bound to its review file and transcript: a changed review file needs a rebuilt packet and a new admission review.",
    "admit prepare": "Admit BEFORE the push and before resume (resume takes the hold); any merge on main voids the plan: rebuild from release.sh.",
    "admission review": "Approve the canonical sha256 only (sm head hash); a re-prepare changes the bytes and needs a new verdict.",
    "admit apply": "Never apply twice: a receipt for the release head is adopted, not applied again.",
    "publish review": "Approving lets resume merge the release PR once the candidate's first attempt is green: that publishes. Read the plan file.",
    "resume": "First-attempt rule: a failed candidate job is never rerun. A failed compose is re-adopted by every resume until a fresh compose "
              "runs: fix the cause, publish the fix, then resume. The SAME intro; a release started with a snapshot resumes with it.",
    "verify": "Published is not live and not accepted: only sm mini verify proves the Mini runs it.",
}


# ----------------------------------------------------------------------------- the release-main-only/v1 packet
# Built from the pair packet (`sm head manifest`) and the evidence of the hooked clone: the port of the hand script that cut
# 0.9.40 to 0.9.42, byte-equal for the same inputs. No network, no registry, no push.
MODE = "release-main-only/v1"
HOOKS = ("pre-commit", "commit-msg", "pre-push")
PACKET_NAME = "release-reservation-packet.json"
PREK = re.compile(r'^PREK="([^"]+)"$', re.MULTILINE)
SHA = re.compile(r"[0-9a-f]{40}")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> str:
    """The digest of a typed value as the route recomputes it: sha256 of canonical JSON (sorted keys, no spaces)."""
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def validator_path() -> Path:
    """The semantic validator of the install that runs this command: the file the route checks the digest of."""
    return Path(__file__).resolve().parent.parent / "native" / "release_admit_lib" / "release_scope.py"


def read_inside(root: Path, relative: str, what: str) -> bytes:
    path = Path(relative)
    if not relative or path.is_absolute() or ".." in path.parts:
        raise Refuse(f"unsafe {what} path {relative!r}: give it relative to the review root", code=USAGE)
    try:
        return (root / path).read_bytes()
    except OSError as exc:
        raise Refuse(f"cannot read the {what} {root / path}: {exc}") from None


def assemble(*, version: str, head: str, base: str, tree: str, changelog_blob: str, pair: dict[str, Any], commit_log: tuple[str, bytes],
             review: tuple[str, bytes], validator_sha256: str, hooks: dict[str, tuple[str, str]], hook_runner: tuple[str, str]) -> dict[str, Any]:
    """The packet as a value. Every file is given as (path, bytes or digest); nothing here touches the disk or git."""
    plan = pair.get("main_manifest")
    if not isinstance(plan, dict):
        raise Refuse("the pair packet has no main_manifest: make it with `sm head manifest` on the release commit")
    if plan.get("base") != base or plan.get("tree") != tree or plan.get("event") != "push":
        raise Refuse("the pair packet's main_manifest is not the push plan of this release commit (base, tree or event differ)")
    last = commit_log[1].decode(errors="replace").strip().splitlines()[-1:] or [""]
    if last[0] != f"v{version}":
        raise Refuse(f"the commit hook transcript does not end with v{version}: it must be the normal hook run of the release commit")
    release = {"version": version, "head": head, "base": base, "tree": tree, "validator_sha256": validator_sha256,
               "hooks": {name: {"path": path, "sha256": digest} for name, (path, digest) in sorted(hooks.items())},
               "hook_runner": {"path": hook_runner[0], "sha256": hook_runner[1]},
               "commit_log": {"path": commit_log[0], "sha256": sha(commit_log[1])}}
    binding = {"path": review[0], "sha256": sha(review[1]), "release_sha256": canonical(release), "manifest_sha256": canonical(plan),
               "changelog_blob": changelog_blob}
    return {"schema": 1, "mode": MODE, "manifest": plan, "release": release, "review": binding}


def render(packet: dict[str, Any]) -> bytes:
    return (json.dumps(packet, sort_keys=True, indent=2) + "\n").encode()


def evidence(providers: Any, clone: str, head: str) -> dict[str, Any]:
    """What only the hooked clone knows: parent, tree, CHANGELOG blob, the three hooks and the interpreter they pin."""

    def git(*argv: str) -> str:
        done = providers.commands.run(["git", "-C", clone, *argv], timeout=60)
        if done.rc != 0:
            raise Refuse(f"git {' '.join(argv)} failed in {clone}: {(done.err or done.out).strip()[:200]}")
        return str(done.out).strip()

    parents = git("rev-list", "--parents", "-n", "1", head).split()
    if len(parents) != 2 or not SHA.fullmatch(head):
        raise Refuse("the release head must be a full commit SHA with exactly one parent")
    hooks: dict[str, tuple[str, str]] = {}
    runner = ""
    for name in HOOKS:
        path = Path(git("rev-parse", "--path-format=absolute", "--git-path", f"hooks/{name}"))
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise Refuse(f"cannot read the hook {path}: {exc}") from None
        hooks[name] = (str(path), sha(raw))
        found = PREK.search(raw.decode(errors="replace"))
        if found is None:
            raise Refuse(f"the hook {path} names no PREK interpreter: it is not the normal hook of the release clone")
        runner = runner or found[1]
        if found[1] != runner:
            raise Refuse("the three hooks pin different PREK interpreters")
    try:
        runner_sha = sha(Path(runner).read_bytes())
    except OSError as exc:
        raise Refuse(f"cannot read the hook interpreter {runner}: {exc}") from None
    return {"base": parents[1], "tree": git("rev-parse", f"{head}^{{tree}}"), "changelog_blob": git("rev-parse", f"{head}:CHANGELOG.md"),
            "hooks": hooks, "hook_runner": (runner, runner_sha)}


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
def pins(ctx: Ctx) -> str:
    try:
        done = ok("release pins", ref="origin/main")
    except Refuse as no:
        raise Refuse(f"the release pins differ from the reviewed files: {no.message}", "sm release pins origin/main") from None
    return f"{len(done.pins)} pins match the reviewed files"


def notes(ctx: Ctx) -> str:
    done = ok("release notes-audit", base="origin/main", checkout=path_input(ctx, "clone", "the hooked release clone"))
    return done.render()[0] if done.render() else "notes audit ok"


# ----------------------------------------------------------------------------- the release-main admission
def reservation(ctx: Ctx) -> Path:
    """The release-main-only/v1 packet that prepare and apply read: next to the pair packet the operator gave."""
    given = path_input(ctx, "packet", "the pair packet")
    return given if _mode(given) == MODE else given.parent / PACKET_NAME


def _mode(path: Path) -> str:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return ""
    return str(doc.get("mode") or "") if isinstance(doc, dict) else ""


def packet(ctx: Ctx) -> str:
    if _mode(path_input(ctx, "packet", "the pair packet")) == MODE:
        return f"the given packet is already {MODE}: used as it is"
    version, clone = ctx.subject.facts["version"], path_input(ctx, "clone", "the hooked release clone")
    root = path_input(ctx, "review_root", "the review root")
    log = need(ctx, "commit_log", "the normal commit-hook transcript of the release commit, relative to review_root")
    review = str(ctx.inputs.get("release_review") or "RELEASE-REVIEW.md")
    pair_path = path_input(ctx, "packet", "the pair packet")
    try:
        pair = json.loads(pair_path.read_text())
    except (OSError, ValueError) as exc:
        raise Refuse(f"cannot read the pair packet {pair_path}: {exc}") from None
    if not isinstance(pair, dict):
        raise Refuse(f"the pair packet {pair_path} is not a JSON object")
    head = ctx.providers.vcs.head(str(clone))
    found = evidence(ctx.providers, str(clone), head)
    built = assemble(
        version=version, head=head, base=found["base"], tree=found["tree"], changelog_blob=found["changelog_blob"], pair=pair,
        commit_log=(log, read_inside(root, log, "commit hook transcript")),
        review=(review, read_inside(root, review, "independent release review")),
        validator_sha256=sha(validator_path().read_bytes()), hooks=found["hooks"], hook_runner=found["hook_runner"])
    raw = render(built)
    target = reservation(ctx)
    scratch = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    scratch.write_bytes(raw)
    scratch.replace(target)  # atomic: a reader sees the old or the new packet, never half
    return f"built {target.name} {sha(raw)[:12]} for {head[:8]} (review {built['review']['sha256'][:12]})"


def prepared(ctx: Ctx) -> Path:
    return path_input(ctx, "packet", "the reviewed release packet").parent / "prepare.out"


def prepare_probe(ctx: Ctx) -> str | None:
    args = prepared(ctx).with_name("prepare.args.json")
    try:
        stored = json.loads(args.read_text())
    except (OSError, ValueError):
        return None
    try:
        doc = json.loads(prepared(ctx).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict) or not isinstance(doc, dict) or doc.get("main") != ctx.subject.facts["main"]:
        return None  # prepared at another main (or unreadable): prepare again; the arguments file names no main
    if stored.get("sha256") != prepared_digest(prepared(ctx)):
        return None  # the arguments belong to another prepared transaction
    return f"prepared {prepared_digest(prepared(ctx))[:12]} at main {ctx.subject.facts['main'][:8]}"


def admit_prepare(ctx: Ctx) -> str:
    nonconsumers = ctx.inputs.get("nonconsumers")
    done = ok("release admit prepare", packet=reservation(ctx), core_repo=path_input(ctx, "clone", "the release clone"),
              review_root=path_input(ctx, "review_root", "the review root"), nonconsumers=Path(str(nonconsumers)).expanduser() if nonconsumers else None, wait_=True)
    return f"prepared {done.sha256[:12]}"


def admission_review(ctx: Ctx) -> tuple[str, str, str]:
    path = prepared(ctx)
    sha = prepared_digest(path)
    ctx.data["admission_sha256"] = sha
    brief = (f"Review the release-main admission of v{ctx.subject.facts['version']}: {path}.\n"
             f"Recompute the digest yourself: sm head hash {path}\n"
             "Check: the head is the release commit of this version on this main, the plan admits exactly the reviewed release packet, "
             "the stuck-queued receipts (if any) are the reviewed ones.")
    return sha, brief, str(path)


def apply_probe(ctx: Ctx) -> str | None:
    head = _packet_head(ctx)
    return "a receipt for the release head exists" if head and head in receipt_heads() else None


def _packet_head(ctx: Ctx) -> str:
    try:
        doc = json.loads(reservation(ctx).read_text())
    except (OSError, ValueError):
        return ""
    release = doc.get("release") if isinstance(doc, dict) else None
    return str(release.get("head") or "") if isinstance(release, dict) else ""


def admit_apply(ctx: Ctx) -> str:
    done = ok("release admit apply", packet=reservation(ctx), approved=ctx.data["admission_sha256"], wait_=True)
    ctx.data["receipt"] = done.receipt_path
    return f"admitted; receipt {done.receipt_path}"


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
            "admission_sha256": ctx.data.get("admission_sha256", ""), "receipt": ctx.data.get("receipt", ""),
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
             "Check: the version, the intro (user-facing, the same as at the cut), the snapshot mode, and that the admission receipt is this "
             "release's. The digest is canonical JSON of the plan. Resume runs with exactly the values of this plan.")
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
        Step("pins", pins, binds=BINDS, trap=TRAPS["pins"], changes="nothing (git reads)"),
        Step("notes", notes, binds=BINDS, trap=TRAPS["notes"], changes="nothing (git reads of the release clone)"),
        Step("packet", packet, binds=BINDS, trap=TRAPS["packet"], changes=f"{PACKET_NAME} next to the pair packet"),
        Step("admit prepare", admit_prepare, probe=prepare_probe, binds=BINDS, trap=TRAPS["admit prepare"],
             changes="prepare.out and prepare.args.json next to the packet (sm release admit prepare)"),
        ReviewStop("admission review", admission_review, binds=BINDS, trap=TRAPS["admission review"]),
        Step("admit apply", admit_apply, probe=apply_probe, external=True, binds=BINDS, trap=TRAPS["admit apply"],
             changes="one repository variable (the release-main plan slot), the journal and a receipt (sm release admit apply)",
             plan=lambda ctx: f"sm release admit apply {reservation(ctx)} --approved {ctx.data.get('admission_sha256', 'SHA256')}"),
        ReviewStop("publish review", publish_review, trap=TRAPS["publish review"]),
        Step("resume", resume, probe=resume_probe, external=True, trap=TRAPS["resume"],
             changes="the release: hold, release PR, candidate wait, merge (publishes), tag, PyPI, compose, acceptance, verdict (sm release resume)",
             plan=_resume_command),
        Step("verify", verify, trap=TRAPS["verify"], changes="nothing (reads PyPI and the GitHub release)"),
    ]
    return Pipeline("release", VERB, "Cut one release: pins, notes, admission with review, a review before publishing, resume, verify; resumable.",
                    steps, identify, inputs=INPUTS, when="Use when a release is due and its release commit and packet exist (release.sh in a hooked clone).",
                    example_arg="0.9.41", done_next="sm release after VERSION, then sm mini verify --minimum VERSION",
                    unit=unit("release cut", anchor, "release-cut.md"))
