"""`sm head admit WORKTREE`: one head from a clean worktree to a queued PR, as one resumable engine pipeline.

    preflight -> packet -> packet review (STOP) -> prepare -> apply review (STOP) -> apply* -> pr* -> queue*

Merge tier (Core ADR 0149, detected from main's own scripts/focused_plan.py; the setting merge_tier_mode forces it): CI selects the lanes itself and needs no
plan, so packet, both reviews, prepare and apply are skipped (adopted as "not needed") unless the head changes a
protected file (PROTECTED_FILES, PROTECTED_PATTERNS: a reviewed execution manifest is required again) or the input
widen asks for an explicit widening. An older Core at main, or a protected list that is not a plain literal, keeps
every step.

Protected-producer route: a head that changes a pinned focused-CI source (the packet's `execution` pins differ from
main's blobs) is prepared with `--reviewed-protected-producer` by this pipeline itself. Nothing is left out against the
hand route: the packet review approves a record that names the changed sources (a verdict on the packet alone does not
match), prepare checks the PR manifest and the paired main manifest with main's own resolver and refuses when the head
changes PROTECTED_FILES, PLAN_VARIABLE or REPOSITORY, and the apply review is a separate verdict on prepare.out, which
says `protected_producer` true.

Each step runs the existing native command (`head preflight`, `head packet preopen`, `head prepare`, `head apply`,
`head pr`, `queue add`) through its review gate; nothing here reimplements one. The state is kept per head (the
exact SHA). A plan binds one head and one main: when main moves before the apply, packet, reviews and prepare are
void and start again; a new head is a new run that supersedes the old one. Past the apply nothing is voided.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Any

from steuermaschine import USAGE
from steuermaschine.engine import Ctx, Pipeline, Refuse, ReviewStop, Step, Subject, identify_head, need_check
from steuermaschine.github import Gh
from steuermaschine.native import unit

from ..native import merge_tier
from ..native.window import lane_branch
from .common import SHA, core_repo, need, ok, packet_sha, path_input, prepared_digest, receipt_heads, run, setting, slug

VERB = "admit"
BINDS = ("head", "main")
INPUTS = {
    "review_root": "the review root; the packet goes to REVIEW_ROOT/BRANCH/m-MAIN8-HEAD8 (required)",
    "sel": "the packet selection JSON (python, web, browser ids) for preflight and packet (required)",
    "junit": "the local JUnit files as LANE=FILE, comma separated (python=F.xml,web=G.xml)",
    "core_repo": "the Git repository holding main and the head (default: the setting core_repo_dir)",
    "pr": "the open PR this head updates (default: the open PR of the branch, found by itself)",
    "validated": "the run ids of the `sm head validate` runs of this head, one per hosted lane preflight named, comma separated (9101,9102)",
    "bead": "the bead this head closes (for the PR)",
    "title": "the PR title, at most 72 characters",
    "body_file": "a file with the PR body (default: `Bead BEAD.`)",
    "reviewed": "the written reason of an independent review, when the diff guard flagged the PR",
    "widen": "yes: build, review and apply a manifest although the merge tier needs none (an explicit widening)",
    "skip_reason": "why the stored preflight may leave checks out (`head preflight --skip`); the packet reviewer sees and approves it",
    "nonconsumers": "a reviewed stuck-queued receipts file for ghost runs GitHub cannot cancel or delete; prepare binds it, apply reads it back",
}
TRAPS = {
    "preflight": "Fix ALL numbered failures in ONE commit: every correction is a new head and a whole new cycle (packet, two reviews, apply, CI).",
    "packet": "The packet binds this exact head and this exact main: after any merge to main it is stale and is rebuilt here.",
    "packet review": "The reviewer reads the packet and REVIEW.md, not the author's summary; a re-built packet has new bytes and needs a new verdict.",
    "prepare": "Prepare refuses while an uncovered Actions run is active; it waits for the idle window (exit 75 = run the same command again).",
    "apply review": "The digest is of canonical JSON (`sm head hash`), not of the file; any re-prepare changes it and needs a new verdict.",
    "apply": ("Apply waits for the validation runs going (the admission hold keeps new ones from starting; exit 75 = same command again); "
              "apply in an idle window only, and never twice: a receipt for this head is adopted, not applied again. Push after apply, never before."),
    "pr": "One push, the checked commit, under the run lock: never push the head by hand while this run is open.",
    "queue": "A green PR unmerged after 15 minutes is an incident, not a wait: sm queue why PR.",
}


def anchor() -> None:
    """Anchor of the review unit of `sm head admit`: this module and every first-party file it imports."""


def identify(providers: Any, settings: dict[str, Any], worktree: str) -> Subject:
    subject = identify_head(providers, settings, worktree)
    wt = subject.facts["worktree"]
    try:
        main = providers.vcs.remote_head(wt, "main") or ""
    except RuntimeError as exc:
        raise Refuse(f"cannot read main on the remote: {exc}") from None
    if len(main) != 40:
        raise Refuse("cannot read main on the remote: no exact commit (a plan binds one main)")
    subject.fingerprint = {"head": subject.key, "main": main}
    subject.facts["main"] = main
    return subject


# ----------------------------------------------------------------------------- preflight
def preflight_probe(ctx: Ctx) -> str | None:
    from ..native.preflight import SUMMARY_KIND

    head = ctx.subject.key
    done = ctx.providers.store.load(SUMMARY_KIND, head[:8]) or {}
    sel = ctx.inputs.get("sel")
    if sel and done.get("head") == head and done.get("selection_sha256") != _sel_sha(ctx):  # R3: the READY was for other selection bytes
        ctx.say("  the stored preflight ran with another selection (or names none): not adopted, it runs again")
        return None
    if done.get("head") == head and done.get("ready") and done.get("base") == ctx.subject.facts["main"]:
        if skipped := [str(name) for name in done.get("skipped") or []]:
            reason = str(ctx.inputs.get("skip_reason") or "").strip()
            if not reason or manifest_skip(ctx):  # no reason, or no packet reviewer to see it: run every check
                ctx.say(f"  the stored preflight skipped {', '.join(skipped)}: not adopted"
                        + ("" if reason else " (--set skip_reason=TEXT puts the skip before the packet reviewer)"))
                return None
            ctx.data["preflight_skipped"] = {"checks": skipped, "reason": reason}
        else:
            ctx.data.pop("preflight_skipped", None)
        hosted = _hosted(ctx, done.get("hosted_lanes") or [], done.get("next", ""))
        ctx.data["preflight_sel_sha256"] = str(done.get("selection_sha256") or "")  # what packet() pairs the selection with
        return hosted or f"READY at main {ctx.subject.facts['main'][:8]}"
    return None


def preflight(ctx: Ctx) -> str:
    wt, sel = ctx.subject.facts["worktree"], path_input(ctx, "sel", "the packet selection the preflight and the packet use")
    before = hashlib.sha256(sel.read_bytes()).hexdigest()
    report = run("head preflight", worktree=Path(wt), sel=sel)
    if not report.ready:
        failed = "; ".join(line.strip() for line in report.render() if line.strip())[:500]
        raise Refuse(f"NOT READY: {failed}", f"fix all of it in one commit, then: sm head admit {wt}")
    ran = str(getattr(report, "selection_sha256", "") or "")
    if not (before == ran == _sel_sha(ctx)):  # R3c: changed before, during or after the run: no READY is bound to these bytes
        raise Refuse(f"the selection changed while the preflight ran (before {before[:12]}, read by the preflight {ran[:12] or '(none)'}, "
                     f"now {_sel_sha(ctx)[:12]}): run admit again, it preflights the selection as it is then", f"sm head admit {wt}")
    hosted = _hosted(ctx, report.hosted_lanes, report.next)
    ctx.data["preflight_sel_sha256"] = before  # the bytes this READY is for
    return hosted or f"READY at main {report.base[:8]}"


VALIDATION_WORKFLOW = ".github/workflows/focused-job.yml"
VALIDATION_OS = "ubuntu-24.04"  # the hosted lanes of CI run there; a lane validated elsewhere proves nothing about CI


def _hosted(ctx: Ctx, lanes: list[str], validate: str) -> str:
    """Hosted lanes are proven by their validation runs, never by a word. Per lane, one given run that
    - this machine dispatched for exactly that lane: a receipt in focused-runs.jsonl (run, kind dispatch, the lane's
      own ref `validation/<branch>-<lane>-<sha8>`, the label of an e2e run of that lane on the CI's OS),
    - GitHub shows as the focused job workflow, event workflow_dispatch, on that ref, completed green,
    - ran on a throwaway commit (never the head SHA itself) whose tree is exactly this head's tree.
    Every read names the configured repository (-R / repos/OWNER/NAME), never the current directory's."""
    if not lanes:
        return ""
    wt = ctx.subject.facts["worktree"]
    again = validate or f"sm head validate {wt}"
    given = [r.strip() for r in str(ctx.inputs.get("validated") or "").split(",") if r.strip()]
    if not given:
        raise Refuse(f"READY, but the head touches hosted lanes ({', '.join(lanes)}): validate them first, never on the PR head SHA. "
                     f"Then: sm head admit {wt} --set validated=RUN[,RUN] (one run per lane)", again)
    if bad := [r for r in given if not r.isdigit()]:
        raise Refuse(f"validated= takes the run ids of the validation runs, not {', '.join(bad)!r}", again, code=USAGE)
    receipts = _receipts()
    tree, runs = _tree(ctx), {r: _run(ctx, r) for r in given}
    proven = []
    for lane in lanes:
        ref = lane_branch(ctx.subject.facts["branch"], lane, ctx.subject.key)
        label = _label(lane, ref)
        ours = [r for r in given if any(str(row.get("run")) == r and row.get("kind") == "dispatch" and row.get("branch") == ref
                                        and row.get("what") == label for row in receipts)]
        if not ours:
            raise Refuse(f"no given run validated lane {lane}: this machine has no dispatch receipt '{label}' for any of {', '.join(given)} "
                         f"(sm head validate records one per lane)", again)
        r, run = ours[0], runs[ours[0]]
        workflow = str(run.get("path") or "").split("@", 1)[0]
        if workflow != VALIDATION_WORKFLOW or run.get("event") != "workflow_dispatch" or run.get("head_branch") != ref:
            raise Refuse(f"run {r} is {workflow or '?'} ({run.get('event') or '?'}) on {run.get('head_branch') or '?'}, not the focused job "
                         f"dispatched on {ref}", again)
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            raise Refuse(f"run {r} of lane {lane} is {run.get('status')}/{run.get('conclusion') or 'no conclusion'}, not completed green",
                         f"sm ci why {r}" if run.get("status") == "completed" else f"sm ci watch {r}")
        sha = str(run.get("head_sha") or "")
        if not SHA.fullmatch(sha):
            raise Refuse(f"run {r} names no exact commit ({sha!r}): its tree cannot be compared with this head's", again)
        if sha == ctx.subject.key:
            raise Refuse(f"run {r} ran on the head SHA itself: checks attach by SHA and the PR would sit BLOCKED; validate on a throwaway commit", again)
        if _commit_tree(ctx, sha) != tree:
            raise Refuse(f"run {r} validated commit {sha[:12]}, whose tree is not this head's tree {tree[:12]}: validate this head again", again)
        proven.append(f"{lane} by run {r}")
    return f"READY; hosted lanes validated on this head's tree: {', '.join(proven)}"


def _label(lane: str, ref: str) -> str:
    """The receipt label `sm head validate` writes for one lane (dispatch_lib.focused.cmd_dispatch)."""
    from ..native.dispatch_lib import rules

    kind, selector, error = rules.selection(lane, [], None)
    if error:
        raise Refuse(f"preflight named lane {lane!r}, which is not a hosted lane: {error}")
    return f"dispatch {kind} {str(selector)[:60]} x1 on {VALIDATION_OS} @ {ref}"


def receipts_file() -> Path:
    """Where the focused dispatches of this machine are recorded (the setting dispatch_state_file, as `sm ci one`)."""
    from steuermaschine.native import runtime

    rt = runtime()
    named = rt.settings.get("dispatch_state_file")
    return Path(str(named)).expanduser() if named else Path(rt.project.home) / "dispatch" / "focused-runs.jsonl"


def _receipts() -> list[dict[str, Any]]:
    path = receipts_file()
    try:
        return [row for line in path.read_text().splitlines() if line.strip() and isinstance(row := json.loads(line), dict)]
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise Refuse(f"unreadable focused-run receipts in {path}: {exc}") from None


def _repo(ctx: Ctx) -> str:
    repo = str(ctx.settings.get("repo") or "")
    if "/" not in repo:
        raise Refuse("no repository: set repo (OWNER/NAME); a run or commit is never looked up in the current directory's repository",
                     "sm self config", code=USAGE)
    return repo


def _gh(ctx: Ctx) -> Gh:
    return Gh(ctx.providers.commands, repo=_repo(ctx))


def _run(ctx: Ctx, run: str) -> dict[str, Any]:
    data, why = _gh(ctx).try_json(["api", f"repos/{_repo(ctx)}/actions/runs/{run}"])
    if not isinstance(data, dict):
        raise Refuse(f"cannot read validation run {run}: {why or 'no answer'}")
    return data


def _commit_tree(ctx: Ctx, sha: str) -> str:
    data, why = _gh(ctx).try_json(["api", f"repos/{_repo(ctx)}/git/commits/{sha}"])
    tree = ((data or {}).get("tree") or {}).get("sha") if isinstance(data, dict) else None
    if not tree:
        raise Refuse(f"cannot read the tree of commit {sha[:12]}: {why or 'no tree in the answer'}")
    return str(tree)


def _tree(ctx: Ctx) -> str:
    done = ctx.providers.commands.run(["git", "-C", ctx.subject.facts["worktree"], "rev-parse", f"{ctx.subject.key}^{{tree}}"], timeout=30)
    if done.rc or not done.out.strip():
        raise Refuse(f"cannot read the tree of the head {ctx.subject.key[:12]}: {(done.err or done.out).strip()[:200]}")
    return str(done.out.strip())


# ----------------------------------------------------------------------------- merge tier
def _git_out(ctx: Ctx, *args: str) -> tuple[int, str]:
    done = ctx.providers.commands.run(["git", "-C", ctx.subject.facts["worktree"], *args], timeout=120)
    return done.rc, done.out


def _literal(plan: str, name: str) -> tuple[str, ...] | None:
    """The strings of the one plain literal assigned to NAME, or None: missing, assigned twice, computed or not strings."""
    try:
        body = ast.parse(plan).body
    except (SyntaxError, ValueError):
        return None
    values: list[ast.expr | None] = []
    for node in body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            values.append(node.value)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name) and node.target.id == name:
            values.append(node.value if isinstance(node, ast.AnnAssign) else None)
    if len(values) != 1 or values[0] is None:
        return None
    try:
        value = ast.literal_eval(values[0])
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None
    if not isinstance(value, (tuple, list, set)) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(value)


def protected_changes(plan: str, changed: list[str]) -> list[str] | None:
    """The changed paths the base's judge pins: PROTECTED_FILES, or a PROTECTED_PATTERNS match in its own directory.

    None when either constant is missing or not a plain literal of strings: the caller must then keep the manifest."""
    files, patterns = _literal(plan, "PROTECTED_FILES"), _literal(plan, "PROTECTED_PATTERNS")
    if files is None or patterns is None:
        return None
    pinned_files = set(files)

    def pinned(path: str) -> bool:
        folder, _, name = path.rpartition("/")
        return path in pinned_files or any(folder == p.rpartition("/")[0] and fnmatch.fnmatchcase(name, p.rpartition("/")[2]) for p in patterns)

    return sorted(p for p in changed if pinned(p))


def manifest_skip(ctx: Ctx) -> str | None:
    """Why this head needs no manifest (the merge tier selects its own lanes), or None: build one as before.

    Read once per head and main, from main's own Core: an older Core (no WHOLE_SUITES) always needs one."""
    f = ctx.subject.facts
    key = (f["worktree"], f["main"], ctx.subject.key, str(ctx.inputs.get("widen") or ""))
    if key not in _DECIDED:
        _DECIDED[key] = _decide(ctx)
    return _DECIDED[key] or None


_DECIDED: dict[tuple[str, str, str, str], str] = {}  # per process: (worktree, main, head, widen) -> why no manifest, or ""


def _decide(ctx: Ctx) -> str:
    main = ctx.subject.facts["main"]
    rc, plan = _git_out(ctx, "show", f"{main}:{merge_tier.PLAN_PATH}")
    if rc != 0:
        plan = ""  # unreadable main: `active` keeps the older rules, a forced tier finds no protected list below
    if not merge_tier.active(str(ctx.settings.get("merge_tier_mode") or "auto"), lambda: plan):
        return ""
    if str(ctx.inputs.get("widen") or "").lower() in ("yes", "1", "true"):
        return ""
    rc, names = _git_out(ctx, "diff", "--name-only", "--no-renames", f"{main}...{ctx.subject.key}")
    if rc != 0:
        return ""  # unknown change: keep the manifest path, it refuses with its own reason
    protected = protected_changes(plan, names.split())
    if protected is None:
        ctx.say("  merge tier, but main's PROTECTED_FILES or PROTECTED_PATTERNS is not a plain literal: a reviewed execution manifest is required")
        return ""
    if protected:
        ctx.say(f"  merge tier, but a protected file changed ({', '.join(protected[:4])}): a reviewed execution manifest is required")
        return ""
    return "not needed: merge tier, CI selects the lanes itself (no protected file changed; --set widen=yes to widen)"


# ----------------------------------------------------------------------------- packet
def packet_dir(ctx: Ctx) -> Path:
    root = path_input(ctx, "review_root", "where packets and reviews live")
    f = ctx.subject.facts
    return root / slug(f["branch"]) / f"m-{f['main'][:8]}-{f['head'][:8]}"


def packet_probe(ctx: Ctx) -> str | None:
    if skip := manifest_skip(ctx):
        return skip
    record = packet_dir(ctx) / "wave.json"
    try:
        data = json.loads(record.read_text())
    except (OSError, ValueError):
        return None
    if data.get("head") != ctx.subject.key or data.get("main") != ctx.subject.facts["main"] or not data.get("packet_sha256"):
        return None
    if not ctx.inputs.get("sel") or not _preflighted(ctx) or not _built_from_sel(ctx, packet_dir(ctx)):  # R3: no bypass by adoption
        ctx.say(f"  the packet in {packet_dir(ctx)} is not adopted: it was not built from the preflighted selection")
        return None
    _remember_packet(ctx, packet_dir(ctx), data["packet_sha256"])
    ctx.data["packet_sel_sha256"] = ctx.data["preflight_sel_sha256"]
    return f"packet {data['packet_sha256'][:12]} in {packet_dir(ctx)}"


def _preflighted(ctx: Ctx) -> bool:
    """Whether the current selection bytes are the ones a READY preflight of this head and main ran with. The engine does
    not probe a done step again, so this is asked where the selection is used, not only at the preflight step."""
    if ctx.data.get("preflight_sel_sha256") == _sel_sha(ctx):
        return True
    return preflight_probe(ctx) is not None and ctx.data.get("preflight_sel_sha256") == _sel_sha(ctx)


def _built_from_sel(ctx: Ctx, directory: Path) -> bool:
    """The packet's own sel.json (written by preopen, empty lanes dropped) is the current selection."""
    try:
        theirs = json.loads((directory / "sel.json").read_text())
        mine = json.loads(path_input(ctx, "sel", "the packet selection").read_text())
    except (OSError, ValueError):
        return False
    return isinstance(mine, dict) and theirs == {k: v for k, v in mine.items() if v != []}


def packet(ctx: Ctx) -> str:
    f, out = ctx.subject.facts, packet_dir(ctx)
    core = core_repo(ctx.inputs.get("core_repo") or setting("core_repo_dir", ""), "--set core_repo=DIR or the setting core_repo_dir")
    if not _preflighted(ctx):  # a done step is not probed again: no packet pairs with a selection that was not preflighted (R3)
        raise Refuse(f"the selection {path_input(ctx, 'sel', 'the packet selection')} is not the one the adopted preflight ran with "
                     "(it changed after the preflight step, or the preflight is not READY at this head and main): preflight it first",
                     f"sm head preflight {f['worktree']} --sel {ctx.inputs.get('sel')}   (then: sm head admit {f['worktree']})")
    _clear_own_attempt(ctx, out)
    ctx.data["packet_attempt"] = str(out)  # saved with the state even when preopen refuses: the next run knows the files are ours
    junit = [x.strip() for x in str(ctx.inputs.get("junit") or "").split(",") if x.strip()]
    built = ok("head packet preopen", branch=f["head"], main=f["main"], core_repo=core, worktree=Path(f["worktree"]), out=out,
               review_root=path_input(ctx, "review_root", "where packets live"), selection=path_input(ctx, "sel", "the packet selection"), junit=junit)
    if _sel_sha(ctx) != ctx.data["preflight_sel_sha256"] or not _built_from_sel(ctx, out):  # changed (or reverted) while preopen read it
        _clear_own_attempt(ctx, out)  # set aside, never adopted
        raise Refuse("the selection changed while the packet was built: preflight it, then admit again", f"sm head admit {f['worktree']}")
    _remember_packet(ctx, Path(built.directory), built.packet_sha256)
    ctx.data["packet_sel_sha256"] = ctx.data["preflight_sel_sha256"]  # the packet is built from exactly the preflighted bytes
    ctx.data.pop("rejected_packet", None)  # a packet built for its own reason answers no earlier reject
    return f"packet {built.packet_sha256[:12]} in {built.directory}"


def _sel_sha(ctx: Ctx) -> str:
    return hashlib.sha256(path_input(ctx, "sel", "the packet selection").read_bytes()).hexdigest()


def _clear_own_attempt(ctx: Ctx, out: Path) -> None:
    """preopen needs its directory new or empty. A refused attempt of THIS run leaves files behind (sel.json, .err
    files): they are moved aside to OUT.failed-N, never deleted. A non-empty directory this run did not start is
    someone else's: refuse and touch nothing."""
    if not out.exists() or not any(out.iterdir()):
        return
    if ctx.data.get("packet_attempt") != str(out):
        raise Refuse(f"{out} is not empty and this run did not create it: it is not touched. Move it away yourself if it is stale",
                     f"ls -la {out}")
    n = 1
    while (aside := out.with_name(f"{out.name}.failed-{n}")).exists():
        n += 1
    out.rename(aside)
    ctx.say(f"  the refused attempt of this run was kept at {aside}")


def _remember_packet(ctx: Ctx, directory: Path, sha: str) -> None:
    ctx.data.update(packet_dir=str(directory), packet=str(directory / "packet.json"), packet_sha256=sha, packet_review=str(directory / "REVIEW.md"))


def packet_review(ctx: Ctx) -> tuple[str, str, str]:
    """The packet under review, bound to its head: an approval of the digest needs the head's `sm check` receipt
    (`steuermaschine.engine.need_check`), so no reviewer approves a head whose cheap checks did not run green."""
    digest, brief, under_review = _packet_review(ctx)
    if not digest:
        return digest, brief, under_review
    facts = ctx.subject.facts
    command = f"sm check {facts['worktree']}"  # typed here, not imported: the check's module would pull its whole unit into this digest
    # The preflight this run adopted proved the head sits on this main (its base is the main the packet is built on),
    # so the checks must have been measured from exactly it: a receipt of a part of the head is refused.
    need_check(ctx.providers.store, digest, facts["head"], command, base=facts["main"])
    return digest, (brief + f"\nAn approval is recorded only when the head {facts['head'][:12]} has a green check receipt measured from main {facts['main'][:12]}: "
                    f"`{command}` writes it. Without it `sm review verdict --approve` refuses, and so does the next admit step."), under_review


def _packet_review(ctx: Ctx) -> tuple[str, str, str]:
    """The digest is recomputed from the packet's bytes on every run: what was reviewed is what admission binds.

    A packet whose verdict is a reject is rebuilt here once the selection file changed (k7fj): the rejected packet
    is kept aside, the new one gets a new digest and with it a new reviewer brief."""
    if manifest_skip(ctx):
        return "", "", ""  # no packet: nothing to review
    digest, brief, under_review = _packet_subject(ctx)
    verdict = ctx.providers.store.get_receipt("verdicts", digest) or {}
    if verdict.get("verdict") != "reject":
        return digest, _after_reject(ctx, brief), under_review
    wt = ctx.subject.facts["worktree"]
    why = f"packet {digest[:12]} was rejected by {verdict.get('reviewer', '?')}: {verdict.get('notes', '')}"
    if ctx.data.get("packet_sel_sha256") == _sel_sha(ctx):
        raise Refuse(f"{why}. The selection is unchanged: add the reviewer's files to it (or point --set sel= at a new file), "
                     "then run admit again; it rebuilds the packet", f"sm head admit {wt} --set sel=SEL.json")
    if preflight_probe(ctx) is None:  # the changed selection is preflighted before a packet is built from it (R3)
        ctx.say(f"  preflight with the changed selection: {preflight(ctx)}")
    out = Path(ctx.data.get("packet_dir") or packet_dir(ctx))
    n = 1
    while (aside := out.with_name(f"{out.name}.rejected-{n}")).exists():
        n += 1
    if out.exists():
        out.rename(aside)
    ctx.say(f"  {why}; kept at {aside}, rebuilding with the changed selection")
    ctx.data.pop("packet_attempt", None)
    ctx.say(f"  {packet(ctx)}")
    ctx.data["rejected_packet"] = {"digest": digest, "reviewer": verdict.get("reviewer", ""), "notes": verdict.get("notes", ""), "kept": str(aside)}
    digest, brief, under_review = _packet_subject(ctx)
    return digest, _after_reject(ctx, brief), under_review


def _after_reject(ctx: Ctx, brief: str) -> str:
    if not (old := ctx.data.get("rejected_packet")):
        return brief
    return (brief + f"\nThis packet replaces {old['digest'][:12]}, rejected by {old['reviewer']}: {old['notes']}\n"
            f"Check that the reject is answered (the rejected packet: {old['kept']}).")


def _packet_subject(ctx: Ctx) -> tuple[str, str, str]:
    sha = packet_sha(Path(ctx.data.get("packet", "")))
    if sha != ctx.data.get("packet_sha256"):
        raise Refuse(f"packet.json changed after it was built (built {str(ctx.data.get('packet_sha256'))[:12]}, now {sha[:12]}): build it again",
                     f"sm head admit {ctx.subject.facts['worktree']}")
    brief = (f"Review the focused admission packet of {ctx.subject.label} at main {ctx.subject.facts['main'][:12]}.\n"
             f"Read: {ctx.data.get('packet')} and the builder's {ctx.data.get('packet_review')}.\n"
             "Check: the selection covers what the head changes, the collected cases match the local JUnit, the resulting tree is the merge of "
             "this head onto this main, nothing outside the packet's scope.")
    skips = ctx.data.get("preflight_skipped")
    if changed := protected_producer(ctx):  # the verdict binds the route: a review of the packet alone does not approve a CI-source change
        record = _protected_record(ctx)
        record.write_text(_protected_bytes(sha, changed, skips))
        brief += (f"\nThis head CHANGES PROTECTED focused-CI sources ({', '.join(changed)}). After your approval `sm head admit` prepares it on the "
                  "protected-producer route by itself (`head prepare --reviewed-protected-producer`).\n"
                  "Approve only if you reviewed those changes as changes of CI itself: what CI runs, selects and judges after the merge.")
        if skips:
            brief += f"\nThe preflight LEFT CHECKS OUT ({', '.join(skips['checks'])}): {skips['reason']}\nApprove only if that reason holds."
        brief += (f"\nThe digest to approve is the sha256 of the bytes of {record} (`shasum -a 256 FILE`); it names the packet's sha256 "
                  "and the changed sources.")
        return packet_sha(record), brief, str(record)
    if skips:  # the verdict binds the skip too: a review of the packet alone does not approve it
        record = Path(ctx.data.get("packet_dir") or Path(ctx.data.get("packet", "")).parent) / "preflight-skips.json"
        record.write_text(json.dumps({"packet_sha256": sha, "preflight_skipped": skips["checks"], "reason": skips["reason"]}, indent=1) + "\n")
        brief += (f"\nThe preflight LEFT CHECKS OUT ({', '.join(skips['checks'])}): {skips['reason']}\n"
                  f"Approve only if that reason holds. The digest to approve is the sha256 of the bytes of {record} "
                  "(`shasum -a 256 FILE`); it names the packet's sha256.")
        return packet_sha(record), brief, str(record)
    return sha, brief, ctx.data.get("packet", "")


# ----------------------------------------------------------------------------- the protected-producer route
PROTECTED_RECORD = "protected-producer.json"


def protected_producer(ctx: Ctx) -> list[str]:
    """The pinned focused-CI sources this head changes: the paths whose blob in the packet's `execution` (the pins of
    the resulting tree, built from main's own protected list at that commit) is not main's blob. Empty: the ordinary
    route. An unreadable packet is empty too: prepare then refuses a protected change with its own reason.

    Read from the packet under review and from main's tree, never from a list in this tool."""
    try:
        execution = json.loads(Path(ctx.data.get("packet", "")).read_text())["manifest"]["execution"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if not isinstance(execution, dict) or not execution or not all(isinstance(k, str) and isinstance(v, str) for k, v in execution.items()):
        return []
    main = ctx.subject.facts["main"]
    rc, out = _git_out(ctx, "ls-tree", "-r", "-z", main, "--", *sorted(execution))
    if rc != 0:
        raise Refuse(f"cannot read the pinned focused-CI sources of main {main[:12]} in {ctx.subject.facts['worktree']}: "
                     "whether this head changes one is unknown", f"git -C {ctx.subject.facts['worktree']} fetch origin main")
    at_main = {path: meta.split()[2] for meta, tab, path in (row.partition("\t") for row in out.split("\0")) if tab and len(meta.split()) == 3}
    return sorted(path for path, blob in execution.items() if at_main.get(path) != blob)


def _protected_record(ctx: Ctx) -> Path:
    return Path(ctx.data.get("packet_dir") or Path(ctx.data.get("packet", "")).parent) / PROTECTED_RECORD


def _protected_bytes(packet: str, changed: list[str], skips: dict[str, Any] | None) -> str:
    """The exact bytes the packet reviewer approves on this route: the packet, the changed sources, a preflight skip."""
    record: dict[str, Any] = {"packet_sha256": packet, "protected_producer": changed}
    if skips:
        record |= {"preflight_skipped": skips["checks"], "reason": skips["reason"]}
    return json.dumps(record, indent=1) + "\n"


def reviewed_protected(ctx: Ctx) -> list[str]:
    """The changed pinned sources, once an independent verdict approves exactly them with this packet; empty on the
    ordinary route. Refuses when the head changes one and no such verdict exists: the route is never taken on a
    verdict that did not name the sources (a verdict on packet.json alone, or on an earlier list)."""
    changed = protected_producer(ctx)
    if not changed:
        return []
    wt = ctx.subject.facts["worktree"]
    expected = _protected_bytes(packet_sha(Path(ctx.data.get("packet", ""))), changed, ctx.data.get("preflight_skipped"))
    digest = hashlib.sha256(expected.encode()).hexdigest()
    record = _protected_record(ctx)
    try:
        same = record.read_text() == expected
    except OSError:
        same = False
    verdict = ctx.providers.store.get_receipt("verdicts", digest) or {}
    if not same or verdict.get("verdict") != "approve" or verdict.get("reviewer") == ctx.author:
        raise Refuse(f"this head changes protected focused-CI sources ({', '.join(changed)}), and no independent verdict approves "
                     f"{record} for exactly them (digest {digest[:12]}): the protected-producer route stays closed. A verdict on "
                     "packet.json alone does not open it; the packet review stop names the sources and asks again",
                     f"sm head admit {wt}")
    return changed


# ----------------------------------------------------------------------------- prepare and apply
def prepared_path(ctx: Ctx) -> Path:
    return Path(ctx.data.get("packet_dir", "")) / "prepare.out"


def prepare_probe(ctx: Ctx) -> str | None:
    if skip := manifest_skip(ctx):
        return skip
    args = Path(ctx.data.get("packet_dir", "")) / "prepare.args.json"
    try:
        stored = json.loads(args.read_text())
    except (OSError, ValueError):
        return None
    if not prepared_path(ctx).is_file() or Path(str(stored.get("packet", ""))).resolve() != Path(ctx.data.get("packet", "")).resolve():
        return None
    try:
        protected = reviewed_protected(ctx)
    except Refuse:
        return None  # not adopted: the prepare step says why the route is closed
    if bool(stored.get("protected")) != bool(protected):
        return None  # prepared for the other route
    return f"prepared {prepared_digest(prepared_path(ctx))[:12]}" + (" on the protected-producer route" if protected else "")


def _branch_pr(ctx: Ctx) -> int | None:
    """The open PR of this head's branch at any head (m598: a new head for an existing PR). None when there is none or
    the list cannot be read: prepare's own pre-open proof then refuses for an open PR it finds."""
    data, _why = _gh(ctx).try_json(["pr", "list", "-R", _repo(ctx), "--head", ctx.subject.facts["branch"], "--state", "open", "--json", "number"])
    numbers = [int(row["number"]) for row in data or [] if isinstance(row, dict) and str(row.get("number", "")).isdigit()]
    return numbers[0] if len(numbers) == 1 else None


def prepare(ctx: Ctx) -> str:
    pr = str(ctx.inputs.get("pr") or "")
    if not pr.isdigit() and (found := _branch_pr(ctx)) is not None:
        ctx.say(f"  the branch has open PR #{found}: this head updates it (prepare binds its current head as the expected remote head)")
        pr = str(found)
    receipts = ctx.inputs.get("nonconsumers")
    protected = reviewed_protected(ctx)
    options: dict[str, Any] = {"reviewed_protected_producer": True} if protected else {}  # the ordinary route passes nothing new
    if protected:
        ctx.say(f"  protected-producer route: the packet review approved the change of {', '.join(protected)}; prepare checks both manifests "
                "with main's resolver and refuses a changed PROTECTED_FILES, PLAN_VARIABLE or REPOSITORY")
    done = ok("head prepare", packet=Path(ctx.data["packet"]), core_repo=Path(ctx.subject.facts["worktree"]),
              review_root=path_input(ctx, "review_root", "where packets live"), pr=int(pr) if pr.isdigit() else None, wait_=True,
              nonconsumers=path_input(ctx, "nonconsumers", "the reviewed receipts file") if receipts else None, **options)
    return f"prepared {done.sha256[:12]} at {done.prepared_path}" + (" on the protected-producer route" if protected else "")


def apply_review(ctx: Ctx) -> tuple[str, str, str]:
    if manifest_skip(ctx):
        return "", "", ""
    path = prepared_path(ctx)
    _same_packet(ctx, path)
    protected = _same_route(ctx, path)
    sha = prepared_digest(path)
    ctx.data["apply_sha256"] = sha  # what the apply step passes as --approved: the digest this stop checked
    brief = (f"Review the prepared registry transaction of {ctx.subject.label}: {path}.\n"
             f"Recompute the digest yourself: sm head hash {path}\n"
             "Check: head, main and slot are this head's; the plan names exactly the packet you (or another reviewer) approved; "
             "nothing else is admitted.")
    if protected:
        brief += (f"\nPROTECTED-PRODUCER ROUTE: this head changes protected focused-CI sources ({', '.join(protected)}); the transaction says "
                  "`protected_producer` true. This verdict is separate from the packet review's: check that the slot carries the PR plan and "
                  f"the paired main plan of this head, and that the packet review approved {_protected_record(ctx)}.")
    return sha, brief, str(path)


def apply_probe(ctx: Ctx) -> str | None:
    if skip := manifest_skip(ctx):
        return skip
    return "a receipt for this head exists" if ctx.subject.key in receipt_heads() else None


def _same_packet(ctx: Ctx, prepared: Path) -> None:
    """The prepared transaction must bind the packet the packet review approved, byte for byte."""
    try:
        bound = str(json.loads(prepared.read_text()).get("packet_sha256") or "")
    except (OSError, ValueError, AttributeError) as exc:
        raise Refuse(f"cannot read the prepared transaction {prepared}: {exc}") from None
    now = packet_sha(Path(ctx.data.get("packet", "")))
    if not (bound == now == ctx.data.get("packet_sha256")):
        raise Refuse(f"the prepared transaction binds packet {bound[:12] or '(none)'}, but the reviewed packet is {str(ctx.data.get('packet_sha256'))[:12]} "
                     f"(its bytes now: {now[:12]}): prepare again from the reviewed packet", f"sm head admit {ctx.subject.facts['worktree']}")


def _same_route(ctx: Ctx, prepared: Path) -> list[str]:
    """The prepared transaction is on the route the packet review approved: `protected_producer` is true exactly when
    this head changes a pinned source (and then only with the verdict that names it). Returns the changed sources."""
    protected = reviewed_protected(ctx)
    try:
        flag = json.loads(prepared.read_text()).get("protected_producer")
    except (OSError, ValueError, AttributeError) as exc:
        raise Refuse(f"cannot read the prepared transaction {prepared}: {exc}") from None
    if bool(flag) is not bool(protected):
        raise Refuse(f"the prepared transaction says protected_producer {flag!r}, but this head "
                     + (f"changes protected sources ({', '.join(protected)})" if protected else "changes no protected source")
                     + ": prepare again", f"sm head admit {ctx.subject.facts['worktree']} --rewind prepare")
    return protected


def apply(ctx: Ctx) -> str:
    _same_packet(ctx, prepared_path(ctx))
    _same_route(ctx, prepared_path(ctx))
    done = ok("head apply", packet=Path(ctx.data["packet"]), approved=ctx.data["apply_sha256"], wait_=True)
    ctx.data["receipt"] = done.receipt_path
    return f"applied; receipt {done.receipt_path}"


# ----------------------------------------------------------------------------- PR and queue
def _pr_of(ctx: Ctx) -> int | None:
    data, _why = _gh(ctx).try_json(["pr", "list", "-R", _repo(ctx), "--head", ctx.subject.facts["branch"], "--state", "open", "--json", "number,headRefOid"])
    for row in data or []:
        if row.get("headRefOid") == ctx.subject.key:
            return int(row["number"])
    return None


def pr_probe(ctx: Ctx) -> str | None:
    number = _pr_of(ctx)
    if number is None:
        return None
    ctx.data["pr"] = number
    return f"PR #{number} is open at this head"


def pr(ctx: Ctx) -> str:
    bead = need(ctx, "bead", "the bead this head closes")
    title = need(ctx, "title", "the PR title")
    body = ctx.inputs.get("body_file")
    done = run("head pr", bead=bead, title=title, body_file=Path(str(body)).expanduser() if body else None, reason=ctx.inputs.get("reviewed") or None,
               worktree=Path(ctx.subject.facts["worktree"]), live=True)
    if done.verdict not in ("done", "needs_review"):
        text = "; ".join(done.lines + done.errors)[:500]
        if done.exit_code == 75:
            from steuermaschine.engine import Wait

            raise Wait(f"sm head pr: {text}", "the push check (main CI or the run lock)")
        raise Refuse(f"sm head pr refused: {text}")
    ctx.data["guard"] = done.verdict
    ctx.data["pr"] = _pr_of(ctx)
    return f"PR #{ctx.data['pr']} open" + (" and queued" if done.verdict == "done" else "; the diff guard flagged it")


def queue_probe(ctx: Ctx) -> str | None:
    number = ctx.data.get("pr")
    if not number:
        return None
    listed = run("queue list", active=False)
    hit = next((e for e in listed.entries if str(e.pr) == str(number)), None)
    return f"PR #{number} is in the queue ({hit.status})" if hit else None


def queue(ctx: Ctx) -> str:
    number = ctx.data.get("pr")
    if not number:
        raise Refuse("no PR is known for this head: the pr step did not record one", f"sm head admit {ctx.subject.facts['worktree']}")
    reviewed = ctx.inputs.get("reviewed")
    if ctx.data.get("guard") == "needs_review" and not reviewed:
        raise Refuse(f"the diff guard flagged PR #{number}: an independent review first, then --set reviewed=\"review: FILE\"", f"sm head guard {number}")
    ok("queue add", pr=[str(number)], bead=ctx.inputs.get("bead") or None, reviewed=reviewed or None, reviewed_head=ctx.subject.key if reviewed else None)
    return f"PR #{number} queued at {ctx.subject.key[:12]}"


def pipeline() -> Pipeline:
    steps = [
        Step("preflight", preflight, probe=preflight_probe, binds=BINDS, trap=TRAPS["preflight"],
             changes="the preflight summary and logs on this machine (sm head preflight)"),
        Step("packet", packet, probe=packet_probe, binds=BINDS, trap=TRAPS["packet"],
             changes="packet files under the review root (sm head packet preopen)"),
        ReviewStop("packet review", packet_review, binds=BINDS, trap=TRAPS["packet review"]),
        Step("prepare", prepare, probe=prepare_probe, binds=BINDS, trap=TRAPS["prepare"],
             changes="prepare.out and prepare.args.json next to the packet (sm head prepare --wait)"),
        ReviewStop("apply review", apply_review, binds=BINDS, trap=TRAPS["apply review"]),
        Step("apply", apply, probe=apply_probe, external=True, binds=BINDS, trap=TRAPS["apply"],
             changes="one repository variable (a focused plan slot) and a receipt (sm head apply)",
             plan=lambda ctx: f"sm head apply {ctx.data.get('packet')} --approved {ctx.data.get('apply_sha256', 'SHA256')}"),
        Step("pr", pr, probe=pr_probe, external=True, trap=TRAPS["pr"], changes="one push of the checked commit, the PR, the guard (sm head pr --live)",
             plan=lambda ctx: f"sm head pr {ctx.inputs.get('bead', 'BEAD')} TITLE --worktree {ctx.subject.facts['worktree']} --live"),
        Step("queue", queue, probe=queue_probe, external=True, trap=TRAPS["queue"], changes="one entry in the merge queue (sm queue add)",
             plan=lambda ctx: f"sm queue add {ctx.data.get('pr', 'PR')}"),
    ]
    return Pipeline("head", VERB, "Admit one head: preflight, packet, two independent reviews, apply, PR and queue; resumable, one step per run.",
                    steps, identify, inputs=INPUTS, when="Use when a head is committed in its own worktree and should ship.", example_arg="~/Code/heinzel-fix",
                    unit=unit("head admit", anchor, "head-admit.md"))
