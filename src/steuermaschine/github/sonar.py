"""Sonar as an optional quality provider: the public web API of SonarCloud (or a SonarQube host), read-only.

    Sonar("org_project").pr_quality(796)       -> QualityReport: gate, new-code debt ratio, duplication, open issues

The HTTP function is injected (`fetch(url, headers, timeout) -> (status, body)`; status 0 = no answer), so tests
use a fake. One retry on a transport error or HTTP 5xx. A part that cannot be read is `unknown: <why>` or None and
the report's `state` says so; nothing raises. A public project needs no token.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from ..protocols import Clock
from .models import QualityCondition, QualityIssue, QualityReport, unknown

METRICS = {"new_sqale_debt_ratio": "debt_ratio", "new_duplicated_lines_density": "duplication", "new_coverage": "coverage"}
GATE = {"OK": "passed", "ERROR": "failed", "WARN": "failed", "NONE": "none"}
Fetch = Callable[[str, dict[str, str], float], tuple[int, str]]


def urllib_fetch(url: str, headers: dict[str, str], timeout: float) -> tuple[int, str]:
    if not url.lower().startswith(("https://", "http://")):
        return 0, "unsupported URL scheme"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, str(getattr(exc, "reason", exc))


class Sonar:
    def __init__(self, project_key: str, host: str = "https://sonarcloud.io", fetch: Fetch | None = None, token: str = "", timeout: float = 20.0,
                 clock: Clock | None = None) -> None:
        self.key, self.host, self.fetch, self.token, self.timeout, self.clock = project_key, host.rstrip("/"), fetch or urllib_fetch, token, timeout, clock

    def get(self, path: str, params: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """(document, "") or ({}, why)."""
        url = f"{self.host}/api/{path}?{urlencode(params)}"
        headers = {"Accept": "application/json", **({"Authorization": f"Bearer {self.token}"} if self.token else {})}
        status, body = self.fetch(url, headers, self.timeout)
        if status == 0 or status >= 500:
            if self.clock is not None:
                self.clock.sleep(1.0)
            status, body = self.fetch(url, headers, self.timeout)
        if status == 0:
            return {}, f"no answer from {self.host}: {body[:160]}"
        try:
            data = json.loads(body) if body.strip() else {}
        except ValueError:
            data = None
        if status != 200:
            said = "; ".join(e.get("msg", "") for e in (data or {}).get("errors", [])) if isinstance(data, dict) else ""
            return {}, f"HTTP {status}" + (f": {said}" if said else "")
        if not isinstance(data, dict):
            return {}, "the answer is not a JSON object"
        return data, ""

    def pr_quality(self, number: int, issues: int = 20) -> QualityReport:
        url = f"{self.host}/dashboard?{urlencode({'id': self.key, 'pullRequest': number})}"
        if not self.key:
            return QualityReport(pr=number, state=unknown("no Sonar project: set [sonar] project_key"), gate=unknown("no project key"))
        problems: list[str] = []
        out: dict[str, Any] = {"conditions": [], "issues": []}

        data, why = self.get("qualitygates/project_status", {"projectKey": self.key, "pullRequest": number})
        if why:
            problems.append(f"gate: {why}")
            out["gate"] = unknown(why)
        else:
            status = data.get("projectStatus") or {}
            out["gate"] = GATE.get(status.get("status", ""), unknown(f"gate status {status.get('status')!r}"))
            out["conditions"] = [QualityCondition(metric=c.get("metricKey", ""), status=str(c.get("status", "")).lower(), actual=str(c.get("actualValue", "")),
                                                  threshold=str(c.get("errorThreshold", "")), comparator=c.get("comparator", "")) for c in status.get("conditions") or []]

        data, why = self.get("measures/component", {"component": self.key, "pullRequest": number, "metricKeys": ",".join(METRICS)})
        if why:
            problems.append(f"measures: {why}")
        else:
            for m in (data.get("component") or {}).get("measures") or []:
                raw = ((m.get("periods") or [{}])[0].get("value") if m.get("periods") else None) or (m.get("period") or {}).get("value") or m.get("value")
                try:
                    out[METRICS[m.get("metric", "")]] = float(raw)
                except (KeyError, TypeError, ValueError):
                    continue

        want = max(0, min(issues, 100))
        data, why = self.get("issues/search", {"componentKeys": self.key, "pullRequest": number, "resolved": "false", "ps": max(1, want)})
        if why:
            problems.append(f"issues: {why}")
        else:
            total = data.get("total", (data.get("paging") or {}).get("total"))
            rows = (data.get("issues") or [])[:want]
            out["open_issues"] = total
            out["issues"] = [QualityIssue(key=i.get("key", ""), severity=str(i.get("severity", "")).lower(), type=str(i.get("type", "")).lower(), rule=i.get("rule", ""),
                                          file=str(i.get("component", "")).split(":", 1)[-1], line=i.get("line"), message=i.get("message", "")) for i in rows]
            out["issues_truncated"] = isinstance(total, int) and total > len(rows)
        return QualityReport(pr=number, project=self.key, state=unknown("; ".join(problems)) if problems else "ok", url=url, **out)
