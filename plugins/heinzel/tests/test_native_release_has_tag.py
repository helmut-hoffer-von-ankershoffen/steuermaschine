"""`has_tag` answers only on a clear reply: a gh failure that is not "Not Found" stops the release instead of
reading as "no tag" (which would freeze main again)."""

import json

import pytest

from steuermaschine_heinzel.native.queue_lib import edge

NOT_FOUND = json.dumps({"message": "Not Found", "documentation_url": "https://docs.github.com", "status": "404"})


@pytest.fixture
def rel(monkeypatch):
    module = edge.fresh(edge.module_of("hz-release"))
    reply = {}
    monkeypatch.setattr(module.hzlib, "gh", lambda *a: reply["v"])
    return module, reply


def test_has_tag_true_only_on_success_and_false_only_on_not_found(rel):
    module, reply = rel
    reply["v"] = (0, '{"ref": "refs/tags/v1.2.3"}')
    assert module.has_tag("o/r", "v1.2.3") is True
    reply["v"] = (1, NOT_FOUND)
    assert module.has_tag("o/r", "v1.2.3") is False


@pytest.mark.parametrize("reply_value", [(124, ""), (1, ""), (1, "not json"),
                                         (1, json.dumps({"message": "Server Error", "status": "502"}))])
def test_has_tag_unclear_failure_stops_and_never_acts(rel, reply_value):
    module, reply = rel
    reply["v"] = reply_value
    with pytest.raises(module.Stop, match=r"cannot tell whether v1\.2\.3 exists"):
        module.has_tag("o/r", "v1.2.3")
    acted = []
    with pytest.raises(module.Stop):
        module.step("freeze", lambda: module.has_tag("o/r", "v1.2.3"), lambda: acted.append(1), 60)
    assert not acted
