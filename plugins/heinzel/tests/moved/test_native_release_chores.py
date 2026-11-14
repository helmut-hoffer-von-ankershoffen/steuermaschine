import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from steuermaschine_heinzel.native.release_read_lib import chores as c


def test_pr_refs():
    assert c.pr_refs("done in #592 and PR 615, pull/1234") == [592, 615, 1234]


def test_triage_only_merged():
    beads = [{"id": "a", "title": "t1"}, {"id": "b", "title": "t2"}]
    assert c.triage(beads, {"a": "see #592", "b": "see #700"}, {592}) == [("a", "t1", [592])]


def test_find_approved_blob():
    h = [("new", b"x2"), ("old", b"x1")]
    assert c.find_approved_blob(h, c.sha(b"x1")) == "old"
    assert c.find_approved_blob(h, "0") is None


def test_help_and_post(capsys):
    c.main(["post-publish", "0.9.31"])
    assert "sm release verify --mini" in capsys.readouterr().out
