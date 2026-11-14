"""hz-release hands acceptance only a FINAL app-vX, also for a held core (review-690 finding 3)."""

import pytest
from steuermaschine_heinzel import state as statedir


@pytest.fixture
def rel(tool, home, monkeypatch):
    module = tool("hz-release")
    assert module.paths()[1] == statedir.root() / "release.json"  # isolated receipt before any write
    module.write_state({"version": "1.2.4"})
    monkeypatch.setattr(module.hzlib, "say", lambda *_: None)
    monkeypatch.setattr(module.hzlib, "sleep", lambda *_: None)
    monkeypatch.setattr(module, "compose_busy", lambda: False)
    return module


def world(rel, monkeypatch, *, core_held=True, apps=(), runs=()):
    """`apps`: successive app-v1.2.4 answers (None = missing); `runs`: successive compose run answers."""
    seen = {"dispatch": [], "apps": list(apps), "runs": list(runs)}

    def published(repo, tag):
        if repo == rel.CORE:
            assert tag == "v1.2.4"
            return {"tag_name": tag, "draft": False, "prerelease": core_held}
        assert (repo, tag) == (rel.PLUGINS, "app-v1.2.4")
        app = seen["apps"].pop(0) if len(seen["apps"]) > 1 else seen["apps"][0]
        return None if app is None else {"tag_name": tag, "draft": False, "prerelease": app == "held"}

    def ours(repo, workflow, key):
        if not seen["dispatch"]:
            return None
        return seen["runs"].pop(0) if len(seen["runs"]) > 1 else seen["runs"][0]

    def dispatch(repo, workflow, ref, key, **inputs):
        seen["dispatch"].append((key, inputs))
        rel.remember(**{key: "2026-01-01T00:00:00Z"})

    monkeypatch.setattr(rel, "published_release", published)
    monkeypatch.setattr(rel, "ours", ours)
    monkeypatch.setattr(rel, "dispatch", dispatch)
    monkeypatch.setattr(rel.hzlib, "gh", lambda *a: (0, "https://example.invalid/heinzel-1.2.4-py3-none-any.whl\n"))
    return seen


DONE = {"databaseId": 7, "status": "completed", "conclusion": "success"}
RUNNING = {"databaseId": 7, "status": "in_progress", "conclusion": None}


def test_held_core_composes_without_inputs_and_waits_for_the_final_app(rel, monkeypatch):
    seen = world(rel, monkeypatch, apps=[None, None, "held", "held", "held", "final"], runs=[RUNNING, DONE])
    rel.compose_step("1.2.4", 60)
    assert seen["dispatch"] == [("compose", {}), ("compose_finalize", {})]


def test_a_held_app_never_satisfies_the_compose_step(rel, monkeypatch):
    world(rel, monkeypatch, apps=["held"], runs=[DONE])
    rel.remember(compose="2026-01-01T00:00:00Z", compose_finalize="2026-01-01T00:00:00Z")
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(rel.time, "time", lambda: next(clock))
    with pytest.raises(rel.Stop, match="timed out"):
        rel.compose_step("1.2.4", 30)


def test_final_core_keeps_the_explicit_dispatch(rel, monkeypatch):
    seen = world(rel, monkeypatch, core_held=False, apps=[None, None, "final"], runs=[DONE])
    rel.compose_step("1.2.4", 60)
    assert seen["dispatch"] == [("compose", {"version": "1.2.4",
                                             "core_wheel": "https://example.invalid/heinzel-1.2.4-py3-none-any.whl"})]


def test_a_failed_finalize_run_stops(rel, monkeypatch):
    failed = {"databaseId": 8, "status": "completed", "conclusion": "failure"}
    world(rel, monkeypatch, apps=["held"], runs=[failed])
    rel.remember(compose="2026-01-01T00:00:00Z", compose_finalize="2026-01-01T00:00:00Z")
    monkeypatch.setattr(rel, "ours", lambda repo, workflow, key: DONE if key == "compose" else failed)
    with pytest.raises(rel.Stop, match="compose run 8 ended failure"):
        rel.compose_step("1.2.4", 60)
