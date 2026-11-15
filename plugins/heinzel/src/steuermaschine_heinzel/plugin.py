"""What steuermaschine-heinzel registers with the core, as hook implementations.

Every command is native (`native/<area>.py`, registered with `@native`); nothing here starts an old script.
What is not ported yet is listed by `sm tools gaps`, from the tool map in toolmap.py.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from steuermaschine import API_VERSION
from steuermaschine.hookspecs import hookimpl

from .native.admission import AdmissionSettings
from .native.affected import SelectSettings
from .native.agents import AgentsSettings
from .native.alarm_asks import AskAlarmSettings
from .native.asks import AsksSettings
from .native.briefs import BriefSettings
from .native.ci_classify import ClassifySettings
from .native.ci_ops import CiOpsSettings
from .native.ci_read import CiSettings
from .native.combine import CombineSettings
from .native.coord import CoordSettings
from .native.dispatch import DispatchSettings
from .native.docs import DocsSettings
from .native.flakes import FlakesSettings
from .native.go import GoSettings
from .native.guards import GuardsSettings
from .native.handoff import HandoffSettings
from .native.leakwatch import LeakwatchSettings
from .native.loadguard import LoadguardSettings
from .native.lock import LockSettings
from .native.loops import LoopsSettings
from .native.merge_tier import MergeTierSettings
from .native.mini import MiniSettings
from .native.plugin_gallery import GallerySettings
from .native.preflight import PreflightSettings
from .native.queue import QueueSettings
from .native.refill import RefillSettings
from .native.rehearse import RehearseSettings
from .native.release_lib.binding import ReleaseSettings
from .native.ship import ShipSettings
from .native.skills import SkillsSettings
from .native.status import StatusSettings
from .native.tick import TickSettings
from .native.train import TrainSettings
from .native.updater_hold import UpdaterHoldSettings
from .native.velocity import VelocitySettings
from .native.why import WhySettings
from .native.window import WindowSettings


class HeinzelSettings(AdmissionSettings, AgentsSettings, AskAlarmSettings, AsksSettings, BriefSettings, CiOpsSettings, CiSettings, ClassifySettings, CombineSettings, CoordSettings, DispatchSettings, DocsSettings, FlakesSettings, GallerySettings, GoSettings, GuardsSettings, HandoffSettings, LeakwatchSettings, LoadguardSettings, LockSettings, LoopsSettings, MergeTierSettings, MiniSettings, PreflightSettings, QueueSettings, RefillSettings, RehearseSettings, ReleaseSettings, SelectSettings, ShipSettings, SkillsSettings, StatusSettings, TickSettings, TrainSettings, UpdaterHoldSettings, VelocitySettings, WhySettings, WindowSettings, BaseModel):
    """The table [heinzel]. Every field can also be set as SM_HEINZEL__<FIELD> in the environment.
    Each area adds its fields as a mixin in its own module and is listed in the bases here."""

    model_config = ConfigDict(extra="forbid")
    repo: str = Field("helmut-hoffer-von-ankershoffen/heinzel", description="The core repository as OWNER/NAME.")
    sonar_project: str = Field("helmut-hoffer-von-ankershoffen_heinzel", description="The Sonar project key.")


CORE_SERVED = frozenset({"doc section", "gate run", "gate status", "lim"})  # command paths the core's own commands serve now


def validation_window(rt: Any) -> Any:
    """The window of `sm head validate`, for the core's `sm ci window status`; imported only when it is asked for."""
    from .native import window

    return window.the_window(rt)


class Heinzel:
    name = "heinzel"

    @hookimpl
    def sm_api_version(self) -> Any:
        return API_VERSION

    @hookimpl
    def sm_settings(self) -> Any:
        return HeinzelSettings

    @hookimpl
    def sm_native(self, settings: Any) -> Any:
        from . import native

        return native.commands()

    @hookimpl
    def sm_pipelines(self, settings: Any) -> Any:
        from .pipelines import pipelines

        return pipelines(settings)

    @hookimpl
    def sm_alarms(self, providers: Any, settings: Any) -> Any:
        from .native import alarm

        return alarm.recorded_alarms(providers, settings)

    @hookimpl
    def sm_tool_map(self) -> Any:
        from .toolmap import ROWS

        return ROWS

    @hookimpl
    def sm_traps(self, settings: Any) -> Any:
        return {}

    @hookimpl
    def sm_provider_extras(self, settings: Any) -> Any:
        """What this project adds to the core's generic commands: the Mini's run lock policy (`sm gate`), the tracking
        document as the default of `sm doc section`, the directory of `sm lim`'s full outputs, and the validation window
        of `sm ci window status` (the focused cap, the settings' lease, the plugin's namespace)."""
        from steuermaschine.commands import doc, gate, lim, window

        from .native import docs, lock

        return {gate.POLICY: lock.policy, doc.DOCUMENT: lambda: docs.default_document(settings), lim.DIRECTORY: settings.get("guards_lim_dir"),
                window.WINDOW: validation_window}

    @hookimpl
    def sm_state_moves(self, settings: Any, home: Any) -> Any:
        from . import state

        return state.moves(home)


plugin = Heinzel()
