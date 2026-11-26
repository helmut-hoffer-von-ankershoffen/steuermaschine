"""Classify the pinned Claude Action's execution file without printing provider errors."""

import json
import os
import re
import secrets
import sys
from pathlib import Path

SPEND_LIMIT = re.compile(r"\AYou've hit your (?:monthly|weekly) spend limit(?:\s|[.!]|$)")


def classify(messages: object, outcome: str) -> tuple[str, str]:
    if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
        raise ValueError("invalid Claude execution file")
    results = [item for item in messages if item.get("type") == "result"]
    if len(results) != 1 or messages[-1] != results[0]:
        raise ValueError("expected one terminal Claude result")
    record = results[0]
    answer = record.get("result")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Claude returned no review result")
    lines = [line for line in answer.splitlines() if line.strip()]
    if outcome == "success" and record.get("is_error") is False and record.get("subtype") == "success":
        return ("approved" if lines[-1] == "VERDICT: APPROVE" and not any(line.strip() == "VERDICT: REJECT" for line in lines)
                else "rejected"), answer
    provider = tuple(record.get(key) for key in ("terminal_reason", "api_error_type", "api_error_status", "api_error_origin"))
    if (outcome == "failure" and record.get("is_error") is True
            and provider == ("api_error", "rate_limit_error", 429, "first_party") and SPEND_LIMIT.match(answer)):
        # The Action includes assistant messages as well as the terminal result. A real rejection still blocks.
        texts = [answer]
        for item in messages:
            if item.get("type") == "assistant":
                texts.extend(part.get("text", "") for part in item["message"]["content"] if part.get("type") == "text")
        if not any(line.strip() == "VERDICT: REJECT" for text in texts for line in text.splitlines()):
            return "skipped-provider-spend-limit", "Claude reached its provider spending limit. Independent review was skipped."
    raise ValueError("Claude review failed without a confirmed provider spending limit")


def main() -> int:
    try:
        expected = Path(os.environ["RUNNER_TEMP"]) / "claude-execution-output.json"
        if Path(os.environ["EXECUTION_FILE"]) != expected or expected.is_symlink():
            raise ValueError("Claude Action did not produce the expected execution file")
        kind, answer = classify(json.loads(expected.read_text(encoding="utf-8")), os.environ["OUTCOME"])
        delimiter = "EOF_" + secrets.token_hex(16)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"kind={kind}\nresult<<{delimiter}\n{answer[:50000]}\n{delimiter}\n")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        # JSONDecodeError and OSError can include file contents or paths. The generic message is enough here.
        message = str(error) if type(error) is ValueError else "could not read the Claude Action result"
        print(f"::error::Gate review failed: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
