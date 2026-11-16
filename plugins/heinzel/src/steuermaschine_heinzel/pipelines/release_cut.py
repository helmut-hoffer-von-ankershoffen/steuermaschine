"""`sm release cut VERSION`: one core release from the release commit to verified surfaces, as one resumable pipeline.

    ci change -> notes -> packet -> admit prepare -> admission review (STOP) -> admit apply* -> publish review (STOP) -> resume* -> verify

The `packet` step builds the release-main-only/v1 reservation packet from the pair packet (`sm head manifest`), the
independent review file and the normal commit-hook transcript; no command has to be run by hand.
Each step runs the existing native command (`release notes-audit`, `release admit prepare|apply`,
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

from steuermaschine import USAGE, verdicts
from steuermaschine.engine import Ctx, Pipeline, Refuse, ReviewStop, Step, Subject, Wait
from steuermaschine.hashing import canonical_sha256
from steuermaschine.native import unit

from .common import command, core_repo, need, ok, path_input, prepared_digest, receipt_heads, setting

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
    "main_ci": "focused (default): reserve a registry plan; full: review a full-CI release without writing the focused registry",
}
TRAPS = {
    "ci change": "A release whose CI paths changed since the last published tag needs a ci_change verdict on its exact commit: "
                 "read the changed paths, then record it; no checksum stands in for that review.",
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
    core = str(path_input(ctx, "clone", "the hooked release clone"))
    official_origin(core, str(ctx.settings.get("repo", "")))
    if ctx.data.get("ci_change_head") == head:
        return str(ctx.data.get("ci_change_detail") or "")
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


def official_origin(clone: str, repository: str) -> None:
    """Check the effective URL (including Git rewrites) before trusting this clone's remote tags or main."""
    from ..native.queue_lib import core_policy

    try:
        remote = core_policy.git_read(clone, "remote", "get-url", "origin").decode().strip().removesuffix(".git")
    except (core_policy.CandidateError, OSError, ValueError) as exc:
        raise Refuse(f"cannot verify the release clone's official origin: {exc}") from None
    if not core_policy.REPOSITORY.fullmatch(repository) or remote not in (
        f"https://github.com/{repository}", f"ssh://git@github.com/{repository}", f"git@github.com:{repository}",
    ):
        raise Refuse("release clone origin is not the official Core repository")


def _clone_head(ctx: Ctx) -> str:
    head = ctx.providers.vcs.head(str(path_input(ctx, "clone", "the hooked release clone")))
    if not SHA.fullmatch(head or ""):
        raise Refuse("the release clone has no exact head commit")
    return head


def _same_commit(ctx: Ctx) -> str:
    """The packet's release commit, judged, and still the release clone's head (a remade commit needs a new packet)."""
    head = _packet_head(ctx)
    clone = _clone_head(ctx)
    if head != clone:
        raise Refuse(f"the release clone moved to {clone[:12]} since the packet named {head[:12] or 'no commit'}: "
                     "rebuild the packet and admit the new commit")
    judged(ctx, head)
    return head


def ci_change(ctx: Ctx) -> str:
    """The release commit's CI paths against the newest published release, by the not-CI paths of that release's policy.
    A change needs an independent `ci_change` verdict on the exact release commit; none, or another kind, refuses."""
    main_ci(ctx)
    return judged(ctx, _clone_head(ctx))


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
    judged(ctx, head)  # the commit this packet names is the commit judged for CI changes
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
    name = "full-ci-plan.json" if main_ci(ctx) == "full" else "prepare.out"
    return path_input(ctx, "packet", "the reviewed release packet").parent / name


def main_ci(ctx: Ctx) -> str:
    """An explicit route, never inferred from a failed registry admission or reader count."""
    mode = str(ctx.inputs.get("main_ci") or "focused")
    if mode not in ("focused", "full"):
        raise Refuse("main_ci must be focused or full", code=USAGE)
    prior = ctx.data.get("main_ci")
    if prior is None and mode == "full" and (ctx.data.get("admission_sha256") or ctx.data.get("receipt")):
        raise Refuse("an existing focused review or receipt cannot be converted to full CI")
    if prior is not None and prior != mode:
        raise Refuse("the cut's main_ci route changed; preserve this cut and start a separately reviewed cut")
    ctx.data["main_ci"] = mode
    return mode


def full_plan(ctx: Ctx) -> dict[str, Any]:
    """No admission receipt: bind the packet and protected base for independent full-CI review."""
    from ..native.queue_lib import core_policy
    from . import full_ci

    head = _same_commit(ctx)
    clone = str(path_input(ctx, "clone", "the hooked release clone"))
    base = str(ctx.subject.facts["main"])
    official_origin(clone, str(ctx.settings.get("repo", "")))
    if ctx.providers.vcs.remote_head(clone, "main") != base:
        raise Refuse("protected main moved before full-CI review; rebuild the release")
    try:
        raw = reservation(ctx).read_bytes()
        doc = json.loads(raw)
        identity = doc["release"]
        if (doc.get("schema") != 1 or doc.get("mode") != MODE or identity["version"] != ctx.subject.facts["version"]
                or identity["head"] != head or identity["base"] != base):
            raise Refuse("full-CI packet does not bind the release head and protected base")
        review = doc["review"]
        root = path_input(ctx, "review_root", "the review root")
        if sha(read_inside(root, review["path"], "independent release review")) != review["sha256"]:
            raise Refuse("full-CI release review changed since the packet was built")
        parents = core_policy.git_read(clone, "rev-list", "--parents", "-n", "1", head).decode().split()
        if parents != [head, base]:
            raise Refuse("full-CI release is not one commit on the protected base")
        for commit in (base, head):
            if core_policy.at_commit_git(clone, commit).repository != ctx.settings.get("repo"):
                raise Refuse("full-CI policy repository differs from the official Core identity")
        full_ci.verify(clone, base, head, ctx.subject.facts["version"])
        tree = core_policy.git_read(clone, "rev-parse", f"{head}^{{tree}}").decode().strip()
        if identity["tree"] != tree:
            raise Refuse("full-CI packet tree differs from the release commit")
    except (OSError, ValueError, KeyError, TypeError, core_policy.CandidateError) as exc:
        raise Refuse(f"cannot bind full-CI release evidence: {exc}") from None
    return {"mode": "full-ci/v1", "main": base, "head": head, "tree": tree, "clone": clone,
            "packet_sha256": sha(raw), "registry_write": False, "candidate": "full-first-attempt"}


def prepare_probe(ctx: Ctx) -> str | None:
    if main_ci(ctx) == "full":
        return None  # recompute local evidence; never adopt a legacy registry prepare
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
    _same_commit(ctx)
    if main_ci(ctx) == "full":
        plan = full_plan(ctx)
        prepared(ctx).write_text(json.dumps(plan, sort_keys=True, indent=2) + "\n")
        return "full-CI plan prepared; no focused registry reservation"
    nonconsumers = ctx.inputs.get("nonconsumers")
    done = ok("release admit prepare", packet=reservation(ctx), core_repo=path_input(ctx, "clone", "the release clone"),
              review_root=path_input(ctx, "review_root", "the review root"), nonconsumers=Path(str(nonconsumers)).expanduser() if nonconsumers else None, wait_=True)
    return f"prepared {done.sha256[:12]}"


def admission_review(ctx: Ctx) -> tuple[str, str, str]:
    path = prepared(ctx)
    sha = prepared_digest(path)
    ctx.data["admission_sha256"] = sha
    if main_ci(ctx) == "full":
        return sha, (f"Review the full-CI plan {path} and its bound release packet. "
                     "Confirm protected main uses full CI without a focused reservation. No registry admission is claimed. "
                     "The release still requires the complete original candidate, source artifact, coverage and normal merge checks."), str(path)
    brief = (f"Review the release-main admission of v{ctx.subject.facts['version']}: {path}.\n"
             f"Recompute the digest yourself: sm head hash {path}\n"
             "Check: the head is the release commit of this version on this main, the plan admits exactly the reviewed release packet, "
             "the stuck-queued receipts (if any) are the reviewed ones.")
    return sha, brief, str(path)


def apply_probe(ctx: Ctx) -> str | None:
    if main_ci(ctx) == "full":
        return None  # a legacy admission receipt cannot stand in for this route's review
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
    _same_commit(ctx)
    if main_ci(ctx) == "full":
        digest = canonical_sha256(json.dumps(full_plan(ctx)))
        if digest != ctx.data.get("admission_sha256") or not _approved(ctx, digest):
            raise Refuse("full-CI plan changed or lacks approval; review the current plan")
        ctx.data["receipt"] = ""
        return "full-CI plan verified; no focused admission applied or receipt created"
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
    if main_ci(ctx) == "full":
        plan["full_ci"] = full_plan(ctx)
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
    _same_commit(ctx)
    if main_ci(ctx) == "full":
        # The native runner owns its configured checkout; do not silently publish from a different clone.
        clone = path_input(ctx, "clone", "the hooked release clone").resolve()
        configured = setting("release_dir")
        if not configured or Path(configured).expanduser().resolve() != clone:
            raise Refuse("full-CI resume requires release_dir to match the reviewed clone",
                         "set SM_HEINZEL__RELEASE_DIR to the reviewed clone, then resume this cut")
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
    return Pipeline("release", VERB, "Cut one release: CI-change review, notes, admission with review, a review before publishing, resume, verify; resumable.",
                    steps, identify, inputs=INPUTS, when="Use when a release is due and its release commit and packet exist (release.sh in a hooked clone).",
                    example_arg="0.9.41", done_next="sm release after VERSION, then sm mini verify --minimum VERSION",
                    unit=unit("release cut", anchor))
