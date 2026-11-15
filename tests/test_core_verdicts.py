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


def test_a_digest_bound_to_a_head_needs_that_heads_green_check_receipt():
    store = MemoryStore()
    head, other = "a" * 40, "b" * 40
    assert verdicts.missing_check(store, D) == ""  # nobody bound the digest to a commit: no receipt is asked for
    verdicts.register(store, D, "packet", "a1")
    verdicts.need_check(store, D, head, "tool check WORKTREE")
    verdicts.need_check(store, D, head, "tool check WORKTREE")  # the same binding again writes nothing
    assert store.load(verdicts.KIND, D) == {"digest": D, "authors": ["a1"], "sources": ["packet"], "check": {"head": head, "command": "tool check WORKTREE"}}
    why = verdicts.missing_check(store, D)
    assert head[:12] in why and "`tool check WORKTREE`" in why and "exactly that commit" in why
    assert verdicts.record_check(store, other, {"branch": "x"}) == "written"  # a receipt of another commit does not count
    assert verdicts.missing_check(store, D) != ""
    assert verdicts.record_check(store, head, {"branch": "feat/x"}) == "written"
    assert store.get_receipt(verdicts.CHECKS, head) == {"branch": "feat/x", "head": head, "green": True}
    assert verdicts.record_check(store, head, {"branch": "later"}) == "kept"  # written once: the first receipt stays
    assert store.get_receipt(verdicts.CHECKS, head)["branch"] == "feat/x"
    assert verdicts.missing_check(store, D) == ""


def test_need_check_makes_the_digest_record_and_register_keeps_the_binding():
    store = MemoryStore()
    verdicts.need_check(store, D, "a" * 40, "tool check")
    verdicts.register(store, D, "prepare", "a1")
    assert store.load(verdicts.KIND, D)["check"]["head"] == "a" * 40 and verdicts.authors_of(store, D, []) == {"a1"}
    store.put_receipt(verdicts.CHECKS, "a" * 40, {"green": False})  # a receipt that is not green is no receipt
    assert "its check receipt is not green" in verdicts.missing_check(store, D)
    assert verdicts.record_check(store, "a" * 40, {}) == "its check receipt is not green"  # and it cannot be replaced: not "kept"


def test_the_receipt_must_be_measured_from_the_base_the_digest_binds():
    """B2 (the reviewer's probe): checks of the last commit only (`--base HEAD~1`) are no checks of the head."""
    store = MemoryStore()
    head, main, part = "a" * 40, "b" * 40, "c" * 40
    verdicts.need_check(store, D, head, "tool check WT", base=main)
    assert store.load(verdicts.KIND, D)["check"] == {"head": head, "command": "tool check WT", "base": main}
    verdicts.record_check(store, head, {"base": part})
    why = verdicts.missing_check(store, D)
    assert f"on the base {main[:12]}" in why and f"measured from {part[:12]}" in why and "`tool check WT`" in why
    other = "e" * 64  # the same head bound on the base the receipt was measured from: accepted
    verdicts.need_check(store, other, head, "tool check WT", base=part)
    assert verdicts.missing_check(store, other) == ""
    verdicts.need_check(store, D, head, "tool check WT", base=part)  # a new binding replaces the old one
    assert verdicts.missing_check(store, D) == ""


class Damaged(MemoryStore):
    def get_receipt(self, kind, key):
        if kind == verdicts.CHECKS:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return super().get_receipt(kind, key)


def test_an_unreadable_check_receipt_is_refused_with_words_and_never_counts_as_kept():
    store = Damaged()
    head = "a" * 40
    store.receipts[(verdicts.CHECKS, head)] = "{"  # half a receipt, as an interrupted write once left it
    assert verdicts.check_receipt(store, head)[0] is None
    verdicts.need_check(store, D, head, "tool check")
    why = verdicts.missing_check(store, D)  # no traceback: a refusal that says what is wrong
    assert "exists but cannot be read (ValueError)" in why and "needs a new sha" in why
    state = verdicts.record_check(store, head, {})
    assert state not in ("written", "kept") and "cannot be read" in state


def test_an_approval_is_not_usable_while_the_bound_head_has_no_check_receipt():
    """C1 (the reviewer's probe): an approval recorded before the digest was bound does not pass where it is used."""
    store = MemoryStore()
    head = "a" * 40
    store.put_receipt("verdicts", D, {"digest": D, "verdict": "approve", "reviewer": "r1"})  # recorded first
    assert verdicts.require_independent(store, D, [], "record it")["reviewer"] == "r1"
    verdicts.need_check(store, D, head, "tool check")  # bound afterwards, by the next run
    with pytest.raises(Refuse, match="no check receipt exists"):
        verdicts.require_independent(store, D, [], "record it")
    verdicts.record_check(store, head, {})
    assert verdicts.require_independent(store, D, [], "record it")["reviewer"] == "r1"


def test_a_file_receipt_is_written_whole_once_and_leaves_no_draft(tmp_path):
    from steuermaschine.defaults import FileStore

    store = FileStore(tmp_path)
    store.put_receipt(verdicts.CHECKS, "a" * 40, {"green": True})
    with pytest.raises(FileExistsError):
        store.put_receipt(verdicts.CHECKS, "a" * 40, {"green": False})
    folder = tmp_path / "receipts" / verdicts.CHECKS
    assert [path.name for path in folder.iterdir()] == ["a" * 40 + ".json"]  # no draft stays, also after the refused write
    assert store.get_receipt(verdicts.CHECKS, "a" * 40) == {"green": True}
    (folder / ("b" * 40 + ".json")).write_text("{")  # a damaged file from before: refused in words
    verdicts.need_check(store, D, "b" * 40, "tool check")
    assert "cannot be read" in verdicts.missing_check(store, D)
