"""`sm ci window status`: who holds the hosted validation window, and which heads wait (`steuermaschine.window`). Read-only.

The window's rules (the cap of runs in flight, the lease, how long a waiting head is kept) and its namespace in the
store are the project's: a plugin hands in a factory through `sm_provider_extras` under the key `WINDOW`,

    window(runtime) -> steuermaschine.window.Window

Without one the core's `Rules()` apply in the namespace `core`. Starting validations and ending a turn stay with the
project: they dispatch its workflows.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..helpdoc import H
from ..native import native, runtime
from ..window import Rules, Window, WindowStatus

WINDOW = "window.factory"  # key in `Providers.extra`: (runtime) -> Window of the project


def the_window() -> Window:
    """The project's window (`WINDOW`), else the core's rules in the namespace `core`."""
    rt: Any = runtime()
    factory: Callable[[Any], Window] | None = (rt.providers.extra or {}).get(WINDOW)
    return factory(rt) if factory else Window(rt.providers.store, rt.providers.clock, Rules(), "core")


@native("ci window status", writes=False, help=H(
    "Who holds the hosted validation window, and which heads wait, in order.",
    "Use when a validation says it waits, or before you start one.",
    "nothing (read-only).",
    "sm ci window status",
    "sm go   (the validation command of the project, once the window is free)"))
def ci_window_status() -> WindowStatus:
    return the_window().status()
