"""The toy's one native write command, to show the review gate in a project that is not Heinzelmännchen.

`sm sim reset` empties the simulated world. It is registered `writes=True`, so the core runs it only once
`reviews/sim-reset.md` in this package ends with `VERDICT: APPROVE` and names the digest of this module
(`sm self reviews` prints it). Until then every call is refused with exit 70 and nothing is run.
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
