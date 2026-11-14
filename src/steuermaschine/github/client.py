"""`Gh`: the one client for the `gh` CLI. Every module that talks to GitHub goes through it; none builds its own
subprocess call.

    gh = Gh(commands, repo="owner/name")       commands: the `Commands` provider (run(argv, cwd, timeout) -> Result)
    gh.run(["pr", "merge", "12"])              -> Result(rc, out, err), never raises
    gh.json(["pr", "view", "12", "--json", "number"])   -> the parsed document; raises GhError with a short reason
    gh.try_json(args)                          -> (data, "") or (None, why): for probes that must not raise
    gh.api("actions/runs/1")                   -> (data, why): one REST call below repos/OWNER/NAME/
    gh.pages("actions/runs", {"status": "queued"}, key="workflow_runs", limit=50)   -> (rows, why, truncated)

Each call has a timeout. A call that fails with a transient error (timeout, HTTP 5xx, a dropped connection) is
tried once more, unless `retry=False` (use that for writes). A listing reads at most `max_pages` pages of 100.
"""

from __future__ import annotations

import json as jsonlib
import re
from typing import Any
from urllib.parse import urlencode

from ..protocols import Clock, Commands, Result

TRANSIENT = re.compile(r"HTTP 5\d\d|time[d ]*out|connection (reset|refused)|unexpected EOF|\bEOF\b|TLS handshake|temporary failure|server error|bad gateway|"
                       r"service unavailable", re.I)
PER_PAGE = 100


class GhError(RuntimeError):
    """`gh` failed or printed no JSON. The message is one short line that says why."""


class Gh:
    def __init__(self, commands: Commands, repo: str = "", timeout: float = 30.0, max_pages: int = 3, clock: Clock | None = None, cwd: str | None = None,
                 binary: str = "gh") -> None:
        self.c, self._repo, self.timeout, self.max_pages, self.clock, self.cwd, self.binary = commands, repo, timeout, max(1, max_pages), clock, cwd, binary

    def run(self, args: list[Any], timeout: float | None = None, retry: bool = True) -> Result:
        """Run `gh ARGS`. Returns the Result; a transient failure is tried once more after a second."""
        argv = [self.binary, *[str(a) for a in args]]
        res = self.c.run(argv, cwd=self.cwd, timeout=timeout or self.timeout)
        if res.rc and retry and (res.rc == 124 or TRANSIENT.search(res.err or "")):
            if self.clock is not None:
                self.clock.sleep(1.0)
            res = self.c.run(argv, cwd=self.cwd, timeout=timeout or self.timeout)
        return res

    @staticmethod
    def why(res: Result) -> str:
        """A failed Result as one short reason."""
        if res.rc == 127:
            return "the gh CLI is not installed or not on PATH"
        if res.rc == 124:
            return f"gh {(res.err or '').strip() or 'timed out'}"
        lines = [ln.strip() for ln in (res.err or "").splitlines() if ln.strip()]
        text = lines[-1] if lines else f"gh exited {res.rc}"
        return text.removeprefix("gh: ")[:240]

    def shown(self, args: list[Any]) -> str:
        """The command line as a person would type it, for dry runs."""
        import shlex

        return " ".join(shlex.quote(str(a)) for a in [self.binary, *args])

    def repo(self) -> tuple[str, str]:
        """(OWNER/NAME, "") or ("", why). Asked from `gh` once when no repository was configured."""
        if not self._repo:
            res = self.run(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
            if res.rc or not res.out.strip():
                return "", f"no repository: set [github] repo = \"OWNER/NAME\" or run inside a clone ({self.why(res)})"
            self._repo = res.out.strip()
        return self._repo, ""

    def try_json(self, args: list[str], timeout: float | None = None, retry: bool = True) -> tuple[Any, str]:
        """(data, "") or (None, why). Empty output is (None, "")."""
        res = self.run(args, timeout, retry)
        if res.rc:
            return None, self.why(res)
        try:
            return (jsonlib.loads(res.out) if res.out.strip() else None), ""
        except ValueError:
            return None, f"gh printed no JSON: {res.out.strip()[:120]!r}"

    def json(self, args: list[str], timeout: float | None = None, retry: bool = True) -> Any:
        data, why = self.try_json(args, timeout, retry)
        if why:
            raise GhError(why)
        return data

    def api(self, path: str, params: dict[str, Any] | None = None, method: str = "GET", fields: dict[str, Any] | None = None) -> tuple[Any, str]:
        """One REST call below repos/OWNER/NAME/. GET parameters go into the URL; `fields` are sent with -f. A call
        with another method than GET is a write and is not retried."""
        repo, why = self.repo()
        if why:
            return None, why
        query = urlencode({k: v for k, v in (params or {}).items() if v not in ("", None)})
        url = f"repos/{repo}/{path}" + (f"?{query}" if query else "")
        args = ["api", url] if method == "GET" else ["api", "-X", method, url]
        for key, value in (fields or {}).items():
            args += ["-f", f"{key}={value}"]
        return self.try_json(args, retry=method == "GET")

    def pages(self, path: str, params: dict[str, Any] | None = None, key: str | None = None, limit: int | None = None) -> tuple[list[Any], str, bool]:
        """Up to max_pages pages of a listing. (rows, why, truncated). `key` names the list inside an object answer
        (`workflow_runs`, `jobs`, `check_runs`, `variables`). truncated: more rows exist than were returned."""
        rows: list[Any] = []
        for page in range(1, self.max_pages + 1):
            data, why = self.api(path, {**(params or {}), "per_page": PER_PAGE, "page": page})
            if why:
                return rows, why, False
            chunk = (data or {}).get(key, []) if key else (data or [])
            rows += chunk
            total = data.get("total_count") if key and isinstance(data, dict) else None
            if limit is not None and len(rows) >= limit:
                return rows[:limit], "", len(rows) > limit or (total or 0) > limit or (total is None and len(chunk) == PER_PAGE)
            if len(chunk) < PER_PAGE:
                return rows, "", False
        return rows, "", True
