"""steuermaschine.verdicts: registered digests, authors, and the independent-verdict requirement."""

import pytest

from steuermaschine import verdicts
from steuermaschine.engine import Refuse
from steuermaschine.testing import MemoryStore

D = "d" * 64
RUN = [{"author": "run-author", "steps": {"review": {"digest": D}, "other": {}}}]


def test_register_merges_authors_and_sources_and_authors_of_adds_runs():
    store = MemoryStore()
    verdicts.register(store, D, "prepare", "a1")
    verdicts.register(store, D, "prepare", "a1")
    verdicts.register(store, D, "other", "a2")
    assert verdicts.authors_of(store, D, RUN) == {"a1", "a2", "run-author"}
    assert store.load(verdicts.KIND, D)["sources"] == ["prepare", "other"]
    assert verdicts.authors_of(store, "e" * 64, RUN) == set()


def test_current_agent_defaults_to_coordinator(monkeypatch):
    monkeypatch.delenv("STEUERMASCHINE_AGENT", raising=False)
    assert verdicts.current_agent() == "coordinator"
    monkeypatch.setenv("STEUERMASCHINE_AGENT", "me")
    assert verdicts.current_agent() == "me"


def test_require_independent():
    store = MemoryStore()
    with pytest.raises(Refuse, match="no verdict"):
        verdicts.require_independent(store, D, RUN, "record it")
    store.put_receipt("verdicts", D, {"verdict": "approve", "reviewer": "run-author"})
    with pytest.raises(Refuse, match="who also produced"):
        verdicts.require_independent(store, D, RUN, "record it")
    assert verdicts.require_independent(store, D, [], "record it")["reviewer"] == "run-author"  # no known author: the receipt stands


def test_require_independent_refuses_a_rejection():
    store = MemoryStore()
    store.put_receipt("verdicts", D, {"verdict": "reject", "reviewer": "r", "notes": "bad"})
    with pytest.raises(Refuse, match="'reject'"):
        verdicts.require_independent(store, D, [], "record it")


def test_a_ci_change_verdict_is_needed_where_the_gate_asks_for_one():
    store = MemoryStore()
    sha = "c" * 40
    verdicts.register(store, sha, "release ci_change", "release-driver")
    store.put_receipt("verdicts", sha, {"digest": sha, "verdict": "approve", "reviewer": "r1"})
    assert verdicts.require_independent(store, sha, [], "sm review verdict")["reviewer"] == "r1"  # a plain verdict
    with pytest.raises(Refuse, match="not a ci_change verdict"):
        verdicts.require_independent(store, sha, [], "sm review verdict", kind=verdicts.CI_CHANGE)
    other = "b" * 40
    verdicts.register(store, other, "release ci_change", "release-driver")
    store.put_receipt("verdicts", other, {"digest": other, "verdict": "approve", "reviewer": "r1", "kind": verdicts.CI_CHANGE})
    assert verdicts.require_independent(store, other, [], "sm review verdict", kind=verdicts.CI_CHANGE)["kind"] == "ci_change"
