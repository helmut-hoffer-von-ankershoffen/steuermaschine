"""The core's own native commands: generic, usable by any project without a plugin. They are the innermost layer:
a plugin's native command with the same path replaces one (docs/DESIGN.md, section 5, the order rule). What a
project adds to them (its run-lock policy, its default document, its log directory) it hands
in through `sm_provider_extras` under the key each module names."""

from __future__ import annotations

from typing import Any

from ..native import collect
from . import doc, gate, lim

MODULES: list[Any] = [doc, gate, lim]


def natives() -> list[Any]:
    return collect(*MODULES)
