"""Which arguments a prepare ran with, rebuilt from the files next to a packet (go_lib/probes.prepare_arguments)."""

import json

from steuermaschine_heinzel.native.go_lib import probes

RECEIPTS = [{"consumer": "a", "sha": "1" * 40}]


def test_prepare_args_file_names_core_repo_review_root_and_nonconsumers(tmp_path):
    d = tmp_path / "packet"
    d.mkdir()
    (tmp_path / "receipts.json").write_text(json.dumps({"receipts": RECEIPTS}))
    (d / "prepare.args").write_text(json.dumps({"cwd": str(tmp_path), "argv": ["--core-repo", "core", "--review-root", "rev", "--nonconsumers", "receipts.json"]}))
    (d / "prepare.out").write_text(json.dumps({"pr": 12, "protected_producer": True, "resulting_main": {}, "nonconsumers": RECEIPTS,
                                               "pr_update": {"old": "a" * 40}}))
    out = probes.prepare_arguments(d, {})
    assert out["core_repo"] == str(tmp_path / "core") and out["review_root"] == str(tmp_path / "rev")
    assert (out["pr"], out["protected_producer"], out["resulting_main_only"], out["expected_remote_head"]) == (12, True, True, "a" * 40)
    assert out["nonconsumers"] == str(tmp_path / "receipts.json") and out["unknown_args"] == []


def test_a_batch_transaction_cannot_be_rebuilt(tmp_path):
    (tmp_path / "prepare.out").write_text(json.dumps({"members": []}))
    assert "batch transaction" in probes.prepare_arguments(tmp_path, {})["unknown_args"][0]


def test_unreadable_args_and_missing_receipts_are_named(tmp_path):
    (tmp_path / "prepare.args").write_text("not json")
    (tmp_path / "prepare.out").write_text(json.dumps({"pr_update": {"old": "short"}, "nonconsumers": RECEIPTS}))
    out = probes.prepare_arguments(tmp_path, {})
    assert out["unknown_args"][0].startswith("prepare.args: unreadable")
    assert any(a.startswith("--expected-remote-head") for a in out["unknown_args"])
    assert out["nonconsumers"] is None and any("jq '{receipts: .nonconsumers}'" in a for a in out["unknown_args"])


def test_a_loop_script_names_the_receipts_file_through_a_variable(tmp_path):
    d = tmp_path / "packet"
    d.mkdir()
    (d / "nc.json").write_text(json.dumps({"receipts": RECEIPTS}))
    (d / "prepare-loop.sh").write_text(f"export NC={d}/nc.json\nsm head prepare P --nonconsumers $NC\nother --nonconsumers ${{UNSET}}/nc.json\n")
    (d / "prepare.out").write_text(json.dumps({"nonconsumers": RECEIPTS}))
    assert probes.prepare_arguments(d, {})["nonconsumers"] == str(d / "nc.json")


def test_an_old_prepare_output_takes_the_first_named_file(tmp_path):
    (tmp_path / "note.md").write_text("ran with --nonconsumers=~/x.json")
    (tmp_path / "prepare.out").write_text(json.dumps({}))
    assert probes.prepare_arguments(tmp_path, {})["nonconsumers"].endswith("x.json")
    (tmp_path / "prepare.out").write_text("broken")
    assert probes.prepare_arguments(tmp_path, {})["nonconsumers"].endswith("x.json")


def test_receipts_of_a_file(tmp_path):
    path = tmp_path / "r.json"
    assert probes._receipts(path) is None
    path.write_text("[]")
    assert probes._receipts(path) is None
    path.write_text(json.dumps({"receipts": RECEIPTS}))
    assert probes._receipts(path) == RECEIPTS
