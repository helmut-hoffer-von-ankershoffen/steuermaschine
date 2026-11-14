"""m598: a new head for an existing PR that `sm head pr` opened (main moved, the builder merged it into the branch).

`sm head pr` records the PR it opened or adopted; focused admission accepts that record as the PR's ownership entry
(a hold in the hold file still decides first); `sm head prepare --pr N` binds the PR's current head as the expected
remote head by itself when the reviewed head is a new one; `sm head admit` finds the branch's open PR."""

import json
from types import SimpleNamespace

import pytest
from test_native_ship_commands import CORE, HEAD, gh, live_pr, nothing_real, rt, worktree  # noqa: F401 - fixtures
from test_pipeline_cov_head_admit import context, native
from test_pipeline_cov_head_admit import runtime as admit_runtime

from steuermaschine.github.testing import FakeGh
from steuermaschine.testing import invoke
from steuermaschine_heinzel.native import pr_owned
from steuermaschine_heinzel.native.admission_lib import focused_admit as a
from steuermaschine_heinzel.native.admission_lib import focused_slots as s
from steuermaschine_heinzel.native.admission_lib import ops
from steuermaschine_heinzel.pipelines import common, head_admit

OLD, NEW = "1" * 40, "2" * 40


def test_head_pr_records_the_pr_it_opened(rt, gh, worktree):  # noqa: F811
    live_pr(gh)
    out = invoke(rt, ["head", "pr", "abc", "fix(web): keep the draft", "--body-file", str(worktree), "--live"])
    assert out.exit_code == 0, out
    found = pr_owned.owned(CORE, 801)  # the plugin's state directory, as admission reads it
    assert found == {"repo": CORE, "pr": 801, "branch": "fix/x", "head": HEAD, "how": "created"}


def test_the_record_names_only_its_own_pr(tmp_path):
    path = pr_owned.record_path("O/Core", 5, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"repo": "o/core", "pr": 6}))
    assert pr_owned.owned("o/core", 5, tmp_path) is None
    path.write_text("{")
    assert pr_owned.owned("o/core", 5, tmp_path) is None
    assert pr_owned.owned("o/core", 7, tmp_path) is None  # none written


def holds_runtime(tmp_path, entries: str):
    (tmp_path / "m" / "tools").mkdir(parents=True)
    (tmp_path / "m" / "tools" / "pr-actions.toml").write_text(entries)
    return SimpleNamespace(management=tmp_path / "m", repo=CORE, cache=tmp_path / "state")  # the hold file's keys name Core


def test_an_sm_record_is_the_ownership_entry_and_a_hold_still_wins(tmp_path, monkeypatch):
    owner = holds_runtime(tmp_path, "")
    with pytest.raises(a.AdmissionError, match="PR ownership entry unavailable"):
        s.check_pr_holds(owner, 809)
    monkeypatch.setattr(pr_owned.statedir, "path", lambda name: tmp_path / "state" / name)
    pr_owned.record(CORE, 809, "fix/x", OLD, "created")
    s.check_pr_holds(owner, 809)  # owned by the record
    held = holds_runtime(tmp_path / "held", '["809"]\nhold = true\n')
    held.cache = tmp_path / "state"
    with pytest.raises(a.AdmissionError, match="PR is held"):
        s.check_pr_holds(held, 809)


class Api:
    repo = "o/core"

    def __init__(self, head):
        self.head = head

    def api(self, path):
        assert path == "repos/o/core/pulls/809"
        return {"number": 809, "head": {"sha": self.head}}


def packet(tmp_path, head=NEW):
    path = tmp_path / "packet.json"
    path.write_text(json.dumps({"manifest": {"head": head}}))
    return path


def test_the_moved_pr_head_is_the_expected_remote_head(tmp_path):
    assert ops.moved_pr_head(Api(OLD), packet(tmp_path), 809) == OLD
    assert ops.moved_pr_head(Api(NEW), packet(tmp_path), 809) is None  # the PR is at the reviewed head: strict mode
    with pytest.raises(a.AdmissionError, match="PR head unreadable"):
        ops.moved_pr_head(Api("nope"), packet(tmp_path), 809)


def test_prepare_with_pr_binds_the_moved_head_and_stores_it_for_apply(tmp_path, monkeypatch):
    seen = {}

    def fake_prepare(admission_rt, packet_path, review_root, **kwargs):
        seen.update(kwargs)
        raise a.AdmissionError("stop after the options")

    monkeypatch.setattr(s, "prepare", fake_prepare)
    with pytest.raises(a.AdmissionError, match="stop after"):
        ops.prepare(packet(tmp_path), tmp_path, tmp_path, pr=809, runtime=Api(OLD))
    assert seen["expected_remote_head"] == OLD and seen["pr"] == 809
    with pytest.raises(a.AdmissionError):
        ops.prepare(packet(tmp_path), tmp_path, tmp_path, pr=809, expected_remote_head="3" * 40, runtime=Api(OLD))
    assert seen["expected_remote_head"] == "3" * 40  # an explicit option is kept
    with pytest.raises(a.AdmissionError):
        ops.prepare(packet(tmp_path), tmp_path, tmp_path, runtime=Api(OLD))
    assert seen["expected_remote_head"] is None  # pre-open: no PR read


def test_head_admit_finds_the_open_pr_of_the_branch(tmp_path):
    seen = {}

    def prepare(**kwargs):
        seen.update(kwargs)
        return ops.PrepareResult(sha256="d" * 64, head="h", main="m", slot="s", pr=kwargs["pr"], prepared_path="p", args_path="a", prepared={})

    common.OVERRIDE["head prepare"] = prepare
    forge = FakeGh().on("pr list -R o/core --head fix/update-all", [{"number": 809}])
    try:
        admit = admit_runtime(tmp_path, forge)
        with native(admit):
            head_admit.prepare(context(admit, tmp_path, {"review_root": str(tmp_path)}, {"packet": str(tmp_path / "packet.json")}))
            assert seen["pr"] == 809
            head_admit.prepare(context(admit, tmp_path, {"review_root": str(tmp_path), "pr": "810"}, {"packet": str(tmp_path / "packet.json")}))
            assert seen["pr"] == 810  # a given PR wins
            forge.on("pr list -R o/core --head fix/update-all", [])
            head_admit.prepare(context(admit, tmp_path, {"review_root": str(tmp_path)}, {"packet": str(tmp_path / "packet.json")}))
            assert seen["pr"] is None
    finally:
        common.OVERRIDE.clear()
