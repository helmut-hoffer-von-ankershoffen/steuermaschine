"""The Mini's updater hold (bead pcvh): sm go and sm mini verify say `held: no acceptance marker` when the product's
updater state file records core_held for the published release. The state file is a fixture; it is only read."""

import json

from test_native_go_cov_b_probes import res, world
from test_native_mini import Mini

from steuermaschine_heinzel.native import updater_hold
from steuermaschine_heinzel.native.go_lib import decide, probes


def state_file(tmp_path, held="0.9.41"):
    path = tmp_path / "auto-update.json"
    path.write_text(json.dumps({"core": None, "core_held": held, "held_plugins": []}))
    return path


def test_held_version_reads_only_an_exact_version(tmp_path):
    assert updater_hold.held_version(None) is None
    assert updater_hold.held_version({"mini_update_state": None}) is None
    assert updater_hold.held_version({"mini_update_state": str(tmp_path / "missing.json")}) is None
    path = state_file(tmp_path)
    before = path.read_bytes()
    assert updater_hold.held_version({"mini_update_state": str(path)}) == "0.9.41"
    assert path.read_bytes() == before  # read, never written
    for bad in (None, "", "0.9", "latest; rm -rf"):
        assert updater_hold.held_version({"mini_update_state": str(state_file(tmp_path, bad))}) is None
    path.write_text("not json")
    assert updater_hold.held_version({"mini_update_state": str(path)}) is None
    path.write_text("[1]")
    assert updater_hold.held_version({"mini_update_state": str(path)}) is None


def test_probe_mini_names_the_hold(tmp_path):
    w = world(tmp_path, {"--version": res("hz 0.9.40\n")}, settings={"mini_update_state": str(state_file(tmp_path))})
    assert probes.probe_mini(w) == {"cli": "0.9.40", "live": None, "live_ok": None, "held": "0.9.41"}
    assert probes.probe_mini(world(tmp_path, {"--version": res("hz 0.9.40\n")}))["held"] is None


def phase(mini):
    return decide.decide({"main": {"sha": ""}, "release": {}, "queue": {}, "train": {}, "holds": {}, "releases": {"latest": "0.9.41"}, "mini": mini}, 0)


def test_go_phase_says_held_instead_of_not_live():
    d = phase({"cli": "0.9.40", "held": "0.9.41"})
    assert d["phase"].startswith("v0.9.41 published, held: no acceptance marker (Mini on 0.9.40")
    assert d["next"] == "sm release status" and d["trap"].startswith("Held is not broken")
    assert phase({"cli": "0.9.40", "held": None})["phase"] == "v0.9.41 published, Mini still on 0.9.40: not live-verified"
    assert "not live-verified" in phase({"cli": "0.9.39", "held": "0.9.40"})["phase"]  # an older hold is not this release's


def test_mini_verify_says_held(tmp_path):
    m = Mini(tmp_path)
    m.rt.settings["mini_update_state"] = str(state_file(tmp_path, "0.9.40"))
    out = m.run("--minimum", "0.9.40")
    assert out.exit_code == 1, out
    assert out.stdout.splitlines()[-1].startswith("NOT-LIVE: held: no acceptance marker: v0.9.40 is published")
    m.rt.settings["mini_update_state"] = str(state_file(tmp_path, "0.9.39"))  # not newer than the installed version
    assert m.run("--minimum", "0.9.40").stdout.splitlines()[-1] == "NOT-LIVE: installed v0.9.39 does not satisfy minimum v0.9.40"
    m.rt.settings["mini_update_state"] = str(state_file(tmp_path, "0.9.40"))  # held is older than the target: not this hold
    m.cli = "0.9.38"
    assert m.run("--minimum", "0.9.41").stdout.splitlines()[-1] == "NOT-LIVE: installed v0.9.38 does not satisfy minimum v0.9.41"
