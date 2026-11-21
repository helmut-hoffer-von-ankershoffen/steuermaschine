"""Releases, read-only: status, the candidate run, published surfaces, the pins, the checklist after a release, the
notes audit, asset probes.

The logic is moved code in `release_read_lib/` and, for the pins and the notes audit, in `release_lib/` (see
`port/move.py verify`). This module turns typed options into calls of it, binds its paths to the settings and returns
result models. Nothing here starts an old script, and nothing here reaches a write on GitHub.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path
from types import SimpleNamespace

import typer
from pydantic import Field

from steuermaschine.engine import Refuse
from steuermaschine.github import GitHub
from steuermaschine.github.models import is_unknown
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime

from .. import state as statedir  # the plugin's state paths
from .release_lib import ios
from .release_lib.binding import assets_cache, captured, conf, core_repo, moved, release_dir, required_core_checkout, train_state_dir
from .release_lib.names import translate, translated

FIRST_ATTEMPT_RULE = ("first-attempt rule: only attempt 1 of the full candidate run counts; a rerun never makes a candidate green "
                      "(a green attempt 2 is evidence for one owner waiver of a KNOWN job, nothing else)")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def github() -> GitHub:
    rt = runtime()
    return GitHub(rt.providers.commands, repo=core_repo(), clock=rt.providers.clock)


# ----------------------------------------------------------------------------- release status
class CheckCounts(Model):
    ok: int = 0
    fail: int = 0
    run: int = 0
    skip: int = 0


class ReleaseStatus(Model):
    """One screen: the release in flight, its candidate, the freeze, what blocks, what is next."""

    active: bool
    version: str = ""
    pull_request: int | None = None
    phase: str = ""
    age_minutes: int | None = None
    candidate_ci: str = Field("", description="Conclusion or status of the candidate's CI run on the release branch.")
    acceptance: str = ""
    acceptance_attempt: int | None = None
    checks: CheckCounts | None = None
    frozen: bool = Field(False, description="The hold of the run lock is set: heavy local commands of builders wait.")
    blockers: list[str] = []
    next: str = ""
    ios: str = Field("", description="The newest release's TestFlight step: build and group, or NOT-CLEAN with its reason (sm release ios).")
    lines: list[str] = []

    def render(self) -> list[str]:
        return self.lines


@native("release status", replaces=("hz-release-status",), help=H(
    "One screen: the release in flight, its PR and candidate CI, acceptance, the freeze, blockers, the next step.",
    "Use when a release is running and you need its state in one call.",
    "nothing (read-only).",
    "sm release status", "sm release candidate VERSION"))
def release_status(line: bool = typer.Option(False, "--line", help="One line for a digest; empty when no release is in flight.")) -> ReleaseStatus:
    rt = runtime()
    status = moved("release_read_lib.status")
    train = status.active_train(str(train_state_dir()))
    now = rt.providers.clock.now()
    if not train or train.get("phase") in (None, "idle"):
        summary = status.summarize(train, None, [], [], False, now)
    else:
        gh, repo, branch = github().gh, core_repo(), f"release/v{train['version']}"
        prs, _ = gh.try_json(["pr", "list", "-R", repo, "--head", branch, "--state", "open", "--json", "number,mergeable,statusCheckRollup"])
        pr = None
        if prs:
            pr = {"number": prs[0]["number"], "mergeable": prs[0]["mergeable"], "checks": status.counts(prs[0]["statusCheckRollup"])}
        runs, _ = gh.try_json(["run", "list", "-R", repo, "--branch", branch, "--limit", "12", "--json", "databaseId,name,status,conclusion,attempt"])
        runs = runs or []
        ci = next((r for r in runs if r["name"].split()[:1] == ["CI"]), None)
        jobs: list[str] = []
        if ci and ci.get("conclusion") == "failure":
            done = gh.run(["run", "view", str(ci["databaseId"]), "-R", repo, "--json", "jobs", "--jq", '.jobs[]|select(.conclusion=="failure")|.name'])
            jobs = done.out.splitlines() if not done.rc else []
        lock_dir, prefix = conf().get("lock_directory") or statedir.root(), conf().get("lock_prefix") or "gate"
        summary = status.summarize(train, pr, runs, jobs, (Path(str(lock_dir)).expanduser() / f"{prefix}.hold").exists(), now)
    if summary.get("active") and not summary.get("pr"):  # past the tag the PR is merged and gone: show the tail stages
        tail = status.read_tail(summary["version"], str(statedir.path("release")))
        if tail is not None:
            summary |= {"tail": status.tail_stages(tail, now)}
    tests = ios.summary(ios.latest_receipt(statedir.dashboard_dir(conf())))
    if not summary.get("active"):
        return ReleaseStatus(active=False, ios=tests, lines=[] if line else [status.render(summary), *filter(None, [tests])])
    text = translate(summary["next"])
    if line and summary.get("tail") is not None:
        lines = [f"RELEASE v{summary['version']} tagged {summary['age_min']}m: tail " + ", ".join(t.split(None, 1)[0] + " " + t.split(None, 1)[1].split(",")[0] for t in summary["tail"]) + f"; {text}"]
    elif line:
        lines = [f"RELEASE v{summary['version']} PR #{summary['pr']} {summary['age_min']}m: CI {summary['ci']}, acceptance {summary['acc']} "
                 f"#{summary['acc_attempt']}, freeze {'on' if summary['frozen'] else 'off'}; {text}"]
    else:
        lines = [*translated(status.render(summary).splitlines()), *filter(None, [tests])]
    return ReleaseStatus(active=True, ios=tests, version=str(summary["version"]), pull_request=summary["pr"], phase=str(summary["phase"] or ""),
                         age_minutes=summary["age_min"], candidate_ci=str(summary["ci"] or ""), acceptance=str(summary["acc"] or ""),
                         acceptance_attempt=summary["acc_attempt"], checks=CheckCounts(**summary["checks"]) if summary["checks"] else None,
                         frozen=bool(summary["frozen"]), blockers=translated(summary["blockers"]), next=text, lines=lines)


# ----------------------------------------------------------------------------- release candidate
class CandidateJob(Model):
    name: str
    conclusion: str
    failed_steps: list[str] = []
    url: str = ""
    log_state: str = "ok"
    log_tail: list[str] = []


class ReleaseCandidate(Model):
    """The candidate run of a release: what failed or was cancelled, and whether it counts."""

    run: str
    version: str = ""
    branch: str = ""
    head: str = ""
    status: str
    conclusion: str = ""
    attempt: int = 1
    first_attempt: bool = Field(description="True when the run is on attempt 1: only then can it be the release's candidate proof.")
    counts: bool = Field(description="True only for a completed, successful first attempt.")
    verdict: str
    rule: str = FIRST_ATTEMPT_RULE
    jobs_state: str = "ok"
    failed: list[CandidateJob] = []
    cancelled: list[CandidateJob] = []
    omitted: list[str] = Field([], description="Failed or cancelled jobs beyond --jobs: named, without a log.")
    url: str = ""

    def render(self) -> list[str]:
        out = [f"candidate run {self.run} {('v' + self.version + ' ') if self.version else ''}attempt {self.attempt}: {self.status} {self.conclusion}".rstrip(),
               f"verdict: {self.verdict}", self.rule]
        if self.jobs_state != "ok":
            out.append(f"jobs: {self.jobs_state}")
        for kind, jobs in (("FAILED", self.failed), ("CANCELLED", self.cancelled)):
            for job in jobs:
                out.append(f"{kind} {job.name}" + (f" (step: {', '.join(job.failed_steps)})" if job.failed_steps else ""))
                out += [f"    {text}" for text in job.log_tail] or ([f"    log: {job.log_state}"] if job.log_state != "ok" else [])
        if self.omitted:
            out.append("not shown: " + ", ".join(self.omitted))
        return out

    @property
    def exit_code(self) -> int:
        return 0 if self.counts else (75 if self.status != "completed" else 1)


@native("release candidate", help=H(
    "The candidate run of a release: failed and cancelled jobs with the end of their logs, and whether it counts.",
    "Use when a release waits on or was refused by its candidate, before any rerun or fix.",
    "nothing (read-only).",
    "sm release candidate 0.9.40", "sm release resume VERSION   (or fix forward; `sm release drop VERSION` for a failed candidate)",
    "TARGET is a version X.Y.Z (the newest CI run on its release branch) or a run ID. Exit 0: green on its first attempt.\n"
    "Exit 1: failed, cancelled, or green only on a rerun. Exit 75: still running."))
def release_candidate(target: str = typer.Argument(..., metavar="VERSION|RUN", help="A version X.Y.Z or a run ID."),
                      lines: int = typer.Option(25, "--lines", min=1, max=400, help="Log lines per job."),
                      jobs: int = typer.Option(6, "--jobs", min=0, max=40, help="Failed or cancelled jobs shown with a log.")) -> ReleaseCandidate:
    hub = github()
    version = target.removeprefix("v") if VERSION.fullmatch(target.removeprefix("v")) else ""
    if version:
        listed = hub.runs(branch=f"release/v{version}", limit=30)
        found = sorted((r for r in listed.runs if r.workflow.split()[:1] == ["CI"]), key=lambda r: r.created)
        if not found:
            why = listed.state if is_unknown(listed.state) else "no CI run on the release branch"
            raise Refuse(f"no candidate run for v{version}: {why}", "sm release status")
        run = found[-1]
    elif target.isdigit():
        run = hub.run(target)
        if is_unknown(run.status):
            raise Refuse(f"run {target}: {run.status}", "sm release status")
    else:
        raise Refuse(f"{target!r} is neither a version X.Y.Z nor a run ID", "sm release candidate --help", code=2)
    listing = hub.jobs(run.id)
    bad = [(j, "failed") for j in listing.jobs if j.name in listing.failed] + [(j, "cancelled") for j in listing.jobs if j.name in listing.cancelled]
    shown: dict[str, list[CandidateJob]] = {"failed": [], "cancelled": []}
    for job, kind in bad[:jobs]:
        tail = hub.log_tail(job.id, lines)
        shown[kind].append(CandidateJob(name=job.name, conclusion=job.conclusion, failed_steps=job.failed_steps, url=job.url, log_state=tail.state, log_tail=tail.lines))
    first = run.attempt == 1
    counts = first and run.status == "completed" and run.conclusion == "success"
    if run.status != "completed":
        verdict = f"running (attempt {run.attempt})" + ("" if first else ": a rerun, it will not count")
    elif counts:
        verdict = "GREEN on its first attempt: it counts"
    elif run.conclusion == "success":
        verdict = f"green only on attempt {run.attempt}: it does NOT count; fix forward, or one owner waiver for a KNOWN job"
    else:
        verdict = f"RED ({run.conclusion}): {len(listing.failed)} failed, {len(listing.cancelled)} cancelled; do not rerun to make it green"
    return ReleaseCandidate(run=run.id, version=version, branch=run.branch, head=run.head, status=run.status, conclusion=run.conclusion, attempt=run.attempt,
                            first_attempt=first, counts=counts, verdict=verdict, jobs_state=listing.state, failed=shown["failed"], cancelled=shown["cancelled"],
                            omitted=[j.name for j, _ in bad[jobs:]], url=run.url)


class CandidateProof(Model):
    """Authenticated full candidate evidence bound to independently reviewed identities."""
    pr: int
    head: str
    base: str
    tree: str
    run_id: int
    run_attempt: int
    job_count: int

    def render(self) -> list[str]:
        return [f"verified original full candidate run {self.run_id} for PR #{self.pr}",
                f"head {self.head} base {self.base} tree {self.tree}; {self.job_count} jobs"]


@native("release candidate-proof", writes=False, help=H(
    "Verify the original full release candidate against reviewed source identities.",
    "Use before an independently reviewed, head-pinned release merge.",
    "nothing (read-only); creates no receipt or approval.",
    "sm release candidate-proof 123 --head SHA --base SHA --tree SHA",
    "review the evidence and merge only the exact approved head.",
    "Checks the complete original candidate job inventory and source artifact using the same verifier as release merge.\n"
    "All three expected Git identities are required; a moved PR, base or tree refuses."))
def release_candidate_proof(
    pr: int = typer.Argument(..., help="The open release pull request number."),
    head: str = typer.Option(..., "--head", help="The independently reviewed release head."),
    base: str = typer.Option(..., "--base", help="The independently reviewed protected base."),
    tree: str = typer.Option(..., "--tree", help="The independently reviewed release tree."),
) -> CandidateProof:
    from .release_lib import candidate_policy

    if pr <= 0 or any(re.fullmatch(r"[0-9a-f]{40}", value) is None for value in (head, base, tree)):
        raise Refuse("candidate proof requires a positive PR and three exact Git identities")
    try:
        evidence = candidate_policy.verify_pr(pr)
    except Exception as error:  # fail closed; do not turn malformed or unavailable evidence into a pass
        raise Refuse(f"candidate proof refused: {type(error).__name__}: {error}") from error
    if (evidence.get("head"), evidence.get("base"), evidence.get("tree")) != (head, base, tree):
        raise Refuse("candidate evidence differs from the independently reviewed head, base or tree")
    return CandidateProof(pr=pr, head=head, base=base, tree=tree, run_id=evidence["run_id"],
                          run_attempt=evidence["run_attempt"], job_count=evidence["job_count"])


# ----------------------------------------------------------------------------- release verify
class SurfaceCheck(Model):
    passed: bool
    text: str


class ReleaseVerify(Model):
    """What is live: one PASS or FAIL per published surface, and the Mini when asked."""

    version: str = ""
    checks: list[SurfaceCheck] = []
    mini_live: bool | None = Field(None, description="With --mini: whether the Mini runs an accepted release at or above --minimum.")
    mini_version: str = ""
    mini_reason: str = ""
    lines: list[str] = []

    def render(self) -> list[str]:
        return self.lines

    @property
    def exit_code(self) -> int:
        return 0 if all(c.passed for c in self.checks) and self.mini_live is not False else 1


SURFACES = ("pypi", "release", "app")


@native("release verify", replaces=("hz-verify",), help=H(
    "Check what is live: the version on PyPI, the core release, the app release, a site page; with --mini the Mini.",
    "Use before saying a version is published or released, and after a release finished.",
    "nothing (read-only); it reads PyPI, GitHub and, with --site, one page.",
    "sm release verify 0.9.40 --homepage heinzel.team --latest", "sm release after VERSION",
    "Prints PASS or FAIL per line; exit 1 on any FAIL. Without --surface the checks are pypi, release (vX.Y.Z in the core\n"
    "repository) and app (app-vX.Y.Z in the plugins repository). --mini adds the read-only check of the live service."))
def release_verify(version: str | None = typer.Argument(None, metavar="[VERSION]", help="X.Y.Z; not needed for --site or --mini alone."),
                   surface: list[str] = typer.Option([], "--surface", metavar="pypi|release|app", help="Check only these surfaces (repeatable)."),
                   package: str = typer.Option("heinzel", "--package", help="The PyPI project."),
                   homepage: str = typer.Option("", "--homepage", metavar="HOST", help="The PyPI project links must mention this host."),
                   latest: bool = typer.Option(False, "--latest", help="The app release must be the latest release of its repository."),
                   site: list[str] = typer.Option([], "--site", metavar="PATH", help="A page that must answer 200 (repeatable)."),
                   contains: list[str] = typer.Option([], "--contains", metavar="TEXT", help="Text every --site page must contain (repeatable)."),
                   base: str = typer.Option("https://heinzel.team", "--base", metavar="URL", help="Base of a relative --site path."),
                   mini: bool = typer.Option(False, "--mini", help="Also prove that the Mini runs an accepted release."),
                   minimum: str = typer.Option("", "--minimum", metavar="X.Y.Z", help="With --mini: the lowest version that satisfies the check.")) -> ReleaseVerify:
    wanted = list(surface) or (list(SURFACES) if version else [])
    if unknown := [s for s in wanted if s not in SURFACES]:
        raise Refuse(f"unknown surface {unknown[0]!r}: one of {', '.join(SURFACES)}", "sm release verify --help", code=2)
    if wanted and not (version and VERSION.fullmatch(version)):
        raise Refuse("a surface check needs a version X.Y.Z", "sm release verify --help", code=2)
    if minimum and not mini:
        raise Refuse("--minimum goes with --mini", "sm release verify --help", code=2)
    if not wanted and not site and not mini:
        raise Refuse("nothing to verify: give a version, --site or --mini", "sm release verify --help", code=2)
    verify = moved("release_read_lib.verify")
    calls = []
    for name in wanted:
        if name == "pypi":
            calls.append((verify.pypi, SimpleNamespace(name=package, version=version, homepage=homepage or None)))
        else:
            tag = f"v{version}" if name == "release" else f"app-v{version}"
            calls.append((verify.release, SimpleNamespace(tag=tag, latest=latest and name == "app", repo=None)))
    calls += [(verify.site, SimpleNamespace(path=path, contains=list(contains), base=base)) for path in site]
    lines: list[str] = []
    for call, args in calls:
        lines += captured(lambda call=call, args=args: 0 if call(args) else 1)[1]
    result = ReleaseVerify(version=version or "", checks=[SurfaceCheck(passed=text.startswith("PASS "), text=text[5:]) for text in lines], lines=list(lines))
    if mini:
        try:
            from .go import world
            from .mini import verify as verify_mini
        except ImportError as exc:
            raise Refuse(f"the Mini check is not part of this build ({exc})", "sm release verify VERSION") from exc
        proof = verify_mini(world(), minimum=minimum or None)
        result.mini_live, result.mini_version, result.mini_reason = bool(proof.live), str(proof.version or ""), str(proof.reason or "")
        result.lines += proof.render()
    return result


# ----------------------------------------------------------------------------- release after
class ReleaseAfter(Model):
    """The checklist after a release was published."""

    version: str
    steps: list[str]
    lines: list[str] = []

    def render(self) -> list[str]:
        return self.lines


@native("release after", replaces=("hz-chores",), help=H(
    "The checklist after a release was published: resume deferred CI, verify the Mini, close beads, tell the owner.",
    "Use after `sm release resume` printed ACCEPTED for the version.",
    "nothing (read-only): it prints the steps, it runs none.",
    "sm release after 0.9.40", "sm release train resume-ci --dry-run"))
def release_after(version: str = typer.Argument(..., metavar="VERSION", help="The published version X.Y.Z.")) -> ReleaseAfter:
    if not VERSION.fullmatch(version):
        raise Refuse(f"{version!r} is not a version X.Y.Z", "sm release after --help", code=2)
    chores = moved("release_read_lib.chores")
    _, out, _ = captured(chores.cmd_post, SimpleNamespace(version=version))
    lines = translated(out)
    return ReleaseAfter(version=version, steps=[text.strip() for text in lines[1:]], lines=lines)


# ----------------------------------------------------------------------------- release notes-audit
class NotesAudit(Model):
    """Members of combined PRs that the newest changelog section of the release checkout does not name."""

    checkout: str
    base: str
    missing: list[str] = Field(description="PR numbers named by commit bodies since the last tag but absent from the newest changelog section.")
    problem: str = Field("", description="Why the audit could not read the changelog.")

    def render(self) -> list[str]:
        if self.problem:
            return [f"notes audit: {self.problem} in {self.checkout}"]
        if self.missing:
            return ["notes audit: the release notes drop combine members " + ", ".join("#" + n for n in self.missing),
                    "fix the combine PR body lines (Combines #N type: title), then cut again"]
        return [f"notes audit: ok (every combine member since the last tag up to {self.base} is in the newest changelog section)"]

    @property
    def exit_code(self) -> int:
        return 1 if self.missing or self.problem else 0


@native("release notes-audit", help=H(
    "Check that the release notes name every member of a combined PR merged since the last tag.",
    "Use before a release PR is pushed, when the cut stopped on dropped combine members.",
    "nothing (read-only); it reads the release checkout with git.",
    "sm release notes-audit", "sm release resume VERSION"))
def release_notes_audit(base: str = typer.Option("origin/main", "--base", metavar="REF", help="Commits since the last tag up to this ref are audited."),
                        checkout: Path | None = typer.Option(None, "--checkout", metavar="DIR", help="The release checkout; default: the setting release_dir.")) -> NotesAudit:
    work = str(checkout.expanduser() if checkout else release_dir())
    absent = moved("release_lib.notes_audit").audit(work, base)
    problem = absent[0] if absent and not absent[0].isdigit() else ""
    return NotesAudit(checkout=work, base=base, missing=[] if problem else absent, problem=problem)


# ----------------------------------------------------------------------------- release assets
class AssetProbe(Model):
    name: str
    url: str
    ok: bool
    status: str


class ReleaseAssets(Model):
    """A real GET of every external asset that hooks and CI download."""

    results: list[AssetProbe] = []
    stable: bool | None = Field(None, description="With --stable: whether N all-OK rounds in a row were reached.")
    digest: str
    lines: list[str] = []

    def render(self) -> list[str]:
        return self.lines

    @property
    def exit_code(self) -> int:
        return 0 if all(r.ok for r in self.results) and self.stable is not False else 1


@native("release assets", writes=True, replaces=("hz-assets-health",), help=H(
    "Probe with a real GET every external asset that hooks and CI download; 200 or 206 is OK.",
    "Use before a release is cut and when CI fails on a download.",
    "the probe cache file (setting release_assets_cache); nothing else.",
    "sm release assets", "sm release resume VERSION",
    "The URLs are parsed from the core checkout (setting release_core_checkout), not hardcoded. Exit 1 on any BAD."))
def release_assets(stable: int = typer.Option(0, "--stable", metavar="N", min=0, help="Wait for N all-OK rounds in a row; at most 240 rounds."),
                   interval: int = typer.Option(30, "--interval", metavar="SECONDS", min=1, help="Seconds between rounds with --stable."),
                   digest: bool = typer.Option(False, "--digest", help="Print the cached digest line only; probe nothing.")) -> ReleaseAssets:
    rt = runtime()
    assets, cache = moved("release_read_lib.assets"), str(assets_cache())
    if digest:
        text = translate(assets.digest_line(cache))
        return ReleaseAssets(digest=text, lines=[text])
    items = assets.assets(str(required_core_checkout("release assets")))
    reached = None
    if stable:
        with contextlib.redirect_stderr(io.StringIO()) as progress:
            reached, found = assets.wait_stable(stable, interval, lambda: assets.check(items), sleep=rt.providers.clock.sleep, report=True)
        for text in progress.getvalue().splitlines():
            rt.say(text)
    else:
        found = assets.check(items)
    assets.save(found, cache)
    text = translate(assets.digest_line(cache))
    lines = [f"{'ok ' if ok else 'BAD'} {status!s:<8} {name:<32} {url[:70]}" for name, url, ok, status in found] + [text]
    return ReleaseAssets(results=[AssetProbe(name=name, url=url, ok=bool(ok), status=str(status)) for name, url, ok, status in found],
                         stable=reached, digest=text, lines=lines)
