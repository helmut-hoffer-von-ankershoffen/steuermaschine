"""hz-release publishes the acceptance verdict as release asset `acceptance.json` (bead m3kj); core auto-update
installs only a release whose marker says accepted."""

import json

import pytest


@pytest.fixture
def rel(tool, home):
    return tool("hz-release")


def fake_gh(rel, monkeypatch, *, upload_code=0, listed=True):
    seen = {}

    def gh(*args):
        if args[:2] == ("release", "upload"):
            seen["args"] = args
            seen["marker"] = json.loads(open(args[3]).read())
            return upload_code, ""
        raise AssertionError(args)

    def ghj(*args, default=None):
        return ["heinzel-1.2.4.tar.gz", *(["acceptance.json"] if listed else [])]

    monkeypatch.setattr(rel.hzlib, "gh", gh)
    monkeypatch.setattr(rel.hzlib, "ghj", ghj)
    monkeypatch.setattr(rel.hzlib, "say", lambda *_: None)
    return seen


@pytest.mark.parametrize("good", [True, False])
def test_the_verdict_is_uploaded_as_the_release_marker(rel, monkeypatch, good):
    seen = fake_gh(rel, monkeypatch)
    rel.real_publish_acceptance("1.2.4", 77, good)
    assert seen["args"][2] == "v1.2.4" and "--clobber" in seen["args"]
    assert seen["args"][3].endswith("/acceptance.json")
    marker = seen["marker"]
    assert marker["version"] == "1.2.4" and marker["accepted"] is good and marker["acceptance_run"] == 77
    assert marker["schema"] == 1 and marker["project"] == "heinzel"


@pytest.mark.parametrize(("code", "listed"), [(1, True), (0, False)])
def test_a_failed_upload_stops_before_finished(rel, monkeypatch, code, listed):
    fake_gh(rel, monkeypatch, upload_code=code, listed=listed)
    with pytest.raises(rel.Stop, match="acceptance marker"):
        rel.real_publish_acceptance("1.2.4", 77, True)
