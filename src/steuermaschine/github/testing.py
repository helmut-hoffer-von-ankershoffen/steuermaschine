"""A scripted `gh` for tests: a `Commands` provider that answers by what the command line contains.

    gh = FakeGh()
    gh.on("pr view 12", {"number": 12, "state": "OPEN"})       # a dict or list is printed as JSON, exit 0
    gh.on("actions/runs/7/cancel", Result(1, "", "gh: Server Error (HTTP 502)"), Result(0, "{}", ""))   # first, then second
    gh.on("actions/jobs/9/logs", "line 1\nline 2\n")           # text
    gh.on("git/ref/heads/x", 404)                               # an HTTP error as gh prints it
    gh.calls                                                    # every argv, in order

The needle is matched against the arguments joined by one space; the longest matching needle wins. A callable
gets the argv and returns any of the above. Nothing matches: exit 1 with a line that names the command.
"""

from __future__ import annotations

import json
from typing import Any

from ..protocols import Result

HTTP = {403: "Resource not accessible by integration", 404: "Not Found", 422: "Validation Failed", 500: "Server Error", 502: "Bad Gateway", 503: "Service Unavailable"}


class FakeGh:
    def __init__(self) -> None:
        self.rules: dict[str, list[Any]] = {}
        self.calls: list[list[str]] = []

    def on(self, needle: str, *answers: Any) -> FakeGh:
        self.rules[needle] = list(answers)
        return self

    def run(self, argv: list[str], cwd: str | None = None, timeout: float | None = None, env: dict[str, str] | None = None) -> Result:
        self.calls.append([str(a) for a in argv])
        line = " ".join(str(a) for a in argv[1:])
        hits = sorted((n for n in self.rules if n in line), key=len, reverse=True)
        if not hits:
            return Result(1, "", f"no fake answer for: gh {line}")
        answers = self.rules[hits[0]]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if callable(answer):
            answer = answer(list(argv))
        if isinstance(answer, Result):
            return answer
        if isinstance(answer, int):
            return Result(1, json.dumps({"message": HTTP.get(answer, "error"), "status": str(answer)}), f"gh: {HTTP.get(answer, 'error')} (HTTP {answer})")
        return Result(0, answer if isinstance(answer, str) else json.dumps(answer), "")

    def lines(self) -> list[str]:
        return [" ".join(c[1:]) for c in self.calls]

    def writes(self) -> list[str]:
        """The calls that change something on GitHub."""
        marks = ("-X POST", "-X PUT", "-X PATCH", "-X DELETE", "pr create", "pr merge", "variable set", "run cancel", "pr ready", "pr close")
        return [ln for ln in self.lines() if any(m in ln for m in marks)]
