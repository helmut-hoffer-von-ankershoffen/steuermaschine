"""Protocols the generic `sm ci` and `sm pr` commands are built on. They extend what the core `Forge` and
`CIProvider` offer with read methods that return result models; a provider may satisfy both sets.

A plugin with its own forge or CI offers these through `Providers.extra`:
    extra["forge_reader"]  a ForgeReader      extra["ci_runs"]  a CIRuns      extra["quality"]  a QualityProvider
Otherwise the commands use the core GitHub and Sonar providers, configured by [github] and [sonar].
Rule for implementers: a read returns a model whose state is `unknown: <why>`; it does not raise.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import BranchTip, CancelResult, ChecksRollup, LogTail, PullRequest, PullRequestList, QualityReport, Run, RunJobs, RunList, WhyReport


class ProviderError(RuntimeError):
    """Raised only by the methods of the small core Protocols, whose return types have no room for `unknown`."""


@runtime_checkable
class ForgeReader(Protocol):
    def pr(self, ref: str) -> PullRequest: ...  # a number, a head branch or a head commit
    def open_prs(self, limit: int = 50) -> PullRequestList: ...
    def pr_checks(self, number: int) -> ChecksRollup: ...
    def why(self, number: int) -> WhyReport: ...
    def branch_tip(self, branch: str) -> BranchTip: ...


@runtime_checkable
class CIRuns(Protocol):
    def run(self, run_id: str) -> Run: ...
    def runs(self, commit: str = "", branch: str = "", status: str = "", limit: int = 20) -> RunList: ...
    def active(self, limit: int = 100) -> RunList: ...
    def jobs(self, run_id: str) -> RunJobs: ...
    def log_tail(self, job_id: str, lines: int = 40) -> LogTail: ...
    def cancel_run(self, run_id: str, live: bool = False) -> CancelResult: ...  # external when live


@runtime_checkable
class QualityProvider(Protocol):
    def pr_quality(self, number: int, issues: int = 20) -> QualityReport: ...
