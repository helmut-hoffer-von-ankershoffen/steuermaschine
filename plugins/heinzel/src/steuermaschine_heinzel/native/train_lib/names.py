"""How the release train's lines are worded: the release code's translation of old names (`release_lib/names.py`, not
changed here), with two additions for the train. The train's own state files are named, never printed as a path:
`<state dir>/v1.2.3.log` and `~/.cache/hz-train/v1.2.3.log` both read `the train's v1.2.3.log`. Any other old name
inside a path stays as the file is named on disk; the release translation would otherwise rewrite the `hz-train` of
`~/.cache/hz-train/` as a command."""

from __future__ import annotations

import re
from pathlib import Path

from ..release_lib import names

STATE = re.escape("~/.cache/hz-train")  # the state directory as the moved code writes it in its own texts
IN_PATH = re.compile(r"(?<=[/.\w-])hz-")  # an old name that is part of a file or directory name
MASK = "\x00"


def named(line: str, directory: str) -> str:
    """`DIR/v1.log` -> `the train's v1.log`; `DIR` alone -> `the train's state directory`. `directory` is a regex."""
    line = re.sub(directory + r"/(?=[\w.-])", "the train's ", line)
    return re.sub(directory + r"/?(?![\w.-])", "the train's state directory", line)


def translate(line: str, state_dir: Path | str | None = None) -> str:
    if state_dir:
        line = named(line, re.escape(str(state_dir)))
    line = named(line, STATE)
    return names.translate(IN_PATH.sub(MASK, line)).replace(MASK, "hz-")


def translated(lines: list[str], state_dir: Path | str | None = None) -> list[str]:
    return [translate(line, state_dir) for line in lines]
