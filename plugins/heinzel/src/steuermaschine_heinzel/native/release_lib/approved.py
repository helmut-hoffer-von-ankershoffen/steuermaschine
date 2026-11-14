"""The reviewed sources of the release candidate, and their pins.

One source of truth: the reviewed bytes themselves, kept in `approved_sources/`. A pin is the sha256 of such a file,
computed here, so the policy, the tests and `sm release pins` can never disagree. To re-pin after an independent
review: replace the file with the reviewed content of the core repository (the diff in this repository is the
review's subject), nothing else.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent / "approved_sources"
# path in the core repository -> file name here (the recorder is kept as text: it is data, never imported)
FILES: dict[str, str] = {
    ".github/workflows/ci.yml": "ci.yml",
    "scripts/release_candidate.py": "release_candidate.py.txt",
}


def file(path: str) -> Path:
    """The reviewed copy of one pinned file, by its path in the core repository."""
    return DIRECTORY / FILES[path]


def blob(path: str) -> bytes:
    return file(path).read_bytes()


def pins() -> dict[str, str]:
    """path in the core repository -> sha256 of its reviewed content."""
    return {path: hashlib.sha256(blob(path)).hexdigest() for path in FILES}
