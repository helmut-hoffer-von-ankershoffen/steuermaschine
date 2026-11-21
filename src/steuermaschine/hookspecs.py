"""The extension points of steuermaschine, as pluggy hook specifications.

A plugin is any object (a module or an instance) with functions named like the specs below and marked
`@hookimpl`. Several plugins can be loaded for one project, in the order of `plugins = [...]` in the settings:

    plugins = ["base", "company", "repo"]        # base first; LATER ENTRIES ARE OUTER LAYERS

What "outer" means, per kind of hook:
- collecting hooks: every plugin's result is used, base first. Where results are keyed (traps, gates by name,
  commands by name, settings by key) a later plugin replaces an earlier one's entry.
- provider hooks (first result wins): the outermost plugin that returns one provides it; an inner plugin's and
  the core's default are not asked.
- the wrapper `sm_step_run`: the outermost plugin's wrapper runs first and last, around all inner ones and the step.

Hooks are called with keyword arguments only, and an implementation names just the arguments it needs.
`settings` is one mapping of all loaded plugins' settings (`settings["key"]`; `settings.of("plugin")` for one table).
"""

from __future__ import annotations

from typing import Any

import pluggy

PROJECT = "steuermaschine"
hookspec = pluggy.HookspecMarker(PROJECT)
hookimpl = pluggy.HookimplMarker(PROJECT)


class Specs:
    # ------------------------------------------------------------------ identity and settings
    @hookspec
    def sm_api_version(self) -> int:
        """Required. The plugin API version the plugin was written for (`steuermaschine.API_VERSION`)."""

    @hookspec
    def sm_settings(self) -> Any:
        """The pydantic model (extra="forbid") of this plugin's settings table `[<plugin name>]`."""

    # ------------------------------------------------------------------ collecting: all results are used
    @hookspec
    def sm_commands(self, runtime: Any) -> Any:
        """A Typer app whose groups and commands are merged at the root of `sm`. `runtime.settings` and, lazily,
        `runtime.providers` are available. A later plugin's name replaces an earlier one's and the core's."""

    @hookspec
    def sm_native_paths(self, settings: Any) -> set[Any]:
        """Command paths ("head admit") where the core's engine command wins over a same-named `sm_commands` command."""

    @hookspec
    def sm_pipelines(self, settings: Any) -> list[Any]:
        """`engine.Pipeline`s. Each becomes `sm <subject> <verb>`. A later plugin's pipeline with the same id replaces."""

    @hookspec
    def sm_steps(self, pipeline: Any, settings: Any) -> list[Any]:
        """Steps to add to another plugin's pipeline: `engine.Insert(step, after="name")` or `before="name"`."""

    @hookspec
    def sm_gates(self, settings: Any) -> list[Any]:
        """`engine.Gate`s by name. A step lists the gates it needs; `sm_steps`/`sm_step_run` can add more."""

    @hookspec
    def sm_traps(self, settings: Any) -> dict[str, Any]:
        """Text shown at a step, keyed "<subject>.<verb>.<step>". A later plugin's text replaces an earlier one's."""

    @hookspec
    def sm_native(self, settings: Any) -> list[Any]:
        """Native commands and group helps: `native.NativeCommand`s (from `@native` functions) and `native.Group`s.
        Use `native.collect(module, ...)`. They are placed after `sm_commands`; a later plugin's path replaces."""

    @hookspec
    def sm_tool_map(self) -> list[Any]:
        """`models.ToolMapRow`s: old tools and the commands that replaced them."""

    @hookspec
    def sm_state_moves(self, settings: Any, home: Any) -> list[Any]:
        """`statemove.Move`s: state paths of an earlier release and their names in this one (`home`: the project's
        state directory). `sm self migrate-state` moves them in one pass; nothing moves at run time."""

    @hookspec
    def sm_journal_migrate(self, providers: Any, settings: Any) -> list[str]:
        """Bring the plugin's own state into the subject journals (`steuermaschine.subjects`); one line per change.
        `sm self migrate-state` calls it after the moves, under the writers' lock."""

    @hookspec
    def sm_alarms(self, providers: Any, settings: Any) -> list[Any]:
        """Read-only. `plugins.Alarm`s that hold now."""

    @hookspec
    def sm_go_items(self, providers: Any, settings: Any) -> list[Any]:
        """Read-only. `plugins.GoItem`s: what else is in flight that the run states do not know."""

    @hookspec
    def sm_go_report(self, report: Any, providers: Any, settings: Any) -> None:
        """Read-only. Last look at the finished `go` report (a dict in the shape of `models.GoReport`): a layer may
        append ALSO items or set the trap. Called base first."""

    @hookspec
    def sm_help_lint_rules(self) -> list[Any]:
        """Extra help rules: callables taking one row of `sm self tree` and returning a problem text or None."""

    # ------------------------------------------------------------------ providers: the outermost result wins
    @hookspec(firstresult=True)
    def sm_forge(self, settings: Any, base: Any) -> Any:
        """A `protocols.Forge`. `base` is the `Providers` with the core defaults."""

    @hookspec(firstresult=True)
    def sm_ci(self, settings: Any, base: Any) -> Any:
        """A `protocols.CIProvider`."""

    @hookspec(firstresult=True)
    def sm_store(self, settings: Any, base: Any) -> Any:
        """A `protocols.Store`."""

    @hookspec(firstresult=True)
    def sm_tracker(self, settings: Any, base: Any) -> Any:
        """A `protocols.Tracker`."""

    @hookspec(firstresult=True)
    def sm_notifier(self, settings: Any, base: Any) -> Any:
        """A `protocols.Notifier`."""

    @hookspec(firstresult=True)
    def sm_verifier(self, settings: Any, base: Any) -> Any:
        """A `protocols.Verifier`."""

    @hookspec(firstresult=True)
    def sm_agent_runner(self, settings: Any, base: Any) -> Any:
        """A `protocols.AgentRunner`."""

    @hookspec(firstresult=True)
    def sm_queue(self, settings: Any, base: Any) -> Any:
        """A `protocols.Queue`."""

    @hookspec(firstresult=True)
    def sm_admission_policy(self, settings: Any, base: Any) -> Any:
        """A `protocols.AdmissionPolicy`."""

    @hookspec(firstresult=True)
    def sm_release_recipe(self, settings: Any, base: Any) -> Any:
        """A `protocols.ReleaseRecipe`."""

    @hookspec(firstresult=True)
    def sm_vcs(self, settings: Any, base: Any) -> Any:
        """A `protocols.Vcs`."""

    @hookspec(firstresult=True)
    def sm_process_runner(self, settings: Any, base: Any) -> Any:
        """A `protocols.Commands`: how local processes are started."""

    @hookspec(firstresult=True)
    def sm_clock(self, settings: Any, base: Any) -> Any:
        """A `protocols.Clock`."""

    @hookspec
    def sm_provider_extras(self, settings: Any, base: Any) -> dict[str, Any]:
        """Plugin-private objects, merged into `Providers.extra` (a later plugin's key replaces)."""

    # ------------------------------------------------------------------ wrapper around every step
    @hookspec(firstresult=True)
    def sm_step_run(self, step: Any, ctx: Any) -> Any:
        """Runs one step and returns its detail line. The core implements it (it calls `step.run(ctx)`). A plugin
        implements it as a wrapper to put policy around ANY step:

            @hookimpl(wrapper=True)
            def sm_step_run(self, step, ctx):
                if step.external and not allowed(ctx):
                    raise Refuse("company policy: no writes on Friday")      # a veto, with a reason
                detail = yield                                               # inner layers and the step
                ctx.providers.store.put_receipt("audit", ..., {...})        # after it
                return detail
        """


# Provider hook name -> field of protocols.Providers
PROVIDER_HOOKS = {"sm_forge": "forge", "sm_ci": "ci", "sm_store": "store", "sm_tracker": "tracker", "sm_notifier": "notifier", "sm_verifier": "verifier",
                  "sm_agent_runner": "agents", "sm_queue": "queue", "sm_admission_policy": "admission", "sm_release_recipe": "recipe", "sm_vcs": "vcs",
                  "sm_process_runner": "commands", "sm_clock": "clock"}


class Core:
    """The core's own implementations: the innermost layer."""

    @hookimpl
    def sm_api_version(self) -> int:
        from . import API_VERSION

        return API_VERSION

    @hookimpl(trylast=True)
    def sm_step_run(self, step: Any, ctx: Any) -> Any:
        return step.run(ctx)
