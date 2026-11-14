"""Result models of the GitHub and Sonar providers. The same model is the provider's return value, the `--format json`
document and the published schema (`sm self schema NAME`).

Convention: a field named `state` (or `status`, `mergeable`, `gate`) that could not be read holds
`unknown: <why>`. `is_unknown(value)` tells. Timestamps are the host's own ISO strings.
"""

from __future__ import annotations

from ..models import Model

UNKNOWN = "unknown"


def unknown(why: str) -> str:
    return f"{UNKNOWN}: {' '.join(str(why).split())[:240] or 'no reason given'}"


def is_unknown(value: object) -> bool:
    return isinstance(value, str) and value.startswith(UNKNOWN)


def _code(*values: object) -> int:
    """Exit code of a read: 1 when what was asked could not be read, else 0. The state field carries the answer."""
    return 1 if any(is_unknown(v) for v in values) else 0


def _more(truncated: bool) -> list[str]:
    return ["(more exist than are listed: raise --limit)"] if truncated else []


# ----------------------------------------------------------------------------- pull requests
class PullRequest(Model):
    """One pull request. Has the fields of the core `PR` (number, branch, head, state, url) and more."""

    state: str  # open | merged | closed | none (no PR for this head) | unknown: <why>
    number: int = 0
    title: str = ""
    branch: str = ""  # head branch
    head: str = ""  # head commit
    base: str = ""  # base branch
    draft: bool = False
    mergeable: str = ""  # mergeable | conflicting | unknown (the host has not computed it)
    merge_state: str = ""  # clean | blocked | behind | dirty | unstable | draft | has_hooks | unknown
    review: str = ""  # approved | changes_requested | review_required | ""
    auto_merge: bool = False
    author: str = ""
    updated: str = ""
    url: str = ""

    @property
    def exit_code(self) -> int:
        return _code(self.state)

    def line(self) -> str:
        flags = " ".join(w for w in ("draft" if self.draft else "", self.merge_state if self.state == "open" else "", "auto-merge" if self.auto_merge else "") if w)
        return f"#{self.number:<6} {self.state:<7} {self.head[:8]:<8} {self.branch}  {flags}  {self.title}".rstrip()

    def render(self) -> list[str]:
        if is_unknown(self.state):
            return [f"PR {self.number or self.branch or self.head}: {self.state}"]
        if self.state == "none":
            return [f"no pull request has the head {self.branch or self.head}"]
        lines = [f"PR {self.number} {self.state}{' (draft)' if self.draft else ''}: {self.title}",
                 f"  head {self.branch} {self.head[:12]} -> {self.base}   by {self.author or '?'}   updated {self.updated}"]
        if self.state == "open":
            lines.append(f"  mergeable {self.mergeable or '?'}, merge state {self.merge_state or '?'}" + (f", review {self.review}" if self.review else "")
                         + (", auto-merge on" if self.auto_merge else ""))
        return [*lines, f"  {self.url}"]


class PullRequestList(Model):
    state: str = "ok"  # ok | unknown: <why>
    truncated: bool = False  # more exist than the limit
    pulls: list[PullRequest] = []

    @property
    def exit_code(self) -> int:
        return _code(self.state)

    def render(self) -> list[str]:
        if is_unknown(self.state):
            return [f"open pull requests: {self.state}"]
        return [p.line() for p in self.pulls] + _more(self.truncated) or ["no open pull request"]


class Check(Model):
    """One check or status context on a commit. Has the fields of the core `Check` (name, state, url)."""

    name: str
    state: str  # green | red | pending | skipped
    required: bool = False
    raw: str = ""  # the host's own word: SUCCESS, FAILURE, IN_PROGRESS, CANCELLED ...
    workflow: str = ""
    event: str = ""
    started: str = ""
    completed: str = ""
    url: str = ""


class ChecksRollup(Model):
    """All checks of one pull request, required ones apart from optional ones."""

    pr: int
    head: str = ""
    state: str  # green | red | pending | none (nothing reported, nothing required) | unknown: <why>
    scope: str = "required"  # required: `state` is over the required contexts; all: none is required, so over every check
    required: list[str] = []  # the contexts the base branch requires
    required_source: str = ""  # rules | protection | rules+protection | checks | unknown: <why>
    missing_required: list[str] = []  # required and not reported on this head at all
    red_required: list[str] = []
    pending_required: list[str] = []
    red_optional: list[str] = []
    pending_optional: list[str] = []
    checks: list[Check] = []

    @property
    def exit_code(self) -> int:
        return _code(self.state)

    def render(self) -> list[str]:
        if is_unknown(self.state) and not self.checks:
            return [f"PR {self.pr} checks: {self.state}"]
        over = "over the required contexts" if self.scope == "required" else "no context is required, so over every check"
        lines = [f"PR {self.pr} checks: {self.state}   ({over}; required read from: {self.required_source or '?'})   head {self.head[:12]}"]
        lines += [f"  MISSING  {n}   (required, has not reported on this head)" for n in self.missing_required]
        lines += [f"  {c.state:<8} {c.name}   (required)  {c.url}".rstrip() for c in self.checks if c.required]
        lines += [f"  {c.state:<8} {c.name}   (optional)  {c.url}".rstrip() for c in self.checks if not c.required and c.state in ("red", "pending")]
        quiet = [c for c in self.checks if not c.required and c.state in ("green", "skipped")]
        if quiet:
            lines.append(f"  optional and quiet: {sum(c.state == 'green' for c in quiet)} green, {sum(c.state == 'skipped' for c in quiet)} skipped")
        return lines


class BranchTip(Model):
    branch: str
    state: str  # found | missing | unknown: <why>
    sha: str = ""


class Reason(Model):
    kind: str  # merged | closed | draft | conflicts | behind | missing_required | red_required | pending_required | review | threads | blocked | computing
    text: str
    next: str = ""  # what helps


class WhyReport(Model):
    """Why a pull request is not mergeable: every reason that could be seen, or that none holds."""

    pr: int
    mergeable: str  # yes | no | unknown: <why>
    head: str = ""
    base: str = ""
    merge_state: str = ""
    behind_by: int | None = None  # commits on the base branch the head does not have
    reasons: list[Reason] = []
    checks: str = ""  # the rollup state
    url: str = ""

    @property
    def exit_code(self) -> int:
        return _code(self.mergeable)

    def render(self) -> list[str]:
        if is_unknown(self.mergeable) and not self.reasons:
            return [f"PR {self.pr}: {self.mergeable}"]
        verdict = {"yes": "is mergeable: nothing visible holds it", "no": "is NOT mergeable"}.get(self.mergeable, self.mergeable)
        facts = [f"merge state {self.merge_state or '?'}", f"checks {self.checks or '?'}"] + ([f"behind {self.base} by {self.behind_by}"] if self.behind_by else [])
        lines = [f"PR {self.pr} {verdict}   ({', '.join(facts)})"]
        for r in self.reasons:
            lines.append(f"  {r.kind}: {r.text}")
            if r.next:
                lines.append(f"    -> {r.next}")
        return lines + ([f"  {self.url}"] if self.url else [])


class PullRequestDraft(Model):
    """What `create_pr` did or would do."""

    state: str  # dry_run | created | exists | unknown: <why>
    live: bool = False
    would_run: str = ""
    pr: PullRequest | None = None

    @property
    def exit_code(self) -> int:
        return _code(self.state)

    def render(self) -> list[str]:
        if self.state == "dry_run":
            return [f"dry run, nothing was created. With --live this runs: {self.would_run}"]
        return [f"pull request: {self.state}"] + (self.pr.render() if self.pr else [])


# ----------------------------------------------------------------------------- CI
class Run(Model):
    """One CI run. Has the fields of the core `Run` (id, status, branch, event, workflow)."""

    id: str
    status: str  # queued | in_progress | completed | waiting | pending | requested | unknown: <why>
    conclusion: str = ""  # success | failure | cancelled | skipped | timed_out | ... ; empty while it runs
    workflow: str = ""
    title: str = ""
    branch: str = ""
    head: str = ""
    event: str = ""
    attempt: int = 1
    created: str = ""
    updated: str = ""
    url: str = ""

    @property
    def exit_code(self) -> int:
        return _code(self.status)

    def line(self) -> str:
        state = self.status + (f"/{self.conclusion}" if self.conclusion else "")
        return f"{self.id:<12} {state:<21} {self.workflow}  {self.branch} {self.head[:8]}  {self.event}  {self.created}".rstrip()

    def render(self) -> list[str]:
        return [self.line()] + ([f"  {self.url}"] if self.url else [])


class RunList(Model):
    state: str = "ok"  # ok | unknown: <why>
    truncated: bool = False
    runs: list[Run] = []

    @property
    def exit_code(self) -> int:
        return _code(self.state)

    def render(self) -> list[str]:
        if is_unknown(self.state):
            return [f"runs: {self.state}"] + [r.line() for r in self.runs]
        return [r.line() for r in self.runs] + _more(self.truncated) or ["no run"]


class Job(Model):
    id: str
    name: str
    status: str  # queued | in_progress | completed | waiting
    conclusion: str = ""
    failed_steps: list[str] = []
    started: str = ""
    completed: str = ""
    url: str = ""


class RunJobs(Model):
    """The jobs of one run (latest attempt), with the failed and the cancelled ones named apart."""

    run: str
    state: str = "ok"  # ok | unknown: <why>
    truncated: bool = False
    total: int = 0
    jobs: list[Job] = []
    failed: list[str] = []  # job names
    cancelled: list[str] = []


class LogTail(Model):
    job: str
    state: str = "ok"  # ok | unknown: <why>
    total_lines: int = 0
    ends_at: str = "end"  # error: the lines end at the last error line of the log; end: at the end of the log
    lines: list[str] = []


class FailedJob(Model):
    job: Job
    log: LogTail


class FailedReport(Model):
    """What failed in one run: its failed and cancelled jobs, each with the end of its log."""

    run: Run
    state: str = "ok"  # ok | unknown: <why>
    failed: list[FailedJob] = []
    cancelled: list[FailedJob] = []
    omitted: list[str] = []  # failed or cancelled jobs beyond the limit: named, without a log

    @property
    def exit_code(self) -> int:
        return _code(self.state, self.run.status)

    def render(self) -> list[str]:
        lines = [f"run {self.run.line()}"] if not is_unknown(self.run.status) else [f"run {self.run.id}: {self.run.status}"]
        if is_unknown(self.state):
            lines.append(f"jobs: {self.state}")
        for word, rows in (("FAILED", self.failed), ("CANCELLED", self.cancelled)):
            for row in rows:
                steps = f"   failed step: {', '.join(row.job.failed_steps)}" if row.job.failed_steps else ""
                lines.append(f"{word} {row.job.name}   (job {row.job.id}){steps}   {row.job.url}".rstrip())
                lines += [f"  log: {row.log.state}"] if is_unknown(row.log.state) else [f"  | {ln}" for ln in row.log.lines]
        if self.omitted:
            lines.append(f"not shown (raise --jobs): {', '.join(self.omitted)}")
        if not (self.failed or self.cancelled or self.omitted) and not is_unknown(self.state) and not is_unknown(self.run.status):
            lines.append("no failed or cancelled job" + ("" if self.run.status == "completed" else " so far: the run is not finished"))
        return lines


class CancelResult(Model):
    run: str
    state: str  # dry_run | cancelled | not_cancellable | unknown: <why>
    live: bool = False
    would_run: str = ""
    detail: str = ""

    @property
    def exit_code(self) -> int:
        return 1 if is_unknown(self.state) or self.state == "not_cancellable" else 0

    def render(self) -> list[str]:
        if self.state == "dry_run":
            return [f"dry run, nothing was cancelled: run {self.run} ({self.detail})", f"with --live this runs: {self.would_run}"]
        if self.state == "not_cancellable":
            return [f"run {self.run} is finished and cannot be cancelled ({self.detail})"]
        return [f"run {self.run}: {self.state}" + (f" ({self.detail})" if self.detail else "")]


class CommitStatus(Model):
    """CI on one commit. Has the fields of the core `CIStatus` (state, failing, url); `state` is exactly
    `unknown` when it could not be read and `detail` says why."""

    state: str  # green | red | pending | unknown
    failing: tuple[str, ...] = ()
    url: str = ""
    detail: str = ""


# ----------------------------------------------------------------------------- variables
class Variable(Model):
    name: str
    state: str  # found | missing | unknown: <why>
    value: str | None = None
    updated: str = ""


class VariableList(Model):
    state: str = "ok"
    truncated: bool = False
    variables: list[Variable] = []


class VariableWrite(Model):
    name: str
    state: str  # dry_run | written | refused: <why> | unknown: <why>
    live: bool = False
    would_run: str = ""


# ----------------------------------------------------------------------------- code quality
class QualityCondition(Model):
    metric: str
    status: str  # ok | error | warn | none
    actual: str = ""
    threshold: str = ""
    comparator: str = ""


class QualityIssue(Model):
    key: str
    severity: str = ""
    type: str = ""
    rule: str = ""
    file: str = ""
    line: int | None = None
    message: str = ""


class QualityReport(Model):
    """Code quality of one pull request, from the quality provider (Sonar)."""

    pr: int
    project: str = ""
    state: str = "ok"  # ok | unknown: <why>  (ok: every part below was read)
    gate: str = ""  # passed | failed | none | unknown: <why>
    conditions: list[QualityCondition] = []
    debt_ratio: float | None = None  # technical debt ratio on new code, percent
    duplication: float | None = None  # duplicated lines on new code, percent
    coverage: float | None = None  # coverage on new code, percent; None when the host has none
    open_issues: int | None = None
    issues: list[QualityIssue] = []
    issues_truncated: bool = False
    url: str = ""

    @property
    def exit_code(self) -> int:
        return _code(self.state)

    def render(self) -> list[str]:
        def pct(value: float | None) -> str:
            return "no value" if value is None else f"{value:g}%"

        lines = [f"PR {self.pr} quality gate: {self.gate or '?'}   (project {self.project or '?'})"]
        if is_unknown(self.state):
            lines.append(f"  not everything could be read: {self.state}")
        lines += [f"  {c.status.upper():<5} {c.metric}: {c.actual} (fails when {c.comparator} {c.threshold})" for c in self.conditions if c.status != "ok"]
        lines.append(f"  new code: debt ratio {pct(self.debt_ratio)}, duplication {pct(self.duplication)}, coverage {pct(self.coverage)}")
        lines.append(f"  open issues: {'unknown' if self.open_issues is None else self.open_issues}")
        lines += [f"    {i.severity:<8} {i.file}{':' + str(i.line) if i.line else ''}  {i.message}  [{i.rule}]" for i in self.issues]
        if self.issues_truncated:
            lines.append("    (more issues exist than are listed: raise --issues)")
        return lines + ([f"  {self.url}"] if self.url else [])


PROVIDER_MODELS: dict[str, type[Model]] = {m.__name__: m for m in (PullRequest, PullRequestList, Check, ChecksRollup, BranchTip, WhyReport, PullRequestDraft, Run, RunList, Job,
                                           RunJobs, LogTail, FailedJob, FailedReport, CancelResult, CommitStatus, Variable, VariableList, VariableWrite, QualityReport)}
