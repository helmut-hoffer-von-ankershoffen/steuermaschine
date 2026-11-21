"""hz-release promotes only an ACCEPTED release out of its pre-release hold (core ADR 0114, bead cgdn)."""

import pytest
from steuermaschine_heinzel import state as statedir


@pytest.fixture
def rel(tool, home):
    module = tool("hz-release")
    assert module.paths()[1] == statedir.root() / "release.json"  # isolated receipt before any write
    return module


WHEEL = "heinzel-1.2.4-py3-none-any.whl"
SNAPSHOT = {"version": "1.2.4", "tag": "v1.2.4", "commit": "c" * 40, "wheel_sha256": "a" * 64, "profile_sha256": "b" * 64}


def release(*, draft=False, prerelease=True, wheel="a" * 64, tag="v1.2.4"):
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease, "assets": [
        {"name": WHEEL, "digest": f"sha256:{wheel}"}, {"name": "artifact-manifest.json", "digest": "sha256:" + "e" * 64}]}


def fake_gh(rel, monkeypatch, states, *, edit_code=0, finals="v1.2.3\n", receipt=None, latest=("v1.2.4",)):
    """`states`: the release objects GitHub gives, in order; `finals`: the published final tags it lists; `latest`: the
    tags releases/latest names, in order (the last one repeats)."""
    named = list(latest)
    rel.write_state(receipt if receipt is not None else {"version": "1.2.4", "snapshot": SNAPSHOT})
    calls = []
    answers = iter(states)

    def gh(*args):
        if args[0] == "api" and args[1].endswith("/releases/latest"):
            return 0, (named.pop(0) if len(named) > 1 else named[0]) + "\n"
        if args[0] == "api":
            assert "releases?per_page=30" in args[1] and "prerelease | not" in args[3]
            return (0, finals) if finals is not None else (1, "")
        calls.append(args)
        assert args[:3] == ("release", "edit", "v1.2.4")
        return edit_code, ""

    def ghj(*args, default=None):
        assert args[1].endswith("/releases/tags/v1.2.4")
        return next(answers)

    monkeypatch.setattr(rel.hzlib, "gh", gh)
    monkeypatch.setattr(rel.hzlib, "ghj", ghj)
    monkeypatch.setattr(rel.hzlib, "say", lambda *_: None)
    return calls


HELD = release()
FINAL = release(prerelease=False)
EDIT = [("release", "edit", "v1.2.4", "--prerelease=false", "--latest", "-R", "CORE")]


def edits(rel):
    return [tuple(rel.CORE if part == "CORE" else part for part in call) for call in EDIT]


def test_a_held_accepted_release_becomes_final_and_latest(rel, monkeypatch):
    calls = fake_gh(rel, monkeypatch, [HELD, FINAL])
    rel.real_promote_release("1.2.4")
    assert calls == edits(rel)
    assert rel.state()["promoted_at"]


def test_the_identity_snapshot_pins_the_promoted_bytes(rel, monkeypatch):
    receipt = {"version": "1.2.4", "acceptance_identity": {"snapshot": SNAPSHOT}}
    assert fake_gh(rel, monkeypatch, [HELD, FINAL], receipt=receipt) == []
    rel.real_promote_release("1.2.4")


def test_promotion_is_idempotent(rel, monkeypatch):
    calls = fake_gh(rel, monkeypatch, [HELD, FINAL, FINAL])
    rel.real_promote_release("1.2.4")
    rel.real_promote_release("1.2.4")
    assert calls == edits(rel)


def test_a_final_release_that_is_not_latest_becomes_latest(rel, monkeypatch):
    # Core's Publish creates the release final but not Latest: promotion marks it Latest after its verdict.
    calls = fake_gh(rel, monkeypatch, [FINAL], latest=("v1.2.3", "v1.2.4"))
    rel.real_promote_release("1.2.4")
    assert calls == [("release", "edit", "v1.2.4", "--latest", "-R", rel.CORE)] and rel.state()["promoted_at"]


def test_a_final_release_never_moves_latest_backwards(rel, monkeypatch):
    calls = fake_gh(rel, monkeypatch, [FINAL], latest=("v1.2.5",))
    rel.real_promote_release("1.2.4")
    assert calls == []


def test_a_final_release_that_does_not_become_latest_stops(rel, monkeypatch):
    fake_gh(rel, monkeypatch, [FINAL], latest=("v1.2.3",))
    with pytest.raises(rel.Stop, match="not Latest"):
        rel.real_promote_release("1.2.4")


@pytest.mark.parametrize(("states", "code"), [([HELD, HELD], 0), ([HELD, FINAL], 1), ([HELD, None], 0),
                                              ([HELD, release(prerelease=False, tag="v1.2.5")], 0)])
def test_a_failed_promotion_stops_before_finished(rel, monkeypatch, states, code):
    fake_gh(rel, monkeypatch, states, edit_code=code)
    with pytest.raises(rel.Stop, match="still a pre-release"):
        rel.real_promote_release("1.2.4")
    assert "promoted_at" not in rel.state()


@pytest.mark.parametrize("state", [None, release(draft=True), release(draft=True, prerelease=False),
                                   release(tag="v1.2.5")])
def test_an_unpublished_release_is_never_promoted(rel, monkeypatch, state):
    assert fake_gh(rel, monkeypatch, [state]) == []
    with pytest.raises(rel.Stop, match="no published release"):
        rel.real_promote_release("1.2.4")


@pytest.mark.parametrize(("receipt", "match"), [
    ({"version": "1.2.4", "snapshot": SNAPSHOT, "absorbed_by": "1.2.5"}, "absorbed"),
    ({"version": "1.2.5", "snapshot": SNAPSHOT}, "receipt"),
    ({"version": "1.2.4"}, "no pinned"),
    ({"version": "1.2.4", "snapshot": SNAPSHOT | {"tag": "v1.2.3"}}, "no pinned"),
    ({"version": "1.2.4", "snapshot": SNAPSHOT | {"wheel_sha256": "f" * 64}}, "differs from the accepted")])
def test_an_absorbed_foreign_or_unpinned_release_is_never_promoted(rel, monkeypatch, receipt, match):
    assert fake_gh(rel, monkeypatch, [HELD], receipt=receipt) == []
    with pytest.raises(rel.Stop, match=match):
        rel.real_promote_release("1.2.4")


def test_an_absorbed_release_already_final_is_not_reported_promoted(rel, monkeypatch):
    receipt = {"version": "1.2.4", "snapshot": SNAPSHOT, "absorbed_by": "1.2.5"}
    assert fake_gh(rel, monkeypatch, [FINAL], receipt=receipt) == []
    with pytest.raises(rel.Stop, match="absorbed"):
        rel.real_promote_release("1.2.4")


@pytest.mark.parametrize("held", [{"assets": []}, {"assets": [{"name": WHEEL, "digest": "sha256:" + "a" * 64}]}])
def test_a_held_release_with_incomplete_assets_is_never_promoted(rel, monkeypatch, held):
    assert fake_gh(rel, monkeypatch, [HELD | held]) == []
    with pytest.raises(rel.Stop, match="promote: v1.2.4"):
        rel.real_promote_release("1.2.4")


@pytest.mark.parametrize("finals", ["v1.2.5\n", "v1.3.0\nv1.2.3\n", None])
def test_latest_never_moves_backwards_or_blindly(rel, monkeypatch, finals):
    assert fake_gh(rel, monkeypatch, [HELD], finals=finals) == []
    with pytest.raises(rel.Stop, match="backwards|cannot read"):
        rel.real_promote_release("1.2.4")


def test_held_newer_releases_and_edge_tags_do_not_block_promotion(rel, monkeypatch):
    calls = fake_gh(rel, monkeypatch, [HELD, FINAL], finals="v1.2.3\nedge-v9.9.9.dev1\nv1.2.3-rc1\n")
    rel.real_promote_release("1.2.4")
    assert calls == edits(rel)


def test_the_newest_tag_counts_held_releases_but_never_edge_or_app(rel, monkeypatch):
    out = "edge-v0.9.36.dev3\nedge-channel\nv0.9.35\nv0.9.34\napp-v0.9.34\n"
    monkeypatch.setattr(rel.hzlib, "gh", lambda *a: (0, out) if "releases?per_page=30" in a[1] else (1, ""))
    assert rel.newest_core_tag() == (0, "v0.9.35")
