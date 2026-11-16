"""GitHub through the `gh` CLI: the core `Forge` and `CIProvider`, and the richer `ForgeReader` and `CIRuns`.

    GitHub(commands, repo="owner/name")          commands: the `Commands` provider

Every process goes through the one `Gh` client (client.py), so tests script `gh`. Reads never raise for what GitHub
answers (the model says `unknown: <why>`). Writes (`cancel_run`, `create_pr`, `write_variable`) are dry unless
`live=True` and say what they would run; `write_variable` additionally needs `variable_writes=True` at construction.
The methods of the small core Protocols (`pr_for_branch`, `checks`, `merge`, ...) raise `ProviderError` where their
return type has no room for `unknown`; `open_pr`, `merge`, `tag` and `set_variable` write and are for external steps.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any
from urllib.parse import quote

from ..protocols import Clock, Commands
from .base import ProviderError
from .client import Gh
from .models import (
    BranchTip,
    CancelResult,
    Check,
    ChecksRollup,
    CommitStatus,
    Job,
    LogTail,
    PullRequest,
    PullRequestDraft,
    PullRequestList,
    Reason,
    Run,
    RunJobs,
    RunList,
    Variable,
    VariableList,
    VariableWrite,
    WhyReport,
    is_unknown,
    unknown,
)

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
STAMP = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z ")
NOT_QUEUED_CANCEL = "Cannot cancel a workflow run that has not been queued yet"
SHA = re.compile(r"^[0-9a-f]{7,40}$")
ACTIVE = ("in_progress", "queued", "waiting", "pending", "requested")  # every status of a run that is not finished
PR_FIELDS = "number,title,state,isDraft,headRefName,headRefOid,baseRefName,mergeable,mergeStateStatus,reviewDecision,url,author,updatedAt,autoMergeRequest"
CHECK_FIELDS = "name,state,bucket,link,workflow,event,startedAt,completedAt"
BUCKET = {"pass": "green", "fail": "red", "cancel": "red", "pending": "pending", "skipping": "skipped"}
THREADS = "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100){nodes{isResolved}}}}}"


def _pr(d: dict[str, Any]) -> PullRequest:
    return PullRequest(state=str(d.get("state", "")).lower() or "unknown: no state", number=d.get("number") or 0, title=d.get("title", ""),
                       branch=d.get("headRefName", ""), head=d.get("headRefOid", ""), base=d.get("baseRefName", ""), draft=bool(d.get("isDraft")),
                       mergeable=str(d.get("mergeable", "")).lower(), merge_state=str(d.get("mergeStateStatus", "")).lower(),
                       review=str(d.get("reviewDecision") or "").lower(), auto_merge=bool(d.get("autoMergeRequest")),
                       author=(d.get("author") or {}).get("login", ""), updated=d.get("updatedAt", ""), url=d.get("url", ""))


def _run(d: dict[str, Any]) -> Run:
    return Run(id=str(d.get("id", "")), status=d.get("status") or "unknown: no status", conclusion=d.get("conclusion") or "", workflow=d.get("name") or "",
               title=d.get("display_title") or "", branch=d.get("head_branch") or "", head=d.get("head_sha") or "", event=d.get("event") or "",
               attempt=d.get("run_attempt") or 1, created=d.get("created_at") or "", updated=d.get("updated_at") or "", url=d.get("html_url") or "")


def _job(d: dict[str, Any]) -> Job:
    return Job(id=str(d.get("id", "")), name=d.get("name") or "", status=d.get("status") or "", conclusion=d.get("conclusion") or "",
               failed_steps=[s.get("name", "") for s in d.get("steps") or [] if s.get("conclusion") in ("failure", "timed_out")],
               started=d.get("started_at") or "", completed=d.get("completed_at") or "", url=d.get("html_url") or "")


def _check_state(status: str, conclusion: str) -> str:
    """A REST check run or commit status as green | red | pending | skipped."""
    status, conclusion = (status or "").lower(), (conclusion or "").lower()
    if status in ("success",):
        return "green"
    if status in ("failure", "error"):
        return "red"
    if status != "completed":
        return "pending"
    if conclusion in ("skipped", "neutral"):
        return "skipped"
    return "green" if conclusion == "success" else "red"


class GitHub:
    def __init__(self, commands: Commands, repo: str = "", timeout: float = 30.0, max_pages: int = 3, clock: Clock | None = None, cwd: str | None = None,
                 merge_method: str = "squash", variable_writes: bool = False, binary: str = "gh") -> None:
        self.gh = Gh(commands, repo, timeout, max_pages, clock, cwd, binary)
        self.merge_method, self.variable_writes = merge_method, variable_writes

    @classmethod
    def from_settings(cls, commands: Commands, settings: Any, clock: Clock | None = None, cwd: str | None = None) -> GitHub:
        """`settings`: a `GitHubSettings` or a dict with its fields."""
        s = settings if isinstance(settings, dict) else settings.model_dump()
        return cls(commands, s.get("repo", ""), s.get("timeout_seconds", 30.0), s.get("max_pages", 3), clock, cwd, s.get("merge_method", "squash"),
                   s.get("variable_writes", False))

    # ------------------------------------------------------------------ pull requests (ForgeReader)
    def pr(self, ref: str) -> PullRequest:
        """By number, by head branch (the open one first, else the newest), or by head commit."""
        ref = str(ref).strip().lstrip("#")
        repo, why = self.gh.repo()
        if why:
            return PullRequest(state=unknown(why))
        if ref.isdigit():
            data, why = self.gh.try_json(["pr", "view", ref, "-R", repo, "--json", PR_FIELDS])
            return _pr(data) if data else PullRequest(state=unknown(why or "gh printed nothing"), number=int(ref))
        rows, why = self.gh.try_json(["pr", "list", "-R", repo, "--head", ref, "--state", "all", "--json", PR_FIELDS, "-L", "20"])
        if why:
            return PullRequest(state=unknown(why), branch=ref)
        if not rows and SHA.match(ref):
            found, why = self.gh.api(f"commits/{ref}/pulls")
            if why:
                return PullRequest(state=unknown(why), head=ref)
            numbers = sorted((p.get("number", 0) for p in found or [] if (p.get("head") or {}).get("sha", "").startswith(ref)), reverse=True)
            opened = [p.get("number", 0) for p in found or [] if p.get("state") == "open" and (p.get("head") or {}).get("sha", "").startswith(ref)]
            if numbers:
                return self.pr(str((opened or numbers)[0]))
            return PullRequest(state="none", head=ref)
        if not rows:
            return PullRequest(state="none", branch=ref)
        rows.sort(key=lambda d: (d.get("state") == "OPEN", d.get("updatedAt", "")), reverse=True)
        return _pr(rows[0])

    def open_prs(self, limit: int = 50) -> PullRequestList:
        repo, why = self.gh.repo()
        if why:
            return PullRequestList(state=unknown(why))
        rows, why = self.gh.try_json(["pr", "list", "-R", repo, "--state", "open", "--json", PR_FIELDS, "-L", str(limit + 1)])
        if why:
            return PullRequestList(state=unknown(why))
        return PullRequestList(truncated=len(rows or []) > limit, pulls=[_pr(d) for d in (rows or [])[:limit]])

    def _pr_check_rows(self, repo: str, number: int, required: bool) -> tuple[list[dict[str, Any]] | None, str]:
        """`gh pr checks` exits 8 while checks are pending and 1 when one failed; the JSON is printed either way."""
        args = ["pr", "checks", str(number), "-R", repo, "--json", CHECK_FIELDS] + (["--required"] if required else [])
        res = self.gh.run(args)
        text = res.out.strip()
        if text.startswith("["):
            try:
                return json.loads(text), ""
            except ValueError:
                return None, "gh pr checks printed broken JSON"
        if re.search(r"no (required )?checks reported", res.err or "", re.I):
            return [], ""
        return None, self.gh.why(res)

    def required_contexts(self, base: str) -> tuple[list[str], str, bool]:
        """(contexts the base branch requires, where that was read, strict). Rulesets and classic branch protection."""
        names: set[str] = set()
        sources, problems, strict = [], [], False
        rules, why = self.gh.api(f"rules/branches/{quote(base, safe='')}")
        if why:
            problems.append(f"rules: {why}")
        else:
            sources.append("rules")
            for rule in rules or []:
                if rule.get("type") == "required_status_checks":
                    params = rule.get("parameters") or {}
                    strict = strict or bool(params.get("strict_required_status_checks_policy"))
                    names |= {c.get("context", "") for c in params.get("required_status_checks") or []}
        prot, why = self.gh.api(f"branches/{quote(base, safe='')}/protection/required_status_checks")
        if not why:
            sources.append("protection")
            strict = strict or bool((prot or {}).get("strict"))
            names |= set((prot or {}).get("contexts") or []) | {c.get("context", "") for c in (prot or {}).get("checks") or []}
        elif "not protected" not in why.lower() and "404" not in why and "403" not in why:
            problems.append(f"protection: {why}")
        source = "+".join(sources) if sources else unknown("; ".join(problems))
        return sorted(n for n in names if n), source, strict

    def pr_checks(self, number: int) -> ChecksRollup:
        pr = self.pr(str(number))
        if is_unknown(pr.state):
            return ChecksRollup(pr=number, state=pr.state)
        repo, _ = self.gh.repo()
        rows, why = self._pr_check_rows(repo, number, False)
        if why or rows is None:
            return ChecksRollup(pr=number, head=pr.head, state=unknown(why or "gh pr checks printed nothing"))
        req_rows, req_why = self._pr_check_rows(repo, number, True)
        reported_required = {r.get("name", "") for r in req_rows or []}
        expected, source, _ = self.required_contexts(pr.base) if pr.base else ([], unknown("the PR has no base branch"), False)
        if is_unknown(source):
            source = "checks" if not req_why else unknown(f"{source.removeprefix('unknown: ')}; gh pr checks --required: {req_why}")
        required = sorted(set(expected) | reported_required)
        checks = [Check(name=r.get("name", ""), state=BUCKET.get(r.get("bucket", ""), "pending"), required=r.get("name", "") in required, raw=r.get("state", ""),
                        workflow=r.get("workflow", ""), event=r.get("event", ""), started=r.get("startedAt") or "", completed=r.get("completedAt") or "",
                        url=r.get("link", "")) for r in rows]
        checks.sort(key=lambda c: (not c.required, {"red": 0, "pending": 1, "green": 2, "skipped": 3}[c.state], c.name))
        by_name: dict[str, list[str]] = {}
        for c in checks:
            by_name.setdefault(c.name, []).append(c.state)

        def names(state: str, req: bool) -> list[str]:
            return sorted(n for n, states in by_name.items() if (n in required) == req and state in states
                          and (state == "red" or "red" not in states))

        missing = sorted(n for n in required if n not in by_name)
        red_req, pend_req = names("red", True), names("pending", True)
        red_opt, pend_opt = names("red", False), names("pending", False)
        if required:
            scope, state = "required", "red" if red_req else "pending" if (pend_req or missing) else "green"
        else:
            scope, state = "all", "none" if not checks else "red" if red_opt else "pending" if pend_opt else "green"
        if is_unknown(source) and state == "green":
            state = unknown(f"the required contexts could not be read ({source.removeprefix('unknown: ')}); every reported check is green")
        return ChecksRollup(pr=number, head=pr.head, state=state, scope=scope, required=required, required_source=source, missing_required=missing,
                            red_required=red_req, pending_required=pend_req, red_optional=red_opt, pending_optional=pend_opt, checks=checks)

    def unresolved_threads(self, number: int) -> tuple[int | None, str]:
        repo, why = self.gh.repo()
        if why:
            return None, why
        owner, _, name = repo.partition("/")
        data, why = self.gh.try_json(["api", "graphql", "-f", f"query={THREADS}", "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={number}"])
        if why:
            return None, why
        try:
            nodes = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        except (KeyError, TypeError):
            return None, "no review threads in the answer"
        return sum(1 for n in nodes if not n.get("isResolved")), ""

    def why(self, number: int) -> WhyReport:
        """Why the pull request is not mergeable. Every visible cause is listed, not only the first."""
        pr = self.pr(str(number))
        if is_unknown(pr.state):
            return WhyReport(pr=number, mergeable=pr.state)
        out: dict[str, Any] = {"pr": number, "head": pr.head, "base": pr.base, "merge_state": pr.merge_state, "url": pr.url}
        if pr.state != "open":
            return WhyReport(mergeable="no", reasons=[Reason(kind=pr.state, text=f"the pull request is {pr.state}")], **out)
        reasons: list[Reason] = []
        if pr.draft:
            reasons.append(Reason(kind="draft", text="it is a draft", next=f"gh pr ready {number}"))
        if pr.mergeable == "conflicting" or pr.merge_state == "dirty":
            reasons.append(Reason(kind="conflicts", text=f"it conflicts with {pr.base}", next=f"rebase {pr.branch} on {pr.base} and resolve the conflicts"))
        rollup = self.pr_checks(number)
        if rollup.missing_required:
            reasons.append(Reason(kind="missing_required", text="required contexts that have not reported on this head: " + ", ".join(rollup.missing_required),
                                  next="find the workflow that reports each one: it did not start, was skipped, or is still queued"))
        if rollup.red_required:
            reasons.append(Reason(kind="red_required", text="required checks are red: " + ", ".join(rollup.red_required), next=f"sm pr checks {number}"))
        if rollup.pending_required:
            reasons.append(Reason(kind="pending_required", text="required checks still run: " + ", ".join(rollup.pending_required), next=f"sm pr checks {number}"))
        _, _, strict = self.required_contexts(pr.base) if pr.base else ([], "", False)
        behind_by = None
        cmp, why = self.gh.api(f"compare/{quote(pr.base, safe='')}...{pr.head}") if pr.base and pr.head else (None, "no base or head")
        if not why and isinstance(cmp, dict):
            behind_by = cmp.get("behind_by")
        if pr.merge_state == "behind" or (strict and behind_by):
            reasons.append(Reason(kind="behind", text=f"it is behind {pr.base}" + (f" by {behind_by} commit(s)" if behind_by else "") + " and the branch must be up to date",
                                  next=f"update {pr.branch} from {pr.base}"))
        if pr.review in ("changes_requested", "review_required"):
            reasons.append(Reason(kind="review", text="a reviewer requested changes" if pr.review == "changes_requested" else "a required review is missing"))
        threads, _ = self.unresolved_threads(number)
        if threads and (pr.merge_state == "blocked" or pr.review):
            reasons.append(Reason(kind="threads", text=f"{threads} review thread(s) are unresolved", next="resolve them on the pull request page"))
        if is_unknown(rollup.state) and not reasons:
            return WhyReport(mergeable=unknown(f"the checks could not be read: {rollup.state.removeprefix('unknown: ')}"), behind_by=behind_by, checks=rollup.state, **out)
        if not reasons and pr.merge_state == "blocked":
            reasons.append(Reason(kind="blocked", text="GitHub says blocked and none of the causes this command can see holds: a rule such as a required deployment, "
                                                       "a signed commit or a merge queue", next="open the pull request page and read the merge box"))
        if not reasons and pr.mergeable not in ("mergeable",):
            return WhyReport(mergeable=unknown("GitHub has not computed mergeability yet; ask again in a few seconds"), behind_by=behind_by, checks=rollup.state,
                             reasons=[Reason(kind="computing", text="GitHub is still computing whether it merges cleanly")], **out)
        return WhyReport(mergeable="no" if reasons else "yes", behind_by=behind_by, reasons=reasons, checks=rollup.state, **out)

    def branch_tip(self, branch: str) -> BranchTip:
        data, why = self.gh.api(f"git/ref/heads/{quote(branch, safe='/')}")
        if why:
            return BranchTip(branch=branch, state="missing" if "404" in why else unknown(why))
        return BranchTip(branch=branch, state="found", sha=((data or {}).get("object") or {}).get("sha", ""))

    def create_pr(self, branch: str, title: str, body_file: str, base: str = "main", live: bool = False) -> PullRequestDraft:
        """Open a pull request with the body read from a file. Dry unless live. An open PR of the branch is adopted."""
        repo, why = self.gh.repo()
        if why:
            return PullRequestDraft(state=unknown(why), live=live)
        args = ["pr", "create", "-R", repo, "--base", base, "--head", branch, "--title", title, "--body-file", body_file]
        existing = self.pr(branch)
        if is_unknown(existing.state):
            return PullRequestDraft(state=existing.state, live=live, would_run=self.gh.shown(args))
        if existing.state == "open":
            return PullRequestDraft(state="exists", live=live, pr=existing)
        if not live:
            return PullRequestDraft(state="dry_run", would_run=self.gh.shown(args))
        if not os.path.isfile(body_file):
            return PullRequestDraft(state=unknown(f"the body file {body_file} does not exist"), live=True, would_run=self.gh.shown(args))
        res = self.gh.run(args, timeout=max(self.gh.timeout, 120), retry=False)  # a write is not repeated blindly
        if res.rc:
            return PullRequestDraft(state=unknown(self.gh.why(res)), live=True, would_run=self.gh.shown(args))
        made = self.pr(branch)
        return PullRequestDraft(state="created" if made.state == "open" else unknown("gh pr create exited 0 but no open PR is listed for the branch yet"),
                                live=True, would_run=self.gh.shown(args), pr=made if made.state == "open" else None)

    # ------------------------------------------------------------------ runs, jobs, logs (CIRuns)
    def run(self, run_id: str) -> Run:
        data, why = self.gh.api(f"actions/runs/{run_id}")
        return _run(data) if data and not why else Run(id=str(run_id), status=unknown(why or "empty answer"))

    def runs(self, commit: str = "", branch: str = "", status: str = "", limit: int = 20) -> RunList:
        rows, why, truncated = self.gh.pages("actions/runs", {"head_sha": commit, "branch": branch, "status": status}, key="workflow_runs", limit=limit)
        if why:
            return RunList(state=unknown(why), runs=[_run(d) for d in rows])
        return RunList(truncated=truncated, runs=[_run(d) for d in rows])

    def active(self, limit: int = 100) -> RunList:
        """Every run that is not finished: in progress, queued, waiting, pending, requested."""
        found: dict[str, Run] = {}
        truncated = False
        for status in ACTIVE:
            part = self.runs(status=status, limit=limit)
            if is_unknown(part.state):
                return RunList(state=part.state, runs=list(found.values()))
            truncated = truncated or part.truncated
            found.update({r.id: r for r in part.runs})
        runs = sorted(found.values(), key=lambda r: r.created)
        return RunList(truncated=truncated or len(runs) > limit, runs=runs[:limit])

    def jobs(self, run_id: str) -> RunJobs:
        rows, why, truncated = self.gh.pages(f"actions/runs/{run_id}/jobs", {"filter": "latest"}, key="jobs")
        jobs = [_job(d) for d in rows]
        return RunJobs(run=str(run_id), state=unknown(why) if why else "ok", truncated=truncated, total=len(jobs), jobs=jobs,
                       failed=[j.name for j in jobs if j.conclusion in ("failure", "timed_out", "startup_failure")],
                       cancelled=[j.name for j in jobs if j.conclusion == "cancelled"])

    def log_tail(self, job_id: str, lines: int = 40) -> LogTail:
        """The end of one job's log, through the jobs API (works while other jobs of the run still run). When the log
        has `##[error]` lines the tail ends at the last one."""
        repo, why = self.gh.repo()
        if why:
            return LogTail(job=str(job_id), state=unknown(why))
        args = ["api", f"repos/{repo}/actions/jobs/{job_id}/logs"]
        res = self.gh.run([*args, "--allow-escape-sequences"], timeout=max(self.gh.timeout, 60))
        if res.rc and "unknown flag" in (res.err or ""):  # a gh older than the flag prints logs as they are
            res = self.gh.run(args, timeout=max(self.gh.timeout, 60))
        if res.rc:
            return LogTail(job=str(job_id), state=unknown(self.gh.why(res)))
        text = [STAMP.sub("", ANSI.sub("", ln)).rstrip()[:400] for ln in res.out.splitlines()]
        errors = [i for i, ln in enumerate(text) if ln.startswith("##[error]")]
        end = errors[-1] + 1 if errors else len(text)  # what follows the last error is the runner cleaning up
        return LogTail(job=str(job_id), total_lines=len(text), ends_at="error" if errors else "end", lines=text[max(0, end - max(1, lines)):end])

    def cancel_run(self, run_id: str, live: bool = False) -> CancelResult:
        repo, why = self.gh.repo()
        if why:
            return CancelResult(run=str(run_id), state=unknown(why), live=live)
        args = ["api", "-X", "POST", f"repos/{repo}/actions/runs/{run_id}/cancel"]
        run = self.run(run_id)
        detail = f"{run.workflow} on {run.branch} ({run.event}), {run.status}" + (f"/{run.conclusion}" if run.conclusion else "")
        if is_unknown(run.status):
            return CancelResult(run=str(run_id), state=run.status, live=live, would_run=self.gh.shown(args))
        if run.status == "completed":
            return CancelResult(run=str(run_id), state="not_cancellable", live=live, detail=detail)
        if not live:
            return CancelResult(run=str(run_id), state="dry_run", would_run=self.gh.shown(args), detail=detail)
        res = self.gh.run(args, retry=False)
        if res.rc:
            error = res.err or ""
            state = "not_queued" if "HTTP 409" in error and NOT_QUEUED_CANCEL in error else unknown(self.gh.why(res))
            return CancelResult(run=str(run_id), state=state, live=True, would_run=self.gh.shown(args), detail=detail)
        return CancelResult(run=str(run_id), state="cancelled", live=True, would_run=self.gh.shown(args), detail=detail)

    # ------------------------------------------------------------------ repository variables
    def variable(self, name: str) -> Variable:
        data, why = self.gh.api(f"actions/variables/{quote(name, safe='')}")
        if why:
            return Variable(name=name, state="missing" if "404" in why else unknown(why))
        return Variable(name=name, state="found", value=(data or {}).get("value"), updated=(data or {}).get("updated_at", ""))

    def variables(self) -> VariableList:
        rows, why, truncated = self.gh.pages("actions/variables", key="variables")
        return VariableList(state=unknown(why) if why else "ok", truncated=truncated,
                            variables=[Variable(name=d.get("name", ""), state="found", value=d.get("value"), updated=d.get("updated_at", "")) for d in rows])

    def write_variable(self, name: str, value: str, live: bool = False) -> VariableWrite:
        """Set one repository variable. Refused unless the provider was built with variable_writes=True; dry unless live."""
        repo, why = self.gh.repo()
        if why:
            return VariableWrite(name=name, state=unknown(why), live=live)
        shown = self.gh.shown(["variable", "set", name, "-R", repo, "--body", f"<{len(value)} characters>"])
        if not self.variable_writes:
            return VariableWrite(name=name, state="refused: variable writes are off ([github] variable_writes = false)", live=live, would_run=shown)
        if not live:
            return VariableWrite(name=name, state="dry_run", would_run=shown)
        res = self.gh.run(["variable", "set", name, "-R", repo, "--body", value], retry=False)
        return VariableWrite(name=name, state=unknown(self.gh.why(res)) if res.rc else "written", live=True, would_run=shown)

    # ------------------------------------------------------------------ core Protocol: Forge
    def pr_for_branch(self, branch: str) -> PullRequest | None:
        pr = self.pr(branch)
        if is_unknown(pr.state):
            raise ProviderError(f"cannot tell whether {branch} has a pull request: {pr.state}")
        return pr if pr.state == "open" else None

    def open_pr(self, branch: str, title: str, body: str) -> PullRequest:  # external
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write(body)
        try:
            repo, _ = self.gh.repo()
            res = self.gh.run(["repo", "view", repo, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"])
            base = res.out.strip() if not res.rc and res.out.strip() else "main"
            made = self.create_pr(branch, title, handle.name, base=base, live=True)
        finally:
            os.unlink(handle.name)
        if made.pr is None:
            raise ProviderError(f"no pull request for {branch}: {made.state}")
        return made.pr

    def checks(self, sha: str) -> list[Check]:
        runs, why, _ = self.gh.pages(f"commits/{sha}/check-runs", key="check_runs")
        if why:
            raise ProviderError(f"cannot read the checks of {sha[:12]}: {why}")
        out = [Check(name=r.get("name", ""), state=_check_state(r.get("status", ""), r.get("conclusion") or ""), raw=(r.get("conclusion") or r.get("status") or "").upper(),
                     started=r.get("started_at") or "", completed=r.get("completed_at") or "", url=r.get("html_url") or "") for r in runs]
        combined, why = self.gh.api(f"commits/{sha}/status")
        if why:
            raise ProviderError(f"cannot read the statuses of {sha[:12]}: {why}")
        out += [Check(name=s.get("context", ""), state=_check_state(s.get("state", ""), ""), raw=str(s.get("state", "")).upper(), url=s.get("target_url") or "")
                for s in (combined or {}).get("statuses") or []]
        return out

    def active_runs(self) -> list[Run]:
        found = self.active()
        if is_unknown(found.state):
            raise ProviderError(f"cannot list the active runs: {found.state}")
        return found.runs

    def merge(self, pr: int, head: str) -> None:  # external; GitHub itself refuses when the PR is not at `head`
        repo, why = self.gh.repo()
        if why:
            raise ProviderError(why)
        res = self.gh.run(["pr", "merge", str(pr), "-R", repo, f"--{self.merge_method}", "--match-head-commit", head], timeout=max(self.gh.timeout, 120), retry=False)
        if res.rc:
            raise ProviderError(f"PR {pr} was not merged: {self.gh.why(res)}")

    def tag(self, name: str, sha: str) -> None:  # external
        _, why = self.gh.api("git/refs", method="POST", fields={"ref": f"refs/tags/{name}", "sha": sha})
        if why:
            raise ProviderError(f"tag {name} was not created: {why}")

    def get_variable(self, name: str) -> str | None:
        var = self.variable(name)
        if is_unknown(var.state):
            raise ProviderError(f"cannot read the variable {name}: {var.state}")
        return var.value

    def set_variable(self, name: str, value: str) -> None:  # external; needs variable_writes=True
        done = self.write_variable(name, value, live=True)
        if done.state != "written":
            raise ProviderError(f"the variable {name} was not written: {done.state}")

    # ------------------------------------------------------------------ core Protocol: CIProvider
    def status(self, sha: str) -> CommitStatus:
        try:
            checks = self.checks(sha)
        except ProviderError as exc:
            return CommitStatus(state="unknown", detail=str(exc))
        red = sorted({c.name for c in checks if c.state == "red"})
        state = "red" if red else "pending" if (not checks or any(c.state == "pending" for c in checks)) else "green"
        repo, _ = self.gh.repo()
        return CommitStatus(state=state, failing=tuple(red), url=f"https://github.com/{repo}/commit/{sha}/checks" if repo else "",
                            detail="" if checks else "nothing has reported on this commit yet")

    def failures(self, sha: str) -> list[str]:
        """One line per red check of the commit, for a builder brief."""
        try:
            return [f"{c.name}: {c.raw.lower()} {c.url}".rstrip() for c in self.checks(sha) if c.state == "red"]
        except ProviderError as exc:
            return [f"unknown: {exc}"]
