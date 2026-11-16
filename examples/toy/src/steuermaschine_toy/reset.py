"""The toy's one native write command, to show a write command in a project that is not Heinzelmännchen.

`sm sim reset` empties the simulated world. It is registered `writes=True`: it runs under the writers' lock, and
`sm self reviews` names it when a change reaches this module.
"""

from __future__ import annotations

from steuermaschine import PROG
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime


class Reset(Model):
    """What `sim reset` did."""

    world: str
    cleared: list[str]

    def render(self) -> list[str]:
        return [f"cleared {', '.join(self.cleared) or 'nothing'} in {self.world}"]


@native("sim reset", writes=True, help=H(
    "Empty the simulated world: pull requests, CI results, tags and the remote.",
    "Use when the quick start should begin again from nothing.",
    "the simulated world file (state other commands read).",
    f"{PROG} sim reset",
    f"{PROG} head ship . --plan"))
def sim_reset() -> Reset:
    world = runtime().providers.extra["world"]
    data = world.read()
    cleared = sorted(key for key, value in data.items() if value)
    world.write({"prs": [], "ci": {}, "tags": {}, "remote": {}})
    return Reset(world=str(world.path), cleared=cleared)
