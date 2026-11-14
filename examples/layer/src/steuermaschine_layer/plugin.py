"""A layer plugin: it has no pipeline of its own. It sits on a base plugin (plugins = ["toy", "layer"]) and adds
what a company or a single repository wants on top:

- one gate (`change-window`) and one step that needs it, inserted before the base plugin's merge step
- one trap, replacing the base plugin's text for the merge step
- a wrapper around EVERY step: an audit receipt after each external step, and a veto with a reason
- one line in `sm go`

Later entries in `plugins = [...]` are outer layers: this wrapper runs around the base plugin's and the step itself.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from steuermaschine import API_VERSION
from steuermaschine.engine import Gate, Insert, Refuse, Step
from steuermaschine.hookspecs import hookimpl


class LayerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frozen: bool = Field(False, description="A change freeze: no step that writes outside this machine may run.")
    window_open: bool = Field(True, description="Whether the change window is open (the gate `change-window`).")


class Layer:
    name = "layer"

    @hookimpl
    def sm_api_version(self):
        return API_VERSION

    @hookimpl
    def sm_settings(self):
        return LayerSettings

    @hookimpl
    def sm_gates(self, settings):
        return [Gate("change-window", "The change window is open.", lambda ctx: (bool(ctx.settings.get("window_open", True)), "the change window is closed"))]

    @hookimpl
    def sm_steps(self, pipeline):
        if not any(s.name == "merge" for s in pipeline.steps):
            return []
        return [Insert(Step("window", lambda ctx: "change window is open", gates=("change-window",)), before="merge")]

    @hookimpl
    def sm_traps(self):
        return {"head.ship.merge": "Company rule: merge only inside the change window; a freeze stops every write."}

    @hookimpl(wrapper=True)
    def sm_step_run(self, step, ctx):
        if step.external and ctx.settings.get("frozen"):
            raise Refuse(f"change freeze: '{step.name}' would write outside this machine", "sm self config")  # a veto, with the reason
        detail = yield  # inner layers and the step itself
        if step.external:
            ctx.providers.store.put_receipt("audit", f"{ctx.subject.key[:12]}.{step.name}", {"step": step.name, "detail": detail, "by": ctx.author})
        return detail

    @hookimpl
    def sm_go_report(self, report, settings):
        if settings.get("frozen"):
            report["also"].append({"phase": "layer: change freeze is ON; no write will run", "next": "", "waiting_on": ""})


plugin = Layer()
