"""Layered plugins: several plugins on one project, with the documented order.

plugins = ["base", "mid", "outer"]: base first, LATER ENTRIES ARE OUTER LAYERS.
"""

from steuermaschine import API_VERSION
from steuermaschine.engine import Engine, Gate, Insert, Options, Pipeline, Refuse, Step, Subject
from steuermaschine.hookspecs import hookimpl
from steuermaschine.plugins import Settings, available, load, register, stack_of
from steuermaschine.testing import fake_providers

LOG: list[str] = []


def identify(providers, settings, arg):
    return Subject(key=arg, label=arg, fingerprint={"head": arg}, facts={})


class Base:
    name = "base"

    @hookimpl
    def sm_api_version(self):
        return API_VERSION

    @hookimpl
    def sm_pipelines(self):
        return [Pipeline("head", "ship", "Ship.", [Step("build", lambda ctx: LOG.append("step build") or "built"),
                                                    Step("merge", lambda ctx: LOG.append("step merge") or "merged")], identify)]

    @hookimpl
    def sm_traps(self):
        return {"head.ship.build": "base trap for build", "head.ship.merge": "base trap for merge"}

    @hookimpl
    def sm_gates(self):
        return [Gate("green", "base: CI is green", lambda ctx: (True, ""))]

    @hookimpl
    def sm_notifier(self):
        return "notifier of base"

    @hookimpl
    def sm_tracker(self):
        return "tracker of base"

    @hookimpl
    def sm_provider_extras(self):
        return {"who": "base", "base_only": 1}


class Mid:
    name = "mid"

    @hookimpl
    def sm_api_version(self):
        return API_VERSION

    @hookimpl(wrapper=True)
    def sm_step_run(self, step, ctx):
        LOG.append(f"mid before {step.name}")
        detail = yield
        LOG.append(f"mid after {step.name}")
        return detail

    @hookimpl
    def sm_traps(self):
        return {"head.ship.merge": "mid trap for merge"}

    @hookimpl
    def sm_notifier(self):
        return "notifier of mid"


class Outer:
    name = "outer"

    @hookimpl
    def sm_api_version(self):
        return API_VERSION

    @hookimpl(wrapper=True)
    def sm_step_run(self, step, ctx):
        LOG.append(f"outer before {step.name}")
        if ctx.inputs.get("veto") == step.name:
            raise Refuse(f"outer layer vetoes {step.name}")
        detail = yield
        LOG.append(f"outer after {step.name}")
        return detail + " (audited)"

    @hookimpl
    def sm_steps(self, pipeline):
        return [Insert(Step("audit", lambda ctx: LOG.append("step audit") or "audited"), before="merge")]

    @hookimpl
    def sm_gates(self):
        return [Gate("green", "outer: CI is green and signed", lambda ctx: (True, "")), Gate("window", "outer: change window", lambda ctx: (True, ""))]

    @hookimpl
    def sm_provider_extras(self):
        return {"who": "outer"}


def stack():
    return stack_of(Base(), Mid(), Outer())


def run(inputs=None):
    LOG.clear()
    s, p = stack(), fake_providers()
    settings = Settings({})
    pl = s.pipelines(settings)[0]
    engine = Engine("proj", s.providers(settings, p), settings, traps=s.traps(settings), out=lambda line: None, run_step=s.run_step)
    return engine.advance(pl, "abc", Options(inputs=inputs or {})), pl


def test_wrappers_nest_with_later_plugins_outside():
    report, _ = run()
    assert report.status == "finished"
    assert LOG[:5] == ["outer before build", "mid before build", "step build", "mid after build", "outer after build"]
    assert report.steps[0]["detail"] == "built (audited)"  # the outermost layer has the last word on the result


def test_an_outer_layer_can_veto_a_step_with_a_reason_and_inner_layers_never_see_it():
    report, _ = run({"veto": "merge"})
    assert report.status == "refused" and "outer layer vetoes merge" in report.phase
    assert "mid before merge" not in LOG and "step merge" not in LOG and "step build" in LOG


def test_a_layer_adds_a_step_to_the_base_pipeline():
    _, pl = run()
    assert [s.name for s in pl.steps] == ["build", "audit", "merge"] and LOG.index("step audit") < LOG.index("step merge")


def test_keyed_results_merge_base_first_and_a_later_plugin_replaces_an_entry():
    s = stack()
    assert s.traps(Settings({})) == {"head.ship.build": "base trap for build", "head.ship.merge": "mid trap for merge"}
    gates = {g.name: g.describe for g in s.gates(Settings({}))}
    assert gates == {"green": "outer: CI is green and signed", "window": "outer: change window"}


def test_for_a_provider_the_outermost_plugin_that_offers_one_wins():
    p = stack().providers(Settings({}), fake_providers())
    assert p.notifier == "notifier of mid"  # outer offers none, mid is outside base
    assert p.tracker == "tracker of base" and p.extra == {"who": "outer", "base_only": 1}
    assert type(p.store).__name__ == "MemoryStore"  # nobody offers one: the default stays


def test_order_comes_from_the_settings_list_and_unlisted_plugins_stay_unloaded():
    for plugin in (Base(), Mid(), Outer()):
        register(plugin)
    assert {"base", "mid", "outer"} <= set(available())
    s = load(["base", "outer"])
    assert s.names == ["base", "outer"] and s.pm.get_plugin("mid") is None and s.pm.is_blocked("mid") and s.pm.is_blocked("toy")
    rows = s.describe()
    assert [(r["order"], r["name"], r["layer"]) for r in rows] == [(1, "base", "base"), (2, "outer", "outermost")]
    assert rows[1]["wrappers"] == ["sm_step_run"]
    assert load(["outer", "base"]).names == ["outer", "base"]  # the list is the order, not the install order
