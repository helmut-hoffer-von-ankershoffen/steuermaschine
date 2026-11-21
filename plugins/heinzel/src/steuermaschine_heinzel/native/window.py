"""The validation window's line, for the admission hold of `sm release admit`: the settings and the window itself.

The line of heads, the per-branch rule and the cap are the core's (`steuermaschine.window`). `sm head validate` and
`sm ci window` are gone with the head admission: Core's CI runs the full matrix on every PR.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from steuermaschine.window import Rules, Turn, Window, process_alive

NAMESPACE = "heinzel.validation"


class WindowSettings(BaseModel):
    """Settings of the validation window, a mixin of the plugin's table."""

    window_lease_minutes: float = Field(180.0, description="A head that never gave the validation window back is passed after this long.")
    window_waiting_minutes: float = Field(30.0, description="A waiting head whose command stopped asking is dropped after this long.")
    release_window_minutes: float = Field(60.0, description="How long a release admit prepare or apply holds the release-window lock "
                                                            "(hosted validations wait meanwhile); renewed by each, given back by a successful apply.")


def the_window(rt) -> Window:
    s = rt.settings
    rules = Rules(lease_minutes=float(s.get("window_lease_minutes") or 180.0), waiting_minutes=float(s.get("window_waiting_minutes") or 30.0))
    return Window(rt.providers.store, rt.providers.clock, rules, NAMESPACE, alive=liveness)


def liveness(turn: Turn) -> bool | None:
    """Whether the command of a turn still runs (the core's check; a seam for the tests)."""
    return process_alive(turn)
