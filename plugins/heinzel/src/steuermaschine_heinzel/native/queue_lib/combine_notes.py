"""Refuse to queue a combine PR whose body would drop its members from the release notes.

core's scripts/changelog_combines.py writes one release-notes entry per `Combines #N type(scope): title` line in the squash
body. A combine whose body has none (for example only "Merged via this PR: #1, #2") lands as one "Miscellaneous" entry and
its members vanish from the notes while the release introduction still describes them (0.9.39, PR #770).
"""

import re

# old name kept: matches titles of commits already on main
COMBINE_HINT = re.compile(r"hz-combine|^combine of\b|merged via this pr|^(?:\w+(?:\([^)]*\))?: )?(?:compose #\d+ into|combine of \d+)\b", re.I | re.M)
MEMBER_LINE = re.compile(r"^Combines #(?P<n>\d+) (?P<type>[a-z]+)(?:\([^)]+\))?!?: \S.*$", re.M)
MERGED_VIA = re.compile(r"^Merged via this PR:\s*(?P<list>[^\n]*)", re.I | re.M)


def problems(title: str, body: str) -> list[str]:
    """Return what is wrong with a combine PR's title and body; empty for a non-combine or a correct combine."""
    text = f"{title}\n{body or ''}"
    if not COMBINE_HINT.search(text):
        return []
    members = {m.group("n") for m in MEMBER_LINE.finditer(body or "")}
    if not members:
        return ["combine PR body has no `Combines #N type(scope): title` line per member; the release notes would drop its members"]
    listed = {n for m in MERGED_VIA.finditer(body or "") for n in re.findall(r"#(\d+)", m.group("list"))}
    missing = sorted(listed - members, key=int)
    return [f"members listed as merged via this PR but without a `Combines #N ...` line: {', '.join('#' + n for n in missing)}"] if missing else []
