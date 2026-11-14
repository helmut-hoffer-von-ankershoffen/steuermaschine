"""What `sm` prints names `sm`: the moved release code still names its old commands and state paths in hints."""

from __future__ import annotations

import re

from ...toolmap import ROWS

# the forms whose options moved, most specific first
SPECIAL: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"hz-release --absorb (\S+)"), r"sm release resume \1 --absorb"),
    (re.compile(r"hz-release --plugin\b"), "sm release plugin"),
    (re.compile(r"hz-release migrate-legacy\b"), "sm release resume migrate-legacy"),
    (re.compile(r"hz-train (status|next|clear|plan)\b"), r"sm release train \1"),
    (re.compile(r"hz-train (tick|run)\b"), "sm daemon train"),
    (re.compile(r"hz-train\b"), "sm release train"),
    (re.compile(r"hz-chores overdue-triage\b"), "sm beads sweep"),
    (re.compile(r"hz-chores pin-diagnose\b"), "sm release pins"),
    (re.compile(r"hz-chores post-publish\b"), "sm release after"),
    (re.compile(r"hz-ci-resume-after-tag\b"), "sm release train resume-ci"),
    (re.compile(r"hz-verify-mini\b"), "sm release verify --mini"),
    (re.compile(r"hz-assets-health\b"), "sm release assets"),
    (re.compile(r"hz-release-status\b"), "sm release status"),
    (re.compile(r"~/\.cache/hz-release/(v[0-9.]+\.json)"), r"the tail receipt \1"),
    (re.compile(r"~/\.cache/hz-release/"), "the tail receipts directory"),
    (re.compile(r"~/\.cache/hz-release\.json"), "the release receipt"),
    (re.compile(r"~/\.cache/hz-train/(v[0-9.]+\.(?:json|log))"), r"the train's \1"),
    (re.compile(r"~/\.cache/hz-train/"), "the train's state directory"),
    (re.compile(r"~/\.cache/hz-assets-health\.json"), "the probe cache"),
    (re.compile(r"tools/release_candidate_policy\.py"), "release_lib/approved_sources/ (replace the reviewed file)"),
)
BY_TOOL = sorted(((row.tool, row.path) for row in ROWS if row.path), key=lambda item: -len(item[0]))
LEFT = re.compile(r"(?<![/.\w-])hz-([a-z][a-z-]*)")  # never inside a path: a file is named as it is on disk


def translate(line: str) -> str:
    for pattern, new in SPECIAL:
        line = pattern.sub(new, line)
    for tool, path in BY_TOOL:
        if tool in line:
            line = re.sub(rf"(?<![/.\w-]){re.escape(tool)}\b(?![-\w])", f"sm {path.split(' --')[0]}", line)
    return LEFT.sub(lambda m: m[1].replace("-", " "), line)  # a tool without a command: its plain words


def translated(lines: list[str]) -> list[str]:
    return [translate(line) for line in lines]
